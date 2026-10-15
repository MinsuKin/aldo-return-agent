"""Ownership on EVERY service function that takes a customer id (requirements 1.2, 1.4).

For each function the intruder (Ben) targets Ava's resource: the call raises NOT_AUTHORIZED, an
``access_denied`` audit event names the intruder and the resource type, and the table is
otherwise unchanged. The owner (Ava) succeeds on the same call, so the denial is about ownership
and not about a broken setup.
"""

from __future__ import annotations

import pytest

from returns_domain import (
    audit, cases, conversations, execution, options, orders, quotes, runs, store,
)
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN, make_agent_order


@pytest.fixture
def world(flow, no_flush):
    """Ava's quote (approved and executed on a second line so status works) plus one of each resource."""
    hq = flow.hero_quote()
    approval = flow.approve(hq)
    executed = flow.hero_quote(order_id="DEMO-1008", line_id="DEMO-1008-L1", reason="CHANGE_OF_MIND", direction="unknown",
                               deadline=None, types=("return_in_store",))
    flow.approve(executed, "return_in_store")
    txn = execution.execute(AVA, executed.quote_id)
    case = cases.escalate(AVA, hq.run_id, "POLICY_REVIEW", "x", "DEMO-1002", "DEMO-1002-L1")
    make_agent_order("DEMO-A2A-81B20E")
    option = hq.option("exchange_store_pickup")
    return {"hq": hq, "approval": approval, "txn": txn, "case": case, "option": option}


CASES = {
    "get_order": ("order", lambda w, who: orders.get_order(who, "DEMO-1001")),
    "get_agent_order": ("order", lambda w, who: orders.get_order(who, "DEMO-A2A-81B20E")),
    "order_context": ("order", lambda w, who: orders.order_context(who, "DEMO-1001")),
    "check_policy": ("order", lambda w, who: orders.check_policy(who, "RUN-X", "DEMO-1001", "DEMO-1001-L1", 1, "FIT_SIZE", "unworn", "too_small")),
    "get_policy_check": ("policy_check", lambda w, who: orders.get_policy_check(who, w["hq"].check.policy_check_id)),
    "find_options": ("policy_check", lambda w, who: options.find_options(who, w["hq"].check.policy_check_id)),
    "create_quote": ("candidate_set", lambda w, who: quotes.create_quote(
        who, "RUN-N", "CONV-N", w["hq"].candidates.candidate_set_id, [w["hq"].candidates.options[2].option_id, w["hq"].candidates.options[3].option_id])),
    "get_quote": ("quote", lambda w, who: quotes.get_quote(who, w["hq"].quote_id)),
    "approve_replay": ("quote", lambda w, who: quotes.approve(who, w["hq"].quote_id, w["option"].option_id, w["option"].payload_hash, "RUN-Z")),
    "execute": ("quote", lambda w, who: execution.execute(who, w["hq"].quote_id)),
    "get_status": ("transaction", lambda w, who: execution.get_status(who, w["txn"].transaction_id)),
    "escalate_order": ("order", lambda w, who: cases.escalate(who, "RUN-C", "POLICY_REVIEW", "x", "DEMO-1002", "DEMO-1002-L1")),
    "get_case": ("case", lambda w, who: cases.get_case(who, w["case"].case_id)),
    "get_run": ("run", lambda w, who: runs.get_run(who, w["hq"].run_id)),
    "get_conversation": ("conversation", lambda w, who: conversations.get_conversation(who, w["hq"].conv_id)),
    "create_conversation": ("order", lambda w, who: conversations.create_conversation(who, "DEMO-1001")),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_intruder_is_denied_audited_and_changes_nothing(world, snapshot, name):
    resource_type, call = CASES[name]
    before = snapshot(exclude=("AUDIT#",))
    audits_before = len(audit.list_audit(action="access_denied"))
    with pytest.raises(DomainError) as exc:
        call(world, BEN)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert snapshot(exclude=("AUDIT#",)) == before  # zero table changes
    denied = audit.list_audit(action="access_denied")
    assert len(denied) == audits_before + 1
    assert denied[-1]["actor"] == BEN and denied[-1]["outcome"] == "denied"
    assert denied[-1]["refs"]["resource_type"] == resource_type


@pytest.mark.parametrize("name", ["get_order", "order_context", "get_policy_check", "get_quote", "get_status", "get_case",
                                  "get_run", "get_conversation", "approve_replay"])
def test_the_owner_succeeds_on_the_same_calls(world, name):
    _, call = CASES[name]
    assert call(world, AVA) is not None


def test_no_denied_call_leaks_the_existence_details(world):
    """The error text is the same whether the resource is someone else's or the intruder asks for a ghost."""
    with pytest.raises(DomainError) as foreign:
        orders.get_order(BEN, "DEMO-1001")
    assert "DEMO-CUST" not in foreign.value.message and "Ava" not in foreign.value.message
    with pytest.raises(DomainError) as foreign_quote:
        quotes.get_quote(BEN, world["hq"].quote_id)
    assert "DEMO-CUST" not in foreign_quote.value.message


def test_every_denial_is_a_distinct_audit_row(world):
    for name in ("get_order", "get_quote", "get_status"):
        with pytest.raises(DomainError):
            CASES[name][1](world, BEN)
    rows = audit.list_audit(action="access_denied", actor=BEN)
    assert sorted(r["refs"]["resource_type"] for r in rows) == ["order", "quote", "transaction"]
    assert len({r["sk"] for r in rows}) == 3


def test_store_level_isolation_between_customers(world):
    """Ben's order list never contains Ava's orders, including a gateway-placed one."""
    assert {o.order_id for o in orders.list_orders(BEN)}.isdisjoint({"DEMO-1001", "DEMO-1002", "DEMO-1008", "DEMO-A2A-81B20E"})
    assert "DEMO-A2A-81B20E" in {o.order_id for o in orders.list_orders(AVA)}
    assert cases.list_cases(BEN) == [] and len(cases.list_cases(AVA)) == 1
