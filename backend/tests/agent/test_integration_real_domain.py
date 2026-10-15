"""The agent against the REAL persistence layer (moto DynamoDB), with a scripted model.

Everything between the model and the table is real: the ten tool wrappers, the Strands loop
(hooks, sequential executor), the runner, ``orders``, ``options``, ``quotes`` (+ approval),
``execution`` (single TransactWriteItems), ``cases``, ``trace``, ``runs`` and ``conversations``.
Only the model is scripted and the write-back transport is a recorder. Assertions are on table
state, not on chat text. This is the deterministic half of task 8.4; the live-model half is
run by the orchestrator against the deployed table.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import runner
from agent.services import Services
from returns_domain import conversations, fixtures, quotes, store, trace

from ..conftest import AVA, BEN, Flow, make_agent_order
from .scripted_model import AdaptiveModel, ScriptedModel, text_turn, tool_turn

AGENT_ORDER = "DEMO-A2A-81B20E"
AGENT_LINE = f"{AGENT_ORDER}-L1"
HERO_MESSAGE = "They're still slightly tight. Can I swap for a half size up before the wedding Saturday?"


class RecordingGateway:
    """Stands in for ``returns_domain.gateway``: records the delivery request, sends nothing."""

    def __init__(self) -> None:
        self.flushed_for: list[str | None] = []

    def flush_outbox(self, customer_id: str | None = None, limit: int = 10, client: Any = None) -> int:
        self.flushed_for.append(customer_id)
        return 0


@pytest.fixture
def gateway() -> RecordingGateway:
    return RecordingGateway()


@pytest.fixture
def services(seeded: None, gateway: RecordingGateway, no_flush: list[str]) -> Services:
    return Services(gateway=gateway)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    def install(model: ScriptedModel) -> ScriptedModel:
        monkeypatch.setattr(runner, "make_model", lambda model_id: model)
        return model

    return install


def body(run_id: str, conv_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id, "conv_id": conv_id, "customer_id": AVA, "order_id": AGENT_ORDER, "kind": "customer",
        "message": HERO_MESSAGE, "quote_id": None, "option_id": None, "approval_id": None,
    }
    payload.update(overrides)
    return payload


def events_of(run_id: str) -> list[dict[str, Any]]:
    return trace.list_events(run_id, 0, 200).events


def hero_turns(*, order_id: str = AGENT_ORDER, line_id: str = AGENT_LINE, reverse_options: bool = False) -> list[Any]:
    """The model's moves for the hero return; ids produced by the real services are read back."""
    return [
        tool_turn("get_order_context", order_id=order_id),
        tool_turn("evaluate_return_policy", order_id=order_id, line_id=line_id, qty=1,
                  reason_category="FIT_SIZE", condition="unworn", size_direction="too_small"),
        tool_turn("get_fit_insight", product_name="Stessy2.0"),
        lambda r: tool_turn("find_resolution_options", deadline_date="2026-10-17",
                            policy_check_id=r["evaluate_return_policy"]["data"]["policy_check_id"]),
        lambda r: tool_turn("record_decision", summary=r["find_resolution_options"]["data"]["ranking"]["reason"]),
        lambda r: tool_turn(
            "create_quote",
            candidate_set_id=r["find_resolution_options"]["data"]["candidate_set_id"],
            # A model that ignores the ranking: the tool still presents the options in ranking order.
            option_ids=(list(reversed(r["find_resolution_options"]["data"]["ranking"]["order"]))
                        if reverse_options else r["find_resolution_options"]["data"]["ranking"]["order"]),
        ),
        text_turn("Please review the option cards and press Approve."),
    ]


