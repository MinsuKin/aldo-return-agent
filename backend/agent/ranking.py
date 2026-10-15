"""Deterministic option order and display text for the return workflow of ALDO bot.

PIVOT_ADDENDUM C9: rank store-pickup exchange first, then the other exchanges, then returns.
Eligible refund options are never dropped here, only ordered (requirement R6.5). In-store exchange
is free (the fee comes from the option itself, CAD 0 in the demo logistics).

The owner's chosen path (S19, addendum 11.5) moves that path to the front and keeps everything else
behind it in the default order: ``preferred_channel`` is ``store_pickup`` (the default order),
``ship`` (exchanges shipped by mail first) or ``return`` (the refund options first). Nothing is
removed, so a refund option stays visible whatever the owner chose.

These are pure helpers over the option dicts returned by ``options.find_options``. The
``find_resolution_options`` tool attaches the result as ``ranking`` so the model follows one
rule instead of re-deriving it, and ``create_quote`` puts the chosen options in that order,
so the cards the customer sees match what the agent says.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from returns_domain import clock, money
from returns_domain.errors import DomainError

PICKUP_EXCHANGE = "exchange_store_pickup"
SHIP_EXCHANGES: tuple[str, ...] = ("exchange_ship_from_store", "exchange_ship_from_dc")
RETURN_TYPES: tuple[str, ...] = ("return_in_store", "return_mail")
EXCHANGE_TYPES: tuple[str, ...] = (PICKUP_EXCHANGE, *SHIP_EXCHANGES)

#: Values of ``preferred_channel`` (the same names as the domain and the tool schema).
CHANNELS: tuple[str, ...] = ("ship", "store_pickup", "return")

_GROUP: dict[str, int] = {PICKUP_EXCHANGE: 0, **{t: 1 for t in SHIP_EXCHANGES}, **{t: 2 for t in RETURN_TYPES}}
#: Group order per chosen path: the chosen kind of option first, the rest in the default order.
_GROUP_BY_CHANNEL: dict[str, dict[str, int]] = {
    "ship": {PICKUP_EXCHANGE: 1, **{t: 0 for t in SHIP_EXCHANGES}, **{t: 2 for t in RETURN_TYPES}},
    "return": {PICKUP_EXCHANGE: 1, **{t: 2 for t in SHIP_EXCHANGES}, **{t: 0 for t in RETURN_TYPES}},
}
_LABELS: dict[str, str] = {
    "exchange_ship_from_store": "Ship-from-store exchange",
    "exchange_ship_from_dc": "Ship-from-warehouse exchange",
    "return_in_store": "In-store return",
    "return_mail": "Mail-in return",
}
_MEETS_RANK = {True: 0, None: 1, False: 2}
_LAST = "9999-12-31"
_UNKNOWN_MINUTES = 10**9


def money_text(cents: object) -> str | None:
    """``12999`` -> ``"CAD 129.99"``; ``None`` if ``cents`` is not a whole number of cents."""
    try:
        return money.format_cad(cents)  # type: ignore[arg-type]
    except DomainError:
        return None


def add_money_text(option: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``option`` with ``refund_text`` / ``fee_text`` next to the cents fields.

    A ``benefit`` the domain attached (a tier perk) gets ``saved_text`` next to ``saved_cents``;
    nothing else in it is touched or invented.
    """
    enriched = dict(option)
    for cents_key, text_key in (("refund_cents", "refund_text"), ("fee_cents", "fee_text")):
        text = money_text(option.get(cents_key))
        if text is not None:
            enriched[text_key] = text
    benefit = option.get("benefit")
    if isinstance(benefit, Mapping):
        saved = money_text(benefit.get("saved_cents"))
        if saved is not None:
            enriched["benefit"] = {**benefit, "saved_text": saved}
    return enriched


def normalize_channel(preferred_channel: object) -> str | None:
    """The channel if it is one of ``CHANNELS``, else None (the default order)."""
    return preferred_channel if isinstance(preferred_channel, str) and preferred_channel in CHANNELS else None


def rank_group(option_type: str, preferred_channel: str | None = None) -> int:
    """0 chosen kind (store pickup by default), 1 and 2 the rest, 3 unknown (kept last)."""
    table = _GROUP_BY_CHANNEL.get(preferred_channel or "", _GROUP)
    return table.get(option_type, 3)


def _minutes(option: Mapping[str, Any]) -> int:
    value = option.get("minutes_away")
    return value if isinstance(value, int) and not isinstance(value, bool) else _UNKNOWN_MINUTES


