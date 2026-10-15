"""execution.py against moto DynamoDB: exactly-once execution, denials and the write-back (task 5.7).

Every assertion reads TABLE STATE (items, stock, counters, events), not return messages.
``no_flush`` replaces the post-commit call into the gateway's ``flush_outbox``.
"""

from __future__ import annotations

import re
import sys
import types

import pytest

from returns_domain import audit, execution, fixtures, memory_updates, orders, quotes, store, trace
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN, CHLOE, make_agent_order

pytestmark = pytest.mark.usefixtures("no_flush")

MTL, LAVAL, TOR, DC = "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01", "DEMO-DC-QC"
HERO_075 = "DEMO-SKU-HERO-BLACK-075"


def atp(sku, location):
    row = store.get_item(store.inventory_key(sku, location))
    return row["atp"], row["version"]


def line_qty(order="DEMO-1001", line="DEMO-1001-L1"):
    return store.get_item(store.line_key(order, line))["returned_or_reserved_qty"]


def keys(prefix):
    return sorted(k for k in store.scan_keys((prefix,)))


def approved(flow, option_type="exchange_store_pickup", **kwargs):
    hq = flow.hero_quote(**kwargs)
    approval = flow.approve(hq, option_type)
    return hq, approval


# ----------------------------------------------------------------------------- happy path


def test_nothing_changes_before_approval(flow, snapshot):
    before = snapshot(exclude=("PCHK#", "CAND#", "QUOTE#", "RUN#", "CONV#", "AUDIT#"))
    hq = flow.hero_quote()
    assert snapshot(exclude=("PCHK#", "CAND#", "QUOTE#", "RUN#", "CONV#", "AUDIT#")) == before
    assert keys("TXN#") == [] and keys("IDEM#") == [] and keys("A2AOUT#") == []
    assert atp(HERO_075, MTL) == (1, 1) and line_qty() == 0
    assert store.get_item(store.approval_key(hq.quote_id)) is None


def test_pickup_exchange_commits_every_item_in_one_transaction(flow):
    hq, approval = approved(flow)
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)

    assert re.fullmatch(r"DEMO-RMA-[0-9A-F]{6}", result.transaction_id)
    assert (result.state, result.option_type, result.replayed) == ("completed", "exchange_store_pickup", False)
    assert (result.quote_id, result.approval_id) == (hq.quote_id, approval.approval_id)
    assert re.fullmatch(r"MU-[0-9A-F]{6}", result.memory_update_id)

    txn = store.get_item(store.txn_key(result.transaction_id))
    assert (txn["customer_id"], txn["quote_id"], txn["approval_id"], txn["state"], txn["simulated"]) == (
        AVA, hq.quote_id, approval.approval_id, "completed", True)
    assert (txn["order_id"], txn["line_id"], txn["qty"], txn["sku"], txn["location_id"], txn["replacement_size"]) == (
        "DEMO-1001", "DEMO-1001-L1", 1, HERO_075, MTL, "7.5")
    assert (txn["refund_cents"], txn["fee_cents"], txn["eta_date"], txn["eta_text"]) == (0, 0, "2026-10-17", "Sat Oct 17")
    assert txn["next_step"] == "Pick up size 7.5 at ALDO Demo Store Montreal from Sat Oct 17; bring the original pair, unworn."
    assert txn["option"]["option_id"] == approval.option_id and txn["option"]["payload_hash"] == approval.payload_hash
    assert keys("TXN#") == [(f"TXN#{result.transaction_id}", "META")]

    idem = store.get_item(store.idem_key(approval.approval_id))
    assert idem["transaction_id"] == result.transaction_id
    assert store.get_item(store.approval_key(hq.quote_id))["consumed_txn_id"] == result.transaction_id
    assert line_qty() == 1
    assert atp(HERO_075, MTL) == (0, 2)  # stock -1 and version +1 at the chosen location only
    assert atp(HERO_075, DC) == (5, 1) and atp(HERO_075, TOR) == (1, 1)
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "executed"


