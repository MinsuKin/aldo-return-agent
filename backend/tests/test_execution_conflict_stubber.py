"""G-04: TransactionConflict and friends are retried until the idempotency record resolves.

moto only models ConditionalCheckFailed, so the transient reasons are injected through a botocore
Stubber on the real client (the response is parsed by botocore exactly as DynamoDB's would be)
and, for the "someone else won" case, by letting a competitor commit between our read and write.
"""

from __future__ import annotations

import pytest
from botocore.stub import Stubber

from returns_domain import execution, store
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA

pytestmark = pytest.mark.usefixtures("no_flush")

HERO_075 = "DEMO-SKU-HERO-BLACK-075"
MTL = "DEMO-STORE-MTL-01"


def keys(prefix):
    return sorted(store.scan_keys((prefix,)))


def stub_cancellation(codes):
    """Run the REAL transact_write against a Stubber that answers with ``codes`` as cancellation reasons."""

    def raise_via_botocore(ops, **kwargs):
        real = _REAL
        with Stubber(store.get_client()) as stub:
            stub.add_client_error(
                "transact_write_items",
                service_error_code="TransactionCanceledException",
                service_message="Transaction cancelled, please refer cancellation reasons for specific reasons",
                http_status_code=400,
                modeled_fields={"CancellationReasons": [{"Code": codes[i % len(codes)], "Message": "m"} for i in range(len(ops))]},
            )
            return real(ops, **kwargs)

    return raise_via_botocore


_REAL = store.transact_write


@pytest.fixture
def approved_quote(flow):
    hq = flow.hero_quote()
    flow.approve(hq)
    return hq


def counting(monkeypatch, behaviour):
    """Replace store.transact_write; ``behaviour(call_number, ops, kwargs)`` decides what each call does."""
    state = {"calls": 0, "ops": []}

    def wrapper(ops, **kwargs):
        state["calls"] += 1
        state["ops"].append([op.label for op in ops])
        return behaviour(state["calls"], ops, kwargs)

    monkeypatch.setattr(store, "transact_write", wrapper)
    return state


def test_transaction_conflict_then_success(approved_quote, monkeypatch):
    hq = approved_quote
    sleeps = []
    monkeypatch.setattr(execution, "_sleep", sleeps.append)
    conflict = stub_cancellation(["TransactionConflict"])
    state = counting(monkeypatch, lambda n, ops, kw: conflict(ops, **kw) if n == 1 else _REAL(ops, **kw))

    result = execution.execute(AVA, hq.quote_id)

    jitter = [s for s in sleeps if s != execution.FLUSH_RETRY_DELAY_SECONDS]  # the other sleep is the flush retry
    assert state["calls"] == 2 and len(jitter) == 1 and 0.05 <= jitter[0] <= 0.2  # one jittered pause
    assert result.replayed is False
    assert keys("TXN#") == [(f"TXN#{result.transaction_id}", "META")]  # the discarded first attempt left no trace
    assert store.get_item(store.idem_key(result.approval_id))["transaction_id"] == result.transaction_id
    assert store.get_item(store.inventory_key(HERO_075, MTL))["atp"] == 0
    assert store.get_item(store.line_key("DEMO-1001", "DEMO-1001-L1"))["returned_or_reserved_qty"] == 1
    assert len(keys("A2AOUT#")) == 1


def test_a_conflict_with_a_competing_execute_resolves_to_the_competitors_transaction(approved_quote, monkeypatch):
    """Same txn id for both callers: the loser retries, finds IDEM and returns the winner's transaction."""
    hq = approved_quote
    monkeypatch.setattr(execution, "_sleep", lambda s: None)
    winners = []
    conflict = stub_cancellation(["TransactionConflict"])

    def behaviour(n, ops, kw):
        if n == 1:  # the competitor commits first (its own commit is call 1 of a nested execute)
            monkeypatch.setattr(store, "transact_write", _REAL)
            winners.append(execution.execute(AVA, hq.quote_id))
            monkeypatch.setattr(store, "transact_write", wrapper_ref["fn"])
            return conflict(ops, **kw)
        return _REAL(ops, **kw)

    wrapper_ref = {}
    state = counting(monkeypatch, behaviour)
    wrapper_ref["fn"] = store.transact_write

    loser = execution.execute(AVA, hq.quote_id)

    assert loser.replayed is True and loser.transaction_id == winners[0].transaction_id
    assert state["calls"] == 1  # the retry never reached the commit: IDEM already existed
    assert len(keys("TXN#")) == 1 and len(keys("A2AOUT#")) == 1
    assert store.get_item(store.inventory_key(HERO_075, MTL))["atp"] == 0  # decremented once, not twice
    assert store.get_item(store.line_key("DEMO-1001", "DEMO-1001-L1"))["returned_or_reserved_qty"] == 1


@pytest.mark.parametrize("reason", ["ThrottlingError", "ProvisionedThroughputExceeded", "TransactionInProgressException"])
def test_other_transient_reasons_are_retried_too(approved_quote, monkeypatch, reason):
    monkeypatch.setattr(execution, "_sleep", lambda s: None)
    transient = stub_cancellation([reason])
    state = counting(monkeypatch, lambda n, ops, kw: transient(ops, **kw) if n <= 2 else _REAL(ops, **kw))
    result = execution.execute(AVA, approved_quote.quote_id)
    assert state["calls"] == 3 and result.replayed is False and len(keys("TXN#")) == 1


