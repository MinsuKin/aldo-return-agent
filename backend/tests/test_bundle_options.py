"""Bundle options and quotes (domain v2 phase 2): ``find_options(policy_check_ids=[...])`` and ``create_quote``.

David returns L2 + L5 + L8 (41997) of his ten-line order by mail; the same totals for a silver member
need a person; preconditions are typed errors that write nothing.
"""

from __future__ import annotations

import json

import pytest

from returns_domain import benefits, options, orders, quotes, store
from returns_domain.errors import DomainError, ErrorCode
from tests.bundle_world import (
    DAVID, DC, JOHN, MTL, ORDER, PERK_OCTOBER, PRICES, SCRIPT, STANDARD_FEE, NEXT_MONTH_NOW,
    Bundles, a2a, keys, ledger, line_id, silver_twin_order,
)
from tests.conftest import AVA, Flow, make_agent_order

pytestmark = pytest.mark.usefixtures("no_flush")


@pytest.fixture
def bundles(flow: Flow) -> Bundles:
    return Bundles(flow)


# ------------------------------------------------------------------ David, three of ten


def test_david_gets_one_mail_option_with_the_platinum_waiver(bundles):
    conv_id, run_id, cand = bundles.find()
    assert [o.type for o in cand.options] == ["return_mail"]  # every store is 430+ minutes away: no drop-off
    option = cand.options[0]
    assert option.option_id == "OPT-1"
    assert (option.fee_cents, option.refund_total_cents, option.net_refund_cents, option.refund_cents) == (0, 41997, 41997, 41997)
    assert option.benefit == PERK_OCTOBER
    assert (option.decision, option.rule_ids, option.authority_limit_cents) == ("eligible", [], 75000)
    assert (option.qty, option.sku, option.location_id, option.eta_date) == (3, "DEMO-SKU-NEW02-TAN-105", DC, "2026-10-26")
    assert [(e["line_id"], e["qty"], e["refund_cents"]) for e in option.lines] == [
        ("DEMO-3001-L2", 1, 17999), ("DEMO-3001-L5", 1, 10999), ("DEMO-3001-L8", 1, 12999)]
    assert sum(e["refund_cents"] for e in option.lines) == option.refund_total_cents
    assert [e["category"] for e in option.lines] == ["boot", "sneaker", "loafer"]  # "the tan boot, the white sneaker ..."
    assert [e["color"] for e in option.lines] == ["TAN", "WHITE", "NAVY"]
    assert "no return label fee" in option.conditions[0] and "1 of 1 this month" in option.conditions[0]
    assert cand.availability["reason_no_exchange"] == "BUNDLE_RETURN_ONLY"  # a bundle is never an exchange
    assert cand.bundle == {"order_id": ORDER, "n_lines": 3, "qty": 3, "refund_total_cents": 41997,
                           "authority_limit_cents": 75000, "tier": "platinum", "decision": "eligible"}
    assert len(cand.policy_check_ids) == 3 and cand.policy_check_id == cand.policy_check_ids[0]
    assert cand.other_sizes_in_stock == [] and cand.inventory_versions == {}


def test_the_option_json_with_and_without_the_benefit_is_exact(bundles):
    _, _, cand = bundles.find()
    body = json.loads(cand.model_dump_json())
    option = body["options"][0]
    assert set(option) >= {"option_id", "type", "sku", "location_id", "location_name", "qty", "refund_cents", "fee_cents",
                           "currency", "eta_date", "eta_text", "conditions", "evidence", "source_refs", "provenance",
                           "lines", "refund_total_cents", "net_refund_cents", "benefit", "decision", "rule_ids",
                           "authority_limit_cents"}
    assert option["benefit"] == {"type": "free_return_shipping", "label": "Platinum free return shipping", "saved_cents": 595,
                                 "used": 1, "allowed": 1, "period": "2026-10"}
    assert option["evidence"]["free_return_shipping"] == "applied" and option["evidence"]["n_lines"] == 3
    assert all(isinstance(v, int) for v in (option["fee_cents"], option["refund_total_cents"], option["net_refund_cents"]))
    # without the perk (used up): same shape, fee 595, no `benefit` key at all
    store.transact_write([benefits.ledger_update(DAVID, "2026-10", 1)])
    _, _, again = bundles.find()
    plain = json.loads(again.model_dump_json())["options"][0]
    assert "benefit" not in plain and plain["fee_cents"] == STANDARD_FEE
    assert (plain["refund_total_cents"], plain["net_refund_cents"], plain["refund_cents"]) == (41997, 41997 - 595, 41997 - 595)
    assert plain["decision"] == "eligible" and plain["evidence"].get("free_return_shipping") is None