def test_hero_return_of_an_agent_placed_order_reaches_a_ranked_quote(
    flow: Flow, services: Services, scripted, snapshot
) -> None:
    make_agent_order(AGENT_ORDER, AVA, delivered_at="2026-10-16")
    conv_id, run_id = flow.start_run(AVA)
    model = scripted(AdaptiveModel(hero_turns(reverse_options=True)))

    out = runner.run(body(run_id, conv_id), services=services)

    assert out["status"] == "completed", out
    assert out["metrics"]["tool_count"] == 6 and out["metrics"]["rounds"] == 7
    events = events_of(run_id)
    assert [e["type"] for e in events] == [
        "run_started", "tool_call", "tool_result", "tool_call", "tool_result", "tool_call", "tool_result",
        "tool_call", "tool_result", "tool_call", "decision", "tool_result", "tool_call", "quote_created",
        "tool_result", "assistant_message", "run_completed",
    ]
    assert all(e["actor"] == "aldo_agent" for e in events)
    assert all(e["payload"]["ok"] for e in events if e["type"] == "tool_result")

    # The quote lists the options in ranking order although the model asked for the reverse.
    quote_id = next(e["payload"]["quote_id"] for e in events if e["type"] == "quote_created")
    quote = quotes.get_quote(AVA, quote_id)
    types = [o.type for o in quote.options]
    assert types[0] == "exchange_store_pickup"
    assert types[-2:] == ["return_in_store", "return_mail"]  # refund options stay visible, last
    assert types.index("exchange_ship_from_dc") > types.index("exchange_store_pickup")
    assert all(o.fee_cents == 0 for o in quote.options if o.type == "exchange_store_pickup")  # free in store
    assert quote.status == "open"

    # Before approval there is no transaction, no stock change and no returned quantity.
    assert [k for k in snapshot() if k[0].startswith(("TXN#", "IDEM#"))] == []
    assert store.get_item(store.line_key(AGENT_ORDER, AGENT_LINE))["returned_or_reserved_qty"] == 0

    # The conversation was persisted for the next turn, the selected order was a separate block.
    saved = conversations.load_messages(conv_id)
    assert saved[0]["role"] == "user" and saved[-1]["role"] == "assistant"
    assert model.calls[0]["messages"][0]["content"][1]["text"] == f"[selected_order_id: {AGENT_ORDER}]"


def test_the_seeded_order_goes_through_the_same_tools(flow: Flow, services: Services, scripted) -> None:
    conv_id, run_id = flow.start_run(AVA)
    scripted(AdaptiveModel(hero_turns(order_id="DEMO-1001", line_id="DEMO-1001-L1")))
    out = runner.run(body(run_id, conv_id, order_id="DEMO-1001"), services=services)
    assert out["status"] == "completed" and out["metrics"]["tool_count"] == 6
    quote_id = next(e["payload"]["quote_id"] for e in events_of(run_id) if e["type"] == "quote_created")
    assert [o.type for o in quotes.get_quote(AVA, quote_id).options][0] == "exchange_store_pickup"


def test_approval_run_executes_exactly_one_exchange_and_delivers_the_write_back(
    flow: Flow, services: Services, scripted, gateway: RecordingGateway, snapshot
) -> None:
    make_agent_order(AGENT_ORDER, AVA, delivered_at="2026-10-16")
    hq = flow.hero_quote(AVA, order_id=AGENT_ORDER, line_id=AGENT_LINE)
    pickup = hq.option("exchange_store_pickup")
    approval, created = quotes.approve(AVA, hq.quote_id, pickup.option_id, pickup.payload_hash, "RUN-APPROVAL01")
    assert created
    conv_id, run_id = flow.start_run(AVA, hq.conv_id, "approval")

    scripted(AdaptiveModel([
        # The model mistypes both ids: an approval run uses the ids named by the server payload.
        tool_turn("execute_approved_resolution", quote_id="QUO-MISTYPED", option_id="OPT-99"),
        lambda r: tool_turn("get_resolution_status", transaction_id=r["execute_approved_resolution"]["data"]["transaction_id"]),
        text_turn("Your exchange is recorded (simulated)."),
    ]))
    before = snapshot(exclude=("AUDIT#",))
    out = runner.run(
        body(run_id, conv_id, kind="approval", message="", quote_id=hq.quote_id, option_id=pickup.option_id,
             approval_id=approval.approval_id),
        services=services,
    )
    assert out["status"] == "completed", out

    events = events_of(run_id)
    created_events = [e for e in events if e["type"] == "transaction_created"]
    assert len(created_events) == 1
    txn_id = created_events[0]["payload"]["transaction_id"]
    assert txn_id.startswith("DEMO-RMA-")

    after = snapshot(exclude=("AUDIT#",))
    new_keys = {k[0].split("#")[0] for k in after if k not in before and not k[0].startswith("RUN#")}
    assert new_keys == {"TXN", "IDEM", "A2AOUT"}  # transaction, idempotency record, write-back outbox item
    inv_key = store.inventory_key(pickup.sku, pickup.location_id)
    assert after[inv_key]["atp"] == before[inv_key]["atp"] - 1  # ATP decreased by exactly one
    assert after[store.line_key(AGENT_ORDER, AGENT_LINE)]["returned_or_reserved_qty"] == 1
    assert after[store.approval_key(hq.quote_id)]["consumed_txn_id"] == txn_id
    assert after[store.quote_key(hq.quote_id)]["status"] == "executed"

    # Pivot delta 4: the wrapper asked the gateway to deliver pending write-backs for this customer.
    assert gateway.flushed_for == [AVA]
    status = next(e for e in events if e["type"] == "tool_result" and e["payload"]["tool"] == "get_resolution_status")
    assert status["payload"]["ok"] is True and status["payload"]["evidence"]["state"] == "completed"
    call = next(e for e in events if e["type"] == "tool_call" and e["payload"]["tool"] == "execute_approved_resolution")
    assert call["payload"]["args"] == {"quote_id": hq.quote_id, "option_id": pickup.option_id}


