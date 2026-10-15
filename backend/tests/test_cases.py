"""cases.py: idempotent escalation, ownership and the cases list (requirement 10)."""

from __future__ import annotations

import re

import pytest

from returns_domain import audit, cases, orders, store, trace
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN


def start_run(flow, customer=AVA):
    return flow.start_run(customer)[1]


def test_escalate_creates_an_open_case_with_derived_id(flow, fixed_now):
    run_id = start_run(flow)
    case = cases.escalate(AVA, run_id, "POLICY_REVIEW", "Final-sale item reported broken; needs a person.")
    assert case.case_id == store.derived_id("DEMO-CASE-", run_id, "POLICY_REVIEW", n=6)
    assert re.fullmatch(r"DEMO-CASE-[0-9A-F]{6}", case.case_id)
    assert (case.status, case.reason_code, case.run_id, case.created_at) == ("open", "POLICY_REVIEW", run_id, "2026-10-16T16:00:00+00:00")
    assert (case.order_id, case.line_id, case.policy_check_id, case.policy_result, case.rule_ids) == (None, None, None, None, [])
    item = store.get_item(store.case_key(case.case_id))
    assert item["customer_id"] == AVA and item["summary"].startswith("Final-sale") and item["status"] == "open"
    assert (item["gsi1pk"], item["gsi1sk"]) == ("CASES", "2026-10-16T16:00:00+00:00")
    assert "customer_id" not in case.model_dump()


def test_escalate_idempotent(flow):
    """G-17: a model-level retry (same run, same reason) returns the same case and creates no duplicate."""
    run_id = start_run(flow)
    first = cases.escalate(AVA, run_id, "TOOL_FAILURE", "The options tool failed twice.")
    again = cases.escalate(AVA, run_id, "TOOL_FAILURE", "A differently worded summary of the same thing.")
    assert again.case_id == first.case_id and again.summary == first.summary
    assert list(store.scan_keys(("CASE#",))) == [(f"CASE#{first.case_id}", "META")]
    assert [e["type"] for e in trace.list_events(run_id).events] == ["case_created"]  # emitted once
    assert len(audit.list_audit(action="case_created")) == 1
    other_reason = cases.escalate(AVA, run_id, "CUSTOMER_REQUEST", "Customer asked for a person.")
    other_run = cases.escalate(AVA, start_run(flow), "TOOL_FAILURE", "Same reason, other run.")
    assert len({first.case_id, other_reason.case_id, other_run.case_id}) == 3


def test_case_created_event_and_audit(flow):
    run_id = start_run(flow)
    case = cases.escalate(AVA, run_id, "LIMIT_REACHED", "Round limit reached.")
    event = trace.list_events(run_id).events[0]
    assert event["type"] == "case_created" and event["payload"] == {"case_id": case.case_id, "reason_code": "LIMIT_REACHED"}
    assert audit.list_audit(action="case_created")[0]["refs"] == {"case_id": case.case_id, "reason_code": "LIMIT_REACHED"}


def test_without_a_run_the_case_id_is_random_and_no_event_is_written(flow):
    one = cases.escalate(AVA, None, "CUSTOMER_REQUEST", "Please call me.")
    two = cases.escalate(AVA, None, "CUSTOMER_REQUEST", "Please call me.")
    assert one.case_id != two.case_id and one.run_id is None
    assert len(list(store.scan_keys(("CASE#",)))) == 2


@pytest.mark.parametrize("reason", ["WHATEVER", "", None, "policy_review"])
def test_reason_codes_are_a_closed_set(flow, reason):
    with pytest.raises(DomainError) as exc:
        cases.escalate(AVA, start_run(flow), reason, "x")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert list(store.scan_keys(("CASE#",))) == []


def test_summary_limits(flow):
    run_id = start_run(flow)
    assert len(cases.escalate(AVA, run_id, "POLICY_REVIEW", "s" * 800).summary) == 800
    for bad in ("s" * 801, "", "   ", None, 5):
        with pytest.raises(DomainError) as exc:
            cases.escalate(AVA, run_id, "TOOL_FAILURE", bad)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_summary_is_stored_trimmed(flow):
    assert cases.escalate(AVA, start_run(flow), "POLICY_REVIEW", "  needs review \n").summary == "needs review"