def test_option_ids_and_the_order_of_lines_are_deterministic(bundles):
    first = bundles.find()[2]
    second = bundles.find()[2]
    assert [o.option_id for o in first.options] == [o.option_id for o in second.options] == ["OPT-1"]
    assert [e["line_id"] for e in first.options[0].lines] == [e["line_id"] for e in second.options[0].lines]
    reordered = bundles.find(numbers=(8, 2, 5))[2]  # the caller's order is kept
    assert [e["line_id"] for e in reordered.options[0].lines] == [line_id(8), line_id(2), line_id(5)]
    assert reordered.options[0].refund_total_cents == 41997


def test_the_second_bundle_of_the_month_pays_the_fee(bundles):
    from returns_domain import execution

    hq = bundles.approved()
    execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    _, _, cand = bundles.find(numbers=(10,))
    option = cand.options[0]
    assert option.benefit is None and option.fee_cents == STANDARD_FEE
    assert (option.refund_total_cents, option.net_refund_cents) == (PRICES[10], PRICES[10] - STANDARD_FEE)
    assert "5.95" in option.conditions[0]


def test_a_new_month_offers_the_perk_again(bundles, monkeypatch):
    store.transact_write([benefits.ledger_update(DAVID, "2026-10", 1)])
    assert bundles.find(numbers=(10,))[2].options[0].benefit is None
    monkeypatch.setenv("DEMO_NOW", NEXT_MONTH_NOW)
    option = bundles.find(numbers=(10,))[2].options[0]
    assert option.benefit == {**PERK_OCTOBER, "period": "2026-11"} and option.fee_cents == 0


def test_the_deadline_and_channel_do_not_change_a_bundle(bundles):
    conv_id, run_id = bundles.flow.start_run(DAVID)
    ids = bundles.checks(DAVID, ORDER, [line_id(2), line_id(5)], run_id)
    cand = options.find_options(DAVID, policy_check_ids=ids, deadline_date="2026-10-20", preferred_channel="return", run_id=run_id)
    assert cand.deadline_date == "2026-10-20" and cand.preferred_channel == "return"
    assert cand.options[0].meets_deadline is None  # a refund has no delivery deadline


def test_a_single_check_still_works_as_a_one_line_bundle(bundles):
    option = bundles.find(numbers=(5,))[2].options[0]
    assert (option.qty, option.refund_total_cents, option.fee_cents, option.benefit["saved_cents"]) == (1, 10999, 0, 595)
    assert len(option.lines) == 1


# ------------------------------------------------------------------- tier authority


def test_a_silver_member_with_the_same_total_needs_a_person(bundles):
    silver_twin_order()
    david = bundles.find(DAVID)[2]
    john = bundles.find(JOHN, order_id="DEMO-A2A-5B1B2C")[2]
    assert david.bundle["decision"] == "eligible" and all(o.decision == "eligible" for o in david.options)
    assert john.bundle["decision"] == "needs_review"
    assert john.bundle["refund_total_cents"] == 41997 and john.bundle["authority_limit_cents"] == 25000
    # the refund options stay visible (R6.5), they are just not grantable by the agent
    assert [o.type for o in john.options] == ["return_in_store", "return_mail"]  # Montreal is 5 minutes from John
    for option in john.options:
        assert (option.decision, option.rule_ids, option.authority_limit_cents) == ("needs_review", ["authority.refund_limit"], 25000)
        assert option.refund_total_cents == 41997
        assert "A person must review this return" in option.conditions[-1] and "CAD 250.00" in option.conditions[-1]
    mail = john.options[1]
    assert mail.benefit is None and mail.fee_cents == STANDARD_FEE  # silver has no shipping perk
    store_option = john.options[0]
    assert (store_option.location_id, store_option.minutes_away, store_option.fee_cents, store_option.net_refund_cents) == (MTL, 5, 0, 41997)