def test_repeating_the_approval_run_creates_no_second_transaction(
    flow: Flow, services: Services, scripted, snapshot
) -> None:
    hq = flow.hero_quote(AVA)
    pickup = hq.option("exchange_store_pickup")
    approval, _ = quotes.approve(AVA, hq.quote_id, pickup.option_id, pickup.payload_hash, "RUN-APPROVAL02")
    transactions: list[str] = []
    for _ in range(2):
        conv_id, run_id = flow.start_run(AVA, hq.conv_id, "approval")
        scripted(AdaptiveModel([
            tool_turn("execute_approved_resolution", quote_id=hq.quote_id, option_id=pickup.option_id),
            text_turn("Done."),
        ]))
        runner.run(
            body(run_id, conv_id, order_id="DEMO-1001", kind="approval", message="", quote_id=hq.quote_id,
                 option_id=pickup.option_id, approval_id=approval.approval_id),
            services=services,
        )
        transactions += [e["payload"]["evidence"] for e in events_of(run_id) if e["type"] == "tool_result"
                         and e["payload"]["tool"] == "execute_approved_resolution"]
    assert [t["replayed"] for t in transactions] == [False, True]
    assert transactions[0]["transaction_id"] == transactions[1]["transaction_id"]  # same transaction both times
    assert len([k for k in snapshot() if k[0].startswith("TXN#")]) == 1  # exactly one transaction exists


def test_list_my_orders_shows_agent_placed_orders_next_to_seeded_ones(flow: Flow, services: Services, scripted) -> None:
    make_agent_order(AGENT_ORDER, AVA, delivered_at="2026-10-16")
    conv_id, run_id = flow.start_run(AVA)
    model = scripted(AdaptiveModel([tool_turn("list_my_orders"), text_turn("You have three orders.")]))
    runner.run(body(run_id, conv_id, order_id=None, message="I need to return my shoes."), services=services)
    orders = {o["order_id"]: o for o in model.seen["list_my_orders"]["data"]["orders"]}
    assert {"DEMO-1001", "DEMO-1002", "DEMO-1008", AGENT_ORDER} <= set(orders)  # Ava's seeded orders and hers
    assert orders[AGENT_ORDER]["channel"] == "agent" and orders["DEMO-1001"]["channel"] == "web"
    assert orders[AGENT_ORDER]["lines"][0]["line_id"] == AGENT_LINE
    assert "DEMO-1003" not in orders  # Ben's order is never listed


def test_requote_run_creates_a_fresh_quote_and_retires_the_old_one(
    flow: Flow, services: Services, scripted, fixed_now
) -> None:
    make_agent_order(AGENT_ORDER, AVA, delivered_at="2026-10-16")
    conv_id, first_run = flow.start_run(AVA)
    scripted(AdaptiveModel(hero_turns()))
    runner.run(body(first_run, conv_id), services=services)
    old_quote_id = next(e["payload"]["quote_id"] for e in events_of(first_run) if e["type"] == "quote_created")

    fixed_now.advance(seconds=901)  # past the 15 minute quote lifetime
    assert quotes.get_quote(AVA, old_quote_id).status == "expired"
    conv_id, second_run = flow.start_run(AVA, conv_id, "requote")
    model = scripted(AdaptiveModel([
        # The policy check id is only in the rehydrated conversation: the agent must find it there.
        lambda r: tool_turn("find_resolution_options", deadline_date="2026-10-17",
                            policy_check_id=r["evaluate_return_policy"]["data"]["policy_check_id"]),
        lambda r: tool_turn("record_decision", summary=r["find_resolution_options"]["data"]["ranking"]["reason"]),
        lambda r: tool_turn("create_quote", candidate_set_id=r["find_resolution_options"]["data"]["candidate_set_id"],
                            option_ids=r["find_resolution_options"]["data"]["ranking"]["order"]),
        text_turn("Fresh options are ready; please approve again."),
    ]))
    out = runner.run(
        body(second_run, conv_id, kind="requote", message="", quote_id=old_quote_id), services=services
    )
    assert out["status"] == "completed", out
    assert model.calls[0]["messages"][-1]["content"][0]["text"].startswith(f"[server] Quote {old_quote_id} expired.")
    new_quote_id = next(e["payload"]["quote_id"] for e in events_of(second_run) if e["type"] == "quote_created")
    assert new_quote_id != old_quote_id
    assert quotes.get_quote(AVA, new_quote_id).status == "open"
    assert quotes.get_quote(AVA, old_quote_id).status == "superseded"
    assert [o.type for o in quotes.get_quote(AVA, new_quote_id).options][0] == "exchange_store_pickup"


