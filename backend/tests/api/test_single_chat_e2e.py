"""John's whole story through the real API, worker and stores, with scripted models (demo plan 11.2).

ALDO bot (the real Strands agent loop, real tools, real gateway) is driven by a scripted model.
The return workflow's runtime is a scripted stand-in that uses the real domain functions (policy
check, options, quote, execution) the way the Returns Agent's tools do. Everything between them is
real: the routes, the conversation mode, the worker, the hand-off, the quote, the approval click,
the transaction, the write-back and the way back to the bot. Moto holds both tables.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from api import returns_chat
from api.handler import build_app
from personal_agent import memory, runner
from returns_domain import (
    conversations,
    execution,
    options,
    orders,
    personal_agent_client,
    quotes,
    runs,
    store,
    trace,
)
from worker import handler as worker

from ..personal_agent.scripted import ScriptedModel, hand_off_policy, john_policy
from ..v2_trace import ensure_v2_event_types
from .conftest import ADMIN_KEY, AVA, BEN, delegated_token, session_token
from .helpers import ApiClient
from .test_worker import RUNTIME_ARN, FakeAgentCore, FakeBoto3, answer

JOHN = "DEMO-CUST-JOHN"
ADMIN = {"X-Demo-Admin": ADMIN_KEY}
PREFIX = "/api/personal-agent/conversations"
BUY = (
    "I was at the ALDO store and loved the black leather shoes, the ALDO Men 001, but they didn't have my size. "
    "Please buy them for me in my size."
)
RETURN = "I want to return these."
REASON = "The colour doesn't go with my outfit, and they're unworn."


class Stack:
    """The deployed system in one object: the API, the worker, the bot's model and the return runtime."""

    def __init__(self, client: ApiClient, enqueued: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
        self.client, self.enqueued, self.monkeypatch = client, enqueued, monkeypatch
        self.policy: Callable[[list, int], Any] | None = None
        self.return_runs: list[dict[str, Any]] = []
        self.bot_payloads: list[dict[str, Any]] = []  # what the bot's runner was handed by the worker
        self.runtime = FakeAgentCore(self.scripted_return_workflow)
        monkeypatch.setattr(worker, "boto3", FakeBoto3(self.runtime))
        monkeypatch.setattr(runner, "build_model", lambda model_id: ScriptedModel(self.policy))
        real_run = runner.run

        def spy(payload: Any, *args: Any, **kwargs: Any) -> Any:
            self.bot_payloads.append(dict(payload))
            return real_run(payload, *args, **kwargs)

        monkeypatch.setattr(runner, "run", spy)
        self.token = session_token(JOHN)

    # ------------------------------------------------------------------ the scripted return workflow

    def scripted_return_workflow(self, kwargs: dict) -> dict:
        """What the Return workflow does with a run, with the real domain functions."""
        payload = json.loads(kwargs["payload"])
        run_id, owner = payload["run_id"], payload["customer_id"]
        assert runs.mark_agent_started(run_id)
        self.return_runs.append(payload)
        if payload["kind"] == "customer":
            order_id = payload["order_id"]
            check = orders.check_policy(owner, run_id, order_id, f"{order_id}-L1", 1, "CHANGE_OF_MIND", "unworn", "unknown")
            candidates = options.find_options(owner, check.policy_check_id, None, None, None, "BROWN", run_id=run_id)
            quotes.create_quote(owner, run_id, payload["conv_id"], candidates.candidate_set_id, [o.option_id for o in candidates.options])
            trace.emit(run_id, "assistant_message", {"text": "Brown is in stock in your size. Exchange in store, free."})
        else:  # the approval continuation: the one execution
            execution.execute(owner, payload["quote_id"], run_id=run_id)
            trace.emit(run_id, "assistant_message", {"text": "Exchange confirmed. The store has been notified."})
        runs.finish_run(run_id, "completed", {})
        return answer({"run_id": run_id, "status": "completed", "metrics": {}})

    # ----------------------------------------------------------------------------- driving it

    def api(self, method: str, path: str, body: Any = None, token: str | None = None):
        return self.client.call(method, path, body=body, token=token or self.token)

    def say(self, conv: str, text: str, *, policy: Callable | None = None) -> dict[str, Any]:
        """Send a chat message, then do what the worker Lambda does with the payload it was handed."""
        self.policy = policy or self.policy
        before = len(self.enqueued)
        reply = self.api("POST", f"{PREFIX}/{conv}/messages", {"text": text})
        assert reply.status == 202, reply.text
        assert len(self.enqueued) == before + 1
        result = worker.process(self.enqueued[-1])
        assert result["status"] == "completed", result
        return {"reply": reply.json, "payload": self.enqueued[-1], "result": result}

    def run_events(self, run_id: str) -> list[dict]:
        page = self.api("GET", f"/api/runs/{run_id}/events")
        assert page.status == 200, page.text
        return page.json["events"]

    def a2a(self) -> list[dict]:
        return self.api("GET", "/api/a2a/events").json["events"]

    def a2a_types(self) -> list[str]:
        return [e["type"] for e in self.a2a()]

    def mode(self, conv: str) -> dict:
        return self.api("GET", f"{PREFIX}/{conv}").json

    def approve(self, quote_id: str) -> dict[str, Any]:
        quote = self.api("GET", f"/api/quotes/{quote_id}").json
        option = next(o for o in quote["options"] if o["type"] == "exchange_store_pickup")
        reply = self.api("POST", f"/api/quotes/{quote_id}/approve", {"option_id": option["option_id"], "payload_hash": option["payload_hash"]})
        assert reply.status == 202, reply.text
        result = worker.process(self.enqueued[-1])
        return {"reply": reply.json, "payload": self.enqueued[-1], "result": result, "option": option}


@pytest.fixture
def stack(ddb, personal_table, monkeypatch, enqueued):
    monkeypatch.setenv("DEMO_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")  # the bot calls the real gateway in process, write-backs too
    monkeypatch.setenv("AGENT_RUNTIME_ARN", RUNTIME_ARN)
    monkeypatch.delenv("AGENT_MODE", raising=False)
    worker.reset_runtime_arn()
    ensure_v2_event_types(monkeypatch)
    client = ApiClient(build_app(enqueue=enqueued.append))
    assert client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN).status == 200
    yield Stack(client, enqueued, monkeypatch)
    personal_agent_client.register_local_handler(None)
    worker.reset_runtime_arn()


