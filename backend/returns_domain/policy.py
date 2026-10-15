"""Deterministic return policy DEMO-POLICY-CA, version 2026.10-demo (requirement 4, design 5.1).

A demo assumption, not official ALDO policy. The function reads only structured facts the
caller loaded from the data store plus two closed vocabularies (reason category, condition).
It takes no free text, so "ignore policy" in a customer message cannot change a result.

Rule order (every rule is evaluated; the FIRST terminal rule decides; ALL matches are
reported in ``rule_ids``, in this order):

1. final_sale True, reason is not QUALITY_DAMAGE   -> ineligible       final_sale.no_return
2. final_sale True, reason is QUALITY_DAMAGE       -> needs_review     final_sale.defect_review
   final_sale None (unknown, never treated as false) -> note only     final_sale.unknown
3. qty > purchased - returned_or_reserved          -> ineligible       qty.exceeds_remaining
4. delivered_at missing                            -> needs_review     window.missing_delivery_date
5. more than 30 days after delivery (Toronto)      -> ineligible       window.expired
6. condition unknown                               -> needs_information condition.unknown
7. condition worn, reason is not QUALITY_DAMAGE    -> ineligible       condition.worn
8. refund above the authority limit                -> needs_review     authority.refund_limit
   (CAD 250.00 by default; the caller passes the member tier's limit, for example CAD 750.00)
9. otherwise                                       -> eligible         window.ok

Exchange option types are allowed when the reason category is one of ``exchange_reasons`` (demo
assumption D5: FIT_SIZE, EXPECTATION and CHANGE_OF_MIND; ``policy.json`` can override the list and
``EXCHANGE_REASONS`` is the fallback). For any other reason only the two return types are allowed.
Member tiers (none, silver, platinum) are demo assumptions too: ``policy.json`` ``tiers`` with
``TIERS_DEFAULT`` as the fallback.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from . import clock, money
from .errors import DomainError, ErrorCode
from .taxonomy import ItemCondition, ReasonCategory, parse_condition, parse_reason_category

POLICY_ID = "DEMO-POLICY-CA"
POLICY_VERSION = "2026.10-demo"
POLICY_LABEL = "Demo assumption, not official ALDO policy"

WINDOW_DAYS = 30  # inclusive: day 30 is eligible, day 31 is not
AUTHORITY_REFUND_LIMIT_CENTS = 25_000  # CAD 250.00; strictly above this needs a person

#: A fit problem: the exchange is for ANOTHER SIZE of the same colour (the size direction decides).
FIT_REASONS: tuple[str, ...] = ("FIT_SIZE",)
#: Reasons that unlock exchange options (PIVOT_ADDENDUM D5). A reason that is not a fit reason
#: but is listed here is a PREFERENCE: the exchange is for another COLOUR in the SAME size.
#: Fallback for ``policy.json`` ``exchange_reasons``.
EXCHANGE_REASONS: tuple[str, ...] = ("FIT_SIZE", "EXPECTATION", "CHANGE_OF_MIND")

TIER_NONE = "none"
#: Fallback for ``policy.json`` ``tiers`` (PIVOT_ADDENDUM D2, D8; demo assumptions, not ALDO policy).
TIERS_DEFAULT: Mapping[str, Mapping[str, Any]] = {
    "none": {"label": "Member", "authority_refund_limit_cents": 25_000},
    "silver": {"label": "Silver member", "authority_refund_limit_cents": 25_000},
    "platinum": {"label": "Platinum member", "authority_refund_limit_cents": 75_000},
}


class Decision(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    NEEDS_INFORMATION = "needs_information"
    NEEDS_REVIEW = "needs_review"


EXCHANGE_OPTION_TYPES: tuple[str, ...] = (
    "exchange_ship_from_dc",
    "exchange_ship_from_store",
    "exchange_store_pickup",
)
RETURN_OPTION_TYPES: tuple[str, ...] = ("return_in_store", "return_mail")
ALL_OPTION_TYPES: tuple[str, ...] = EXCHANGE_OPTION_TYPES + RETURN_OPTION_TYPES

#: Every rule id this module can report (same order as fixtures/demo/policy.json).
RULE_IDS: tuple[str, ...] = (
    "final_sale.no_return",
    "final_sale.defect_review",
    "qty.exceeds_remaining",
    "window.missing_delivery_date",
    "window.expired",
    "window.ok",
    "condition.unknown",
    "condition.worn",
    "authority.refund_limit",
    "final_sale.unknown",
)


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    rule_ids: tuple[str, ...]
    missing_fields: tuple[str, ...]
    allowed_option_types: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    policy_id: str = POLICY_ID
    #: Server-computed facts the agent may quote (nothing for the model to calculate).
    facts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_eligible(self) -> bool:
        return self.decision is Decision.ELIGIBLE

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (plain str / int / list) for PCHK items, tool results and traces."""
        return {
            "decision": self.decision.value,
            "rule_ids": list(self.rule_ids),
            "missing_fields": list(self.missing_fields),
            "allowed_option_types": list(self.allowed_option_types),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "facts": dict(self.facts),
        }


