"""David's bundle return (S19b) against the REAL persistence layer (moto DynamoDB) with a scripted model.

Everything between the model and the table is real: the ten tool wrappers, the Strands loop, the runner, ``orders``,
``options`` (bundle), ``quotes`` (+ approval), ``execution`` (ONE TransactWriteItems for the bundle), ``benefits``
(the monthly ledger), ``cases``, ``trace``, ``runs`` and ``conversations``, with the shipped v2 fixtures: David is
``DEMO-CUST-DAVID`` (Platinum, no store within reach), his order ``DEMO-3001`` has ten one-unit lines. Only the model is
scripted and the write-back transport is a recorder. Each owner message is its own run on the same conversation, as in
the app. Assertions are on table state, not on chat text.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import runner
from agent.services import Services
from returns_domain import benefits, execution, quotes, store, trace

from ..bundle_world import DAVID, JOHN, ORDER, PERK_OCTOBER, Bundles, a2a, keys, ledger, line_id, reserved, silver_twin_order
from ..conftest import Flow
from .scripted_model import AdaptiveModel, text_turn, tool_turn
from .test_integration_real_domain import RecordingGateway, body, events_of, latest, tool_events

ITEMS = ["the tan boot", "the white sneaker", "the navy loafer"]
OWNER_LINES = {
    "handoff": "I ordered the whole new collection and I'm returning 3 of the 10 items.",
    "items": "The tan boot, the white sneaker and the navy loafer. I just don't like them, they're all unworn.",
    "second": "Also return the grey sneaker. I just don't like it either, it's unworn.",
}
MEMORY = {"size_system": "US_M", "typical_size": "10.0", "preferred_colors": ["BROWN"]}


@pytest.fixture
def gateway() -> RecordingGateway:
    return RecordingGateway()


@pytest.fixture
def services(seeded: None, gateway: RecordingGateway, no_flush: list[str]) -> Services:
    return Services(gateway=gateway)


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):
    def install(model: AdaptiveModel) -> AdaptiveModel:
        monkeypatch.setattr(runner, "make_model", lambda model_id: model)
        return model

    return install


def talk(flow: Flow, services: Services, scripted, conv_id: str | None, message: str, turns: list[Any], *,
         customer_id: str = DAVID, order_id: str = ORDER, memory: dict | None = None, kind: str = "customer",
         **payload: Any) -> tuple[str, str, AdaptiveModel]:
    """One owner message = one run. Returns ``(conv_id, run_id, model)``."""
    conv_id, run_id = flow.start_run(customer_id, conv_id, kind)
    model = scripted(AdaptiveModel(turns))
    extra = {"memory_context": memory} if memory else {}
    out = runner.run(
        body(run_id, conv_id, customer_id=customer_id, order_id=order_id, message=message, kind=kind, **extra, **payload),
        services=services,
    )
    assert out["status"] == "completed", out
    return conv_id, run_id, model


def select_turns(items: list[str], numbers: tuple[int, ...], picked: dict[str, Any]) -> list[Any]:
    """get_order_context with the owner's words, then one policy call per matched line (ids kept in ``picked``)."""
    turns: list[Any] = [tool_turn("get_order_context", order_id=ORDER, items=items)]

    def policy(n: int):
        def turn(results: dict[str, Any]) -> Any:
            selection = latest(results, "get_order_context")["selection"]
            assert selection["complete"] is True, selection
            wanted = f"{ORDER}-L{n}"
            assert wanted in selection["selected_line_ids"]
            if n != numbers[0]:
                picked.setdefault("ids", []).append(latest(results, "evaluate_return_policy")["policy_check_id"])
            return tool_turn("evaluate_return_policy", order_id=ORDER, line_id=wanted, qty=1,
                             reason_category="CHANGE_OF_MIND", condition="unworn")
        return turn

    turns += [policy(n) for n in numbers]
    return turns


def bundle_quote_turns(picked: dict[str, Any], numbers_count: int) -> list[Any]:
    def find(results: dict[str, Any]) -> Any:
        picked.setdefault("ids", []).append(latest(results, "evaluate_return_policy")["policy_check_id"])
        assert len(picked["ids"]) == numbers_count
        return tool_turn("find_resolution_options", policy_check_ids=list(picked["ids"]))

    return [
        find,
        lambda r: tool_turn("record_decision", summary=latest(r, "find_resolution_options")["ranking"]["reason"]),
        lambda r: tool_turn(
            "create_quote", candidate_set_id=latest(r, "find_resolution_options")["candidate_set_id"],
            option_ids=latest(r, "find_resolution_options")["ranking"]["order"], preferred_channel="return",
        ),
        text_turn("Please review the card and confirm it."),
    ]


