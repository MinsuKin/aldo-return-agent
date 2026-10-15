"""Resolution options for an eligible policy check (requirement 6, design 5.3).

``find_options`` returns FACTS: which exchanges and returns are possible, with server-computed
amounts, ETAs, a deadline flag and the evidence behind each. Ranking stays the agent's job.
It requires an eligible policy check, never mutates inventory and persists the answer as
``CAND#{id}`` (with the inventory versions of the stock it offered) for ``quotes.create_quote``.

``eta_for`` is the pure date rule for one option type; the gateway reuses it so a pre-purchase
offer and a post-purchase exchange promise the same dates.

Stable option order and ids (``OPT-1`` ... after filtering): exchange from the DC, exchange
shipped from one store, exchange pickup at each home-region store (by location id), return in
store, return by mail.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from . import clock, fixtures, money, orders, policy, store
from .errors import DomainError, ErrorCode

PREFERRED_CHANNELS: tuple[str, ...] = ("ship", "store_pickup", "return")
OPTION_PROVENANCE = "synthetic"
DEFAULT_MAIL_REFUND_DAYS = 10

_TYPE_ALIASES = {
    "ship_from_dc": "exchange_ship_from_dc",
    "ship_from_store": "exchange_ship_from_store",
    "store_pickup": "exchange_store_pickup",
}


class Option(BaseModel):
    model_config = ConfigDict(extra="ignore")

    option_id: str
    type: str
    sku: str
    replacement_size: str | None = None
    location_id: str
    location_name: str
    qty: int
    refund_cents: int
    fee_cents: int
    currency: str = "CAD"
    eta_date: str
    eta_text: str
    meets_deadline: bool | None = None
    conditions: list[str]
    evidence: dict[str, Any]
    source_refs: list[str]
    provenance: str = OPTION_PROVENANCE


class CandidateSet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate_set_id: str
    policy_check_id: str
    options: list[Option]
    availability: dict[str, Any]
    other_sizes_in_stock: list[dict[str, Any]]
    inventory_versions: dict[str, int]
    deadline_date: str | None = None
    created_at: str
    preferred_channel: str | None = None


# ------------------------------------------------------------------------------ ETA rule


def normalize_option_type(option_type: str) -> str:
    """``store_pickup`` and ``exchange_store_pickup`` name the same option type."""
    resolved = _TYPE_ALIASES.get(option_type, option_type)
    if resolved not in policy.ALL_OPTION_TYPES:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"unknown option type {option_type!r}")
    return resolved


def eta_for(
    location: Mapping[str, Any],
    option_type: str,
    now: date | datetime | str | None = None,
    *,
    mail_refund_days: int = DEFAULT_MAIL_REFUND_DAYS,
) -> date:
    """Calendar date the option is met, counting calendar days from ``now`` (default DEMO_NOW).

    Exchange shipped from the DC or a store: ``now + ship_days`` of that location. Store pickup:
    ``now + pickup_ready_days``. Return in store: ``now``. Return by mail: ``now +
    mail_refund_days`` (the refund ETA after drop-off). Pure: no I/O. Accepts the gateway names
    (``ship_from_dc``, ``ship_from_store``, ``store_pickup``) as well as the ``exchange_`` ones.
    """
    kind = normalize_option_type(option_type)
    start = clock.demo_today() if now is None else clock.to_toronto_date(now)
    if kind in ("exchange_ship_from_dc", "exchange_ship_from_store"):
        return start + timedelta(days=_days(location, "ship_days"))
    if kind == "exchange_store_pickup":
        return start + timedelta(days=_days(location, "pickup_ready_days"))
    if kind == "return_in_store":
        return start
    return start + timedelta(days=_days({"mail_refund_days": mail_refund_days}, "mail_refund_days"))


def _days(source: Mapping[str, Any], name: str) -> int:
    value = source.get(name)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"location has no usable {name}")
    return value


# ----------------------------------------------------------------------- find_options


def find_options(
    customer_id: str,
    policy_check_id: str,
    replacement_size: str | None = None,
    deadline_date: str | None = None,
    preferred_channel: str | None = None,
    *,
    run_id: str | None = None,
) -> CandidateSet:
    """Options for an eligible policy check; persisted as ``CAND#{id}`` for the quote step."""
    check = orders.load_policy_check_item(customer_id, policy_check_id)  # ownership + audit
    result = check["result"]
    if result.get("decision") != "eligible":
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"policy check is not eligible (decision={result.get('decision')})",
        )
    allowed = [t for t in result.get("allowed_option_types", []) if t in policy.ALL_OPTION_TYPES]
    deadline = _parse_deadline(deadline_date)
    channel = _parse_channel(preferred_channel)
    qty = int(check["qty"])

    _, lines = orders.load_order_items(customer_id, check["order_id"], run_id=run_id)
    line = lines.get(check["line_id"])
    if line is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Order line not found on this order.")
    profile = store.get_item(store.customer_key(customer_id)) or {}
    home_region = profile.get("home_region")
    locations = _load_locations()
    logistics = fixtures.load_logistics()
    today = clock.demo_today()

    exchange_allowed = any(t in policy.EXCHANGE_OPTION_TYPES for t in allowed)
    chosen_size: str | None = None
    replacement_sku: str | None = None
    reason_no_exchange: str | None = None
    stock: dict[str, dict[str, Any]] = {}
    style_products: list[dict[str, Any]] = []

    if not exchange_allowed:
        reason_no_exchange = "NOT_FIT_REASON"
    else:
        chosen_size = _resolve_replacement_size(line["size"], check.get("size_direction"), replacement_size)
        style_products = _style_run(line["style"], line["color"])
        product = next((p for p in style_products if p["size"] == chosen_size), None)
        if product is None:
            reason_no_exchange = "NO_SUCH_SIZE"
        else:
            replacement_sku = product["sku"]
            stock = {row["location_id"]: row for row in store.query(f"INV#{replacement_sku}")}

    options: list[dict[str, Any]] = []
    inventory_versions: dict[str, int] = {}
    policy_ref = f"{policy.POLICY_ID}@{result.get('policy_version', policy.POLICY_VERSION)}"

    def atp_at(location_id: str) -> int:
        return int(stock.get(location_id, {}).get("atp", 0))

    if replacement_sku is not None:
        ship_fee = int(logistics["exchange_ship"]["fee_cents"])
        ship_rate = logistics["exchange_ship"]["rate_id"]
        pick_fee = int(logistics["exchange_pickup"]["fee_cents"])
        pick_rate = logistics["exchange_pickup"]["rate_id"]

        def exchange(kind: str, loc: Mapping[str, Any], fee: int, rate: str, conditions: list[str]) -> dict[str, Any]:
            row = stock[loc["location_id"]]
            eta = eta_for(loc, kind, today)
            inventory_versions[f"{replacement_sku}|{loc['location_id']}"] = int(row.get("version", 1))
            return _option(
                kind, replacement_sku, chosen_size, loc, qty, 0, fee, eta,
                _meets(eta, deadline), conditions,
                {"atp": atp_at(loc["location_id"]), "inventory_version": int(row.get("version", 1)),
                 "rate_id": rate, "lead_days": (eta - today).days},
                [f"INV#{replacement_sku}/LOC#{loc['location_id']}", rate, policy_ref],
            )

        dcs = [loc for loc in locations if loc.get("type") == "DC" and atp_at(loc["location_id"]) >= qty]
        if "exchange_ship_from_dc" in allowed and dcs:
            options.append(exchange("exchange_ship_from_dc", dcs[0], ship_fee, ship_rate,
                                    ["Send back the original pair, unworn"]))
        stores = [loc for loc in locations if loc.get("type") == "STORE" and atp_at(loc["location_id"]) >= qty]
        if "exchange_ship_from_store" in allowed and stores:
            nearest = min(
                stores,
                key=lambda loc: (_days(loc, "ship_days"), 0 if loc.get("region") == home_region else 1, loc["location_id"]),
            )
            options.append(exchange("exchange_ship_from_store", nearest, ship_fee, ship_rate,
                                    ["Send back the original pair, unworn"]))
        if "exchange_store_pickup" in allowed:
            for loc in stores:
                if loc.get("region") == home_region:
                    options.append(exchange("exchange_store_pickup", loc, pick_fee, pick_rate,
                                            ["Bring the original pair, unworn"]))
        if not options:
            reason_no_exchange = "OUT_OF_STOCK_EVERYWHERE"

    unit = int(line["unit_paid_cents"])
    gross = unit * qty
    line_ref = f"ORDER#{check['order_id']}/LINE#{check['line_id']}"
    all_stores = [loc for loc in locations if loc.get("type") == "STORE"]
    home_stores = [loc for loc in all_stores if loc.get("region") == home_region]
    drop_off = (home_stores or all_stores or [None])[0]
    if "return_in_store" in allowed and drop_off is not None:
        rate = logistics["in_store_return"]
        fee = int(rate["fee_cents"])
        options.append(_option(
            "return_in_store", line["sku"], None, drop_off, qty, max(gross - fee, 0), fee,
            eta_for(drop_off, "return_in_store", today), None,
            ["Bring the original pair, unworn", "The refund is processed at drop-off"],
            {"rate_id": rate["rate_id"], "unit_paid_cents": unit},
            [line_ref, rate["rate_id"], policy_ref],
        ))
    mail_hub = next((loc for loc in locations if loc.get("type") == "DC"), None)
    if "return_mail" in allowed and mail_hub is not None:
        rate = logistics["mail_return"]
        fee = int(rate["fee_cents"])
        days = int(rate["refund_eta_days_after_dropoff"])
        options.append(_option(
            "return_mail", line["sku"], None, mail_hub, qty, max(gross - fee, 0), fee,
            eta_for(mail_hub, "return_mail", today, mail_refund_days=days), None,
            [f"The return label fee of {money.format_cad(fee)} is deducted from the refund",
             f"The refund is expected about {days} days after drop-off"],
            {"rate_id": rate["rate_id"], "unit_paid_cents": unit, "refund_eta_days_after_dropoff": days},
            [line_ref, rate["rate_id"], policy_ref],
        ))

    for index, option in enumerate(options, start=1):
        option["option_id"] = f"OPT-{index}"

    candidate_set_id = store.new_id("CAND-", 8)
    created_at = store.now_iso()
    availability = {
        "replacement_size": chosen_size,
        "replacement_sku": replacement_sku,
        "atp_by_location": {loc_id: int(row.get("atp", 0)) for loc_id, row in sorted(stock.items())},
        "dc_atp": sum(int(row.get("atp", 0)) for loc_id, row in stock.items()
                      if any(loc["location_id"] == loc_id and loc.get("type") == "DC" for loc in locations)),
        "reason_no_exchange": reason_no_exchange,
    }
    other_sizes = _other_sizes(style_products, locations, {line["size"], chosen_size}) if style_products else []

    pk, sk = store.cand_key(candidate_set_id)
    store.put_item(
        {
            "pk": pk,
            "sk": sk,
            "candidate_set_id": candidate_set_id,
            "customer_id": customer_id,
            "policy_check_id": policy_check_id,
            "order_id": check["order_id"],
            "line_id": check["line_id"],
            "qty": qty,
            "run_id": run_id,
            "options": options,
            "availability": availability,
            "other_sizes_in_stock": other_sizes,
            "inventory_versions": inventory_versions,
            "deadline_date": deadline,
            "preferred_channel": channel,
            "created_at": created_at,
        },
        condition="attribute_not_exists(#pk)",
    )
    return CandidateSet(
        candidate_set_id=candidate_set_id,
        policy_check_id=policy_check_id,
        options=[Option(**o) for o in options],
        availability=availability,
        other_sizes_in_stock=other_sizes,
        inventory_versions=inventory_versions,
        deadline_date=deadline,
        created_at=created_at,
        preferred_channel=channel,
    )