def gross_refund_cents(line: Mapping[str, Any], qty: int) -> int:
    """Refund basis for the authority limit: the price paid times the quantity (fees excluded)."""
    raw_unit = _as_mapping(line, "line").get("unit_paid_cents")
    unit = money.ensure_cents(raw_unit, "unit_paid_cents", minimum=0)
    return unit * _whole_number(qty, "qty", minimum=1)


# ------------------------------------------------------------- policy.json (optional keys)


def _policy_json() -> Mapping[str, Any]:
    """``policy.json`` through the fixtures loader; ``{}`` when it cannot be read.

    The optional keys below always have a constant fallback, so a missing, stale or broken
    file can never make an evaluation fail or change the shipped behaviour.
    """
    try:
        from . import fixtures  # lazy: the rules stay importable without the DynamoDB client

        data = fixtures.load_policy_json()
    except Exception:
        return {}
    return data if isinstance(data, Mapping) else {}


def exchange_reasons() -> tuple[str, ...]:
    """Reason categories that allow exchange options: ``policy.json`` ``exchange_reasons``, else the constant.

    A list that is empty, not a list, or holds anything outside the reason taxonomy is ignored as
    a whole (no partial application of a broken list).
    """
    raw = _policy_json().get("exchange_reasons")
    if isinstance(raw, list) and raw:
        try:
            return tuple(dict.fromkeys(parse_reason_category(item).value for item in raw))
        except DomainError:
            pass
    return EXCHANGE_REASONS


def is_fit_reason(reason_category: object) -> bool:
    """True for a size problem (the exchange changes the SIZE); preference reasons change the colour.

    Accepts the taxonomy spellings (``FIT_SIZE``, ``fit_size``, the enum member); anything that is
    not a reason category is not a fit reason.
    """
    try:
        return parse_reason_category(reason_category).value in FIT_REASONS
    except DomainError:
        return False


def load_tiers() -> dict[str, dict[str, Any]]:
    """Member tiers: ``TIERS_DEFAULT`` overlaid with the valid entries of ``policy.json`` ``tiers``.

    Every tier has a ``label`` (text) and an ``authority_refund_limit_cents`` (whole cents);
    other keys of a tier (for example ``benefits``) are passed through untouched.
    """
    tiers = {name: dict(config) for name, config in TIERS_DEFAULT.items()}
    raw = _policy_json().get("tiers")
    if isinstance(raw, Mapping):
        for name, config in raw.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(config, Mapping):
                continue
            merged = {**tiers.get(name.strip().lower(), {}), **copy.deepcopy(dict(config))}
            label, limit = merged.get("label"), merged.get("authority_refund_limit_cents")
            if (
                isinstance(label, str)
                and label.strip()
                and isinstance(limit, int)
                and not isinstance(limit, bool)
                and limit >= 0
            ):
                tiers[name.strip().lower()] = merged
    return tiers


def tier_config(tier: object) -> dict[str, Any]:
    """The tier's settings as ``{"tier", "label", "authority_refund_limit_cents", ...}``.

    ``tier`` is a customer's stored tier; ``None``, blank or a tier that does not exist is the
    plain ``none`` member (no perks, the default authority limit).
    """
    tiers = load_tiers()
    name = tier.strip().lower() if isinstance(tier, str) else ""
    if name not in tiers:
        name = TIER_NONE
    return {"tier": name, **tiers[name]}