def stock(sku: str, location: str) -> int:
    return int(store.get_item(store.inventory_key(sku, location))["atp"])


# ====================================================================================== the story


def john_until_the_quote(stack: Stack) -> SimpleNamespace:
    """Acts 1 and 2 of the story, up to the return workflow's quote. Returns what the next steps need."""
    # ---- act 1: ALDO bot proposes, the owner clicks, the order exists, the delivery notice comes
    conv = stack.api("POST", PREFIX, {}).json["conv_id"]
    assert stack.mode(conv) == {"conv_id": conv, "mode": "bot", "returns_order_id": None}
    bought = stack.say(conv, BUY, policy=john_policy())
    assert bought["reply"]["agent"] == "personal" and bought["reply"]["mode"] == "bot"
    events = stack.run_events(bought["reply"]["run_id"])
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_completed"
    proposal = next(e for e in events if e["type"] == "size_recommended")["payload"]
    assert (proposal["chosen_size"], proposal["option_type"], proposal["status"]) == ("9.5", "store_pickup", "proposed")
    reply = next(e for e in events if e["type"] == "assistant_message")["payload"]["text"]
    assert "confirm" in reply.lower() and "set aside one review" in reply and "ignore all previous" not in reply.lower()
    request_id = next(e for e in events if e["type"] == "tool_result" and e["payload"]["tool"] == "ask_aldo_size_assist")["payload"]["evidence"]["size_request_id"]
    assert not list(store.scan_keys(("ORDER#DEMO-A2A-",))) and stock("DEMO-SKU-MEN001-BLACK-095", "DEMO-STORE-LAVAL-01") == 1

    card = stack.api("GET", f"/api/purchases/{request_id}").json
    assert (card["status"], card["size"], card["option_type"]) == ("proposed", "9.5", "store_pickup")
    placed = stack.api("POST", f"/api/purchases/{request_id}/confirm", {"option_id": card["option_id"]})
    assert placed.status == 200
    order_id = placed.json["order_id"]
    assert order_id.startswith("DEMO-A2A-") and stock("DEMO-SKU-MEN001-BLACK-095", "DEMO-STORE-LAVAL-01") == 0
    assert placed.json["next_step"] == "Order placed. Pick up size 9.5 at ALDO Demo Store Laval from Sat Oct 17."  # shown as it is
    assert stack.api("POST", "/api/demo/simulate-delivery", {"order_id": order_id}).status == 200
    # what the phone UI reads from the middle timeline
    confirmed = next(e for e in stack.a2a() if e["type"] == "size_confirmed")
    assert {"order_id", "size", "location_name", "eta_date", "approved_by"} <= set(confirmed["payload"])
    assert (confirmed["payload"]["size"], confirmed["payload"]["approved_by"]) == ("9.5", "owner_click")
    delivered = next(e for e in stack.a2a() if e["type"] == "delivered")
    assert delivered["actor"] == "system" and delivered["payload"]["order_id"] == order_id
    assert {"order_id", "product_name", "size"} <= set(delivered["payload"])
    reviews = next(e for e in stack.a2a() if e["type"] == "reviews_shared")
    assert reviews["actor"] == "aldo_gateway" and {"n_reviews", "n_flagged"} <= set(reviews["payload"])

    # ---- act 2: "I want to return these." -> hand-off, the mode is returns, nobody else is named
    handed = stack.say(conv, RETURN, policy=hand_off_policy(order_id))
    assert handed["reply"]["agent"] == "personal"
    assert handed["payload"]["agent"] == "personal" and "orders" not in handed["payload"]  # the API never sends it
    assert stack.bot_payloads[-1]["orders"][0]["order_id"] == order_id  # the worker read it: server data
    sentence = next(e for e in stack.run_events(handed["reply"]["run_id"]) if e["type"] == "assistant_message")["payload"]["text"]
    assert sentence == "Sure, let's sort out your return."
    assert stack.mode(conv) == {"conv_id": conv, "mode": "returns", "returns_order_id": order_id}
    assert stack.a2a_types()[-2:] == ["handoff_to_returns", "memory_consulted"]
    handoff, consulted = stack.a2a()[-2:]
    assert (handoff["actor"], consulted["actor"]) == ("personal_agent", "aldo_agent")
    assert handoff["payload"]["order_id"] == order_id and consulted["payload"]["fields"] == ["typical_size", "preferred_colors", "aldo_items"]

    # ---- the owner's next message goes straight to the return workflow (no second model hop)
    routed = stack.say(conv, REASON)
    assert routed["reply"]["agent"] == "returns" and routed["reply"]["mode"] == "returns"
    payload = stack.return_runs[0]
    assert payload["agent"] == "aldo" and payload["kind"] == "customer" and payload["customer_id"] == JOHN
    assert payload["order_id"] == order_id and payload["selected_order_id"] == order_id
    assert payload["message"] == f"{RETURN}\n{REASON}"
    assert payload["memory_context"] == {
        "size_system": "US_M", "typical_size": "10.0", "preferred_colors": ["BROWN", "WHITE"],
        "aldo_items": [{"order_id": order_id, "product_name": "ALDO Men 001", "color": "BLACK", "size": "9.5", "status": "delivered"}],
        "source": "ALDO bot memory",
    }
    from agent import owner_context  # the Return workflow's own validator keeps every field (it drops only the constant `source`)

    assert owner_context.sanitize(payload["memory_context"]) == {k: v for k, v in payload["memory_context"].items() if k != "source"}
    run_events = stack.run_events(routed["reply"]["run_id"])
    quote_id = next(e for e in run_events if e["type"] == "quote_created")["payload"]["quote_id"]
    assert "Brown is in stock" in next(e for e in run_events if e["type"] == "assistant_message")["payload"]["text"]
    assert stack.mode(conv)["mode"] == "returns"  # a quote is not the end: the owner still has to approve

    # the quote says which conversation it belongs to: the requote route is addressed with that id, not the chat's
    quote = stack.api("GET", f"/api/quotes/{quote_id}").json
    assert quote["conv_id"] == payload["conv_id"] and quote["order_id"] == order_id
    assert stack.api("POST", f"/api/conversations/{conv}/requote", {"quote_id": quote_id}).status == 404
    assert stack.api("POST", f"/api/conversations/{quote['conv_id']}/requote", {"quote_id": quote_id}).status == 422  # found; only an expired quote is re-quoted
    return SimpleNamespace(conv=conv, order_id=order_id, payload=payload, quote_id=quote_id)


