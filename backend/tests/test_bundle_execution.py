"""execute() for a bundle return (domain v2 phase 2): one TXN with N lines, the perk ledger, replays and races.

``no_flush`` replaces the post-commit call into the gateway. Everything runs on the shipped fixtures.
"""

from __future__ import annotations

import json

import pytest

from returns_domain import audit, benefits, execution, memory_updates, quotes, store, trace
from returns_domain.errors import DomainError, ErrorCode
from tests.bundle_world import (
    DAVID, JOHN, MTL, NEXT_MONTH_NOW, ORDER, PERK_OCTOBER, PRICES, STANDARD_FEE,
    Bundles, a2a, keys, ledger, line_id, reserved, silver_twin_order,
)
from tests.conftest import Flow

pytestmark = pytest.mark.usefixtures("no_flush")

SCRIPT_LINES = (2, 5, 8)
REAL_TRANSACT = store.transact_write


@pytest.fixture
def bundles(flow: Flow) -> Bundles:
    return Bundles(flow)


@pytest.fixture
def commits(monkeypatch):
    """Record the labels of every TransactWriteItems that is sent (the real call still happens)."""
    sent: list[list[str]] = []

    def record(ops, **kwargs):
        sent.append([op.label for op in ops])
        return REAL_TRANSACT(ops, **kwargs)

    monkeypatch.setattr(store, "transact_write", record)
    return sent


def txn_item(result):
    return store.get_item(store.txn_key(result.transaction_id))


# ----------------------------------------------------------------- David, 3 of 10


def test_three_of_ten_is_one_transaction_with_three_lines_and_the_perk(bundles, commits):
    hq = bundles.approved()
    assert keys("TXN#") == [] and ledger() is None  # nothing before the click was executed
    result = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)

    assert (result.option_type, result.replayed, result.state, result.store_notice) == ("return_mail", False, "completed", None)
    assert result.transaction_id.startswith("DEMO-RET-") and result.benefit == PERK_OCTOBER
    assert keys("TXN#") == [(f"TXN#{result.transaction_id}", "META")]  # exactly ONE transaction
    txn = txn_item(result)
    assert [e["line_id"] for e in txn["lines"]] == [line_id(2), line_id(5), line_id(8)]
    assert all("policy_check_id" not in e for e in txn["lines"])
    assert (txn["fee_cents"], txn["refund_total_cents"], txn["net_refund_cents"], txn["refund_cents"]) == (0, 41997, 41997, 41997)
    assert (txn["qty"], txn["line_id"], txn["order_id"], txn["benefit"]) == (3, line_id(2), ORDER, PERK_OCTOBER)
    assert txn["next_step"] == (
        "Mail the 3 items, unworn, in one parcel with the return label (no label fee: Platinum free return shipping). "
        "Your refund of CAD 419.97 is expected by Mon Oct 26 after the parcel is received.")

    assert ledger()["used"] == 1  # used once, never 2
    assert reserved(ORDER, SCRIPT_LINES) == {2: 1, 5: 1, 8: 1}
    assert reserved(ORDER, (1, 3, 4, 6, 7, 9, 10)) == dict.fromkeys((1, 3, 4, 6, 7, 9, 10), 0)
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "executed"
    assert store.get_item(store.approval_key(hq.quote_id))["consumed_txn_id"] == result.transaction_id
    assert store.get_item(store.idem_key(result.approval_id))["transaction_id"] == result.transaction_id

    # ONE TransactWriteItems: txn, idem, approval, one update per line, ledger, quote, outbox
    assert commits == [["txn", "idem", "approval", f"line:{line_id(2)}", f"line:{line_id(5)}", f"line:{line_id(8)}",
                        "benefit", "quote", "outbox"]]