def test_transaction_ids_follow_the_option_kind(flow):
    exchange = approved(flow, "exchange_store_pickup")[0]
    assert execution.execute(AVA, exchange.quote_id).transaction_id.startswith("DEMO-RMA-")
    ret = flow.hero_quote(order_id="DEMO-1008", line_id="DEMO-1008-L1", reason="CHANGE_OF_MIND", direction="unknown",
                          deadline=None, types=("return_in_store",))
    flow.approve(ret, "return_in_store")
    assert execution.execute(AVA, ret.quote_id).transaction_id.startswith("DEMO-RET-")


def test_return_in_store_touches_no_stock_and_creates_no_writeback(flow, no_flush):
    hq, _ = approved(flow, "return_in_store")
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    assert result.option_type == "return_in_store" and result.memory_update_id is None
    assert atp(HERO_075, MTL) == (1, 1) and atp(HERO_075, DC) == (5, 1)
    assert atp("DEMO-SKU-HERO-BLACK-070", DC)[1] == 1
    assert line_qty() == 1 and keys("A2AOUT#") == []
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert (txn["refund_cents"], txn["fee_cents"], txn["location_id"]) == (12999, 0, LAVAL)
    assert "memory_update_id" not in txn and no_flush == []  # no write-back, so nothing to flush
    assert txn["next_step"] == (
        "Bring the original pair, unworn, to ALDO Demo Store Laval. Your refund of CAD 129.99 is processed at drop-off.")


def test_return_by_mail_amounts_come_from_the_server(flow):
    hq, _ = approved(flow, "return_mail", types=("return_mail", "exchange_store_pickup"))
    result = execution.execute(AVA, hq.quote_id)
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert (txn["refund_cents"], txn["fee_cents"], txn["location_id"], txn["eta_date"]) == (12404, 595, DC, "2026-10-26")
    assert "CAD 5.95" in txn["next_step"] and "CAD 124.04" in txn["next_step"] and "Mon Oct 26" in txn["next_step"]
    assert keys("A2AOUT#") == [] and atp(HERO_075, DC) == (5, 1)


def test_ship_exchange_from_the_dc_reserves_dc_stock(flow):
    hq, _ = approved(flow, "exchange_ship_from_dc")
    result = execution.execute(AVA, hq.quote_id)
    assert result.option_type == "exchange_ship_from_dc" and atp(HERO_075, DC) == (4, 2)
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert txn["next_step"] == ("Size 7.5 ships from ALDO Demo Distribution Centre and should arrive by Tue Oct 20; "
                                "send back the original pair, unworn.")
    assert txn["eta_date"] == "2026-10-20"


def test_dc_stockout_pickup(flow):
    fixtures.reset("dc_stockout")
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id)
    assert result.option_type == "exchange_store_pickup" and atp(HERO_075, MTL) == (1, 2) and atp(HERO_075, DC) == (0, 1)


# ------------------------------------------------------------------- exactly once and replay


def test_execute_replay_returns_the_same_transaction_and_writes_nothing(flow, snapshot):
    hq, _ = approved(flow)
    first = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    before = snapshot(exclude=("AUDIT#",))
    again = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    third = execution.execute(AVA, hq.quote_id, hq.option("exchange_store_pickup").option_id)
    assert snapshot(exclude=("AUDIT#",)) == before  # no table change at all, events included
    assert again.replayed and third.replayed and not first.replayed
    assert again.transaction_id == third.transaction_id == first.transaction_id
    assert again.memory_update_id == first.memory_update_id and again.option_type == first.option_type
    assert len(keys("TXN#")) == 1 and atp(HERO_075, MTL) == (0, 2) and line_qty() == 1 and len(keys("A2AOUT#")) == 1


def test_a_second_approval_attempt_cannot_create_a_second_transaction(flow):
    hq, approval = approved(flow)
    again, created = quotes.approve(AVA, hq.quote_id, approval.option_id, approval.payload_hash, "RUN-LATER")
    assert not created and again.approval_id == approval.approval_id
    first = execution.execute(AVA, hq.quote_id)
    assert execution.execute(AVA, hq.quote_id).transaction_id == first.transaction_id
    assert len(keys("TXN#")) == 1