def test_an_unknown_tool_is_traced_and_the_run_carries_on(flow: Flow, services: Services, scripted) -> None:
    conv_id, run_id = flow.start_run(AVA)
    scripted(ScriptedModel([tool_turn("drop_all_tables"), text_turn("Sorry, I cannot do that.")]))
    out = runner.run(body(run_id, conv_id), services=services)
    assert out["status"] == "completed" and out["metrics"]["tool_count"] == 1
    events = events_of(run_id)
    call = next(e for e in events if e["type"] == "tool_call")
    result = next(e for e in events if e["type"] == "tool_result")
    assert call["payload"]["tool"] == "drop_all_tables"
    assert result["payload"]["ok"] is False and result["payload"]["code"] == "VALIDATION_ERROR"


def test_another_customers_order_is_refused_through_the_agent(flow: Flow, services: Services, scripted) -> None:
    conv_id, run_id = flow.start_run(BEN)
    scripted(ScriptedModel([tool_turn("get_order_context", order_id="DEMO-1001"), text_turn("I cannot see that order.")]))
    out = runner.run(body(run_id, conv_id, customer_id=BEN, order_id="DEMO-1001"), services=services)
    assert out["status"] == "completed"
    result = next(e for e in events_of(run_id) if e["type"] == "tool_result")
    assert result["payload"]["ok"] is False and result["payload"]["code"] == "NOT_AUTHORIZED"


def test_an_undelivered_agent_order_goes_to_a_person(flow: Flow, services: Services, scripted) -> None:
    """delivered_at is null until 'Simulate delivery': the policy says needs_review, the agent escalates."""
    make_agent_order(AGENT_ORDER, AVA, delivered_at=None)
    conv_id, run_id = flow.start_run(AVA)
    scripted(AdaptiveModel([
        tool_turn("evaluate_return_policy", order_id=AGENT_ORDER, line_id=AGENT_LINE, qty=1,
                  reason_category="FIT_SIZE", condition="unworn", size_direction="too_small"),
        tool_turn("escalate_case", reason_code="POLICY_REVIEW", order_id=AGENT_ORDER, line_id=AGENT_LINE,
                  summary="No delivery date on file for this agent-placed order."),
        text_turn("A person will review your case."),
    ]))
    out = runner.run(body(run_id, conv_id), services=services)
    assert out["status"] == "completed"
    events = events_of(run_id)
    policy_result = next(e for e in events if e["type"] == "tool_result")
    assert policy_result["payload"]["evidence"]["decision"] == "needs_review"
    case_event = next(e for e in events if e["type"] == "case_created")
    assert case_event["payload"]["reason_code"] == "POLICY_REVIEW"
    case = store.get_item(store.case_key(case_event["payload"]["case_id"]))
    assert case["order_id"] == AGENT_ORDER and "window.missing_delivery_date" in case["rule_ids"]


def test_dc_stockout_still_leads_with_store_pickup(flow: Flow, scripted, gateway: RecordingGateway, no_flush) -> None:
    fixtures.reset("dc_stockout")
    conv_id, run_id = flow.start_run(AVA)
    model = scripted(AdaptiveModel([
        tool_turn("evaluate_return_policy", order_id="DEMO-1001", line_id="DEMO-1001-L1", qty=1,
                  reason_category="FIT_SIZE", condition="unworn", size_direction="too_small"),
        lambda r: tool_turn("find_resolution_options", deadline_date="2026-10-17",
                            policy_check_id=r["evaluate_return_policy"]["data"]["policy_check_id"]),
        text_turn("ok"),
    ]))
    runner.run(body(run_id, conv_id, order_id="DEMO-1001"), services=Services(gateway=gateway))
    data = model.seen["find_resolution_options"]["data"]
    by_id = {o["option_id"]: o for o in data["options"]}
    ordered_types = [by_id[i]["type"] for i in data["ranking"]["order"]]
    assert "exchange_ship_from_dc" not in ordered_types  # no DC stock in this variant
    assert ordered_types[0] == "exchange_store_pickup" and ordered_types[-2:] == ["return_in_store", "return_mail"]
    assert data["availability"]["dc_atp"] == 0
    first = by_id[data["ranking"]["order"][0]]
    assert first["meets_deadline"] is True and first["eta_text"] == "Sat Oct 17" and first["fee_text"] == "CAD 0.00"
    assert data["ranking"]["reason"].startswith("Store pickup at ALDO Demo Store")