def test_create_quote_refuses_an_option_that_needs_review(bundles):
    silver_twin_order()
    conv_id, run_id, cand = bundles.find(JOHN, order_id="DEMO-A2A-5B1B2C")
    before = keys("QUOTE#")
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(JOHN, run_id, conv_id, cand.candidate_set_id, [cand.options[1].option_id])
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "needs a person's review" in exc.value.message
    assert exc.value.details == {"decision": "needs_review", "rule_ids": ["authority.refund_limit"]}
    assert keys("QUOTE#") == before == []


def test_platinum_is_fine_up_to_75000_and_needs_review_above(bundles):
    big = bundles.find(DAVID, numbers=tuple(range(1, 11)))[2]  # all ten lines: 152990 (the fixtures README says 151990)
    assert big.bundle["refund_total_cents"] == 152990
    assert big.bundle["decision"] == "needs_review" and big.options[0].decision == "needs_review"
    assert big.options[0].benefit is None  # a bundle nobody can grant is not given the perk either
    ok = bundles.find(DAVID, numbers=(1, 2, 3, 4))[2]  # 15999 + 17999 + 16999 + 19999 = 70996
    assert ok.bundle["refund_total_cents"] == 70996 and ok.bundle["decision"] == "eligible"
    edge = bundles.find(DAVID, numbers=(1, 2, 3, 4, 7))[2]  # + 18999 = 89995 > 75000
    assert edge.bundle["decision"] == "needs_review"


def test_a_silver_bundle_under_the_limit_is_eligible_with_a_store_option(bundles):
    silver_twin_order()
    cand = bundles.find(JOHN, numbers=(5, 8), order_id="DEMO-A2A-5B1B2C")[2]  # 10999 + 12999 = 23998
    assert cand.bundle["decision"] == "eligible"
    assert [(o.type, o.decision, o.fee_cents) for o in cand.options] == [("return_in_store", "eligible", 0), ("return_mail", "eligible", 595)]


# ---------------------------------------------------------------------- the quote


def test_the_quote_carries_the_lines_and_the_benefit(bundles):
    hq = bundles.quote()
    quote = hq.quote
    assert quote.status == "open" and quote.order_id == ORDER and quote.line_id == line_id(2)
    assert quote.policy_check_ids == hq.candidates.policy_check_ids and quote.policy_check_id == quote.policy_check_ids[0]
    assert [e["line_id"] for e in quote.lines] == [line_id(2), line_id(5), line_id(8)]
    option = quote.options[0]
    assert option.benefit == PERK_OCTOBER and option.fee_cents == 0 and option.net_refund_cents == 41997
    assert len(option.payload_hash) == 64 and option.lines == quote.lines
    body = json.loads(quotes.get_quote(DAVID, quote.quote_id).model_dump_json())
    assert body["lines"] == json.loads(json.dumps(quote.lines)) and body["policy_check_ids"] == quote.policy_check_ids
    assert body["options"][0]["benefit"] == PERK_OCTOBER and "customer_id" not in body
    # the stored quote item has them too
    item = store.get_item(store.quote_key(quote.quote_id))
    assert item["policy_check_ids"] == quote.policy_check_ids and len(item["lines"]) == 3
    assert [e["type"] for e in a2a(DAVID, "quote_created")] == ["quote_created"]
    assert a2a(DAVID, "quote_created")[0]["payload"]["primary_option_type"] == "return_mail"