def david_until_the_quote(flow: Flow, services: Services, scripted, memory: dict | None = None):
    """Run 1 lists the items, run 2 selects, checks every line, finds the bundle options and quotes."""
    conv_id, run_1, _ = talk(flow, services, scripted, None, OWNER_LINES["handoff"],
                             [tool_turn("get_order_context", order_id=ORDER), text_turn("Here are your 10 items ...")],
                             memory=memory)
    picked: dict[str, Any] = {}
    conv_id, run_2, model = talk(
        flow, services, scripted, conv_id, OWNER_LINES["items"],
        [*select_turns(ITEMS, (2, 5, 8), picked), *bundle_quote_turns(picked, 3)], memory=memory,
    )
    return conv_id, run_1, run_2, model, picked


def quote_of(run_id: str):
    quote_id = next(e["payload"]["quote_id"] for e in events_of(run_id) if e["type"] == "quote_created")
    return quotes.get_quote(DAVID, quote_id)


def approve_and_execute(flow: Flow, services: Services, scripted, conv_id: str, quote, turns: list[Any] | None = None,
                        *, replay: bool = False):
    option = quote.options[0]
    approval, created = quotes.approve(DAVID, quote.quote_id, option.option_id, option.payload_hash, "RUN-APPROVAL-D1")
    assert created is not replay  # the second click on the same quote finds the approval that exists
    default = [
        tool_turn("execute_approved_resolution", quote_id="QUO-MISTYPED", option_id="OPT-99"),
        lambda r: tool_turn("get_resolution_status", transaction_id=latest(r, "execute_approved_resolution")["transaction_id"]),
        text_turn("Return label created."),
    ]
    return talk(flow, services, scripted, conv_id, "", turns or default, kind="approval", quote_id=quote.quote_id,
                option_id=option.option_id, approval_id=approval.approval_id)


# ------------------------------------------------------------------------------------ the dialogue