def test_losing_the_race_returns_the_winners_transaction(flow, monkeypatch):
    """Two callers: the second one's commit is cancelled on idem/approval and it returns the first's transaction."""
    hq, _ = approved(flow)
    real = store.transact_write
    winner = {}

    def lose_the_race(ops, **kwargs):
        if not winner:
            monkeypatch.setattr(store, "transact_write", real)  # the competitor commits for real
            winner["result"] = execution.execute(AVA, hq.quote_id)
            monkeypatch.setattr(store, "transact_write", lose_the_race)
        return real(ops, **kwargs)  # our commit now fails its idem/approval conditions

    monkeypatch.setattr(store, "transact_write", lose_the_race)
    loser = execution.execute(AVA, hq.quote_id)
    assert loser.replayed is True and loser.transaction_id == winner["result"].transaction_id
    assert len(keys("TXN#")) == 1 and atp(HERO_075, MTL) == (0, 2) and line_qty() == 1 and len(keys("A2AOUT#")) == 1


# -------------------------------------------------------------------------------- denials


def test_no_approval(flow, snapshot):
    hq = flow.hero_quote()
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    assert exc.value.code == ErrorCode.APPROVAL_REQUIRED
    assert snapshot(exclude=("AUDIT#",)) == before
    denied = audit.list_audit(action="execute_denied")
    assert len(denied) == 1 and denied[0]["outcome"] == "denied" and denied[0]["actor"] == AVA
    assert denied[0]["refs"] == {"quote_id": hq.quote_id, "code": "APPROVAL_REQUIRED", "run_id": hq.run_id}


def test_option_mismatch(flow, snapshot):
    hq, approval = approved(flow, "exchange_store_pickup")
    other = hq.option("return_in_store").option_id
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id, other)  # the model asks for something the customer did not approve
    assert exc.value.code == ErrorCode.APPROVAL_REQUIRED
    assert snapshot(exclude=("AUDIT#",)) == before
    assert len(audit.list_audit(action="execute_denied")) == 1
    assert execution.execute(AVA, hq.quote_id, approval.option_id).option_type == "exchange_store_pickup"


def test_other_customer_quote(flow, snapshot):
    hq, _ = approved(flow)
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(BEN, hq.quote_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert snapshot(exclude=("AUDIT#",)) == before
    denied = audit.list_audit(action="access_denied")
    assert len(denied) == 1 and denied[0]["actor"] == BEN
    assert denied[0]["refs"]["resource_type"] == "quote" and denied[0]["refs"]["resource_id"] == hq.quote_id
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, "QUO-MISSING")
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_replay_after_window(flow, fixed_now, snapshot):
    """Replays always succeed; a fresh execution after the 5-minute window is refused."""
    hq, _ = approved(flow)
    first = execution.execute(AVA, hq.quote_id)
    fixed_now.advance(seconds=301)
    before = snapshot(exclude=("AUDIT#",))
    again = execution.execute(AVA, hq.quote_id)
    assert again.replayed and again.transaction_id == first.transaction_id
    assert snapshot(exclude=("AUDIT#",)) == before

    late, _ = approved(flow, "return_in_store", customer_id=BEN, order_id="DEMO-1003", line_id="DEMO-1003-L1",
                       reason="CHANGE_OF_MIND", direction="unknown", deadline=None)  # approved, never executed
    fixed_now.advance(seconds=301)
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(BEN, late.quote_id)
    assert exc.value.code == ErrorCode.QUOTE_EXPIRED
    assert snapshot(exclude=("AUDIT#",)) == before


def test_an_approval_made_late_in_the_quote_life_still_executes_inside_its_window(flow, fixed_now):
    """R8.2: approve-time validation plus a 5 minute execute window, not a second expiry check."""
    hq = flow.hero_quote()
    fixed_now.advance(seconds=14 * 60)
    flow.approve(hq)
    fixed_now.advance(seconds=120)  # the quote itself expired a minute ago
    assert quotes.get_quote(AVA, hq.quote_id).status == "expired"
    assert execution.execute(AVA, hq.quote_id).replayed is False
    assert len(keys("TXN#")) == 1