def _sort_key(index: int, option: Mapping[str, Any], preferred_channel: str | None) -> tuple[Any, ...]:
    kind = str(option.get("type"))
    group = rank_group(kind, preferred_channel)
    eta = str(option.get("eta_date") or _LAST)
    fee = option.get("fee_cents")
    fee_rank = fee if isinstance(fee, int) and not isinstance(fee, bool) else 0
    if kind in EXCHANGE_TYPES:
        meets = _MEETS_RANK.get(option.get("meets_deadline"), 1)
        return (group, meets, eta, fee_rank, _minutes(option), index)
    if kind == "return_in_store":  # in-store before mail-in, whichever group they are in
        return (group, 0, 0, fee_rank, eta, index)
    return (group, 0, 1, fee_rank, eta, index)


def rank_options(
    options: Sequence[Mapping[str, Any]], preferred_channel: str | None = None
) -> list[dict[str, Any]]:
    """Options in the order they should be presented (stable for ties)."""
    channel = normalize_channel(preferred_channel)
    ordered = sorted(enumerate(options), key=lambda pair: _sort_key(pair[0], pair[1], channel))
    return [dict(option) for _, option in ordered]


def ranking_reason(
    ordered: Sequence[Mapping[str, Any]],
    deadline_date: str | None = None,
    preferred_channel: str | None = None,
) -> str:
    """One server-authored line explaining why the first option leads (<= 240 characters)."""
    if not ordered:
        return ""
    channel = normalize_channel(preferred_channel)
    top = ordered[0]
    top_type = str(top.get("type"))
    has_return = any(str(o.get("type")) in RETURN_TYPES for o in ordered)
    has_exchange = any(str(o.get("type")) in EXCHANGE_TYPES for o in ordered)
    if top_type in RETURN_TYPES:
        if channel == "return":
            reason = "Refund options lead because you asked for a refund."
        elif not has_exchange:
            reason = "No exchange is available for this size, so the refund options are listed."
        else:
            reason = "Refund options lead because no exchange option applies."
        return reason[:240]

    where = str(top.get("location_name") or top.get("location_id") or "the selected location")
    eta = str(top.get("eta_text") or top.get("eta_date") or "soon")
    fee = top.get("fee_cents")
    fee_phrase = "free" if fee == 0 else (money_text(fee) or "fee shown on the card")
    color = top.get("replacement_color")
    pair = f" of the {color.strip().title()} pair" if isinstance(color, str) and color.strip() else ""
    away = _minutes(top)
    away_phrase = (
        f", {away} minute{'' if away == 1 else 's'} away"
        if away != _UNKNOWN_MINUTES and top_type == PICKUP_EXCHANGE
        else ""
    )
    if top_type == PICKUP_EXCHANGE:
        core = f"Store pickup{pair} at {where} is {fee_phrase} and ready {eta}{away_phrase}"
    else:
        core = f"{_LABELS.get(top_type, 'Exchange')}{pair} from {where} is {fee_phrase} and arrives {eta}"
    core += _deadline_clause(top.get("meets_deadline"), deadline_date)
    if channel == "ship" and top_type in SHIP_EXCHANGES:
        core += ", as you asked for delivery by mail"
    tail = " Refund options stay available." if has_return else ""
    return f"{core}.{tail}"[:240]


def _deadline_clause(meets: object, deadline_date: str | None) -> str:
    if not deadline_date or meets is None:
        return ""
    try:
        deadline_text = clock.format_day(date.fromisoformat(deadline_date))
    except ValueError:
        return ""
    return (
        f", which meets your {deadline_text} deadline"
        if meets is True
        else f", which does not meet your {deadline_text} deadline"
    )


def ranking_summary(
    options: Sequence[Mapping[str, Any]],
    deadline_date: str | None = None,
    preferred_channel: str | None = None,
) -> dict[str, Any]:
    """``{"order": [option_id, ...], "reason": str, "rule": str}`` for the tool result."""
    ordered = rank_options(options, preferred_channel)
    return {
        "order": [str(o.get("option_id")) for o in ordered],
        "reason": ranking_reason(ordered, deadline_date, preferred_channel),
        "rule": "store pickup exchange first, then other exchanges, then returns; refund options stay visible",
    }


def reorder_ids(option_ids: Sequence[str], known_order: Sequence[str]) -> list[str]:
    """``option_ids`` sorted by their position in ``known_order`` (unknown ids keep their place last)."""
    position = {option_id: index for index, option_id in enumerate(known_order)}
    indexed = list(enumerate(option_ids))
    indexed.sort(key=lambda pair: (position.get(pair[1], len(position)), pair[0]))
    seen: set[str] = set()
    result: list[str] = []
    for _, option_id in indexed:
        if option_id not in seen:
            seen.add(option_id)
            result.append(option_id)
    return result