@pytest.mark.parametrize("memory", [None, MEMORY], ids=["plain", "with-owner-context"])
def test_davids_whole_dialogue_ends_in_one_transaction_with_three_lines_fee_zero_and_the_ledger_at_one(
    flow: Flow, services: Services, scripted, snapshot, memory: dict | None
) -> None:
    conv_id, run_1, run_2, model, picked = david_until_the_quote(flow, services, scripted, memory)

    # Run 1 only read the order. Run 2 matched the owner's words in code, checked each line, found ONE bundle, quoted.
    assert [e["payload"]["tool"] for e in events_of(run_1) if e["type"] == "tool_call"] == ["get_order_context"]
    selection = latest(model.seen, "get_order_context")["selection"]
    assert [m["line_id"] for m in selection["matches"]] == [f"{ORDER}-L{n}" for n in (2, 5, 8)]
    assert [e["payload"]["tool"] for e in events_of(run_2) if e["type"] == "tool_call"] == [
        "get_order_context", *["evaluate_return_policy"] * 3, "find_resolution_options", "record_decision", "create_quote",
    ]
    find_args = tool_events(run_2, "find_resolution_options")[0]["args"]
    assert find_args["policy_check_id"] is None and len(find_args["policy_check_ids"]) == 3

    found = latest(model.seen, "find_resolution_options")
    (option,) = found["options"]  # David is far from every store: mail only
    assert option["type"] == "return_mail" and "minutes_away" not in option
    assert found["availability"]["reason_no_exchange"] == "BUNDLE_RETURN_ONLY"
    assert option["fee_cents"] == 0 and option["refund_total_cents"] == 41997 and option["refund_total_text"] == "CAD 419.97"
    assert option["benefit"]["saved_text"] == "CAD 5.95" and option["benefit"]["used"] == 1
    assert [l["product_name"] for l in option["lines"]] == ["Brando", "Oscar", "Patten"]
    assert found["ranking"]["reason"].startswith("No store is within reach, so returning 3 items by mail is free")

    quote = quote_of(run_2)
    assert len(quote.lines) == 3 and quote.options[0].type == "return_mail" and quote.options[0].fee_cents == 0
    assert quote.options[0].benefit["saved_cents"] == 595
    summary = latest(model.seen, "create_quote")["bundle"]
    assert summary["options"][0]["net_refund_text"] == "CAD 419.97" and summary["options"][0]["fee_text"] == "CAD 0.00"
    # Nothing was executed, reserved or consumed before the click.
    assert keys("TXN#") == [] and keys("BENEFIT#") == [] and reserved(ORDER, (2, 5, 8)) == {2: 0, 5: 0, 8: 0}

    # The owner approves; the approval run executes and verifies.
    before = snapshot(exclude=("AUDIT#",))
    conv_id, run_3, model_3 = approve_and_execute(flow, services, scripted, conv_id, quote)
    txns = keys("TXN#")
    assert len(txns) == 1  # ONE transaction for the three lines
    txn = store.get_item(txns[0])
    assert len(txn["lines"]) == 3 and txn["fee_cents"] == 0 and txn["option_type"] == "return_mail"
    assert [l["line_id"] for l in txn["lines"]] == [f"{ORDER}-L{n}" for n in (2, 5, 8)]
    assert ledger()["used"] == 1 and reserved(ORDER, (2, 5, 8, 1, 10)) == {2: 1, 5: 1, 8: 1, 1: 0, 10: 0}

    status = latest(model_3.seen, "get_resolution_status")
    assert status["state"] == "completed" and status["fee_text"] == "CAD 0.00" and status["net_refund_text"] == "CAD 419.97"
    assert status["benefit"]["saved_text"] == "CAD 5.95" and status["memory_update_id"]
    assert "Mail the 3 items" in status["next_step"] and len(status["lines"]) == 3
    executed = latest(model_3.seen, "execute_approved_resolution")
    assert executed["transaction_id"] == status["transaction_id"] and executed["benefit"]["saved_text"] == "CAD 5.95"

    after = snapshot(exclude=("AUDIT#",))
    new = {k[0].split("#")[0] for k in after if k not in before and not k[0].startswith("RUN#")}
    assert {"TXN", "IDEM", "BENEFIT"} <= new and "STORENOTE" not in new  # mail: no store notice
    assert [e["type"] for e in a2a(DAVID, "benefit_applied")] == ["benefit_applied"]
    for event in events_of(run_3):
        assert DAVID not in str(event["payload"])  # no customer id in the trace


def test_replaying_the_approval_run_creates_no_second_transaction_and_uses_the_perk_once(
    flow: Flow, services: Services, scripted
) -> None:
    conv_id, _, run_2, _, _ = david_until_the_quote(flow, services, scripted)
    quote = quote_of(run_2)
    approve_and_execute(flow, services, scripted, conv_id, quote)
    _, _, again = approve_and_execute(flow, services, scripted, conv_id, quote, replay=True)
    assert latest(again.seen, "execute_approved_resolution")["replayed"] is True
    assert len(keys("TXN#")) == 1 and ledger()["used"] == 1


def test_an_ambiguous_phrase_gets_a_question_not_a_guess(flow: Flow, services: Services, scripted) -> None:
    conv_id, run_id, model = talk(
        flow, services, scripted, None, "I'm returning the sneaker.",
        [tool_turn("get_order_context", order_id=ORDER, items=["the sneaker"]),
         text_turn("Two sneakers: number 5 (White) or number 10 (Grey). Which one?")],
    )
    selection = latest(model.seen, "get_order_context")["selection"]
    assert selection["complete"] is False and selection["selected_line_ids"] == []
    (match,) = selection["matches"]
    assert match["status"] == "ambiguous" and [c["number"] for c in match["candidates"]] == [5, 10]
    assert [e["payload"]["tool"] for e in events_of(run_id) if e["type"] == "tool_call"] == ["get_order_context"]
    assert keys("PCHK#") == []  # no policy check was made for a guessed line


# ------------------------------------------------------------------------ second bundle, same month


