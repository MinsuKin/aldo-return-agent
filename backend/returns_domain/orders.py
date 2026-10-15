"""Ownership-checked order reads and the persisted policy check (requirements 1.4, 3, 4).

Every read goes through the customer id of the verified token (or the trusted run context):
an order that exists but belongs to someone else raises NOT_AUTHORIZED and writes an
``access_denied`` audit event. Reads of the order partition are consistent. Policy inputs
(price, dates, flags) come from the store only, never from a tool argument, and the result is
persisted as ``PCHK#{id}`` whatever the decision, because ``options.find_options`` requires it.

Orders placed by the agent-to-agent purchase (id ``DEMO-A2A-XXXXXX``, channel ``agent``,
``delivered_at`` null until the delivery is simulated) have the same shape as fixture orders,
so they work with every function here; the policy treats the missing date as ``needs_review``.

``customer_profile`` is the one place that turns a customer's ``CUST#`` record into the member
tier (label and authority limit from ``policy.json`` ``tiers``) and the minutes to each store;
``check_policy`` passes the tier's authority limit to the policy.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, model_serializer

from . import audit, clock, policy, store, taxonomy
from .errors import DomainError, ErrorCode


class OrderLineView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line_id: str
    sku: str
    product_name: str
    style: str
    size: str
    color: str
    purchased_qty: int
    returned_or_reserved_qty: int
    unit_paid_cents: int
    final_sale: bool | None = None
    currency: str = "CAD"
    source: str = "synthetic"
    #: What the shoe is ("boot", "sneaker", "loafer" ...) when the fixture says so; left out of the
    #: JSON otherwise. Lets the agent resolve "the tan boot" when the style names do not say it.
    category: str | None = None

    @model_serializer(mode="wrap")
    def _omit_category_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("category") is None:
            data.pop("category", None)
        return data


class OrderView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: str
    banner: str
    currency: str
    channel: str = "web"
    ordered_at: str
    delivered_at: str | None = None
    lines: list[OrderLineView]
    source: str = "synthetic"
    fulfillment: dict[str, Any] | None = None  # agent-placed orders: where and when to pick up


class PolicyCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")

    policy_check_id: str
    order_id: str
    line_id: str
    qty: int
    reason_category: str
    condition: str
    size_direction: str
    refund_cents: int
    currency: str = "CAD"
    result: dict[str, Any]
    policy_version: str
    created_at: str


# ---------------------------------------------------------------------------- reads


def load_order_items(
    customer_id: str, order_id: str, *, run_id: str | None = None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Raw ``(order META item, {line_id: line item})`` after the ownership check.

    ORDER_NOT_FOUND when the order does not exist; NOT_AUTHORIZED plus an ``access_denied``
    audit event when it belongs to another customer. Consistent read of the whole partition.
    """
    items = store.query(store.order_key(order_id)[0])
    meta = next((item for item in items if item["sk"] == "META"), None)
    if meta is None:
        raise DomainError(ErrorCode.ORDER_NOT_FOUND, "Order not found.")
    if meta.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "order", order_id, run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Order not found for this customer.")
    lines = {
        item["sk"][len("LINE#") :]: item for item in items if item["sk"].startswith("LINE#")
    }
    return meta, lines


def _view(meta: Mapping[str, Any], lines: Mapping[str, Mapping[str, Any]]) -> OrderView:
    return OrderView(
        **{k: v for k, v in meta.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk", "customer_id")},
        lines=[OrderLineView(**{k: v for k, v in lines[lid].items()}) for lid in sorted(lines)],
    )


def list_orders(customer_id: str) -> list[OrderView]:
    """The customer's orders, newest first (GSI1 for the list, consistent reads for the content)."""
    listed = store.query(store.customer_key(customer_id)[0], index="GSI1", forward=False)
    views: list[OrderView] = []
    for entry in listed:
        order_id = entry.get("order_id")
        if not order_id or entry.get("customer_id") != customer_id:
            continue
        try:
            meta, lines = load_order_items(customer_id, order_id)
        except DomainError:
            continue  # deleted by a reset between the index read and the base read
        views.append(_view(meta, lines))
    return views


def get_order(customer_id: str, order_id: str, *, run_id: str | None = None) -> OrderView:
    meta, lines = load_order_items(customer_id, order_id, run_id=run_id)
    return _view(meta, lines)


def order_context(customer_id: str, order_id: str, *, run_id: str | None = None) -> dict[str, Any]:
    """Tool payload for ``get_order_context``. No names, e-mail addresses or customer ids."""
    order = get_order(customer_id, order_id, run_id=run_id)
    lines = []
    for line in order.lines:
        remaining = line.purchased_qty - line.returned_or_reserved_qty
        entry = {
            "line_id": line.line_id,
            "sku": line.sku,
            "product_name": line.product_name,
            "size": line.size,
            "color": line.color,
            "purchased_qty": line.purchased_qty,
            "returned_or_reserved_qty": line.returned_or_reserved_qty,
            "already_returned_qty": line.returned_or_reserved_qty,
            "remaining_qty": max(remaining, 0),
            "unit_paid_cents": line.unit_paid_cents,
            "final_sale": line.final_sale,
        }
        if line.category:  # additive: only lines whose fixture names what the shoe is
            entry["category"] = line.category
        lines.append(entry)
    return store.json_safe(
        {
            "order_id": order.order_id,
            "delivered_at": order.delivered_at,
            "ordered_at": order.ordered_at,
            "currency": order.currency,
            "lines": lines,
            "source_refs": [f"ORDER#{order.order_id}/META"]
            + [f"ORDER#{order.order_id}/LINE#{line.line_id}" for line in order.lines],
        }
    )


# ------------------------------------------------------------------------------ profile


def customer_profile(customer_id: str) -> dict[str, Any]:
    """Member tier and store travel times of one customer, read from their own ``CUST#`` record.

    Returns ``display_name``, ``home_region``, ``tier`` (``none`` when the record has none or an
    unknown one), ``tier_label`` and ``authority_limit_cents`` (from ``policy.json`` ``tiers``,
    constant fallback) and ``travel_minutes`` (``{store location id: minutes}``, ``{}`` when the
    record has none). A customer without a record is the plain ``none`` member with no travel
    times. The result holds no customer id, so it is safe to hand to a tool or an event.
    """
    item = store.get_item(store.customer_key(customer_id)) or {}
    tier = policy.tier_config(item.get("tier"))
    display_name, home_region = item.get("display_name"), item.get("home_region")
    return {
        "display_name": display_name if isinstance(display_name, str) and display_name.strip() else None,
        "home_region": home_region if isinstance(home_region, str) and home_region.strip() else None,
        "tier": tier["tier"],
        "tier_label": tier["label"],
        "authority_limit_cents": tier["authority_refund_limit_cents"],
        "travel_minutes": _travel_minutes(item.get("travel_minutes")),
    }


def _travel_minutes(raw: object) -> dict[str, int]:
    """Whole, non-negative minutes per store location id; anything else is dropped (unknown)."""
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(location): minutes
        for location, minutes in raw.items()
        if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 0
    }


