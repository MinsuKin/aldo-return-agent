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

from .canned import load_assist, load_reviews
from .scripted import ScriptedModel, john_policy, tool_results

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


def test_a_us_m_owner_sends_us_m_and_the_gateway_now_serves_it(both: MemoryRepo) -> None:
    ctx = new_context(both, owner=BEN)
    assert ctx.size_system == "US_M"
    result = tools.tool_ask_aldo_size_assist(ctx, "Stessy")
    assert result["ok"] is True and result["data"]["product_name"]  # the gateway accepts both size systems (demo v2)
    assert store.get_item(store.sizereq_key(result["data"]["size_request_id"]))["size_system"] == "US_M"


JOHN = "DEMO-CUST-JOHN"
JOHN_MESSAGE = "I loved the Toney in black but the store did not have my size. Please buy them for me."


def test_johns_whole_purchase_against_the_real_gateway(both: MemoryRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory + the real fit signal + the real reviews (one is the injection) -> a 9.5 proposal, nothing ordered."""
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    monkeypatch.setattr(runner, "build_model", lambda model_id: ScriptedModel(john_policy()))
    stock = lambda: int(store.get_item(store.inventory_key("DEMO-SKU-MEN001-BLACK-095", "DEMO-STORE-LAVAL-01"))["atp"])
    before = stock()
    result = runner.run(
        {"run_id": "PRUN-JOHN01", "conv_id": "PCONV-JOHN01", "customer_id": JOHN, "message": JOHN_MESSAGE, "agent": "personal"},
        repo=both,
    )
    assert result["status"] == "completed", result
    assert result["metrics"]["reviews_read"] is True and result["metrics"]["reviews_flagged"] == 1
    events = both.list_events(JOHN, "PRUN-JOHN01")["events"]
    ask = next(e for e in events if e["type"] == "tool_result" and e["payload"]["tool"] == "ask_aldo_size_assist")["payload"]
    assert ask["evidence"]["signal"] == "runs_large" and ask["evidence"]["style"]
    request_id = ask["evidence"]["size_request_id"]
    read = next(e for e in events if e["type"] == "tool_result" and e["payload"]["tool"] == "read_product_reviews")["payload"]
    assert read["ok"] is True and read["evidence"]["n_reviews"] == 16 and read["evidence"]["n_flagged"] == 1

    record = store.get_item(store.sizereq_key(request_id))
    assert (record["status"], record["proposed_size"], record["size_system"]) == ("proposed", "9.5", "US_M")
    assert record["offered"][record["proposed_option_id"]]["type"] == "store_pickup"
    assert record["offered"][record["proposed_option_id"]]["location_id"] == "DEMO-STORE-LAVAL-01"
    for leak in ("Retailer", "boot", "10.5", "10.0", "ignore", "order size 6"):
        assert leak not in record["rationale_public"], leak
    assert list(store.scan_keys(("ORDER#DEMO-A2A-",))) == [] and stock() == before  # nothing is ordered yet

    stream = trace.list_a2a_events(JOHN).events
    assert [e["type"] for e in stream] == [
        "size_assist_requested", "fit_signal_shared", "size_requested", "reviews_shared", "size_recommended",
    ]
    assert stream[3]["actor"] == "aldo_gateway" and stream[3]["payload"]["n_flagged"] == 1
    public = str(stream).lower()
    assert "ignore all previous" not in public and "retailer" not in public and JOHN.lower() not in public

    order = gateway.confirm_purchase(JOHN, request_id)  # the owner's click
    assert order.order_id.startswith("DEMO-A2A-") and stock() == before - 1
    gateway.simulate_delivery(JOHN, order.order_id)
    assert [e["type"] for e in trace.list_a2a_events(JOHN).events][-2:] == ["size_confirmed", "delivered"]


def test_the_canned_mens_answer_is_still_what_the_real_gateway_says(both: MemoryRepo) -> None:
    """The canned data was written by the real gateway; this stops it drifting from the fixtures."""
    from returns_domain import auth

    token = auth.sign_token(JOHN, scope="delegated_gateway", actor="personal_agent")
    real = gateway.size_assist(
        auth.verify_claims(token.token, "delegated_gateway"),
        {"intent": "size_assist", "product_query": "Toney", "color": "BLACK", "size_system": "US_M", "deadline_date": None, "channel_pref": None},
    ).model_dump(mode="json")
    canned = load_assist("men001")
    for key in ("style_id", "product_name", "color", "price_cents", "currency", "fit_signal", "sizes", "question"):
        assert real[key] == canned[key], key


@pytest.mark.parametrize("variant,style", [("men001", "DEMO-STYLE-MEN001"), ("default", "DEMO-STYLE-HERO"), ("runs_large", "DEMO-STYLE-CONTRAST")])
def test_the_canned_reviews_are_still_what_the_real_gateway_says(both: MemoryRepo, variant: str, style: str) -> None:
    from returns_domain import auth

    token = auth.sign_token(JOHN, scope="delegated_gateway", actor="personal_agent")
    real = gateway.get_reviews(auth.verify_claims(token.token, "delegated_gateway"), {"style_id": style, "limit": 20})
    assert real.model_dump(mode="json") == load_reviews(variant)


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