def test_the_second_bundle_of_the_month_pays_the_standard_fee_and_leaves_the_ledger_at_one(
    flow: Flow, services: Services, scripted
) -> None:
    conv_id, _, run_2, _, _ = david_until_the_quote(flow, services, scripted)
    approve_and_execute(flow, services, scripted, conv_id, quote_of(run_2))
    assert ledger()["used"] == 1

    picked: dict[str, Any] = {}
    conv_id, run_4, model = talk(
        flow, services, scripted, conv_id, OWNER_LINES["second"],
        [*select_turns(["the grey sneaker"], (10,), picked), *bundle_quote_turns(picked, 1)],
    )
    found = latest(model.seen, "find_resolution_options")
    (option,) = found["options"]
    assert "benefit" not in option and option["fee_cents"] == 595 and option["fee_text"] == "CAD 5.95"
    assert found["member_perk"] == {
        "type": "free_return_shipping", "label": "Platinum free return shipping", "status": "used_this_month",
        "used": 1, "allowed": 1, "period": PERK_OCTOBER["period"],
    }
    assert option["refund_total_text"] == "CAD 99.99" and option["net_refund_text"] == "CAD 94.04"
    assert "free" not in found["ranking"]["reason"]

    quote = quote_of(run_4)
    assert quote.options[0].fee_cents == 595 and not quote.options[0].benefit
    approve_and_execute(flow, services, scripted, conv_id, quote)
    assert len(keys("TXN#")) == 2 and ledger()["used"] == 1  # a second transaction, no second perk
    assert reserved(ORDER, (2, 5, 8, 10)) == {2: 1, 5: 1, 8: 1, 10: 1}
    assert len(a2a(DAVID, "benefit_applied")) == 1  # only the first shipment used the perk


# ------------------------------------------------------------------------------- BENEFIT_USED


def test_a_perk_used_meanwhile_makes_the_agent_requote_without_the_waiver(
    flow: Flow, services: Services, scripted, snapshot
) -> None:
    conv_id, _, run_2, _, picked = david_until_the_quote(flow, services, scripted)
    first_quote = quote_of(run_2)
    assert first_quote.options[0].benefit  # quoted with the perk

    # Meanwhile another shipment (the grey sneaker) takes the month's only free shipping.
    other = Bundles(flow).approved(numbers=(10,))
    execution.execute(DAVID, other.quote_id, other.option("return_mail").option_id, run_id="RUN-OTHER-SHIP")
    assert ledger()["used"] == 1 and len(keys("TXN#")) == 1

    option = first_quote.options[0]
    approval, _ = quotes.approve(DAVID, first_quote.quote_id, option.option_id, option.payload_hash, "RUN-APPROVAL-D2")
    before = snapshot(exclude=("AUDIT#",))
    conv_id, run_5, model = talk(
        flow, services, scripted, conv_id, "", [
            tool_turn("execute_approved_resolution", quote_id=first_quote.quote_id, option_id=option.option_id),
            lambda r: tool_turn("find_resolution_options", policy_check_ids=list(picked["ids"])),
            lambda r: tool_turn("record_decision", summary=latest(r, "find_resolution_options")["ranking"]["reason"]),
            lambda r: tool_turn("create_quote", candidate_set_id=latest(r, "find_resolution_options")["candidate_set_id"],
                                option_ids=latest(r, "find_resolution_options")["ranking"]["order"],
                                preferred_channel="return"),
            text_turn("The free return shipping was just used, so the standard fee now applies."),
        ], kind="approval", quote_id=first_quote.quote_id, option_id=option.option_id, approval_id=approval.approval_id,
    )
    failed = model.seen["execute_approved_resolution"]
    assert failed["ok"] is False and failed["error"]["code"] == "STOCK_CHANGED"
    assert failed["error"]["details"] == {"reason": "BENEFIT_USED"}
    # The failed execution wrote nothing: one transaction (the other shipment), ledger still 1, lines untouched.
    assert len(keys("TXN#")) == 1 and ledger()["used"] == 1
    assert reserved(ORDER, (2, 5, 8)) == {2: 0, 5: 0, 8: 0}
    assert store.get_item(store.quote_key(first_quote.quote_id))["status"] != "executed"

    found = latest(model.seen, "find_resolution_options")
    (new_option,) = found["options"]
    assert new_option["fee_cents"] == 595 and "benefit" not in new_option and found["member_perk"]["status"] == "used_this_month"
    requote = quotes.get_quote(DAVID, latest(model.seen, "create_quote")["quote_id"])
    assert requote.quote_id != first_quote.quote_id and requote.options[0].fee_cents == 595
    assert requote.options[0].net_refund_cents == 41997 - 595 and not requote.options[0].benefit
    assert [l["line_id"] for l in requote.lines] == [f"{ORDER}-L{n}" for n in (2, 5, 8)]