# ------------------------------------------------------------------------------ helpers


def _option(
    kind: str,
    sku: str,
    replacement_size: str | None,
    location: Mapping[str, Any],
    qty: int,
    refund_cents: int,
    fee_cents: int,
    eta: date,
    meets_deadline: bool | None,
    conditions: list[str],
    evidence: dict[str, Any],
    source_refs: list[str],
) -> dict[str, Any]:
    return {
        "option_id": "",  # assigned once the list is final
        "type": kind,
        "sku": sku,
        "replacement_size": replacement_size,
        "location_id": location["location_id"],
        "location_name": location["name"],
        "qty": qty,
        "refund_cents": refund_cents,
        "fee_cents": fee_cents,
        "currency": money.CURRENCY,
        "eta_date": eta.isoformat(),
        "eta_text": clock.format_day(eta),
        "meets_deadline": meets_deadline,
        "conditions": conditions,
        "evidence": evidence,
        "source_refs": source_refs,
        "provenance": OPTION_PROVENANCE,
    }


def _meets(eta: date, deadline: str | None) -> bool | None:
    return None if deadline is None else eta <= date.fromisoformat(deadline)


def _parse_deadline(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "deadline_date must be YYYY-MM-DD")
    text = value.strip()
    if len(text) != 10:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "deadline_date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "deadline_date must be YYYY-MM-DD") from None