def test_stock_changed_at_execute_writes_nothing(flow, snapshot):
    hq, _ = approved(flow)
    store.update_item(store.inventory_key(HERO_075, MTL), set={"atp": 0}, add={"version": 1})  # someone else took it
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id)
    assert exc.value.code == ErrorCode.STOCK_CHANGED
    assert snapshot(exclude=("AUDIT#",)) == before
    assert keys("TXN#") == [] and keys("IDEM#") == [] and keys("A2AOUT#") == [] and line_qty() == 0
    assert "consumed_txn_id" not in store.get_item(store.approval_key(hq.quote_id))
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "open"


def test_stock_condition_failure_inside_the_transaction_cancels_everything(flow, monkeypatch):
    """The stock vanishes between the read and the commit: the INV condition fails and nothing else lands."""
    hq, _ = approved(flow)
    real = store.transact_write

    def steal_then_commit(ops, **kwargs):
        store.update_item(store.inventory_key(HERO_075, MTL), set={"atp": 0})
        return real(ops, **kwargs)

    monkeypatch.setattr(store, "transact_write", steal_then_commit)
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id)
    assert exc.value.code == ErrorCode.STOCK_CHANGED
    assert keys("TXN#") == [] and keys("IDEM#") == [] and keys("A2AOUT#") == [] and line_qty() == 0
    assert "consumed_txn_id" not in store.get_item(store.approval_key(hq.quote_id))
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "open"


def test_quantity_condition_failure_inside_the_transaction_cancels_everything(flow, monkeypatch):
    hq, _ = approved(flow)
    real = store.transact_write

    def return_it_elsewhere_then_commit(ops, **kwargs):
        store.update_item(store.line_key("DEMO-1001", "DEMO-1001-L1"), add={"returned_or_reserved_qty": 1})
        return real(ops, **kwargs)

    monkeypatch.setattr(store, "transact_write", return_it_elsewhere_then_commit)
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "quantity" in exc.value.message
    assert keys("TXN#") == [] and keys("A2AOUT#") == [] and atp(HERO_075, MTL) == (1, 1)
    assert line_qty() == 1  # only the concurrent change; ours did not land twice


@pytest.mark.parametrize(
    ("change", "decision"),
    [({"line": {"returned_or_reserved_qty": 1}}, "ineligible"),  # quantity already used up
     ({"order": {"delivered_at": "2026-09-01"}}, "ineligible"),  # window expired
     ({"order": {"delivered_at": None}}, "needs_review")],
)
def test_policy_is_re_evaluated_against_current_data(flow, snapshot, change, decision):
    hq, _ = approved(flow)
    if "line" in change:
        store.update_item(store.line_key("DEMO-1001", "DEMO-1001-L1"), set=change["line"])
    if "order" in change:
        store.update_item(store.order_key("DEMO-1001"), set=change["order"])
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and f"decision={decision}" in exc.value.message
    assert snapshot(exclude=("AUDIT#",)) == before and keys("TXN#") == []


def test_server_recomputed_amounts_detect_a_tampered_quote(flow, snapshot):
    hq, _ = approved(flow, "return_in_store")
    stored = store.get_item(store.quote_key(hq.quote_id))
    for option in stored["options"]:
        option["refund_cents"] = 99999  # someone edits the stored amount
    store.put_item(stored)
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert snapshot(exclude=("AUDIT#",)) == before and keys("TXN#") == []


def test_a_tampered_approval_hash_is_detected(flow, snapshot):
    hq, _ = approved(flow)
    store.update_item(store.approval_key(hq.quote_id), set={"payload_hash": "0" * 64})
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert snapshot(exclude=("AUDIT#",)) == before


def test_the_approved_option_not_the_callers_is_what_runs(flow):
    hq, approval = approved(flow, "return_in_store")
    result = execution.execute(AVA, hq.quote_id)
    assert result.option_type == "return_in_store"
    assert store.get_item(store.txn_key(result.transaction_id))["option"]["option_id"] == approval.option_id
    assert atp(HERO_075, MTL) == (1, 1)  # the exchange options in the same quote were not executed


# ---------------------------------------------------------------------------- audit and trace


def test_a_successful_execution_is_audited(flow):
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    rows = audit.list_audit(action="execute")
    assert len(rows) == 1 and rows[0]["outcome"] == "allowed" and rows[0]["actor"] == AVA
    assert rows[0]["refs"] == {"quote_id": hq.quote_id, "transaction_id": result.transaction_id, "option_type": "exchange_store_pickup"}