# ------------------------------------------------------------------------------ authority limit


def test_a_silver_member_with_the_same_total_gets_a_case_and_no_quote_no_grant(
    flow: Flow, services: Services, scripted, snapshot
) -> None:
    order_id = silver_twin_order()  # John (Silver): L2, L5, L8, L10 of David's order, CAD 250 limit
    picked: dict[str, Any] = {}
    turns = [
        tool_turn("get_order_context", order_id=order_id, items=ITEMS),
    ]

    def policy(n: int, first: bool):
        def turn(results: dict[str, Any]) -> Any:
            if not first:
                picked.setdefault("ids", []).append(latest(results, "evaluate_return_policy")["policy_check_id"])
            selection = latest(results, "get_order_context")["selection"]
            assert selection["complete"] is True and f"{order_id}-L{n}" in selection["selected_line_ids"]
            return tool_turn("evaluate_return_policy", order_id=order_id, line_id=f"{order_id}-L{n}", qty=1,
                             reason_category="CHANGE_OF_MIND", condition="unworn")
        return turn

    turns += [policy(2, True), policy(5, False), policy(8, False)]

    def find(results: dict[str, Any]) -> Any:
        picked.setdefault("ids", []).append(latest(results, "evaluate_return_policy")["policy_check_id"])
        return tool_turn("find_resolution_options", policy_check_ids=list(picked["ids"]))

    turns += [
        find,
        # A model that tries to quote anyway is refused by the domain; it then follows the case path.
        lambda r: tool_turn("create_quote", candidate_set_id=latest(r, "find_resolution_options")["candidate_set_id"],
                            option_ids=["OPT-1"], preferred_channel="return"),
        lambda r: tool_turn("escalate_case", reason_code="POLICY_REVIEW", order_id=order_id,
                            summary="Bundle of 3 items, total CAD 419.97, above the CAD 250.00 limit."),
        lambda r: text_turn(f"A person will review this. Case {latest(r, 'escalate_case')['case_id']}."),
    ]
    before = snapshot(exclude=("AUDIT#", "RUN#"))
    conv_id, run_id, model = talk(flow, services, scripted, None, OWNER_LINES["items"], turns, customer_id=JOHN,
                                  order_id=order_id)

    found = latest(model.seen, "find_resolution_options")
    # John lives 5 minutes from a store: in-store and mail options, both marked for review, neither with a perk
    assert [o["type"] for o in found["options"]] == ["return_in_store", "return_mail"]
    assert found["bundle"]["decision"] == "needs_review"
    for option in found["options"]:
        assert option["decision"] == "needs_review" and option["rule_ids"] == ["authority.refund_limit"]
        assert "benefit" not in option and option["fee_cents"] in (0, 595)
        assert option["refund_total_text"] == "CAD 419.97" and option["authority_limit_text"] == "CAD 250.00"
    assert found["ranking"]["reason"].startswith("A person must review this return: CAD 419.97 for 3 items")
    assert "member_perk" not in found
    refused = model.seen["create_quote"]
    assert refused["ok"] is False and refused["error"]["details"]["decision"] == "needs_review"

    case_event = next(e for e in events_of(run_id) if e["type"] == "case_created")
    assert case_event["payload"]["reason_code"] == "POLICY_REVIEW"
    case = store.get_item(store.case_key(case_event["payload"]["case_id"]))
    assert case["order_id"] == order_id and case["status"] == "open"
    # Nothing was granted: no quote, no approval, no transaction, no perk, no reserved quantity.
    after = snapshot(exclude=("AUDIT#", "RUN#"))
    new = {k[0].split("#")[0] for k in after if k not in before}
    assert new <= {"PCHK", "CAND", "CASE", "CASES", "CONV"} and not new & {"QUOTE", "TXN", "IDEM", "BENEFIT"}
    assert keys("QUOTE#") == [] and keys("TXN#") == [] and keys("BENEFIT#") == []
    assert benefits.ledger_usage(JOHN)["allowed"] == 0  # Silver has no shipping perk to talk about