def test_a_single_line_quote_keeps_its_old_json(flow):
    hq = flow.hero_quote()
    body = json.loads(hq.quote.model_dump_json())
    assert "lines" not in body and "policy_check_ids" not in body
    for option in body["options"]:
        assert not set(option) & {"lines", "refund_total_cents", "net_refund_cents", "benefit", "decision", "rule_ids",
                                  "authority_limit_cents"}
    cand = json.loads(hq.candidates.model_dump_json())
    assert "bundle" not in cand and "policy_check_ids" not in cand and "needs_choice" not in cand


def test_the_payload_hash_binds_the_lines_and_the_perk(bundles):
    hq = bundles.quote()
    stored = store.json_safe(hq.quote.options[0])
    policy_version = hq.quote.policy_version
    original = quotes.compute_payload_hash(hq.quote_id, stored, policy_version)
    assert original == stored["payload_hash"]
    swapped = json.loads(json.dumps(stored))
    swapped["lines"][0]["line_id"] = line_id(4)
    assert quotes.compute_payload_hash(hq.quote_id, swapped, policy_version) != original
    dearer = json.loads(json.dumps(stored))
    dearer["lines"][0]["refund_cents"] += 1
    assert quotes.compute_payload_hash(hq.quote_id, dearer, policy_version) != original
    no_perk = json.loads(json.dumps(stored))
    del no_perk["benefit"]
    assert quotes.compute_payload_hash(hq.quote_id, no_perk, policy_version) != original


def test_a_quote_whose_perk_was_used_since_find_options_is_refused(bundles):
    conv_id, run_id, cand = bundles.find()
    store.transact_write([benefits.ledger_update(DAVID, "2026-10", 1)])  # someone used this month's perk
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(DAVID, run_id, conv_id, cand.candidate_set_id, ["OPT-1"])
    assert exc.value.code == ErrorCode.STOCK_CHANGED and exc.value.details == {"reason": "BENEFIT_USED"}
    assert keys("QUOTE#") == []


# -------------------------------------------------------------- typed precondition errors


