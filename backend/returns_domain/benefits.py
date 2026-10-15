"""Member tier and the monthly perk ledger (PIVOT_ADDENDUM D2, D8; domain v2 phase 2).

The tier lives on the customer's ``CUST#`` record; its label, authority limit and benefits come from
``policy.json`` ``tiers`` (demo assumptions, ``policy.TIERS_DEFAULT`` is the fallback). The only
benefit today is ``free_return_shipping``: ONE return label fee waived per calendar month, only for
``return_mail``. How often it was used lives in a ledger item per customer and month:

    BENEFIT#{customer_id}#{yyyy-mm}/META   {used, period, customer_id, benefit, updated_at, ...}

The month is the America/Toronto month of the DEMO clock (``clock.demo_now``), never of the wall
clock, so the demo behaves the same whenever it is run and a test can move into the next month by
changing ``DEMO_NOW``. The ledger is only ever written by ``ledger_update``, a conditional update
that ``execution`` puts in the SAME TransactWriteItems as the bundle it pays for, so the perk is
consumed if and only if the return exists, and never twice in one month. A demo reset removes the
``BENEFIT#`` items (``store.RUNTIME_PREFIXES``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from . import clock, orders, policy, store

FREE_RETURN_SHIPPING = "free_return_shipping"
#: The only option type the perk applies to (``policy.json`` ``applies_to`` must list it).
MAIL_RETURN = "return_mail"
#: Condition of the ledger update: the first use creates the item, later uses need room left.
LEDGER_CONDITION = "attribute_not_exists(#used) OR #used < :per_month"


def period_for(now: date | datetime | str | None = None) -> str:
    """``yyyy-mm`` of the Toronto calendar month of ``now`` (default: the DEMO clock)."""
    day = clock.demo_today() if now is None else clock.to_toronto_date(now)
    return f"{day.year:04d}-{day.month:02d}"


def tier_info(customer_id: str) -> dict[str, Any]:
    """The customer's member tier: ``tier`` (``none`` when unknown), ``tier_label``,
    ``authority_limit_cents`` and the tier's ``benefits`` mapping (``{}`` when it has none).
    No customer id in the result."""
    profile = orders.customer_profile(customer_id)
    config = policy.tier_config(profile["tier"])
    benefits = config.get("benefits")
    return {
        "tier": profile["tier"],
        "tier_label": profile["tier_label"],
        "authority_limit_cents": profile["authority_limit_cents"],
        "benefits": dict(benefits) if isinstance(benefits, Mapping) else {},
    }


def free_return_shipping_config(customer_id: str) -> dict[str, Any] | None:
    """The tier's valid ``free_return_shipping`` setting (``per_month`` whole number of at least 1,
    ``applies_to`` lists ``return_mail``, ``waives_fee`` true), or None when the tier has none."""
    return _perk(tier_info(customer_id))


def _perk(info: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = info["benefits"].get(FREE_RETURN_SHIPPING)
    if not isinstance(raw, Mapping):
        return None
    per_month, applies_to = raw.get("per_month"), raw.get("applies_to")
    if (
        isinstance(per_month, bool)
        or not isinstance(per_month, int)
        or per_month < 1
        or not isinstance(applies_to, list)
        or MAIL_RETURN not in applies_to
        or raw.get("waives_fee") is not True
    ):
        return None
    return {"per_month": per_month, "applies_to": list(applies_to), "waives_fee": True}


def ledger_usage(customer_id: str, now: date | datetime | str | None = None) -> dict[str, Any]:
    """How much of the monthly perk the customer has used: ``{"benefit", "tier", "period", "used",
    "allowed"}``. ``allowed`` is 0 for a tier without the perk. Consistent read of the ledger."""
    info = tier_info(customer_id)
    perk = _perk(info)
    period = period_for(now)
    item = store.get_item(store.benefit_key(customer_id, period))
    used = int(item.get("used", 0)) if item else 0
    return {
        "benefit": FREE_RETURN_SHIPPING,
        "tier": info["tier"],
        "period": period,
        "used": used,
        "allowed": perk["per_month"] if perk else 0,
    }


def free_return_shipping_available(customer_id: str, now: date | datetime | str | None = None) -> bool:
    """True when the customer's tier has the perk and this month's ledger still has room."""
    usage = ledger_usage(customer_id, now)
    return usage["used"] < usage["allowed"]


def benefit_note(
    customer_id: str, saved_cents: int, now: date | datetime | str | None = None
) -> dict[str, Any] | None:
    """The note an option carries when the perk would be applied to it, or None when it is not
    available (no perk, used up, or nothing to save). ``used`` is the count AFTER this use.

    ``{"type": "free_return_shipping", "label": "Platinum free return shipping", "saved_cents": 595,
    "used": 1, "allowed": 1, "period": "2026-10"}``
    """
    if isinstance(saved_cents, bool) or not isinstance(saved_cents, int) or saved_cents < 1:
        return None
    usage = ledger_usage(customer_id, now)
    if usage["used"] >= usage["allowed"]:
        return None
    return {
        "type": FREE_RETURN_SHIPPING,
        "label": f"{usage['tier'].capitalize()} free return shipping",
        "saved_cents": saved_cents,
        "used": usage["used"] + 1,
        "allowed": usage["allowed"],
        "period": usage["period"],
    }


def ledger_update(
    customer_id: str,
    period: str,
    per_month: int,
    *,
    transaction_id: str | None = None,
    label: str = "benefit",
) -> store.TxUpdate:
    """The TransactWriteItems operation that uses the perk once.

    Adds 1 to ``used`` under the condition ``attribute_not_exists(used) OR used < :per_month`` and
    creates the item on first use (``must_exist=False``), so two bundles can never both take the
    last free shipping of a month: the second one's transaction is cancelled with ``label``.
    """
    values: dict[str, Any] = {
        "customer_id": customer_id,
        "period": period,
        "benefit": FREE_RETURN_SHIPPING,
        "updated_at": store.now_iso(),
    }
    if transaction_id:
        values["last_transaction_id"] = transaction_id
    return store.TxUpdate(
        store.benefit_key(customer_id, period),
        set=values,
        add={"used": 1},
        condition=LEDGER_CONDITION,
        values={"per_month": per_month},
        label=label,
        must_exist=False,
    )