def test_the_status_view_and_the_trace_events(bundles):
    hq = bundles.approved()
    result = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    view = execution.get_status(DAVID, result.transaction_id)
    body = json.loads(view.model_dump_json())
    assert body["option_type"] == "return_mail" and body["fee_cents"] == 0 and body["benefit"] == PERK_OCTOBER
    assert (body["refund_total_cents"], body["net_refund_cents"], body["refund_cents"], body["qty"]) == (41997, 41997, 41997, 3)
    assert [e["line_id"] for e in body["lines"]] == [line_id(2), line_id(5), line_id(8)]
    assert body["store_notice"] is None and body["simulated"] is True and "customer_id" not in body
    assert body["reservation"]["lines"] == [
        {"line_id": line_id(n), "returned_or_reserved_qty": 1, "purchased_qty": 1} for n in SCRIPT_LINES]
    assert body["reservation"]["location_atp"] is None

    events = [(e["type"], e["actor"]) for e in a2a(DAVID)]
    assert events == [("quote_created", "aldo_agent"), ("transaction_created", "aldo_agent"), ("benefit_applied", "aldo_agent")]
    perk = a2a(DAVID, "benefit_applied")[0]["payload"]
    assert perk == {"type": "free_return_shipping", "benefit": "free_return_shipping", "label": "Platinum free return shipping",
                    "tier_label": "Platinum member", "saved_cents": 595, "fee_cents": 0, "used": 1, "per_month": 1,
                    "period": "2026-10", "order_id": ORDER, "transaction_id": result.transaction_id}
    assert "DEMO-CUST" not in json.dumps(a2a(DAVID))
    created = a2a(DAVID, "transaction_created")[0]["payload"]
    assert (created["option_type"], created["fee_cents"], created["order_id"], created["replayed"]) == ("return_mail", 0, ORDER, False)
    run_events = [e["type"] for e in trace.list_events(hq.run_id).events]
    assert "transaction_created" in run_events and "benefit_applied" not in run_events  # the perk is on the A2A stream


def test_the_status_of_a_bundle_belongs_to_its_owner(bundles):
    hq = bundles.approved()
    result = execution.execute(DAVID, hq.quote_id)
    with pytest.raises(DomainError) as exc:
        execution.get_status(JOHN, result.transaction_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED


def test_the_second_bundle_of_the_month_pays_and_the_ledger_stays_at_one(bundles, commits):
    first = bundles.approved()
    execution.execute(DAVID, first.quote_id)
    second = bundles.approved(numbers=(10,))
    result = execution.execute(DAVID, second.quote_id)

    txn = txn_item(result)
    assert (txn["fee_cents"], txn["refund_total_cents"], txn["net_refund_cents"]) == (STANDARD_FEE, 9999, 9999 - STANDARD_FEE)
    assert "benefit" not in txn and result.benefit is None and "benefit" not in json.loads(result.model_dump_json())
    assert "5.95 is deducted" in txn["next_step"] and "CAD 94.04" in txn["next_step"]
    assert ledger()["used"] == 1  # no "used 2"
    assert len(keys("TXN#")) == 2
    assert commits[1] == ["txn", "idem", "approval", f"line:{line_id(10)}", "quote", "outbox"]  # no ledger operation
    assert len(a2a(DAVID, "benefit_applied")) == 1
    assert reserved(ORDER, (10,)) == {10: 1}


def test_a_new_month_grants_the_perk_again(bundles, monkeypatch):
    execution.execute(DAVID, bundles.approved().quote_id)
    monkeypatch.setenv("DEMO_NOW", NEXT_MONTH_NOW)  # the DEMO clock moves into November
    later = bundles.approved(numbers=(10,))
    assert later.option("return_mail").benefit == {**PERK_OCTOBER, "period": "2026-11"}
    result = execution.execute(DAVID, later.quote_id)
    assert txn_item(result)["fee_cents"] == 0 and result.benefit["period"] == "2026-11"
    assert ledger()["used"] == 1 and ledger(period="2026-11")["used"] == 1  # one item per month, each at 1
    assert [e["payload"]["period"] for e in a2a(DAVID, "benefit_applied")] == ["2026-10", "2026-11"]


def test_a_silver_bundle_in_store_notifies_the_store_and_uses_no_perk(bundles, commits):
    silver_twin_order()
    order = "DEMO-A2A-5B1B2C"
    hq = bundles.approved(JOHN, (5, 8), order_id=order, kind="return_in_store")  # 23998, under the silver limit
    result = execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)
    assert result.option_type == "return_in_store" and result.benefit is None
    assert result.store_notice == {
        "store_id": MTL, "store_name": "ALDO Demo Store Montreal", "display_name": "John", "tier_label": "Silver member",
        "eta_minutes": 5, "summary": "ALDO Demo Store Montreal is ready for your return of 2 items, John is about 5 minutes away"}
    txn = txn_item(result)
    assert (txn["fee_cents"], txn["net_refund_cents"], txn["location_id"]) == (0, 23998, MTL)
    assert txn["next_step"] == "Bring the 2 items, unworn, to ALDO Demo Store Montreal. Your refund of CAD 239.98 is processed at drop-off."
    assert keys("STORENOTE#") == [(f"STORENOTE#{result.transaction_id}", "META")]
    assert commits[-1][-1] == "store_notice" and "benefit" not in commits[-1]
    assert keys("BENEFIT#") == []
    assert [e["type"] for e in a2a(JOHN)] == ["quote_created", "transaction_created", "store_notified"]
    assert a2a(JOHN, "store_notified")[0]["actor"] == "aldo_store"
    assert reserved(order, (5, 8)) == {5: 1, 8: 1} and reserved(order, (2, 10)) == {2: 0, 10: 0}