def test_preconditions_are_typed_errors_and_write_nothing(bundles, snapshot):
    flow = bundles.flow
    other = make_agent_order("DEMO-A2A-AAAAAA", DAVID, delivered_at="2026-10-13")
    conv_id, run_id = flow.start_run(DAVID)
    l2, l5, l8 = bundles.checks(DAVID, ORDER, [line_id(2), line_id(5), line_id(8)], run_id)
    elsewhere = bundles.checks(DAVID, other, [f"{other}-L1"], run_id)[0]
    worn = orders.check_policy(DAVID, run_id, ORDER, line_id(4), 1, "CHANGE_OF_MIND", "worn", "unknown").policy_check_id
    again_l2 = bundles.checks(DAVID, ORDER, [line_id(2)], run_id)[0]
    ava_check = orders.check_policy(AVA, "RUN-AVA", "DEMO-1001", "DEMO-1001-L1", 1, "FIT_SIZE", "unworn", "too_small").policy_check_id
    ten = bundles.checks(DAVID, ORDER, [line_id(n) for n in range(1, 11)], run_id)
    before = snapshot(exclude=("AUDIT#",))

    def refused(code, fragment, **kwargs):
        with pytest.raises(DomainError) as exc:
            options.find_options(DAVID, run_id=run_id, **kwargs)
        assert exc.value.code == code and fragment in exc.value.message, exc.value.message
        assert snapshot(exclude=("AUDIT#",)) == before  # nothing was written
        return exc.value

    refused(ErrorCode.VALIDATION_ERROR, "same order", policy_check_ids=[l2, elsewhere])  # mixed orders
    refused(ErrorCode.VALIDATION_ERROR, "not eligible", policy_check_ids=[l2, worn])  # a line that is not eligible
    refused(ErrorCode.VALIDATION_ERROR, "at most 10", policy_check_ids=[*ten, elsewhere])  # eleven lines
    refused(ErrorCode.VALIDATION_ERROR, "only be in a bundle once", policy_check_ids=[l2, l2])  # the same check twice
    refused(ErrorCode.VALIDATION_ERROR, "only be in a bundle once", policy_check_ids=[l2, again_l2])  # two checks, one line
    refused(ErrorCode.VALIDATION_ERROR, "at least one", policy_check_ids=[])
    refused(ErrorCode.VALIDATION_ERROR, "at least one", policy_check_ids="PCHK-1")
    refused(ErrorCode.VALIDATION_ERROR, "policy check ids", policy_check_ids=[l2, 7])
    refused(ErrorCode.VALIDATION_ERROR, "not both", policy_check_id=l2, policy_check_ids=[l5])
    refused(ErrorCode.VALIDATION_ERROR, "is required")  # neither
    refused(ErrorCode.VALIDATION_ERROR, "no exchange", policy_check_ids=[l2, l5], replacement_size="10.5")
    refused(ErrorCode.VALIDATION_ERROR, "no exchange", policy_check_ids=[l2, l5], replacement_color="BROWN")
    refused(ErrorCode.MISSING_POLICY, "No policy check", policy_check_ids=[l2, "PCHK-NOPE"])
    with pytest.raises(DomainError) as exc:  # another customer's check
        options.find_options(DAVID, policy_check_ids=[l2, ava_check], run_id=run_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert keys("CAND#") == []  # not one candidate set was stored


def test_ten_lines_are_allowed_and_eleven_are_not(bundles):
    conv_id, run_id = bundles.flow.start_run(DAVID)
    ids = bundles.checks(DAVID, ORDER, [line_id(n) for n in range(1, 11)], run_id)
    cand = options.find_options(DAVID, policy_check_ids=ids, run_id=run_id)
    assert len(cand.options[0].lines) == 10 and cand.options[0].qty == 10
    with pytest.raises(DomainError) as exc:
        options.find_options(DAVID, policy_check_ids=[*ids, ids[0]], run_id=run_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "at most 10" in exc.value.message


def test_a_line_that_was_already_returned_cannot_join_a_bundle(bundles):
    from returns_domain import execution

    conv_id, run_id = bundles.flow.start_run(DAVID)
    ids = bundles.checks(DAVID, ORDER, [line_id(2), line_id(5), line_id(8)], run_id)  # all eligible NOW
    single = bundles.flow.hero_quote(  # L5 goes back on its own first
        DAVID, order_id=ORDER, line_id=line_id(5), reason="CHANGE_OF_MIND", direction="unknown", deadline=None,
        types=("return_mail",))
    bundles.flow.approve(single, "return_mail")
    execution.execute(DAVID, single.quote_id)
    before_cands = keys("CAND#")
    with pytest.raises(DomainError) as exc:
        options.find_options(DAVID, policy_check_ids=ids, run_id=run_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and line_id(5) in exc.value.message
    assert "already returned or reserved" in exc.value.message
    assert keys("CAND#") == before_cands  # nothing new stored


def test_another_customers_order_cannot_be_checked_for_a_bundle(bundles):
    silver_twin_order()
    conv_id, run_id = bundles.flow.start_run(DAVID)
    with pytest.raises(DomainError) as exc:
        bundles.checks(DAVID, "DEMO-A2A-5B1B2C", [line_id(2, "DEMO-A2A-5B1B2C")], run_id)  # John's order
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED


def test_the_exchange_reason_does_not_make_a_bundle_an_exchange(bundles):
    for reason in ("FIT_SIZE", "EXPECTATION", "CHANGE_OF_MIND"):
        cand = bundles.find(numbers=(2, 5), reason=reason)[2]
        assert {o.type for o in cand.options} <= {"return_mail", "return_in_store"}
        assert cand.availability["replacement_sku"] is None
