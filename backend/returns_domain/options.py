"""Resolution options for an eligible policy check (requirement 6, design 5.3).

``find_options`` returns FACTS: which exchanges and returns are possible, with server-computed
amounts, ETAs, a deadline flag and the evidence behind each. Ranking stays the agent's job.
It requires an eligible policy check, never mutates inventory and persists the answer as
``CAND#{id}`` (with the inventory versions of the stock it offered) for ``quotes.create_quote``.

``eta_for`` is the pure date rule for one option type; the gateway reuses it so a pre-purchase
offer and a post-purchase exchange promise the same dates.

Stable option order and ids (``OPT-1`` ... after filtering): exchange from the DC, exchange
shipped from one store, exchange pickup at each home-region store (nearest first, then by
location id), return in store, return by mail.

Two kinds of exchange (PIVOT_ADDENDUM D5, 11.4):

* a FIT problem (``FIT_SIZE``) exchanges for another SIZE of the same colour, as always;
* a PREFERENCE (``EXPECTATION``, ``CHANGE_OF_MIND``) exchanges for another COLOUR of the same
  style in the SAME size. Without ``replacement_color`` there are no exchange options yet and
  ``needs_choice`` lists the colours in stock in that size so the agent can ask; with a colour
  the exchange options carry ``replacement_sku`` and ``replacement_color``.

Every option at a store carries ``minutes_away`` (the customer's ``travel_minutes`` to that store;
the field is absent when unknown) and the store options are ordered nearest first.

A BUNDLE return (``find_options(policy_check_ids=[...])``, PIVOT_ADDENDUM D9) covers up to ten lines
of one order in one shipment: return options only, ONE shipping fee, per-line refunds in ``lines``,
the member's free return shipping on ``return_mail`` while this month's ledger (``benefits``) has
room, and the tier authority limit checked on the TOTAL (``decision`` ``needs_review`` above it).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, model_serializer

from . import benefits, clock, fixtures, money, orders, policy, store
from .errors import DomainError, ErrorCode

PREFERRED_CHANNELS: tuple[str, ...] = ("ship", "store_pickup", "return")
OPTION_PROVENANCE = "synthetic"
DEFAULT_MAIL_REFUND_DAYS = 10
MAX_COLOR_CHARS = 40
#: Sort key of a store whose distance is unknown: after every store with a known distance.
UNKNOWN_MINUTES = 10**9

_TYPE_ALIASES = {
    "ship_from_dc": "exchange_ship_from_dc",
    "ship_from_store": "exchange_ship_from_store",
    "store_pickup": "exchange_store_pickup",
}

#: Optional option fields that are LEFT OUT of the JSON when unknown (never ``null``), so a fit-size
#: option looks exactly as it always did.
_OMIT_WHEN_NONE: tuple[str, ...] = (
    "replacement_sku",
    "replacement_color",
    "minutes_away",
    "location_address",
    # bundle returns only (domain v2 phase 2)
    "lines",
    "refund_total_cents",
    "net_refund_cents",
    "benefit",
    "decision",
    "rule_ids",
    "authority_limit_cents",
)

#: A bundle return takes up to this many lines of ONE order in one shipment (PIVOT_ADDENDUM D9).
MAX_BUNDLE_LINES = 10
#: A bundle can be dropped off in store only when the store is at most this many minutes away.
BUNDLE_STORE_MAX_MINUTES = 120
BUNDLE_AUTHORITY_RULE = "authority.refund_limit"


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
    # Domain v2: colour exchange (replacement_sku equals sku) and the customer's travel time.
    replacement_sku: str | None = None
    replacement_color: str | None = None
    minutes_away: int | None = None
    # Street address of the store (shown on the card); absent when the location record has none.
    location_address: str | None = None
    # Bundle return (several lines of one order, one shipment). ``sku`` is the first line's sku,
    # ``qty`` the total quantity and ``refund_cents`` the NET refund (``net_refund_cents``);
    # ``lines`` is the authoritative per-line list. ``decision`` is the tier authority check on the
    # TOTAL: ``needs_review`` means a person must decide, the agent cannot grant it.
    lines: list[dict[str, Any]] | None = None
    refund_total_cents: int | None = None
    net_refund_cents: int | None = None
    benefit: dict[str, Any] | None = None
    decision: str | None = None
    rule_ids: list[str] | None = None
    authority_limit_cents: int | None = None

    @model_serializer(mode="wrap")
    def _omit_unknown_optionals(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in _OMIT_WHEN_NONE:
            if data.get(name) is None:
                data.pop(name, None)
        return data


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
    #: ``{"replacement_color": [{"color", "total_atp", "nearest_store_minutes"}]}`` when a
    #: preference exchange still needs the colour; left out of the JSON otherwise.
    needs_choice: dict[str, Any] | None = None
    #: Bundle returns only: every policy check id (``policy_check_id`` is the first) and a summary
    #: ``{"order_id", "n_lines", "qty", "refund_total_cents", "authority_limit_cents", "tier", "decision"}``.
    policy_check_ids: list[str] | None = None
    bundle: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_optionals(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in ("needs_choice", "policy_check_ids", "bundle"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


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
    policy_check_id: str | None = None,
    replacement_size: str | None = None,
    deadline_date: str | None = None,
    preferred_channel: str | None = None,
    replacement_color: str | None = None,
    *,
    run_id: str | None = None,
    policy_check_ids: list[str] | None = None,
) -> CandidateSet:
    """Options for an eligible policy check; persisted as ``CAND#{id}`` for the quote step.

    ``replacement_color`` only applies to a PREFERENCE reason (see the module note): omitted, the
    result has no exchange options and ``needs_choice`` lists the colours the agent can offer;
    given, the exchange options are for that colour in the current size. For a fit problem it
    must be omitted (or be the current colour), and ``replacement_size`` must be omitted (or the
    current size) for a colour exchange.

    Exactly one of ``policy_check_id`` and ``policy_check_ids`` is given. ``policy_check_ids``
    (one check per line of ONE order, 1 to ``MAX_BUNDLE_LINES`` lines) asks for a BUNDLE return:
    one shipment, one fee, return options only (see ``_find_bundle_options``).
    """
    if policy_check_ids is not None:
        if policy_check_id is not None:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR, "give either policy_check_id or policy_check_ids, not both"
            )
        return _find_bundle_options(
            customer_id, policy_check_ids, replacement_size, deadline_date, preferred_channel, replacement_color, run_id
        )
    if policy_check_id is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "policy_check_id (or policy_check_ids) is required")
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
    color = _parse_color(replacement_color)
    qty = int(check["qty"])

    _, lines = orders.load_order_items(customer_id, check["order_id"], run_id=run_id)
    line = lines.get(check["line_id"])
    if line is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Order line not found on this order.")
    profile = orders.customer_profile(customer_id)
    home_region = profile["home_region"]
    travel = profile["travel_minutes"]
    locations = _load_locations()
    logistics = fixtures.load_logistics()
    today = clock.demo_today()

    exchange_allowed = any(t in policy.EXCHANGE_OPTION_TYPES for t in allowed)
    preference = exchange_allowed and not policy.is_fit_reason(check.get("reason_category"))
    if color is not None and not preference and not _same_color(color, line["color"]):
        preference_reasons = [r for r in policy.exchange_reasons() if not policy.is_fit_reason(r)]
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "replacement_color only applies to a change of colour after "
            + (" or ".join(preference_reasons) if preference_reasons else "a preference reason")
            + "; leave it out for this reason",
        )
    chosen_size: str | None = None
    chosen_color: str | None = None
    replacement_sku: str | None = None
    reason_no_exchange: str | None = None
    needs_choice: dict[str, Any] | None = None
    stock: dict[str, dict[str, Any]] = {}
    style_products: list[dict[str, Any]] = []

    if not exchange_allowed:
        reason_no_exchange = "NOT_FIT_REASON"
    elif preference:
        # A colour exchange keeps the style and the size; only the colour changes.
        chosen_size = store.size_str(line["size"])
        if replacement_size is not None and not (isinstance(replacement_size, str) and not replacement_size.strip()):
            if store.size_str(replacement_size) != chosen_size:
                raise DomainError(
                    ErrorCode.VALIDATION_ERROR,
                    "a colour exchange keeps the current size; leave replacement_size out",
                )
        if color is not None and _same_color(color, line["color"]):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "replacement_color must differ from the current colour")
        catalog = _style_catalog(line["style"])
        matched = None if color is None else next((p["color"] for p in catalog if _same_color(p["color"], color)), None)
        if matched is not None:
            chosen_color = matched
            product = next((p for p in catalog if p["color"] == matched and p["size"] == chosen_size), None)
            if product is None:
                reason_no_exchange = "NO_SUCH_SIZE"
            else:
                replacement_sku = product["sku"]
                stock = {row["location_id"]: row for row in store.query(f"INV#{replacement_sku}")}
        else:
            choices = _color_choices(catalog, line["color"], chosen_size, qty, locations, home_region, travel)
            needs_choice = {"replacement_color": choices}
            if color is not None:
                reason_no_exchange = "NO_SUCH_COLOR"
            elif choices:
                reason_no_exchange = "NEEDS_COLOR_CHOICE"
            elif all(_same_color(p["color"], line["color"]) for p in catalog):
                reason_no_exchange = "NO_OTHER_COLOR"  # the style only comes in the colour the customer owns
            else:
                reason_no_exchange = "OUT_OF_STOCK_EVERYWHERE"  # other colours exist, none is in stock in this size
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
                replacement_color=chosen_color, minutes_away=_minutes_away(loc, travel),
            )

        dcs = [loc for loc in locations if loc.get("type") == "DC" and atp_at(loc["location_id"]) >= qty]
        if "exchange_ship_from_dc" in allowed and dcs:
            options.append(exchange("exchange_ship_from_dc", dcs[0], ship_fee, ship_rate,
                                    ["Send back the original pair, unworn"]))
        stores = [loc for loc in locations if loc.get("type") == "STORE" and atp_at(loc["location_id"]) >= qty]
        if "exchange_ship_from_store" in allowed and stores:
            nearest = min(
                stores,
                key=lambda loc: (
                    _days(loc, "ship_days"),
                    0 if loc.get("region") == home_region else 1,
                    *_proximity(loc, travel),
                ),
            )
            options.append(exchange("exchange_ship_from_store", nearest, ship_fee, ship_rate,
                                    ["Send back the original pair, unworn"]))
        if "exchange_store_pickup" in allowed:
            for loc in sorted((s for s in stores if s.get("region") == home_region), key=lambda s: _proximity(s, travel)):
                options.append(exchange("exchange_store_pickup", loc, pick_fee, pick_rate,
                                        ["Bring the original pair, unworn"]))
        if not options:
            reason_no_exchange = "OUT_OF_STOCK_EVERYWHERE"

    unit = int(line["unit_paid_cents"])
    gross = unit * qty
    line_ref = f"ORDER#{check['order_id']}/LINE#{check['line_id']}"
    all_stores = [loc for loc in locations if loc.get("type") == "STORE"]
    home_stores = [loc for loc in all_stores if loc.get("region") == home_region]
    drop_pool = home_stores or all_stores
    drop_off = min(drop_pool, key=lambda s: _proximity(s, travel)) if drop_pool else None
    if "return_in_store" in allowed and drop_off is not None:
        rate = logistics["in_store_return"]
        fee = int(rate["fee_cents"])
        options.append(_option(
            "return_in_store", line["sku"], None, drop_off, qty, max(gross - fee, 0), fee,
            eta_for(drop_off, "return_in_store", today), None,
            ["Bring the original pair, unworn", "The refund is processed at drop-off"],
            {"rate_id": rate["rate_id"], "unit_paid_cents": unit},
            [line_ref, rate["rate_id"], policy_ref],
            minutes_away=_minutes_away(drop_off, travel),
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
    if preference:
        availability["replacement_color"] = chosen_color
    other_sizes = _other_sizes(style_products, locations, {line["size"], chosen_size}) if style_products else []

    pk, sk = store.cand_key(candidate_set_id)
    item: dict[str, Any] = {
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
    }
    if needs_choice is not None:
        item["needs_choice"] = needs_choice
    store.put_item(item, condition="attribute_not_exists(#pk)")
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
        needs_choice=needs_choice,
    )


# ------------------------------------------------------------------------ bundle returns


def _find_bundle_options(
    customer_id: str,
    policy_check_ids: Any,
    replacement_size: str | None,
    deadline_date: str | None,
    preferred_channel: str | None,
    replacement_color: str | None,
    run_id: str | None,
) -> CandidateSet:
    """Return options for SEVERAL eligible lines of one order shipped or dropped off together.

    Preconditions, each a typed error that writes nothing: 1 to ``MAX_BUNDLE_LINES`` distinct
    policy checks; all belong to this customer (NOT_AUTHORIZED otherwise), to the same order, all
    ``eligible`` and for distinct lines whose quantity is still left to return; no
    ``replacement_size`` or ``replacement_color`` (a bundle is never an exchange).

    Options (only return types, ids ``OPT-1`` ... in this order): ``return_in_store`` at the
    nearest store at most ``BUNDLE_STORE_MAX_MINUTES`` away (only when there is one), ``return_mail``.
    Each carries ``lines`` (per-line refund), ONE shipping fee for the shipment, ``refund_total_cents``
    and ``net_refund_cents`` (``refund_cents`` equals it, as for a single line). The member's
    free return shipping is applied to ``return_mail`` (fee 0, a ``benefit`` note) while the month's
    ledger has room. The tier authority limit is checked on the TOTAL: above it every option has
    ``decision: needs_review`` and ``create_quote`` refuses it (a person decides); below, ``eligible``.
    """
    check_ids = _clean_check_ids(policy_check_ids)
    if not _blank(replacement_size) or not _blank(replacement_color):
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, "a bundle return has no exchange; leave replacement_size and replacement_color out"
        )
    deadline = _parse_deadline(deadline_date)
    channel = _parse_channel(preferred_channel)

    checks = [orders.load_policy_check_item(customer_id, check_id) for check_id in check_ids]  # ownership + audit
    order_ids = {check["order_id"] for check in checks}
    if len(order_ids) != 1:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "all lines of a bundle must belong to the same order")
    order_id = checks[0]["order_id"]
    line_ids = [check["line_id"] for check in checks]
    if len(set(line_ids)) != len(line_ids):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "a line can only be in a bundle once")
    for check in checks:
        decision = check["result"].get("decision")
        if decision != "eligible":
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"policy check for line {check['line_id']} is not eligible (decision={decision})",
            )

    _, order_lines = orders.load_order_items(customer_id, order_id, run_id=run_id)
    entries: list[dict[str, Any]] = []
    for check in checks:
        line = order_lines.get(check["line_id"])
        if line is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Order line not found on this order.")
        qty = int(check["qty"])
        if qty > int(line["purchased_qty"]) - int(line.get("returned_or_reserved_qty", 0)):
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"line {check['line_id']} was already returned or reserved; leave it out of the bundle",
            )
        entries.append(_bundle_line(check, line, qty))

    profile = orders.customer_profile(customer_id)
    travel = profile["travel_minutes"]
    limit = int(profile["authority_limit_cents"])
    total = sum(entry["refund_cents"] for entry in entries)
    total_qty = sum(entry["qty"] for entry in entries)
    needs_review = total > limit
    bundle_decision = "needs_review" if needs_review else "eligible"
    allowed = set.intersection(*(set(check["result"].get("allowed_option_types", [])) for check in checks))
    locations = _load_locations()
    logistics = fixtures.load_logistics()
    today = clock.demo_today()
    policy_version = checks[0]["result"].get("policy_version", policy.POLICY_VERSION)
    policy_ref = f"{policy.POLICY_ID}@{policy_version}"
    line_refs = [f"ORDER#{order_id}/LINE#{line_id}" for line_id in line_ids]
    review_note = (
        [f"A person must review this return: the total {money.format_cad(total)} is above the "
         f"{money.format_cad(limit)} limit the agent can approve"]
        if needs_review
        else []
    )

    def bundle_option(kind: str, location: Mapping[str, Any], fee: int, eta: date, conditions: list[str],
                      evidence: dict[str, Any], refs: list[str], benefit: dict[str, Any] | None = None,
                      minutes_away: int | None = None) -> dict[str, Any]:
        option = _option(
            kind, entries[0]["sku"], None, location, total_qty, max(total - fee, 0), fee, eta, None,
            conditions + review_note,
            {**evidence, "n_lines": len(entries), "refund_total_cents": total, "authority_limit_cents": limit,
             "tier": profile["tier"]},
            line_refs + refs + [policy_ref],
            minutes_away=minutes_away,
        )
        option.update(
            {
                "lines": [dict(entry) for entry in entries],
                "refund_total_cents": total,
                "net_refund_cents": max(total - fee, 0),
                "decision": bundle_decision,
                "rule_ids": [BUNDLE_AUTHORITY_RULE] if needs_review else [],
                "authority_limit_cents": limit,
            }
        )
        if benefit is not None:
            option["benefit"] = benefit
        return option

    options: list[dict[str, Any]] = []
    near = sorted(
        (
            loc
            for loc in locations
            if loc.get("type") == "STORE" and travel.get(loc["location_id"], BUNDLE_STORE_MAX_MINUTES + 1) <= BUNDLE_STORE_MAX_MINUTES
        ),
        key=lambda loc: _proximity(loc, travel),
    )
    if "return_in_store" in allowed and near:
        rate = logistics["in_store_return"]
        options.append(bundle_option(
            "return_in_store", near[0], int(rate["fee_cents"]), eta_for(near[0], "return_in_store", today),
            ["Bring all the original pairs, unworn", "The refund is processed at drop-off"],
            {"rate_id": rate["rate_id"]}, [rate["rate_id"]], minutes_away=_minutes_away(near[0], travel),
        ))
    mail_hub = next((loc for loc in locations if loc.get("type") == "DC"), None)
    if "return_mail" in allowed and mail_hub is not None:
        rate = logistics["mail_return"]
        standard_fee = int(rate["fee_cents"])
        days = int(rate["refund_eta_days_after_dropoff"])
        note = None if needs_review else benefits.benefit_note(customer_id, standard_fee)
        fee = 0 if note else standard_fee
        fee_text = (
            f"{note['label']}: no return label fee ({note['used']} of {note['allowed']} this month, "
            f"you save {money.format_cad(note['saved_cents'])})"
            if note
            else f"One return label fee of {money.format_cad(fee)} for the whole shipment is deducted from the refund"
        )
        evidence = {"rate_id": rate["rate_id"], "refund_eta_days_after_dropoff": days}
        if note:
            evidence[benefits.FREE_RETURN_SHIPPING] = "applied"
        options.append(bundle_option(
            "return_mail", mail_hub, fee, eta_for(mail_hub, "return_mail", today, mail_refund_days=days),
            [fee_text, f"The refund is expected about {days} days after drop-off"],
            evidence, [rate["rate_id"]], benefit=note,
        ))
    for index, option in enumerate(options, start=1):
        option["option_id"] = f"OPT-{index}"

    candidate_set_id = store.new_id("CAND-", 8)
    created_at = store.now_iso()
    availability = {
        "replacement_size": None,
        "replacement_sku": None,
        "atp_by_location": {},
        "dc_atp": 0,
        "reason_no_exchange": "BUNDLE_RETURN_ONLY",
    }
    bundle = {
        "order_id": order_id,
        "n_lines": len(entries),
        "qty": total_qty,
        "refund_total_cents": total,
        "authority_limit_cents": limit,
        "tier": profile["tier"],
        "decision": bundle_decision,
    }
    pk, sk = store.cand_key(candidate_set_id)
    item: dict[str, Any] = {
        "pk": pk,
        "sk": sk,
        "candidate_set_id": candidate_set_id,
        "customer_id": customer_id,
        "policy_check_id": check_ids[0],
        "policy_check_ids": check_ids,
        "bundle": bundle,
        "order_id": order_id,
        "line_id": line_ids[0],
        "qty": total_qty,
        "run_id": run_id,
        "options": options,
        "availability": availability,
        "other_sizes_in_stock": [],
        "inventory_versions": {},
        "deadline_date": deadline,
        "preferred_channel": channel,
        "created_at": created_at,
    }
    store.put_item(item, condition="attribute_not_exists(#pk)")
    return CandidateSet(
        candidate_set_id=candidate_set_id,
        policy_check_id=check_ids[0],
        policy_check_ids=check_ids,
        bundle=bundle,
        options=[Option(**o) for o in options],
        availability=availability,
        other_sizes_in_stock=[],
        inventory_versions={},
        deadline_date=deadline,
        created_at=created_at,
        preferred_channel=channel,
    )


def _bundle_line(check: Mapping[str, Any], line: Mapping[str, Any], qty: int) -> dict[str, Any]:
    """One line of a bundle option: what is returned and its refund (price paid times quantity)."""
    unit = money.ensure_cents(line["unit_paid_cents"], "unit_paid_cents", minimum=0)
    entry: dict[str, Any] = {
        "line_id": check["line_id"],
        "policy_check_id": check["policy_check_id"],
        "sku": line["sku"],
        "product_name": line["product_name"],
        "color": line["color"],
        "size": line["size"],
        "qty": qty,
        "unit_paid_cents": unit,
        "refund_cents": unit * qty,
    }
    if line.get("category"):  # display-only label such as "boot" or "sneaker"
        entry["category"] = line["category"]
    return entry


def _clean_check_ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "policy_check_ids must list at least one policy check")
    if len(value) > MAX_BUNDLE_LINES:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"a bundle return takes at most {MAX_BUNDLE_LINES} lines"
        )
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise DomainError(ErrorCode.VALIDATION_ERROR, "policy_check_ids must be policy check ids")
        if item.strip() in cleaned:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "a line can only be in a bundle once")
        cleaned.append(item.strip())
    return cleaned


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


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
    *,
    replacement_color: str | None = None,
    minutes_away: int | None = None,
) -> dict[str, Any]:
    option = {
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
    if location.get("address"):
        option["location_address"] = str(location["address"])
    if replacement_color is not None:  # a colour exchange: the replacement is this colour's SKU
        option["replacement_sku"] = sku
        option["replacement_color"] = replacement_color
    if minutes_away is not None:  # absent, never null, when the distance is unknown
        option["minutes_away"] = minutes_away
    return option


def _minutes_away(location: Mapping[str, Any] | None, travel: Mapping[str, int]) -> int | None:
    """The customer's travel time to a STORE (None for a DC or when it is not on file)."""
    if location is None or location.get("type") != "STORE":
        return None
    return travel.get(location["location_id"])