def test_ten_lines_commit_in_one_transaction_of_sixteen_operations(bundles, commits):
    """All ten lines (152990) are above the platinum limit, so shrink the order's prices to fit the
    75000 limit: the point is the size of the transaction with ten line updates (moto accepts it)."""
    for n in range(1, 11):
        store.update_item(store.line_key(ORDER, line_id(n)), set={"unit_paid_cents": 5000})
    hq = bundles.approved(numbers=tuple(range(1, 11)))
    result = execution.execute(DAVID, hq.quote_id)
    assert len(commits[0]) == 3 + 10 + 3 == 16  # txn, idem, approval, 10 lines, benefit, quote, outbox
    assert len(txn_item(result)["lines"]) == 10 and ledger()["used"] == 1
    assert reserved(ORDER, tuple(range(1, 11))) == dict.fromkeys(range(1, 11), 1)


# --------------------------------------------------------------- replays and races


def test_a_double_execute_is_a_replay_and_writes_nothing(bundles, snapshot, commits):
    hq = bundles.approved()
    first = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    before = snapshot(exclude=("AUDIT#",))
    events_before = a2a(DAVID)
    again = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    assert again.replayed is True and again.transaction_id == first.transaction_id
    assert again.benefit == PERK_OCTOBER and again.memory_update_id == first.memory_update_id
    assert snapshot(exclude=("AUDIT#",)) == before
    assert a2a(DAVID) == events_before and len(commits) == 1
    assert ledger()["used"] == 1 and len(keys("TXN#")) == 1


def test_two_executes_of_the_same_quote_write_one_transaction_and_use_the_perk_once(bundles, monkeypatch):
    """A competitor commits between our validation and our commit; ours is cancelled on the idempotency
    record and returns the competitor's transaction (the sequential twin of two concurrent callers)."""
    hq = bundles.approved()
    winners: list[execution.ExecutionResult] = []

    def competitor_first(ops, **kwargs):
        monkeypatch.setattr(store, "transact_write", REAL_TRANSACT)
        winners.append(execution.execute(DAVID, hq.quote_id, run_id=hq.run_id))
        return REAL_TRANSACT(ops, **kwargs)  # our own commit now loses

    monkeypatch.setattr(store, "transact_write", competitor_first)
    loser = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    assert winners[0].replayed is False and loser.replayed is True and loser.transaction_id == winners[0].transaction_id
    assert len(keys("TXN#")) == 1 and ledger()["used"] == 1
    assert reserved(ORDER, SCRIPT_LINES) == {2: 1, 5: 1, 8: 1}  # not 2
    assert len(a2a(DAVID, "benefit_applied")) == 1 and len(a2a(DAVID, "transaction_created")) == 1
    assert len(keys("A2AOUT#")) == 1


