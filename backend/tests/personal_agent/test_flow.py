"""The whole stream end to end, the way the API and the worker use it (no Bedrock, moto table).

Owner message -> POST /messages (202, enqueue) -> worker runs the personal agent in process ->
the UI polls the run events -> the owner reads the memory -> ALDO writes back (HMAC) -> the memory
shows the provisional update, then the confirmed one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from personal_agent import memory, runner
from personal_agent.gateway_client import LocalGatewayClient
from personal_agent.memory import MemoryRepo

from .canned import CannedGateway
from .scripted import ScriptedModel, good_policy
from .test_routes import AVA, Api, session, signed, update_body

MESSAGE = "Order me the ALDO Stessy heels for the wedding Saturday."


def worker_runs(api: Api, monkeypatch: pytest.MonkeyPatch, backend: CannedGateway) -> dict[str, Any]:
    """What the worker Lambda does with the payload the API enqueued."""
    payload = api.enqueued[-1]
    monkeypatch.setattr(runner, "build_model", lambda model_id: ScriptedModel(good_policy()))
    gateway = LocalGatewayClient(runner.mint_delegated_token(payload["customer_id"]), backend=backend)
    return runner.run(payload, repo=api.repo, gateway=gateway)


def poll(api: Api, run_id: str) -> list[dict[str, Any]]:
    """The UI loop: poll with the cursor until run_completed."""
    seen: list[dict[str, Any]] = []
    after = 0
    for _ in range(10):
        status, page, _ = api.call(
            "GET", f"/personal-agent/runs/{run_id}/events", token=session(), query={"after": str(after)}
        )
        assert status == 200
        seen.extend(page["events"])
        after = page["next_after"]
        if seen and seen[-1]["type"] == "run_completed":
            return seen
    raise AssertionError("run_completed never arrived")


def test_purchase_turn_then_writeback_lifecycle(repo: MemoryRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    api = Api(repo)
    conv = api.conversation()
    status, accepted, _ = api.message(conv, MESSAGE, token=session())
    assert status == 202
    run_id = accepted["run_id"]
    assert api.call("GET", f"/personal-agent/runs/{run_id}/events", token=session())[1]["status"] == "queued"
    assert api.message(conv, "again", token=session())[0] == 409  # one active run per conversation

    backend = CannedGateway("default")
    result = worker_runs(api, monkeypatch, backend)
    assert result["status"] == "completed" and result["run_id"] == run_id

    events = poll(api, run_id)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    types = [e["type"] for e in events]
    assert types[0] == "run_started" and types[-1] == "run_completed"
    for expected in ("size_assist_requested", "decision", "size_recommended", "assistant_message"):
        assert expected in types
    reply = next(e for e in events if e["type"] == "assistant_message")["payload"]["text"]
    assert "confirm" in reply.lower()
    assert [p["chosen_size"] for p in backend.proposals] == ["8.0"]  # a proposal, never an order
    assert json.dumps(events).count(AVA) == 0  # no customer id in the trace the UI shows

    # the conversation is free again, and the second turn sees the first
    assert api.message(conv, "Has my order been placed yet?", token=session())[0] == 202
    assert len(api.repo.load_messages(AVA, conv)) > 0

    # ALDO writes back after the exchange (provisional), then after pickup (confirmed)
    raw, signature = signed(update_body())
    assert api.call("POST", "/personal-agent/memory-updates", raw=raw, signature=signature)[1]["applied"] is True
    shown = api.call("GET", "/personal-agent/memory", token=session())[1]["updates"]
    assert [(u["update_id"], u["confidence"], u["new_size"]) for u in shown] == [("MU-5C0A11", "provisional", "8.5")]
    final = update_body("MU-FINAL1", outcome="exchange_completed_kept", provisional=False, ts="2026-10-17T15:00:00+00:00")
    raw, signature = signed(final)
    assert api.call("POST", "/personal-agent/memory-updates", raw=raw, signature=signature)[1]["applied"] is True
    shown = api.call("GET", "/personal-agent/memory", token=session())[1]["updates"]
    assert [(u["update_id"], u["confidence"]) for u in shown] == [("MU-FINAL1", "confirmed")]


def test_the_next_purchase_uses_the_learned_offset_from_the_writeback(repo: MemoryRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.apply_update(update_body())
    seen: dict[str, Any] = {}

    def policy(messages: list, call: int):
        from .scripted import tool_results

        if call == 1:
            return "tools", [("recall_size_memory", {})]
        seen["recall"] = tool_results(messages)[0]["data"]
        return "text", "ok"

    monkeypatch.setattr(runner, "build_model", lambda model_id: ScriptedModel(policy))
    api = Api(repo)
    conv = api.conversation()
    api.message(conv, "Order me another pair of the ALDO Stessy.", token=session())
    runner.run(api.enqueued[-1], repo=repo, gateway=LocalGatewayClient(runner.mint_delegated_token(AVA), backend=CannedGateway()))
    assert seen["recall"]["aldo_updates"][0]["new_size"] == "8.5"
    assert seen["recall"]["aldo_updates"][0]["confidence"] == "provisional"
    assert memory.get_repo() is repo