def test_transaction_created_event_on_the_aldo_run_once(flow):
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    execution.execute(AVA, hq.quote_id, run_id=hq.run_id)  # replay
    events = [e for e in trace.list_events(hq.run_id).events if e["type"] == "transaction_created"]
    assert len(events) == 1 and events[0]["actor"] == "aldo_agent"
    assert events[0]["payload"] == {"transaction_id": result.transaction_id, "state": "completed",
                                    "option_type": "exchange_store_pickup", "quote_id": hq.quote_id, "replayed": False}


def test_no_run_event_without_a_run_id_but_the_a2a_mirror_still_happens(flow):
    hq, _ = approved(flow)
    execution.execute(AVA, hq.quote_id)
    assert [e["type"] for e in trace.list_events(hq.run_id).events] == ["quote_created"]
    assert [e["type"] for e in trace.list_a2a_events(AVA).events] == ["transaction_created"]


# ------------------------------------------------------------------------ write-back outbox


def test_exchange_creates_the_provisional_writeback_in_the_same_transaction(flow):
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id)
    assert keys("A2AOUT#") == [(f"A2AOUT#{result.memory_update_id}", "META")]
    row = store.get_item(store.outbox_key(result.memory_update_id))
    assert (row["status"], row["attempts"], row["customer_id"]) == ("pending", 0, AVA)
    assert (row["gsi1pk"], row["gsi1sk"]) == ("A2AOUT#pending", row["created_at"])
    payload = row["payload"]
    assert payload["update_id"] == result.memory_update_id == store.derived_id("MU-", result.transaction_id, "exchange_created", n=6)
    assert {k: payload[k] for k in ("owner_ref", "order_id", "style_id", "product_name", "color", "original_size", "new_size",
                                    "reason", "outcome", "provisional", "source")} == {
        "owner_ref": AVA, "order_id": "DEMO-1001", "style_id": "DEMO-STYLE-HERO", "product_name": payload["product_name"],
        "color": "BLACK", "original_size": "7.0", "new_size": "7.5", "reason": "TOO_SMALL",
        "outcome": "exchange_created", "provisional": True, "source": "ALDO_CA"}
    assert [i["update_id"] for i in memory_updates.list_pending(AVA)] == [result.memory_update_id]
    assert store.get_item(store.txn_key(result.transaction_id))["memory_update_id"] == result.memory_update_id


def test_writeback_exists_if_and_only_if_the_exchange_does(flow, monkeypatch):
    hq, _ = approved(flow)
    store.update_item(store.inventory_key(HERO_075, MTL), set={"atp": 0})
    with pytest.raises(DomainError):
        execution.execute(AVA, hq.quote_id)
    assert keys("A2AOUT#") == [] and keys("TXN#") == []


def test_too_big_exchange_writes_a_too_big_update(flow):
    hq, _ = approved(flow, "exchange_ship_from_dc", customer_id=CHLOE, order_id="DEMO-1009", line_id="DEMO-1009-L1",
                     direction="too_big", deadline=None, types=("exchange_ship_from_dc", "return_in_store"))
    result = execution.execute(CHLOE, hq.quote_id)
    payload = store.get_item(store.outbox_key(result.memory_update_id))["payload"]
    assert (payload["original_size"], payload["new_size"], payload["reason"], payload["owner_ref"]) == ("8.0", "7.5", "TOO_BIG", CHLOE)


def test_exchange_on_a_gateway_order_after_delivery(flow):
    """Act 2: size 8.0 bought through the gateway is delivered; the exchange to 8.5 queues the update."""
    make_agent_order("DEMO-A2A-81B20E", delivered_at="2026-10-16")
    hq, _ = approved(flow, order_id="DEMO-A2A-81B20E", line_id="DEMO-A2A-81B20E-L1", direction="too_small")
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    payload = store.get_item(store.outbox_key(result.memory_update_id))["payload"]
    assert (payload["order_id"], payload["original_size"], payload["new_size"], payload["reason"], payload["provisional"]) == (
        "DEMO-A2A-81B20E", "8.0", "8.5", "TOO_SMALL", True)
    assert line_qty("DEMO-A2A-81B20E", "DEMO-A2A-81B20E-L1") == 1
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert txn["order_id"] == "DEMO-A2A-81B20E" and txn["replacement_size"] == "8.5" and txn["original_size"] == "8.0"
    mirrored = trace.list_a2a_events(AVA).events[-1]["payload"]
    assert (mirrored["order_id"], mirrored["original_size"], mirrored["new_size"]) == ("DEMO-A2A-81B20E", "8.0", "8.5")