def test_a_transient_conflict_on_a_bundle_is_retried_into_one_transaction(bundles, monkeypatch):
    """A TransactionConflict on the first attempt is retried; the bundle still commits once and the perk is used once.
    (Real threads are not used: moto's in-process DynamoDB is not thread-safe.)"""
    from botocore.stub import Stubber

    hq = bundles.approved()
    monkeypatch.setattr(execution, "_sleep", lambda seconds: None)
    calls = {"n": 0}

    def conflict_once(ops, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            with Stubber(store.get_client()) as stub:
                stub.add_client_error(
                    "transact_write_items", service_error_code="TransactionCanceledException",
                    service_message="cancelled", http_status_code=400,
                    modeled_fields={"CancellationReasons": [{"Code": "TransactionConflict", "Message": "m"} for _ in ops]})
                return REAL_TRANSACT(ops, **kwargs)
        return REAL_TRANSACT(ops, **kwargs)

    monkeypatch.setattr(store, "transact_write", conflict_once)
    result = execution.execute(DAVID, hq.quote_id)
    assert calls["n"] == 2 and result.replayed is False
    assert len(keys("TXN#")) == 1 and ledger()["used"] == 1
    assert reserved(ORDER, SCRIPT_LINES) == {2: 1, 5: 1, 8: 1}


def test_a_ledger_condition_that_fails_at_commit_is_benefit_used_and_writes_nothing(bundles, monkeypatch, snapshot):
    hq = bundles.approved()
    before_commit = snapshot(exclude=("AUDIT#",))

    def perk_used_meanwhile(ops, **kwargs):
        REAL_TRANSACT([benefits.ledger_update(DAVID, "2026-10", 1)])  # another shipment takes the last free label
        return REAL_TRANSACT(ops, **kwargs)

    monkeypatch.setattr(store, "transact_write", perk_used_meanwhile)
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    assert exc.value.code == ErrorCode.STOCK_CHANGED and exc.value.details == {"reason": "BENEFIT_USED"}
    assert exc.value.to_dict()["details"] == {"reason": "BENEFIT_USED"}
    monkeypatch.setattr(store, "transact_write", REAL_TRANSACT)

    after = snapshot(exclude=("AUDIT#",))
    changed = {k for k in after if after[k] != before_commit.get(k)}
    assert changed == {("BENEFIT#DEMO-CUST-DAVID#2026-10", "META")}  # only the competitor's ledger write; nothing of ours
    assert keys("TXN#") == [] and keys("A2AOUT#") == [] and keys("IDEM#") == []
    assert reserved(ORDER, SCRIPT_LINES) == {2: 0, 5: 0, 8: 0} and ledger()["used"] == 1
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "open"
    assert "consumed_txn_id" not in store.get_item(store.approval_key(hq.quote_id))
    assert a2a(DAVID, "benefit_applied") == []


def test_a_bundle_whose_perk_was_taken_by_another_bundle_between_quote_and_execute(bundles, snapshot):
    quote_a = bundles.approved(numbers=SCRIPT_LINES)
    quote_b = bundles.approved(numbers=(10,))
    assert quote_a.option("return_mail").benefit and quote_b.option("return_mail").benefit  # both were quoted with the perk
    assert execution.execute(DAVID, quote_a.quote_id).benefit == PERK_OCTOBER

    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, quote_b.quote_id, run_id=quote_b.run_id)
    assert exc.value.code == ErrorCode.STOCK_CHANGED and exc.value.details == {"reason": "BENEFIT_USED"}
    assert snapshot(exclude=("AUDIT#",)) == before  # nothing written
    assert audit.list_audit(action="execute_denied")[-1]["refs"]["code"] == "STOCK_CHANGED"
    assert ledger()["used"] == 1 and reserved(ORDER, (10,)) == {10: 0}

    # the agent re-quotes without the waiver: the fee is the standard one and the new quote can run
    again = bundles.approved(numbers=(10,))
    option = again.option("return_mail")
    assert option.benefit is None and option.fee_cents == STANDARD_FEE
    assert execution.execute(DAVID, again.quote_id).benefit is None
    assert ledger()["used"] == 1


def test_a_downgraded_tier_loses_the_perk_at_execution(bundles, snapshot):
    hq = bundles.approved()
    store.update_item(store.customer_key(DAVID), set={"tier": "silver"})
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, hq.quote_id)
    # the silver limit (25000) is checked on the total first: 41997 needs a person
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "decision=needs_review" in exc.value.message
    assert snapshot(exclude=("AUDIT#",)) == before and keys("TXN#") == []


def test_a_tier_without_the_perk_cannot_use_a_quoted_waiver(bundles, snapshot):
    hq = bundles.approved(numbers=(5, 8))  # 23998: under the silver limit too
    store.update_item(store.customer_key(DAVID), set={"tier": "silver"})
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, hq.quote_id)
    assert exc.value.code == ErrorCode.STOCK_CHANGED and exc.value.details == {"reason": "BENEFIT_UNAVAILABLE"}
    assert snapshot(exclude=("AUDIT#",)) == before


# --------------------------------------------------------- lines that cannot go back twice