# ------------------------------------------------------------------------------ policy


def check_policy(
    customer_id: str,
    run_id: str | None,
    order_id: str,
    line_id: str,
    qty: int,
    reason_category: str,
    condition: str,
    size_direction: str = "unknown",
) -> PolicyCheck:
    """Evaluate the demo policy for ``qty`` units of a line and persist the result as ``PCHK#``.

    Price, delivery date and the final-sale flag are read from the store; the caller supplies
    only closed vocabularies and a quantity. ``size_direction`` is stored for the options
    layer (it decides the default replacement size).
    """
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "qty must be a whole number of at least 1")
    reason = taxonomy.parse_reason_category(reason_category)
    item_condition = taxonomy.parse_condition(condition)
    direction = taxonomy.parse_size_direction(size_direction)

    meta, lines = load_order_items(customer_id, order_id, run_id=run_id)
    line = lines.get(line_id)
    if line is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Order line not found on this order.")
    refund_cents = policy.gross_refund_cents(line, qty)
    result = policy.evaluate(
        meta,
        line,
        qty,
        reason,
        item_condition,
        refund_cents,
        now=clock.demo_now(),
        authority_limit_cents=customer_profile(customer_id)["authority_limit_cents"],
    )

    policy_check_id = store.new_id("PCHK-", 8)
    created_at = store.now_iso()
    pk, sk = store.pchk_key(policy_check_id)
    item: dict[str, Any] = {
        "pk": pk,
        "sk": sk,
        "policy_check_id": policy_check_id,
        "customer_id": customer_id,
        "run_id": run_id,
        "order_id": order_id,
        "line_id": line_id,
        "qty": qty,
        "reason_category": reason.value,
        "condition": item_condition.value,
        "size_direction": direction.value,
        "refund_cents": refund_cents,
        "currency": "CAD",
        "result": result.to_dict(),
        "policy_version": result.policy_version,
        "created_at": created_at,
    }
    if run_id:  # lets ``cases.escalate`` find the latest check of a run without a scan
        item["gsi1pk"], item["gsi1sk"] = store.gsi1_pchk(run_id, created_at, policy_check_id)
    store.put_item(item, condition="attribute_not_exists(#pk)")
    return _check_view(item)


def _check_view(item: Mapping[str, Any]) -> PolicyCheck:
    return PolicyCheck(**{k: v for k, v in item.items() if k not in ("pk", "sk", "gsi1pk", "gsi1sk")})


def load_policy_check_item(customer_id: str, policy_check_id: str) -> dict[str, Any]:
    """Raw ``PCHK#`` item after the ownership check (MISSING_POLICY when absent)."""
    item = store.get_item(store.pchk_key(policy_check_id))
    if item is None:
        raise DomainError(ErrorCode.MISSING_POLICY, "No policy check with this id. Evaluate the policy first.")
    if item.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "policy_check", policy_check_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Policy check not found for this customer.")
    return item


def get_policy_check(customer_id: str, policy_check_id: str) -> PolicyCheck:
    return _check_view(load_policy_check_item(customer_id, policy_check_id))


def latest_policy_check(
    customer_id: str, run_id: str, order_id: str, line_id: str
) -> PolicyCheck | None:
    """Newest policy check of ``run_id`` for this order line (index read), or None."""
    pk = store.gsi1_pchk(run_id, "x", "x")[0]
    for item in store.query(pk, index="GSI1", forward=False):
        if (
            item.get("customer_id") == customer_id
            and item.get("order_id") == order_id
            and item.get("line_id") == line_id
        ):
            return _check_view(item)
    return None