# -------------------------------------------------------------------- A2A mirror and flush


def test_transaction_created_is_mirrored_to_the_a2a_stream_with_public_fields_only(flow):
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    events = trace.list_a2a_events(AVA).events
    assert len(events) == 1 and events[0]["type"] == "transaction_created" and events[0]["actor"] == "aldo_agent"
    assert events[0]["payload"] == {
        "transaction_id": result.transaction_id, "state": "completed", "option_type": "exchange_store_pickup",
        "order_id": "DEMO-1001", "replayed": False, "original_size": "7.0", "new_size": "7.5",
        "location_id": MTL, "fee_cents": 0}
    execution.execute(AVA, hq.quote_id, run_id=hq.run_id)  # a replay mirrors nothing
    assert len(trace.list_a2a_events(AVA).events) == 1
    stored = store.get_item(store.event_key(trace.a2a_run_id(AVA), 1))
    assert "DEMO-CUST" not in str(stored["payload"]) and "owner_ref" not in stored["payload"]
    assert set(stored["payload"]) <= trace.A2A_PUBLIC_FIELDS["transaction_created"]


def test_returns_are_mirrored_too(flow):
    hq, _ = approved(flow, "return_mail", types=("return_mail", "exchange_store_pickup"))
    result = execution.execute(AVA, hq.quote_id)
    mirrored = trace.list_a2a_events(AVA).events[0]["payload"]
    assert mirrored == {"transaction_id": result.transaction_id, "state": "completed", "option_type": "return_mail",
                        "order_id": "DEMO-1001", "replayed": False, "original_size": "7.0", "new_size": None,
                        "location_id": DC, "fee_cents": 595}


def test_flush_runs_once_after_an_exchange_when_the_gateway_delivered(flow, monkeypatch):
    calls = []

    def deliver(customer_id):
        calls.append(customer_id)
        for item in memory_updates.list_pending(customer_id):
            memory_updates.mark_delivered(item["update_id"])
        return 1

    monkeypatch.setattr(execution, "_flush_outbox", deliver)
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id)
    assert calls == [AVA]
    assert store.get_item(store.outbox_key(result.memory_update_id))["status"] == "delivered"
    execution.execute(AVA, hq.quote_id)  # a replay does not flush again
    assert calls == [AVA]


def test_flush_gets_a_second_try_when_nothing_was_attempted(flow, no_flush):
    """The pending index can lag behind the commit; an item with attempts == 0 is flushed once more."""
    hq, _ = approved(flow)
    execution.execute(AVA, hq.quote_id)
    assert no_flush == [AVA, AVA]


def test_a_failed_delivery_attempt_is_left_to_the_worker_retry(flow, monkeypatch):
    calls = []

    def fail_delivery(customer_id):
        calls.append(customer_id)
        for item in memory_updates.list_pending(customer_id):
            memory_updates.mark_failed(item["update_id"], "connection refused")
        return 0

    monkeypatch.setattr(execution, "_flush_outbox", fail_delivery)
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id)
    assert calls == [AVA]  # attempted once; the periodic flush retries later
    row = store.get_item(store.outbox_key(result.memory_update_id))
    assert row["status"] == "pending" and row["attempts"] == 1


def test_a_flush_failure_never_fails_the_exchange(flow, monkeypatch):
    def explode(customer_id):
        raise RuntimeError("gateway is down")

    monkeypatch.setattr(execution, "_flush_outbox", explode)
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    assert result.replayed is False and store.get_item(store.txn_key(result.transaction_id))["state"] == "completed"
    assert atp(HERO_075, MTL) == (0, 2) and line_qty() == 1
    assert store.get_item(store.outbox_key(result.memory_update_id))["status"] == "pending"
    warnings = [e for e in trace.list_events(hq.run_id).events if e["type"] == "warning"]
    assert len(warnings) == 1 and warnings[0]["payload"]["code"] == "WRITEBACK_PENDING"
    assert "gateway is down" not in str(warnings[0])