def _parse_channel(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in PREFERRED_CHANNELS:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"preferred_channel must be one of: {', '.join(PREFERRED_CHANNELS)}"
        )
    return value


def _resolve_replacement_size(current: str, direction: str | None, requested: Any) -> str:
    """The size to offer: the requested one, else current +/- 0.5 from the stored direction."""
    current_size = store.size_str(current)
    if requested is not None and not (isinstance(requested, str) and not requested.strip()):
        chosen = store.size_str(requested)
    elif direction == "too_small":
        chosen = store.size_str(Decimal(current_size) + Decimal("0.5"))
    elif direction == "too_big":
        chosen = store.size_str(Decimal(current_size) - Decimal("0.5"))
    else:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "replacement_size is required when the size problem is not known to be too small or too big",
        )
    if chosen == current_size:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "replacement_size must differ from the current size")
    return chosen


def _load_locations() -> list[dict[str, Any]]:
    rows = store.batch_get([store.location_key(loc_id) for loc_id in fixtures.location_ids()])
    return sorted(rows, key=lambda loc: loc["location_id"])


def _style_run(style: str, color: str) -> list[dict[str, Any]]:
    """Every size of one style and colour, smallest first (GSI1)."""
    return store.query(store.gsi1_product(style, color, "7.0")[0], index="GSI1")


def _other_sizes(
    products: list[dict[str, Any]], locations: list[dict[str, Any]], excluded: set[str | None]
) -> list[dict[str, Any]]:
    """Same style and colour sizes with stock somewhere, excluding the current and chosen size."""
    candidates = [p for p in products if p["size"] not in excluded]
    if not candidates:
        return []
    keys = [store.inventory_key(p["sku"], loc["location_id"]) for p in candidates for loc in locations]
    totals: dict[str, int] = {}
    for row in store.batch_get(keys):
        totals[row["sku"]] = totals.get(row["sku"], 0) + int(row.get("atp", 0))
    found = [
        {"size": p["size"], "sku": p["sku"], "total_atp": totals.get(p["sku"], 0)}
        for p in candidates
        if totals.get(p["sku"], 0) >= 1
    ]
    return sorted(found, key=lambda entry: Decimal(entry["size"]))