def test_a_line_returned_by_a_single_return_blocks_a_stale_bundle(bundles, snapshot):
    hq = bundles.approved()
    flow = bundles.flow
    single = flow.hero_quote(
        DAVID, order_id=ORDER, line_id=line_id(5), reason="CHANGE_OF_MIND", direction="unknown", deadline=None,
        types=("return_mail",))
    flow.approve(single, "return_mail")
    execution.execute(DAVID, single.quote_id)
    assert reserved(ORDER, (5,)) == {5: 1}

    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "line DEMO-3001-L5" in exc.value.message
    assert snapshot(exclude=("AUDIT#",)) == before
    assert len(keys("TXN#")) == 1 and ledger() is None  # the bundle did not consume the perk


def test_a_line_taken_between_validation_and_commit_is_refused_by_the_per_line_guard(bundles, monkeypatch):
    hq = bundles.approved()

    def line_taken(ops, **kwargs):
        store.update_item(store.line_key(ORDER, line_id(8)), set={"returned_or_reserved_qty": 1})
        return REAL_TRANSACT(ops, **kwargs)

    monkeypatch.setattr(store, "transact_write", line_taken)
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "no longer available" in exc.value.message
    assert keys("TXN#") == [] and ledger() is None
    assert reserved(ORDER, (2, 5)) == {2: 0, 5: 0}  # all or nothing: the other two lines were not touched


def test_a_bundle_cannot_return_the_same_line_twice(bundles):
    execution.execute(DAVID, bundles.approved().quote_id)
    with pytest.raises(DomainError) as exc:  # a fresh policy check is refused: nothing is left to return
        bundles.find(numbers=(2, 5))
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_a_price_change_since_the_quote_voids_the_approval(bundles, snapshot):
    hq = bundles.approved()
    store.update_item(store.line_key(ORDER, line_id(2)), set={"unit_paid_cents": 17000})
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "no longer matches" in exc.value.message
    assert snapshot(exclude=("AUDIT#",)) == before


def test_execute_still_needs_the_owners_approval_and_the_approved_option(bundles, snapshot):
    hq = bundles.quote()
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(DAVID, hq.quote_id)
    assert exc.value.code == ErrorCode.APPROVAL_REQUIRED
    assert snapshot(exclude=("AUDIT#",)) == before
    bundles.flow.approve(hq, "return_mail")
    with pytest.raises(DomainError) as other:  # someone else's quote
        execution.execute(JOHN, hq.quote_id)
    assert other.value.code == ErrorCode.NOT_AUTHORIZED
    with pytest.raises(DomainError) as wrong:
        execution.execute(DAVID, hq.quote_id, "OPT-9")
    assert wrong.value.code == ErrorCode.APPROVAL_REQUIRED
    assert keys("TXN#") == []


# ------------------------------------------------------------------------- write-back


def test_a_bundle_sends_one_small_write_back_update(bundles):
    hq = bundles.approved()
    result = execution.execute(DAVID, hq.quote_id)
    assert result.memory_update_id and result.memory_update_id.startswith("MU-")
    item = store.get_item(store.outbox_key(result.memory_update_id))
    assert item["status"] == "pending" and item["customer_id"] == DAVID and item["attempts"] == 0
    update = item["payload"]
    assert set(update) == {"update_id", "owner_ref", "order_id", "reason", "outcome", "provisional", "source", "ts", "items"}
    assert (update["outcome"], update["provisional"], update["reason"], update["order_id"]) == ("return_created", True, "PREFERENCE", ORDER)
    assert "fit_feedback" not in update and "original_size" not in update and "new_size" not in update
    assert [(i["product_name"], i["color"], i["size"], i["reason"]) for i in update["items"]] == [
        ("Aahliyah", "TAN", "10.5", "PREFERENCE"), ("Alanie", "WHITE", "10.0", "PREFERENCE"), ("Valegyn", "NAVY", "10.0", "PREFERENCE")]
    assert all(set(i) == {"style_id", "product_name", "color", "size", "reason"} for i in update["items"])
    assert len(json.dumps(update)) < 1000  # small
    assert update["update_id"] == store.derived_id("MU-", result.transaction_id, "return_created", n=6)

    # idempotent on update_id: a replay changes nothing and enqueueing the same update again is a no-op
    assert execution.execute(DAVID, hq.quote_id).memory_update_id == result.memory_update_id
    assert memory_updates.enqueue(update) is False
    assert len(keys("A2AOUT#")) == 1