def _proximity(location: Mapping[str, Any], travel: Mapping[str, int]) -> tuple[int, str]:
    """Sort key of a store: nearest first, a store with unknown distance last, then location id."""
    return (travel.get(location["location_id"], UNKNOWN_MINUTES), location["location_id"])


def _same_color(a: object, b: object) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.strip().casefold() == b.strip().casefold()


def _parse_color(value: object) -> str | None:
    """A colour name as given (case is matched later); None for no value or a blank one."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "replacement_color must be a colour name such as BROWN")
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > MAX_COLOR_CHARS:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"replacement_color is longer than {MAX_COLOR_CHARS} characters")
    return text


def _style_catalog(style: str) -> list[dict[str, Any]]:
    """Every colour and size of one style (the fixture catalog; the table's PRODUCT# items mirror it)."""
    return [p for p in fixtures.load_bundle().products if p.get("style") == style]


def _color_choices(
    catalog: list[dict[str, Any]],
    current_color: str,
    size: str,
    qty: int,
    locations: list[dict[str, Any]],
    home_region: str | None,
    travel: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Other colours of the style that can be exchanged in ``size`` (stock for ``qty`` somewhere).

    ``total_atp`` is the stock in that size across all locations; ``nearest_store_minutes`` is the
    customer's travel time to the nearest home-region store that has it (None when no such store
    has it or the distance is unknown). Sorted by colour, facts only.
    """
    wanted = [p for p in catalog if p["size"] == size and not _same_color(p["color"], current_color)]
    if not wanted:
        return []
    rows = store.batch_get([store.inventory_key(p["sku"], loc["location_id"]) for p in wanted for loc in locations])
    atp: dict[str, dict[str, int]] = {}
    for row in rows:
        atp.setdefault(row["sku"], {})[row["location_id"]] = int(row.get("atp", 0))
    pickup_stores = {
        loc["location_id"] for loc in locations if loc.get("type") == "STORE" and loc.get("region") == home_region
    }
    choices: list[dict[str, Any]] = []
    for product in sorted(wanted, key=lambda p: p["color"]):
        stock = atp.get(product["sku"], {})
        if not any(units >= qty for units in stock.values()):
            continue
        minutes = [
            travel[loc_id]
            for loc_id, units in stock.items()
            if units >= qty and loc_id in pickup_stores and loc_id in travel
        ]
        choices.append(
            {
                "color": product["color"],
                "total_atp": sum(stock.values()),
                "nearest_store_minutes": min(minutes) if minutes else None,
            }
        )
    return choices


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