def test_escalate_pulls_the_latest_policy_check_of_the_run_for_the_line(flow, fixed_now):
    run_id = start_run(flow)
    orders.check_policy(AVA, run_id, "DEMO-1002", "DEMO-1002-L1", 1, "CHANGE_OF_MIND", "unworn")
    fixed_now.advance(seconds=3)
    latest = orders.check_policy(AVA, run_id, "DEMO-1002", "DEMO-1002-L1", 1, "QUALITY_DAMAGE", "damaged")
    other_run_check = orders.check_policy(AVA, start_run(flow), "DEMO-1002", "DEMO-1002-L1", 1, "CHANGE_OF_MIND", "unworn")
    case = cases.escalate(AVA, run_id, "POLICY_REVIEW", "Heel broke on a final-sale pair.", "DEMO-1002", "DEMO-1002-L1")
    assert case.policy_check_id == latest.policy_check_id != other_run_check.policy_check_id
    assert case.policy_result["decision"] == "needs_review" and case.rule_ids == ["final_sale.defect_review"]
    assert case.order_id == "DEMO-1002" and case.line_id == "DEMO-1002-L1"
    assert store.get_item(store.case_key(case.case_id))["policy_result"]["policy_version"] == "2026.10-demo"


def test_escalate_with_an_explicit_policy_check(flow):
    run_id = start_run(flow)
    check = orders.check_policy(AVA, "RUN-OTHER", "DEMO-1002", "DEMO-1002-L1", 1, "QUALITY_DAMAGE", "damaged")
    case = cases.escalate(AVA, run_id, "POLICY_REVIEW", "x", "DEMO-1002", "DEMO-1002-L1", check.policy_check_id)
    assert case.policy_check_id == check.policy_check_id and case.rule_ids == ["final_sale.defect_review"]


def test_no_policy_check_means_no_policy_result(flow):
    case = cases.escalate(AVA, start_run(flow), "POLICY_REVIEW", "x", "DEMO-1002", "DEMO-1002-L1")
    assert case.policy_check_id is None and case.policy_result is None and case.rule_ids == []


def test_escalate_ownership_of_orders_and_checks_writes_nothing(flow, snapshot):
    run_id = start_run(flow)
    foreign_check = orders.check_policy(BEN, "RUN-B", "DEMO-1003", "DEMO-1003-L1", 1, "CHANGE_OF_MIND", "unworn")
    before = snapshot(exclude=("AUDIT#",))
    with pytest.raises(DomainError) as exc:
        cases.escalate(AVA, run_id, "POLICY_REVIEW", "x", "DEMO-1006", "DEMO-1006-L1")  # Chloe's order
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    with pytest.raises(DomainError) as exc:
        cases.escalate(AVA, run_id, "POLICY_REVIEW", "x", "DEMO-1002", "DEMO-1002-L1", foreign_check.policy_check_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert snapshot(exclude=("AUDIT#",)) == before
    assert [d["refs"]["resource_type"] for d in audit.list_audit(action="access_denied")] == ["order", "policy_check"]


def test_escalate_validates_order_and_line_references(flow):
    run_id = start_run(flow)
    with pytest.raises(DomainError) as exc:
        cases.escalate(AVA, run_id, "POLICY_REVIEW", "x", "DEMO-9999")
    assert exc.value.code == ErrorCode.ORDER_NOT_FOUND
    with pytest.raises(DomainError) as exc:
        cases.escalate(AVA, run_id, "POLICY_REVIEW", "x", "DEMO-1002", "DEMO-1001-L1")
    assert exc.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as exc:
        cases.escalate(AVA, run_id, "POLICY_REVIEW", "x", None, "DEMO-1002-L1")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert list(store.scan_keys(("CASE#",))) == []


def test_list_cases_returns_only_own_cases_newest_first(flow, fixed_now):
    a = cases.escalate(AVA, start_run(flow), "POLICY_REVIEW", "first")
    fixed_now.advance(seconds=10)
    cases.escalate(BEN, start_run(flow, BEN), "POLICY_REVIEW", "ben's")
    fixed_now.advance(seconds=10)
    b = cases.escalate(AVA, start_run(flow), "TOOL_FAILURE", "second")
    assert [c.case_id for c in cases.list_cases(AVA)] == [b.case_id, a.case_id]
    assert [c.summary for c in cases.list_cases(BEN)] == ["ben's"]
    assert cases.list_cases("DEMO-CUST-CHLOE") == []
    assert all("customer_id" not in c.model_dump() for c in cases.list_cases(AVA))


def test_get_case_ownership(flow):
    case = cases.escalate(AVA, start_run(flow), "POLICY_REVIEW", "x")
    assert cases.get_case(AVA, case.case_id) == case
    with pytest.raises(DomainError) as exc:
        cases.get_case(BEN, case.case_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert audit.list_audit(action="access_denied")[0]["refs"] == {"resource_type": "case", "resource_id": case.case_id}
    with pytest.raises(DomainError) as exc:
        cases.get_case(AVA, "DEMO-CASE-NOPE00")
    assert exc.value.code == ErrorCode.NOT_FOUND