def test_a_missing_gateway_module_is_not_fatal(flow, monkeypatch, no_flush):
    """gateway.py is written by another stream: the lazy import sits inside the try/except."""
    import returns_domain

    monkeypatch.setattr(execution, "_flush_outbox", no_flush.original)  # the real function
    monkeypatch.setitem(sys.modules, "returns_domain.gateway", None)  # `from . import gateway` now fails
    monkeypatch.delattr(returns_domain, "gateway", raising=False)
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    assert result.transaction_id and len(keys("TXN#")) == 1
    assert [e["payload"]["code"] for e in trace.list_events(hq.run_id).events if e["type"] == "warning"] == ["WRITEBACK_PENDING"]


def test_the_real_flush_calls_gateway_flush_outbox_with_the_customer_and_limit_3(flow, monkeypatch, no_flush):
    import returns_domain

    seen = []
    fake = types.ModuleType("returns_domain.gateway")
    fake.flush_outbox = lambda customer_id=None, limit=10, client=None: seen.append((customer_id, limit)) or 0
    monkeypatch.setitem(sys.modules, "returns_domain.gateway", fake)
    monkeypatch.setattr(returns_domain, "gateway", fake, raising=False)
    assert no_flush.original(AVA) == 0 and seen == [(AVA, 3)]
    monkeypatch.setattr(execution, "_flush_outbox", no_flush.original)
    hq, _ = approved(flow)
    execution.execute(AVA, hq.quote_id)
    assert seen[1:] == [(AVA, 3), (AVA, 3)]  # attempts == 0 and still pending: one more flush after the pause


# ------------------------------------------------------------------------------- status


def test_get_status_reads_the_transaction_and_the_current_reservation(flow):
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id)
    view = execution.get_status(AVA, result.transaction_id)
    assert (view.transaction_id, view.state, view.option_type, view.order_id, view.line_id, view.qty) == (
        result.transaction_id, "completed", "exchange_store_pickup", "DEMO-1001", "DEMO-1001-L1", 1)
    assert (view.sku, view.location_id, view.location_name, view.replacement_size) == (HERO_075, MTL, "ALDO Demo Store Montreal", "7.5")
    assert (view.refund_cents, view.fee_cents, view.currency, view.eta_date, view.eta_text, view.simulated) == (
        0, 0, "CAD", "2026-10-17", "Sat Oct 17", True)
    assert view.reservation == {"line_returned_or_reserved_qty": 1, "line_purchased_qty": 1, "location_atp": 0}
    assert view.next_step.startswith("Pick up size 7.5") and view.quote_id == hq.quote_id
    assert (view.original_size, view.memory_update_id) == ("7.0", result.memory_update_id)
    body = view.model_dump()
    assert "customer_id" not in body and "approval_id" not in body and "option" not in body
    store.update_item(store.inventory_key(HERO_075, MTL), add={"atp": 5})  # reads are live, not snapshots
    assert execution.get_status(AVA, result.transaction_id).reservation["location_atp"] == 5


def test_get_status_for_a_return_has_no_location_stock(flow):
    hq, _ = approved(flow, "return_in_store")
    view = execution.get_status(AVA, execution.execute(AVA, hq.quote_id).transaction_id)
    assert view.reservation["location_atp"] is None and view.replacement_size is None and view.memory_update_id is None


def test_get_status_ownership_and_missing(flow, snapshot):
    hq, _ = approved(flow)
    result = execution.execute(AVA, hq.quote_id)
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.get_status(BEN, result.transaction_id, run_id="RUN-BEN")
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert snapshot(exclude=("AUDIT#",)) == before
    assert audit.list_audit(action="access_denied")[-1]["refs"]["resource_type"] == "transaction"
    with pytest.raises(DomainError) as exc:
        execution.get_status(AVA, "DEMO-RMA-NOPE00")
    assert exc.value.code == ErrorCode.NOT_FOUND