def evaluate(
    order: Mapping[str, Any],
    line: Mapping[str, Any],
    qty: int,
    reason_category: ReasonCategory | str,
    condition: ItemCondition | str,
    refund_cents: int | None = None,
    now: date | datetime | str | None = None,
    authority_limit_cents: int | None = None,
) -> PolicyResult:
    """Evaluate the demo policy for ``qty`` units of ``line`` on ``order``.

    ``order`` and ``line`` are the store items (``delivered_at``; ``purchased_qty``,
    ``returned_or_reserved_qty``, ``unit_paid_cents``, ``final_sale``). ``refund_cents``
    defaults to the gross refund (``unit_paid_cents * qty``); callers must never pass a
    model-supplied value. ``now`` defaults to DEMO_NOW and is compared on the Toronto
    calendar. ``authority_limit_cents`` is the member tier's limit that the CALLER looked up in
    the store (``orders.customer_profile``); it defaults to the CAD 250.00 constant and is
    reported in ``facts``.
    """
    order_item = _as_mapping(order, "order")
    line_item = _as_mapping(line, "line")
    quantity = _whole_number(qty, "qty", minimum=1)
    reason = parse_reason_category(reason_category)
    item_condition = parse_condition(condition)

    purchased = _count(line_item, "purchased_qty")
    already = _count(line_item, "returned_or_reserved_qty", default=0)
    remaining = purchased - already
    final_sale = _final_sale_flag(line_item)
    delivered = _delivered_on(order_item)
    refund = (
        gross_refund_cents(line_item, quantity)
        if refund_cents is None
        else money.ensure_cents(refund_cents, "refund_cents", minimum=0)
    )
    limit = (
        AUTHORITY_REFUND_LIMIT_CENTS
        if authority_limit_cents is None
        else money.ensure_cents(authority_limit_cents, "authority_limit_cents", minimum=0)
    )
    today = clock.demo_today() if now is None else clock.to_toronto_date(now)
    days = None if delivered is None else (today - delivered).days
    defect = reason is ReasonCategory.QUALITY_DAMAGE

    # (rule_id, decision or None for a note) in rule order.
    hits: list[tuple[str, Decision | None]] = []
    if final_sale is True:
        if defect:
            hits.append(("final_sale.defect_review", Decision.NEEDS_REVIEW))
        else:
            hits.append(("final_sale.no_return", Decision.INELIGIBLE))
    elif final_sale is None:
        hits.append(("final_sale.unknown", None))
    if quantity > remaining:
        hits.append(("qty.exceeds_remaining", Decision.INELIGIBLE))
    if days is None:
        hits.append(("window.missing_delivery_date", Decision.NEEDS_REVIEW))
    elif days > WINDOW_DAYS:
        hits.append(("window.expired", Decision.INELIGIBLE))
    if item_condition is ItemCondition.UNKNOWN:
        hits.append(("condition.unknown", Decision.NEEDS_INFORMATION))
    elif item_condition is ItemCondition.WORN and not defect:
        hits.append(("condition.worn", Decision.INELIGIBLE))
    if refund > limit:
        hits.append(("authority.refund_limit", Decision.NEEDS_REVIEW))

    decision = next((d for _, d in hits if d is not None), Decision.ELIGIBLE)
    rule_ids = [rule_id for rule_id, _ in hits]
    if decision is Decision.ELIGIBLE:
        rule_ids.append("window.ok")

    if decision is not Decision.ELIGIBLE:
        allowed: tuple[str, ...] = ()
    elif reason.value in exchange_reasons():
        allowed = ALL_OPTION_TYPES
    else:
        allowed = RETURN_OPTION_TYPES

    return PolicyResult(
        decision=decision,
        rule_ids=tuple(rule_ids),
        missing_fields=("condition",) if decision is Decision.NEEDS_INFORMATION else (),
        allowed_option_types=allowed,
        facts={
            "days_since_delivery": days,
            "window_days": WINDOW_DAYS,
            "requested_qty": quantity,
            "remaining_qty": max(remaining, 0),
            "refund_cents": refund,
            "authority_limit_cents": limit,
            "currency": money.CURRENCY,
        },
    )


# ----------------------------------------------------------------------------- inputs


def _as_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"{label} must be an item mapping")
    return value


def _whole_number(value: object, label: str, *, minimum: int) -> int:
    """int (or integral Decimal from boto3) at or above ``minimum``; never bool or float."""
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"{label} must be a whole number of at least {minimum}"
        )
    return value


def _count(item: Mapping[str, Any], name: str, *, default: int | None = None) -> int:
    raw = item.get(name, default)
    if raw is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"line is missing {name}")
    return _whole_number(raw, name, minimum=0)


def _final_sale_flag(line: Mapping[str, Any]) -> bool | None:
    """True / False / None. None means unknown and is never read as False."""
    flag = line.get("final_sale")
    if flag is None or isinstance(flag, bool):
        return flag
    raise DomainError(ErrorCode.VALIDATION_ERROR, "final_sale must be true, false or null")


def _delivered_on(order: Mapping[str, Any]) -> date | None:
    raw = order.get("delivered_at")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return clock.to_toronto_date(raw)