def test_john_buys_returns_the_colour_and_ends_back_with_the_bot(stack: Stack, enqueued):
    story = john_until_the_quote(stack)
    conv, order_id, payload, quote_id = story.conv, story.order_id, story.payload, story.quote_id

    # ---- the owner clicks Confirm exchange: one transaction, the store is told, back to the bot
    approved = stack.approve(quote_id)
    assert approved["payload"]["kind"] == "approval" and approved["payload"]["conv_id"] == payload["conv_id"]
    for run in (approved["payload"], stack.return_runs[1]):  # the approval is a run of the same hand-off: the same order and context
        assert run["kind"] == "approval" and run["order_id"] == order_id and run["selected_order_id"] == order_id
        assert run["memory_context"] == payload["memory_context"]
    assert approved["result"]["status"] == "completed"
    approval_events = stack.run_events(approved["reply"]["run_id"])
    assert "transaction_created" in [e["type"] for e in approval_events]
    assert stack.mode(conv) == {"conv_id": conv, "mode": "bot", "returns_order_id": None}
    types = stack.a2a_types()
    for earlier, later in (
        ("size_confirmed", "delivered"), ("delivered", "handoff_to_returns"), ("memory_consulted", "quote_created"),
        ("quote_created", "transaction_created"), ("transaction_created", "handback"),
    ):
        assert types.index(earlier) < types.index(later), (earlier, later, types)
    assert types.count("handback") == 1 and types.count("transaction_created") == 1 and "store_notified" in types
    handback = next(e for e in stack.a2a() if e["type"] == "handback")
    assert handback["actor"] == "aldo_agent" and handback["payload"]["outcome"] == "completed"
    assert handback["payload"]["order_id"] == order_id and handback["payload"]["mode"] == "bot"

    # ---- ALDO wrote back: the size fit (a colour exchange), and the bot remembers it
    updates = stack.api("GET", "/api/personal-agent/memory").json["updates"]
    assert [(u["reason"], u["fit_feedback"], u["original_size"], u["new_size"]) for u in updates] == [("PREFERENCE", "size_ok", "9.5", "9.5")]

    # ---- and the chat is the bot's again
    after = stack.say(conv, "Thanks!", policy=lambda messages, call: ("text", "You're welcome. Anything else I can help with?"))
    assert after["reply"]["agent"] == "personal" and after["reply"]["mode"] == "bot"
    assert len(stack.return_runs) == 2  # the return workflow ran twice (quote, approval) and not again