def test_a_conflict_that_never_clears_raises_transaction_conflict_and_writes_nothing(approved_quote, monkeypatch, snapshot):
    monkeypatch.setattr(execution, "_sleep", lambda s: None)
    conflict = stub_cancellation(["TransactionConflict"])
    state = counting(monkeypatch, lambda n, ops, kw: conflict(ops, **kw))
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, approved_quote.quote_id)
    assert exc.value.code == ErrorCode.TRANSACTION_CONFLICT and exc.value.retryable is True
    assert state["calls"] == 1 + execution.MAX_TRANSIENT_RETRIES == 5
    assert snapshot(exclude=("AUDIT#",)) == before and keys("TXN#") == []


def test_the_top_level_conflict_exception_is_also_retried(approved_quote, monkeypatch):
    monkeypatch.setattr(execution, "_sleep", lambda s: None)

    def top_level(ops, **kw):
        with Stubber(store.get_client()) as stub:
            stub.add_client_error("transact_write_items", service_error_code="TransactionConflictException",
                                  service_message="conflict", http_status_code=400)
            return _REAL(ops, **kw)

    state = counting(monkeypatch, lambda n, ops, kw: top_level(ops, **kw) if n == 1 else _REAL(ops, **kw))
    result = execution.execute(AVA, approved_quote.quote_id)
    assert state["calls"] == 2 and len(keys("TXN#")) == 1 and result.replayed is False


def test_a_mixed_cancellation_with_a_real_condition_failure_is_not_retried(approved_quote, monkeypatch):
    """A failed inventory condition is final even if another operation reported a conflict."""
    monkeypatch.setattr(execution, "_sleep", lambda s: None)
    codes = ["None", "None", "None", "None", "ConditionalCheckFailed", "TransactionConflict", "None"]

    def mixed(ops, **kw):
        with Stubber(store.get_client()) as stub:
            stub.add_client_error(
                "transact_write_items", service_error_code="TransactionCanceledException", service_message="x",
                http_status_code=400,
                modeled_fields={"CancellationReasons": [{"Code": c, "Message": "m"} for c in codes[: len(ops)]]})
            return _REAL(ops, **kw)

    state = counting(monkeypatch, lambda n, ops, kw: mixed(ops, **kw))
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, approved_quote.quote_id)
    assert state["ops"][0] == ["txn", "idem", "approval", "line", "inventory", "quote", "outbox", "store_notice"]
    assert exc.value.code == ErrorCode.STOCK_CHANGED and state["calls"] == 1


def test_a_caller_that_validates_after_the_winner_committed_gets_the_winners_transaction(approved_quote, monkeypatch):
    """The stale caller read IDEM before the winner committed. By the time it validates, the winner has
    used up the line, so validation fails; it must re-read IDEM and return the winner's transaction
    instead of reporting an ineligible second request."""
    hq = approved_quote
    first = execution.execute(AVA, hq.quote_id)
    real_get = store.get_item
    hidden = {"on": True}

    def hide_idem_once(key, **kwargs):
        if hidden["on"] and key[0].startswith("IDEM#"):
            hidden["on"] = False
            return None
        return real_get(key, **kwargs)

    monkeypatch.setattr(store, "get_item", hide_idem_once)
    result = execution.execute(AVA, hq.quote_id)
    assert result.replayed is True and result.transaction_id == first.transaction_id
    assert len(keys("TXN#")) == 1 and store.get_item(store.inventory_key(HERO_075, MTL))["atp"] == 0
    assert store.get_item(store.line_key("DEMO-1001", "DEMO-1001-L1"))["returned_or_reserved_qty"] == 1


def test_the_commit_has_the_documented_labelled_operations(approved_quote, monkeypatch):
    # a store pickup exchange: write-back outbox AND the store-desk notice ride in the same transaction
    state = counting(monkeypatch, lambda n, ops, kw: _REAL(ops, **kw))
    execution.execute(AVA, approved_quote.quote_id)
    assert state["ops"] == [["txn", "idem", "approval", "line", "inventory", "quote", "outbox", "store_notice"]]


def test_a_ship_exchange_has_the_outbox_but_no_store_notice(flow, monkeypatch):
    hq = flow.hero_quote()
    flow.approve(hq, "exchange_ship_from_dc")
    state = counting(monkeypatch, lambda n, ops, kw: _REAL(ops, **kw))
    execution.execute(AVA, hq.quote_id)
    assert state["ops"] == [["txn", "idem", "approval", "line", "inventory", "quote", "outbox"]]


def test_return_commits_have_no_inventory_or_outbox_operation(flow, monkeypatch):
    hq = flow.hero_quote()
    flow.approve(hq, "return_in_store")
    state = counting(monkeypatch, lambda n, ops, kw: _REAL(ops, **kw))
    execution.execute(AVA, hq.quote_id)
    assert state["ops"] == [["txn", "idem", "approval", "line", "quote", "store_notice"]]  # in store: the desk is told


def test_a_mail_return_commit_is_the_five_original_operations(flow, monkeypatch):
    hq = flow.hero_quote(types=("return_mail", "exchange_store_pickup"))
    flow.approve(hq, "return_mail")
    state = counting(monkeypatch, lambda n, ops, kw: _REAL(ops, **kw))
    execution.execute(AVA, hq.quote_id)
    assert state["ops"] == [["txn", "idem", "approval", "line", "quote"]]
