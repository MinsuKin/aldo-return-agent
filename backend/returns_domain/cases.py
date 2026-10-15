"""Human-review cases (requirement 10, design 5.6).

``escalate`` records a case for a person when the agent cannot or must not decide (policy says
``needs_review``, a tool failed, the round limit was hit, a verification mismatch, the customer
asked for a person). The case id is derived from the run and the reason, so a repeated call, or a
model-level retry, returns the existing case instead of creating a duplicate. Cases are listed
through GSI1 under the ``CASES`` partition.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from . import audit, orders, store, trace
from .errors import DomainError, ErrorCode

CASE_REASONS: tuple[str, ...] = (
    "POLICY_REVIEW",
    "TOOL_FAILURE",
    "LIMIT_REACHED",
    "VERIFICATION_MISMATCH",
    "CUSTOMER_REQUEST",
)
MAX_SUMMARY_CHARS = 800


class Case(BaseModel):
    model_config = ConfigDict(extra="ignore")

    case_id: str
    reason_code: str
    summary: str
    order_id: str | None = None
    line_id: str | None = None
    policy_check_id: str | None = None
    policy_result: dict[str, Any] | None = None
    rule_ids: list[str] = []
    status: str = "open"
    created_at: str
    run_id: str | None = None


def escalate(
    customer_id: str,
    run_id: str | None,
    reason_code: str,
    summary: str,
    order_id: str | None = None,
    line_id: str | None = None,
    policy_check_id: str | None = None,
) -> Case:
    """Open a case (or return the existing one for the same run and reason)."""
    if reason_code not in CASE_REASONS:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"reason_code must be one of: {', '.join(CASE_REASONS)}")
    if not isinstance(summary, str) or not summary.strip():
        raise DomainError(ErrorCode.VALIDATION_ERROR, "summary is required")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"summary must be at most {MAX_SUMMARY_CHARS} characters")

    if line_id is not None and order_id is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "line_id needs an order_id")
    if order_id is not None:
        _, lines = orders.load_order_items(customer_id, order_id, run_id=run_id)  # ownership + audit
        if line_id is not None and line_id not in lines:
            raise DomainError(ErrorCode.NOT_FOUND, "Order line not found on this order.")

    check: orders.PolicyCheck | None = None
    if policy_check_id is not None:
        check = orders.get_policy_check(customer_id, policy_check_id)  # ownership + audit
    elif run_id and order_id and line_id:
        check = orders.latest_policy_check(customer_id, run_id, order_id, line_id)

    case_id = store.derived_id("DEMO-CASE-", run_id, reason_code, n=6) if run_id else store.new_id("DEMO-CASE-")
    created_at = store.now_iso()
    pk, sk = store.case_key(case_id)
    g_pk, g_sk = store.gsi1_case(created_at)
    item = {
        "pk": pk,
        "sk": sk,
        "gsi1pk": g_pk,
        "gsi1sk": g_sk,
        "case_id": case_id,
        "customer_id": customer_id,
        "run_id": run_id,
        "reason_code": reason_code,
        "summary": summary.strip(),
        "order_id": order_id,
        "line_id": line_id,
        "policy_check_id": check.policy_check_id if check else None,
        "policy_result": check.result if check else None,
        "rule_ids": list(check.result.get("rule_ids", [])) if check else [],
        "status": "open",
        "created_at": created_at,
    }
    try:
        store.put_item(item, condition="attribute_not_exists(#pk)")
    except store.ConditionFailed:
        return get_case(customer_id, case_id)  # a repeat of the same run and reason
    audit.write_audit(customer_id, "case_created", "allowed", {"case_id": case_id, "reason_code": reason_code})
    if run_id:
        trace.emit_safe(run_id, "case_created", {"case_id": case_id, "reason_code": reason_code})
    return _view(item)


def _view(item: Mapping[str, Any]) -> Case:
    return Case(**{k: v for k, v in item.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk", "customer_id")})


def list_cases(customer_id: str) -> list[Case]:
    """The customer's own cases, newest first (GSI1 ``CASES`` partition)."""
    rows = store.query(store.gsi1_case("x")[0], index="GSI1", forward=False)
    return [_view(row) for row in rows if row.get("customer_id") == customer_id]


def get_case(customer_id: str, case_id: str) -> Case:
    item = store.get_item(store.case_key(case_id))
    if item is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Case not found.")
    if item.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "case", case_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Case not found for this customer.")
    return _view(item)