def test_a_requote_in_the_hand_off_brings_the_order_and_the_context_again(fixed_now, stack: Stack, enqueued):
    """Each owner message, the approval and a requote are separate runs: every one of them carries both.

    ``fixed_now`` comes first so the stack's session token is signed on the frozen clock too."""
    story = john_until_the_quote(stack)
    fixed_now.advance(seconds=16 * 60)  # the quote expires before the owner clicks
    quote = stack.api("GET", f"/api/quotes/{story.quote_id}").json
    assert quote["status"] == "expired"

    requote = stack.api("POST", f"/api/conversations/{quote['conv_id']}/requote", {"quote_id": story.quote_id})

    assert requote.status == 202, requote.text
    run = enqueued[-1]
    assert run["run_id"] == requote.json["run_id"] and run["kind"] == "requote" and run["quote_id"] == story.quote_id
    assert run["order_id"] == story.order_id and run["selected_order_id"] == story.order_id
    assert run["memory_context"] == story.payload["memory_context"]


def test_nothing_is_ordered_or_returned_without_the_owners_clicks(stack: Stack, enqueued):
    """The two security invariants of the story: no agent run creates an order or an approval."""
    conv = stack.api("POST", PREFIX, {}).json["conv_id"]
    stack.say(conv, BUY, policy=john_policy())
    assert not list(store.scan_keys(("ORDER#DEMO-A2A-",)))  # the bot proposed; nothing was ordered
    assert not [pk for pk, _ in store.scan_keys(("TXN#",))] and not [pk for pk, _ in store.scan_keys(("QUOTE#",))]


def test_a_stolen_order_id_cannot_start_somebody_elses_return(stack: Stack, enqueued):
    """The model names an order that is not John's (a mistake or an injection): refused, back to the bot."""
    conv = stack.api("POST", PREFIX, {}).json["conv_id"]
    stack.say(conv, RETURN, policy=hand_off_policy("DEMO-1001"))  # Ava's order: the tool does not know it
    assert stack.mode(conv)["mode"] == "bot"
    # even if a hand-off got through with it, the API refuses and the audit trail records the attempt
    memory.get_repo().set_returns_mode(JOHN, conv, "DEMO-1001", handoff_text=RETURN)
    refused = stack.api("POST", f"{PREFIX}/{conv}/messages", {"text": REASON})
    assert refused.status == 403 and refused.code == "NOT_AUTHORIZED"
    assert stack.mode(conv)["mode"] == "bot" and not [p for p in enqueued if p.get("agent") == "aldo"]
