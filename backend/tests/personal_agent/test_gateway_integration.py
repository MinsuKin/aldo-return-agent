"""ALDO bot against the REAL ``returns_domain.gateway`` on moto (not the canned answers).

Two moto tables live side by side: the ALDO returns table (fixtures, size requests, inventory,
A2A stream) and the personal-agent table. The model is scripted; everything else is real: the
delegated token, the ``local`` transport, the gateway's validation and persistence, the proposal,
and then the owner's confirmation (``gateway.confirm_purchase``, the simulated click).
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from personal_agent import memory, runner, tools
from personal_agent.gateway_client import GatewayCallError, LocalGatewayClient
from personal_agent.memory import MemoryRepo
from personal_agent.tools import PersonalRunContext
from returns_domain import gateway, store, trace

from .scripted import ScriptedModel, tool_results

AVA, BEN = "DEMO-CUST-AVA", "DEMO-CUST-BEN"
MESSAGE = "Order me the ALDO Stessy heels for the wedding Saturday."


@pytest.fixture
def both(seeded: None) -> Iterator[MemoryRepo]:
    """The seeded ALDO table (tests/conftest.py) plus a seeded personal-agent table, one moto session."""
    table = MemoryRepo()
    table.create_table()
    table.seed()
    memory.set_repo(table)
    yield table


def run_policy(answer_for: Any) -> Any:
    """Recall, ask, record, then propose what ``answer_for(ask_result)`` picks, then reply."""

    def policy(messages: list[dict[str, Any]], call: int):
        found = tool_results(messages)
        if call == 1:
            return "tools", [("recall_size_memory", {})]
        if call == 2:
            return "tools", [("ask_aldo_size_assist", {"product_query": "Stessy", "deadline_date": "2026-10-17"})]
        if call == 3:
            return "tools", [("record_decision", {"summary": "Size 8.0, Montreal pickup"})]
        if call == 4:
            size, option_id = answer_for(found[1]["data"])
            return "tools", [
                (
                    "send_size_decision",
                    {
                        "size_request_id": found[1]["data"]["size_request_id"],
                        "option_id": option_id,
                        "chosen_size": size,
                        "rationale_public": "Retailer C sneaker 8.0 was too big, usual 7.5; style runs small per ALDO fit signal",
                        "confidence": "high",
                    },
                )
            ]
        return "text", "Your proposal for size 8.0 is ready. Please confirm it in the app."

    return policy


def montreal_pickup(ask: dict[str, Any]) -> tuple[str, str]:
    for entry in ask["sizes"]:
        if entry["size"] == "8.0":
            option = next(o for o in entry["options"] if o["type"] == "store_pickup" and "Montreal" in o["location_name"])
            return "8.0", option["option_id"]
    raise AssertionError("size 8.0 is not offered")


def hero_stock(location: str = "DEMO-STORE-MTL-01") -> int:
    return int(store.get_item(store.inventory_key("DEMO-SKU-HERO-BLACK-080", location))["atp"])


def test_a_real_proposal_is_recorded_without_an_order_and_the_owner_can_confirm_it(
    both: MemoryRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    monkeypatch.setattr(runner, "build_model", lambda model_id: ScriptedModel(run_policy(montreal_pickup)))
    stock_before = hero_stock()
    result = runner.run(
        {"run_id": "PRUN-REAL01", "conv_id": "PCONV-REAL01", "customer_id": AVA, "message": MESSAGE, "agent": "personal"},
        repo=both,  # the gateway client is built by the runner: delegated token + local transport
    )
    assert result["status"] == "completed", result
    events = both.list_events(AVA, "PRUN-REAL01")["events"]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_completed"
    assert "size_recommended" in kinds and "decision" in kinds

    # what the real gateway answered, as the model saw it
    answer = next(e for e in events if e["type"] == "tool_result" and e["payload"]["tool"] == "ask_aldo_size_assist")
    assert answer["payload"]["ok"] is True and answer["payload"]["evidence"]["signal"] == "runs_small"
    assert answer["payload"]["evidence"]["n_fit"] == 582
    recommended = next(e for e in events if e["type"] == "size_recommended")["payload"]
    assert (recommended["chosen_size"], recommended["option_type"], recommended["status"]) == ("8.0", "store_pickup", "proposed")
    assert recommended["location_name"] == "ALDO Demo Store Montreal" and recommended["meets_deadline"] is True
    request_id = next(e for e in events if e["type"] == "tool_result" and e["payload"]["tool"] == "ask_aldo_size_assist")["payload"]["evidence"]["size_request_id"]

    # the real record: a proposal, with the sanitized rationale, and NOTHING else changed
    record = store.get_item(store.sizereq_key(request_id))
    assert record["status"] == "proposed" and record["proposed_size"] == "8.0" and record["confidence"] == "high"
    for leak in ("Retailer", "sneaker", "7.5", "8.0 was"):
        assert leak not in record["rationale_public"]
    assert "ALDO fit signal" in record["rationale_public"]
    assert list(store.scan_keys(("ORDER#DEMO-A2A-",))) == []  # no order yet
    assert hero_stock() == stock_before  # no stock change

    # the A2A stream carries the public events only, in order, written by the gateway
    a2a_events = trace.list_a2a_events(AVA).events
    assert [e["type"] for e in a2a_events] == ["size_assist_requested", "fit_signal_shared", "size_requested", "size_recommended"]
    assert "Retailer" not in str(a2a_events) and "sneaker" not in str(a2a_events) and AVA not in str(a2a_events)
    public_rationale = a2a_events[-1]["payload"]["rationale_public"]
    assert "7.5" not in public_rationale and "8.0 was" not in public_rationale  # the owner's numbers stay home
    assert public_rationale == record["rationale_public"] and a2a_events[-1]["actor"] == "personal_agent"

    # the owner clicks Confirm: the real gateway turns the proposal into an order
    order = gateway.confirm_purchase(AVA, request_id)
    assert order.order_id.startswith("DEMO-A2A-") and order.fulfillment.location_name == "ALDO Demo Store Montreal"
    assert hero_stock() == stock_before - 1
    assert store.get_item(store.sizereq_key(request_id))["status"] == "decided"
    assert trace.list_a2a_events(AVA).events[-1]["type"] == "size_confirmed"


def test_the_real_gateway_refuses_a_choice_the_bot_did_not_make_from_the_offer(both: MemoryRepo) -> None:
    ctx = new_context(both)
    ask = tools.tool_ask_aldo_size_assist(ctx, "Stessy", deadline_date="2026-10-17")
    assert ask["ok"] is True
    # bypass the local offer check, as a forged id from an earlier turn would: ALDO still says no
    ctx.offered.clear()
    refused = tools.tool_send_size_decision(
        ctx, ask["data"]["size_request_id"], "SO-999", "8.0", "runs small", "high"
    )
    assert refused["ok"] is False and refused["error"]["code"] == "INVALID_SIZE_CHOICE"
    record = store.get_item(store.sizereq_key(ask["data"]["size_request_id"]))
    assert record["status"] == "open"  # nothing was recorded


def test_an_unknown_style_reaches_the_model_with_its_candidates(both: MemoryRepo) -> None:
    ctx = new_context(both)
    result = tools.tool_ask_aldo_size_assist(ctx, "Zzzzz")
    assert result["ok"] is False and result["error"]["code"] == "UNKNOWN_STYLE"
    assert result["error"].get("candidates", []) == [] or all("product_name" in c for c in result["error"]["candidates"])


def test_the_hero_answer_has_the_shape_the_tools_expect(both: MemoryRepo) -> None:
    ctx = new_context(both)
    data = tools.tool_ask_aldo_size_assist(ctx, "Stessy", deadline_date="2026-10-17")["data"]
    assert data["fit_signal"]["signal"] == "runs_small" and data["fit_signal"]["n_fit"] == 582
    assert data["product_name"] and data["price_cents"] == 12999 and data["currency"] == "CAD"
    sizes = {entry["size"]: entry["options"] for entry in data["sizes"]}
    assert {"7.5", "8.0"} <= set(sizes)
    pickups = [o for o in sizes["8.0"] if o["type"] == "store_pickup"]
    assert [o["location_name"] for o in pickups] == ["ALDO Demo Store Montreal"]
    assert all(o["meets_deadline"] is True for o in pickups)
    assert all(o["meets_deadline"] is False for o in sizes["8.0"] if o["type"] != "store_pickup")
    assert all(set(o) == {"option_id", "type", "location_id", "location_name", "eta_date", "eta_text", "meets_deadline", "atp"} for o in sizes["8.0"])
    assert data["aldo_question"].startswith("This style runs small")


def test_dc_stockout_leaves_only_the_montreal_store_options(both: MemoryRepo) -> None:
    from returns_domain import fixtures

    fixtures.reset("dc_stockout")
    ctx = new_context(both)
    data = tools.tool_ask_aldo_size_assist(ctx, "Stessy", deadline_date="2026-10-17")["data"]
    options = {entry["size"]: entry["options"] for entry in data["sizes"]}["8.0"]
    assert {o["type"] for o in options} == {"ship_from_store", "store_pickup"}
    assert {o["location_name"] for o in options} == {"ALDO Demo Store Montreal"}


def test_a_us_m_owner_is_refused_by_the_gateway_with_a_clear_error(both: MemoryRepo) -> None:
    ctx = new_context(both, owner=BEN)
    assert ctx.size_system == "US_M"
    result = tools.tool_ask_aldo_size_assist(ctx, "Stessy")
    assert result["ok"] is False and result["error"]["code"] == "VALIDATION_ERROR"


def test_a_session_token_cannot_drive_the_gateway_client(both: MemoryRepo) -> None:
    from returns_domain import auth

    client = LocalGatewayClient(auth.sign_token(AVA).token)  # a session token, not a delegated one
    with pytest.raises(GatewayCallError) as caught:
        client.size_assist({"intent": "size_assist", "product_query": "Stessy"})
    assert caught.value.code == "NOT_AUTHORIZED"


def new_context(repo: MemoryRepo, owner: str = AVA) -> PersonalRunContext:
    conv = repo.create_conversation(owner)
    run_id = memory.new_run_id()
    repo.create_run(owner, conv, run_id)
    token = runner.mint_delegated_token(owner)
    return PersonalRunContext(
        run_id=run_id,
        conv_id=conv,
        owner_id=owner,
        gateway=LocalGatewayClient(token),
        repo=repo,
        size_system=repo.get_owner(owner)["size_system"],
    )
