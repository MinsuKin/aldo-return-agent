"""The trusted memory context the personal agent hands to the return workflow (PIVOT_ADDENDUM 12.3).

At hand-off the return workflow gets a minimal, server-built summary of what the personal agent remembers
about the owner, so it can offer the colours the owner usually keeps first and avoid size
ping-pong. It travels in the return run's payload as ``memory_context`` (the AgentCore runtime has
no access to the personal-agent table) and is at most ``MAX_CONTEXT_BYTES`` of JSON::

    {"size_system": "US_M", "typical_size": "10.0", "preferred_colors": ["BROWN", "WHITE"],
     "aldo_items": [{"order_id": "DEMO-A2A-AB12CD", "product_name": "ALDO Men 001", "color": "BLACK",
                     "size": "9.5", "status": "delivered"}],
     "source": "personal agent memory"}

Privacy by construction: the builder reads only the owner's sizing basics, the COLOURS of items the
owner kept and the owner's agent-placed ALDO orders. It never copies a retailer label, an item
name of another purchase, an outcome, a memory id or an event, so there is nothing to scrub later.
Everything is rebuilt field by field from whitelisted values, never forwarded as a whole.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Mapping, Sequence

CONTEXT_SOURCE = "personal agent memory"
MAX_CONTEXT_BYTES = 1024
MAX_PREFERRED_COLORS = 5
MAX_ALDO_ITEMS = 3
AGENT_CHANNEL = "agent"  # orders placed through the personal agent (ids DEMO-A2A-XXXXXX)

#: The fields a context may hold, in the order the trace lists them.
CONTEXT_FIELDS = ("typical_size", "preferred_colors", "aldo_items")

_SIZE = re.compile(r"^\d{1,2}\.\d$")
_SIZE_SYSTEM = re.compile(r"^US_[MW]$")
_COLOR = re.compile(r"^[A-Za-z][A-Za-z -]{0,19}$")
_ORDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_WHITESPACE = re.compile(r"\s+")


def build_memory_context(
    memory: Any, orders: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """The return workflow's context for one owner, at most ``MAX_CONTEXT_BYTES`` of JSON.

    ``memory`` is the owner's ``OwnerMemory`` (or its dict form); ``orders`` are the owner's
    recent ALDO orders as the worker attaches them (``order_id``, ``channel``, ``delivered_at``
    and ``lines`` with ``product_name``, ``color``, ``size``). Preferred colours are the colours
    of items the owner KEPT (at other retailers and confirmed ALDO size exchanges), most frequent
    first, ties in the order they were remembered; a provisional ALDO update does not count, and
    neither does a colour exchange (its update names the colour the owner gave back).
    """
    view = _as_mapping(memory)
    context: dict[str, Any] = {}
    size_system, typical = view.get("size_system"), view.get("typical_size")
    if isinstance(size_system, str) and _SIZE_SYSTEM.match(size_system):
        context["size_system"] = size_system
    if isinstance(typical, str) and _SIZE.match(typical):
        context["typical_size"] = typical
    colors = preferred_colors(view)
    if colors:
        context["preferred_colors"] = colors
    items = aldo_items(orders or ())
    if items:
        context["aldo_items"] = items
    context["source"] = CONTEXT_SOURCE
    return _fit(context)


def preferred_colors(memory: Any) -> list[str]:
    """Colours of kept items (remembered purchases, confirmed ALDO exchanges), most frequent first."""
    view = _as_mapping(memory)
    seen: list[str] = []
    counts: Counter[str] = Counter()

    def count(color: object) -> None:
        clean = _color(color)
        if clean is None:
            return
        if clean not in counts:
            seen.append(clean)
        counts[clean] += 1

    for entry in view.get("memory") or ():
        entry = _as_mapping(entry)
        if entry.get("outcome") == "kept":
            count(entry.get("color"))
    for update in view.get("updates") or ():
        update = _as_mapping(update)
        confirmed = update.get("provisional") is False or update.get("confidence") == "confirmed"
        # A colour exchange (PREFERENCE) carries the colour the owner gave back, not the one kept.
        if confirmed and update.get("status", "active") == "active" and update.get("reason") != "PREFERENCE":
            count(update.get("color"))
    ranked = sorted(seen, key=lambda color: (-counts[color], seen.index(color)))
    return ranked[:MAX_PREFERRED_COLORS]


def aldo_items(orders: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Items of the owner's agent-placed ALDO orders, newest order first (``orders`` come newest first)."""
    items: list[dict[str, str]] = []
    for order in orders:
        order = _as_mapping(order)
        order_id = order.get("order_id")
        if order.get("channel") != AGENT_CHANNEL or not isinstance(order_id, str) or not _ORDER_ID.match(order_id):
            continue
        status = "delivered" if order.get("delivered_at") else "ordered"
        for line in order.get("lines") or ():
            line = _as_mapping(line)
            name, color, size = _text(line.get("product_name"), 60), _color(line.get("color")), line.get("size")
            if not name or color is None or not isinstance(size, str) or not _SIZE.match(size):
                continue
            items.append(
                {"order_id": order_id, "product_name": name, "color": color, "size": size, "status": status}
            )
            if len(items) >= MAX_ALDO_ITEMS:
                return items
    return items


def consulted_fields(context: Mapping[str, Any]) -> list[str]:
    """Which of ``CONTEXT_FIELDS`` the context holds (the public ``fields`` of ``memory_consulted``)."""
    return [name for name in CONTEXT_FIELDS if context.get(name)]


def entry_count(context: Mapping[str, Any]) -> int:
    """How many facts the context holds: the typical size, each preferred colour, each ALDO item."""
    colors, items = context.get("preferred_colors"), context.get("aldo_items")
    return (
        (1 if context.get("typical_size") else 0)
        + (len(colors) if isinstance(colors, list) else 0)
        + (len(items) if isinstance(items, list) else 0)
    )


def encoded_size(context: Mapping[str, Any]) -> int:
    return len(json.dumps(context, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# ----------------------------------------------------------------------------- internals


def _fit(context: dict[str, Any]) -> dict[str, Any]:
    """Drop the least important facts until the JSON fits: ALDO items first, then colours."""
    for key in ("aldo_items", "preferred_colors"):
        while encoded_size(context) > MAX_CONTEXT_BYTES and context.get(key):
            context[key] = context[key][:-1]
            if not context[key]:
                del context[key]
    return context


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else {}


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = _WHITESPACE.sub(" ", value).strip()
    return text[:limit].rstrip()


def _color(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = _WHITESPACE.sub(" ", value).strip()
    return text.upper() if _COLOR.match(text) else None