def test_the_write_back_reason_comes_from_the_policy_checks(bundles):
    conv_id, run_id = bundles.flow.start_run(DAVID)
    from returns_domain import options, orders

    ids = [
        orders.check_policy(DAVID, run_id, ORDER, line_id(2), 1, "FIT_SIZE", "unworn", "too_small").policy_check_id,
        orders.check_policy(DAVID, run_id, ORDER, line_id(5), 1, "CHANGE_OF_MIND", "unworn", "unknown").policy_check_id,
    ]
    cand = options.find_options(DAVID, policy_check_ids=ids, run_id=run_id)
    quote = quotes.create_quote(DAVID, run_id, conv_id, cand.candidate_set_id, ["OPT-1"])
    option = quote.options[0]
    approval, _ = quotes.approve(DAVID, quote.quote_id, option.option_id, option.payload_hash, store.new_id("RUN-", 10))
    result = execution.execute(DAVID, quote.quote_id)
    update = store.get_item(store.outbox_key(result.memory_update_id))["payload"]
    # a size problem on L2 is `TOO_SMALL` from the policy check's size direction; the colour dislike is PREFERENCE
    assert [i["reason"] for i in update["items"]] == ["TOO_SMALL", "PREFERENCE"] and update["reason"] == "OTHER"


def test_the_bundle_update_builder_validates_its_input():
    with pytest.raises(DomainError):
        memory_updates.build_bundle_update(customer_id=DAVID, order_id=ORDER, transaction_id="DEMO-RET-AAAAAA", items=[])
    item = {"style_id": "DEMO-STYLE-NEW02", "product_name": "Aahliyah", "color": "TAN", "size": "10.5", "reason": "PREFERENCE"}
    with pytest.raises(DomainError):
        memory_updates.build_bundle_update(customer_id=DAVID, order_id=ORDER, transaction_id="DEMO-RET-AAAAAA", items=[item] * 11)
    with pytest.raises(DomainError):
        memory_updates.build_bundle_update(
            customer_id=DAVID, order_id=ORDER, transaction_id="DEMO-RET-AAAAAA", items=[{**item, "reason": "LOVE"}])
    update = memory_updates.build_bundle_update(customer_id=DAVID, order_id=ORDER, transaction_id="DEMO-RET-AAAAAA", items=[item])
    again = memory_updates.build_bundle_update(customer_id=DAVID, order_id=ORDER, transaction_id="DEMO-RET-AAAAAA", items=[item])
    assert update["update_id"] == again["update_id"] and update["reason"] == "PREFERENCE"
    # an exchange builder is untouched: a single-line return has no write-back and a pickup of a bundle is not a thing
    assert memory_updates.update_from_txn_item({"option_type": "return_mail", "transaction_id": "DEMO-RET-AAAAAA"}) is None
    assert memory_updates.update_from_txn_item(
        {"option_type": "return_mail", "memory_items": [item], "customer_id": DAVID, "order_id": ORDER,
         "transaction_id": "DEMO-RET-AAAAAA"}, outcome="exchange_completed_kept") is None


# --------------------------------------------------------------------- order context


def test_order_context_names_what_each_shoe_is(bundles):
    from returns_domain import orders

    context = orders.order_context(DAVID, ORDER)
    by_line = {line["line_id"]: line for line in context["lines"]}
    assert (by_line[line_id(2)]["category"], by_line[line_id(5)]["category"], by_line[line_id(8)]["category"]) == (
        "boot", "sneaker", "loafer")
    assert (by_line[line_id(2)]["color"], by_line[line_id(5)]["color"], by_line[line_id(8)]["color"]) == ("TAN", "WHITE", "NAVY")
    assert len(context["lines"]) == 10
    view = json.loads(orders.get_order(DAVID, ORDER).model_dump_json())
    assert {line["line_id"]: line["category"] for line in view["lines"]}[line_id(2)] == "boot"
    # additive: a line without a category has no such key
    ava = orders.order_context("DEMO-CUST-AVA", "DEMO-1001")
    assert all("category" not in line for line in ava["lines"])
    assert all("category" not in line for line in json.loads(orders.get_order("DEMO-CUST-AVA", "DEMO-1001").model_dump_json())["lines"])


def test_the_agent_can_resolve_the_script_by_colour_and_category(bundles):
    from returns_domain import orders

    lines = orders.order_context(DAVID, ORDER)["lines"]
    picked = [
        next(line for line in lines if line["color"] == color and line.get("category") == category)["line_id"]
        for color, category in (("TAN", "boot"), ("WHITE", "sneaker"), ("NAVY", "loafer"))
    ]
    assert picked == [line_id(2), line_id(5), line_id(8)]
    assert sum(PRICES[n] for n in SCRIPT_LINES) == 41997
