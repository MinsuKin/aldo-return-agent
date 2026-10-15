"""ALDO Fit Gateway: purchase-time sizing, the owner-confirmed simulated order, the write-back.

PIVOT_ADDENDUM sections 2 (C1-C10), 5.2-5.6 and 10 (owner confirmation click, ALDO bot);
contracts.md C.8 and F.3-F.7. Deterministic code, no LLM, each call well under a second.

Purchase flow (addendum 10.1). No agent can place an order:

1. ``size_assist(claims, request)``: the ALDO bot asks "help me size this" with a DELEGATED
   token. ALDO answers from its own data only (fit signal, stock, delivery options, a template
   question) and persists ``SIZEREQ#{id}`` with a 15 minute real-clock expiry. Nothing else.
2. ``decide_size(claims, request)``: the ALDO bot's choice is only a PROPOSAL. The checks of
   F.4 (1)-(8) run, then one conditional write records ``status = proposed`` and the chosen
   option. No order, no stock change. A newer proposal replaces an older one while the request
   is ``open`` or ``proposed``.
3. ``get_proposal(customer_id, size_request_id)``: the owner's confirmation card, rendered from
   server data (session token).
4. ``confirm_purchase(customer_id, size_request_id, option_id=None)``: the owner's click (session
   token). ONE ``TransactWriteItems``: request ``proposed -> decided``, the ORDER
   (``DEMO-A2A-XXXXXX``, channel ``agent``, ``delivered_at`` null, CAD, banner ALDO_CA), its LINE,
   ATP of the chosen location minus one (condition ``atp >= 1``; ``STOCK_CHANGED`` writes
   nothing). The order id derives from the request id, so a repeat returns the same order
   (``replayed: true``) and a concurrent duplicate cannot create a second one.

Facts come from server data only: fit from ``fit.get_fit_insight`` (headline scope, ALDO_CA
unless that is too thin), products and sizes from the GSI1 ``STYLE#{style}#{color}`` query,
stock from the consistent ``INV#{sku}`` rows, the owner's ``home_region`` from ALDO's own
customer record (the request never carries it), ETAs in calendar days from DEMO_NOW.
Everything the ALDO bot sends is untrusted data: schema-validated with ``extra="forbid"``,
enumerations and lengths checked, ``rationale_public`` shown as data and never put in a prompt.

Options per size: ``ship_from_dc`` (a DC with stock), ``ship_from_store`` (the single home-region
store with the lowest ``ship_days``, ties by lowest location id), ``store_pickup`` (every
home-region store with stock). A size without any option is omitted. Option ids run
``SO-1 ... SO-n`` across the whole response, sizes ascending.

Trace events go to the owner's A2A stream only AFTER the write they describe succeeded, so a
refused call leaves no trace at all. ``emit_a2a`` keeps only the public fields of its allowlist.

Write-back (C2, 5.4): ``build_memory_update``, ``enqueue_memory_update`` and ``flush_outbox`` are
thin wrappers over ``memory_updates`` (the single implementation of the update payload, the
outbox item and its state changes). ``flush_outbox`` never raises; only the caller that wins the
``pending -> delivered`` transition emits ``memory_updated``, so it appears exactly once.
``memory_updates`` is bound late (imported on first use).
"""

from __future__ import annotations

import importlib
import logging
import random
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import audit, auth, clock, config, fit, fixtures, money, personal_agent_client, store, trace
from .errors import DomainError, ErrorCode
from .personal_agent_client import PersonalAgentClient, get_client

__all__ = [
    "FitSignalView",
    "FulfillmentView",
    "GatewayError",
    "PersonalAgentClient",
    "SizeAssistRequest",
    "SizeAssistResponse",
    "SizeChoices",
    "SizeDecisionRequest",
    "SizeDecisionResponse",
    "SizeOption",
    "SizeProposalResponse",
    "agent_card",
    "build_memory_update",
    "build_offered_options",
    "build_purchase",
    "confirm_purchase",
    "decide_size",
    "enqueue_memory_update",
    "eta_for_option",
    "flush_outbox",
    "get_client",
    "get_proposal",
    "group_sizes",
    "next_step_text",
    "question_text",
    "resolve_style",
    "simulate_delivery",
    "simulate_pickup",
    "size_assist",
]

logger = logging.getLogger(__name__)


class _LateModule:
    """Handle on a sibling module that is imported on first attribute access."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._module: Any = None

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("__"):
            raise AttributeError(attr)
        if self._module is None:
            self._module = importlib.import_module(f"{__package__}.{self._name}")
        return getattr(self._module, attr)


#: Written by the persistence stream; bound late. Tests replace this name with a fake.
memory_updates: Any = _LateModule("memory_updates")

# --------------------------------------------------------------------------- constants

BANNER = "ALDO_CA"
CHANNEL_AGENT = "agent"
SIZE_SYSTEM = "US_W"  # the only size scale of the demo catalog

TYPE_SHIP_DC = "ship_from_dc"
TYPE_SHIP_STORE = "ship_from_store"
TYPE_PICKUP = "store_pickup"
OPTION_TYPES: tuple[str, ...] = (TYPE_SHIP_DC, TYPE_SHIP_STORE, TYPE_PICKUP)

REQUEST_ID_PREFIX = "SZR-"
STATUS_OPEN = "open"
STATUS_PROPOSED = "proposed"
STATUS_DECIDED = "decided"
ORDER_STATUS = "placed_simulated"
APPROVED_BY = "owner_click"

ACTOR_PERSONAL_AGENT = "personal_agent"
ACTOR_GATEWAY = "aldo_gateway"

OUTCOME_CREATED = "exchange_created"
OUTCOME_KEPT = "exchange_completed_kept"

MAX_CANDIDATES = 10
PENDING_LIMIT_CAP = 50
_TRANSACTION_ATTEMPTS = 4

_REQUEST_ID = re.compile(r"SZR-[0-9A-F]{6}")
_OPTION_ID = re.compile(r"SO-[0-9]{1,3}")
_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class GatewayError(DomainError):
    """A DomainError that carries extra top-level fields, e.g. the ``candidates`` of UNKNOWN_STYLE."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        extra: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(code, message, retryable, details=extra)
        self.extra: dict[str, Any] = dict(extra) if extra else {}

    def to_dict(self) -> dict[str, Any]:
        """``{"code", "message", "retryable", **extra}`` (the extras sit beside the code)."""
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        body.update(self.extra)
        return body


# ------------------------------------------------------------------ request models (C7)


class _Request(BaseModel):
    """Everything the ALDO bot sends is untrusted: unknown fields are refused."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _blank_to_none(value: Any) -> Any:
    return None if isinstance(value, str) and not value.strip() else value


class SizeAssistRequest(_Request):
    intent: Literal["size_assist"]
    product_query: str = Field(min_length=2, max_length=80)
    color: str | None = Field(default=None, max_length=20)
    size_system: Literal["US_W"] = "US_W"
    deadline_date: str | None = None
    channel_pref: Literal["ship", "store_pickup"] | None = None

    @field_validator("color", "deadline_date", "channel_pref", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("deadline_date")
    @classmethod
    def _deadline(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            if _DATE_ONLY.fullmatch(value):
                date.fromisoformat(value)
                return value
        except ValueError:
            pass
        raise ValueError("deadline_date must be a calendar date like 2026-10-17")


class SizeDecisionRequest(_Request):
    size_request_id: str = Field(pattern=r"^SZR-[0-9A-F]{6}$")
    option_id: str = Field(pattern=r"^SO-[0-9]{1,3}$")
    chosen_size: str | int | float
    rationale_public: str = ""
    confidence: Literal["low", "medium", "high"] = "medium"

    @field_validator("chosen_size")
    @classmethod
    def _size(cls, value: str | int | float) -> str:
        if isinstance(value, bool):
            raise ValueError("chosen_size must be a size such as 8.0")
        try:
            return store.size_str(value)
        except DomainError as exc:
            raise ValueError(exc.message) from None

    @field_validator("rationale_public")
    @classmethod
    def _rationale(cls, value: str) -> str:
        text = " ".join(_CONTROL.sub(" ", value).split())
        limit = config.get_settings().rationale_max_chars
        if len(text) > limit:
            raise ValueError(f"rationale_public must be at most {limit} characters")
        return text


def _validate(model: type[BaseModel], data: object) -> Any:
    if not isinstance(data, Mapping):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "The request body must be a JSON object.")
    try:
        return model.model_validate(dict(data))
    except ValidationError as exc:
        # Report where and why, never the offending value (it is untrusted and may be large).
        problems = [
            f"{'.'.join(str(part) for part in err['loc']) or 'request'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)[:3]
        ]
        raise DomainError(ErrorCode.VALIDATION_ERROR, "; ".join(problems)) from None


# -------------------------------------------------------------------- response models


class FitSignalView(BaseModel):
    signal: str
    too_small_share: float | None
    n_fit: int
    scope: str
    provenance: Literal["derived"] = "derived"


class SizeOption(BaseModel):
    option_id: str
    type: Literal["ship_from_dc", "ship_from_store", "store_pickup"]
    location_id: str
    location_name: str
    eta_date: str
    eta_text: str
    meets_deadline: bool | None
    atp: int


class SizeChoices(BaseModel):
    size: str
    options: list[SizeOption]


class SizeAssistResponse(BaseModel):
    size_request_id: str
    style_id: str
    product_name: str
    color: str
    price_cents: int
    currency: str = "CAD"
    fit_signal: FitSignalView
    sizes: list[SizeChoices]
    question: str
    expires_at: str


class FulfillmentView(BaseModel):
    type: str
    location_id: str
    location_name: str
    eta_date: str


class SizeDecisionResponse(BaseModel):
    order_id: str
    status: Literal["placed_simulated"] = "placed_simulated"
    line: dict[str, Any]
    fulfillment: FulfillmentView
    next_step: str
    replayed: bool = False


class SizeProposalResponse(BaseModel):
    """Answer to ``decide_size``: the proposal is recorded and awaits the owner's click."""

    size_request_id: str
    status: Literal["proposed", "decided"] = "proposed"
    awaiting: Literal["owner_confirmation"] | None = "owner_confirmation"
    expires_at: str
    summary: dict[str, Any]
    order_id: str | None = None
    replayed: bool = False


# -------------------------------------------------------------------------- catalog


def _catalog() -> dict[str, dict[str, list[str]]]:
    """``{product_name: {style_id: [colors]}}`` from the fixture products."""
    grouped: dict[str, dict[str, set[str]]] = {}
    for record in fixtures.load_bundle().products:
        name, style, color = record.get("product_name"), record.get("style"), record.get("color")
        if all(isinstance(part, str) and part for part in (name, style, color)):
            grouped.setdefault(name, {}).setdefault(style, set()).add(color)
    if not grouped:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "the product catalog is empty")
    return {
        name: {style: sorted(colors) for style, colors in styles.items()}
        for name, styles in grouped.items()
    }


def _candidates(names: Iterable[str], catalog: Mapping[str, Mapping[str, list[str]]]) -> list[dict]:
    return [
        {"style_id": style_id, "product_name": name, "colors": list(colors)}
        for name in names
        for style_id, colors in catalog[name].items()
    ][:MAX_CANDIDATES]


def resolve_style(product_query: str, color: str | None = None) -> tuple[str, str, str]:
    """``(product_name, style_id, color)`` for a case-insensitive name query (F.3 step 3).

    No match, several products, a name shared by several styles, a missing color for a style
    with several, or a color the style is not sold in: ``GatewayError(UNKNOWN_STYLE)`` whose
    ``candidates`` are ``{"style_id", "product_name", "colors"}`` (empty when nothing matched).
    """
    if not isinstance(product_query, str) or not product_query.strip():
        raise DomainError(ErrorCode.VALIDATION_ERROR, "product_query is required")
    catalog = _catalog()
    names = list(catalog)
    text = " ".join(product_query.split())
    matches = (
        fit.search_product_names(text, names, limit=len(names))
        if len(text) <= fit.MAX_QUERY_CHARS
        else []
    )
    exact = [name for name in matches if name.casefold() == text.casefold()]
    chosen = exact if len(exact) == 1 else matches
    if len(chosen) != 1:
        message = (
            "No product matches the query."
            if not chosen
            else "Several products match the query; use one of the candidates."
        )
        raise GatewayError(
            ErrorCode.UNKNOWN_STYLE, message, extra={"candidates": _candidates(chosen, catalog)}
        )
    name = chosen[0]
    styles = catalog[name]
    if len(styles) != 1:
        raise GatewayError(
            ErrorCode.UNKNOWN_STYLE,
            "Several styles share this name.",
            extra={"candidates": _candidates([name], catalog)},
        )
    style_id, colors = next(iter(styles.items()))
    if color is None:
        if len(colors) == 1:
            return name, style_id, colors[0]
        raise GatewayError(
            ErrorCode.UNKNOWN_STYLE,
            "Choose one of the colors of this style.",
            extra={"candidates": _candidates([name], catalog)},
        )
    wanted = next((c for c in colors if c.casefold() == color.casefold()), None)
    if wanted is None:
        raise GatewayError(
            ErrorCode.UNKNOWN_STYLE,
            "This style is not sold in that color.",
            extra={"candidates": _candidates([name], catalog)},
        )
    return name, style_id, wanted


# -------------------------------------------------------------- pure fact builders (5.2)


def _transit_days(location: Mapping[str, Any], field: str) -> int:
    days = location.get(field)
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise DomainError(ErrorCode.INTERNAL_ERROR, f"a location has no usable {field}")
    return days


def eta_for_option(location: Mapping[str, Any], option_type: str, today: date) -> date:
    """Calendar-day ETA of ``option_type`` from ``location`` on ``today`` (no business days).

    Pickup uses ``pickup_ready_days``; both ship types use ``ship_days``. This is the same
    calculation as ``options.eta_for`` in the persistence stream; keep the two in step.
    """
    field = "pickup_ready_days" if option_type == TYPE_PICKUP else "ship_days"
    return today + timedelta(days=_transit_days(location, field))


def _location_id(item: Mapping[str, Any]) -> str:
    raw = item.get("location_id")
    if isinstance(raw, str) and raw:
        return raw
    return str(item.get("sk") or item.get("pk") or "").removeprefix("LOC#")


def _atp(row: Mapping[str, Any]) -> int:
    value = row.get("atp")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _version(row: Mapping[str, Any]) -> int:
    value = row.get("version")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


def _eta_text(eta_date: str) -> str:
    return clock.format_day(date.fromisoformat(eta_date))


def build_offered_options(
    *,
    products: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Sequence[Mapping[str, Any]]],
    locations: Mapping[str, Mapping[str, Any]],
    home_region: str | None,
    today: date,
    deadline: date | None,
) -> list[dict[str, Any]]:
    """Flat, ordered list of offered options with ids ``SO-1`` ... (pure, F.3 step 5).

    ``products`` are the GSI1 items of one style and color, ``inventory`` maps sku to its
    ``INV#`` rows, ``locations`` maps location id to the location item. Sizes ascend; within a
    size the order is ``ship_from_dc``, ``ship_from_store``, ``store_pickup`` (by location id).
    A size with no option is omitted. Each entry carries ``size``, ``sku``, ``type``,
    ``location_id``, ``location_name``, ``eta_date``, ``meets_deadline`` (None without a
    deadline), ``atp`` and ``inventory_version``.
    """
    offered: list[dict[str, Any]] = []
    for product in sorted(products, key=lambda item: Decimal(item["size"])):
        sku, size = product["sku"], product["size"]
        stocked = sorted(
            (
                (_location_id(row), row)
                for row in inventory.get(sku, ())
                if _atp(row) >= 1 and _location_id(row) in locations
            ),
            key=lambda pair: pair[0],
        )
        chosen: list[tuple[str, str, Mapping[str, Any]]] = [
            (TYPE_SHIP_DC, loc, row) for loc, row in stocked if locations[loc].get("type") == "DC"
        ]
        home_stores = [
            (loc, row)
            for loc, row in stocked
            if home_region
            and locations[loc].get("type") == "STORE"
            and locations[loc].get("region") == home_region
        ]
        if home_stores:
            loc, row = min(
                home_stores,
                key=lambda pair: (_transit_days(locations[pair[0]], "ship_days"), pair[0]),
            )
            chosen.append((TYPE_SHIP_STORE, loc, row))
        chosen.extend((TYPE_PICKUP, loc, row) for loc, row in home_stores)

        for option_type, loc, row in chosen:
            eta = eta_for_option(locations[loc], option_type, today)
            offered.append(
                {
                    "size": size,
                    "sku": sku,
                    "type": option_type,
                    "location_id": loc,
                    "location_name": str(locations[loc].get("name") or loc),
                    "eta_date": eta.isoformat(),
                    "meets_deadline": None if deadline is None else eta <= deadline,
                    "atp": _atp(row),
                    "inventory_version": _version(row),
                }
            )
    for number, option in enumerate(offered, start=1):
        option["option_id"] = f"SO-{number}"
    return offered


def group_sizes(offered: Iterable[Mapping[str, Any]]) -> list[SizeChoices]:
    """The response ``sizes`` list: sizes ascending, each with its options in offered order."""
    grouped: dict[str, list[SizeOption]] = {}
    for option in offered:
        grouped.setdefault(option["size"], []).append(
            SizeOption(
                option_id=option["option_id"],
                type=option["type"],
                location_id=option["location_id"],
                location_name=option["location_name"],
                eta_date=option["eta_date"],
                eta_text=_eta_text(option["eta_date"]),
                meets_deadline=option["meets_deadline"],
                atp=option["atp"],
            )
        )
    return [
        SizeChoices(size=size, options=grouped[size]) for size in sorted(grouped, key=Decimal)
    ]


def fit_signal_view(insight: Mapping[str, Any]) -> FitSignalView:
    """``fit.get_fit_insight`` headline as the response ``fit_signal``."""
    return FitSignalView(
        signal=insight["signal"],
        too_small_share=insight["too_small_share"],
        n_fit=insight["n_fit"],
        scope=insight["scope"],
    )


def question_text(signal: FitSignalView | Mapping[str, Any]) -> str:
    """The server-authored question with the real numbers (C.8 template, never model text)."""
    view = signal if isinstance(signal, FitSignalView) else FitSignalView(**signal)
    ask = "Which size and option should I reserve for your owner?"
    n_fit, share = view.n_fit, view.too_small_share
    if view.signal == fit.RUNS_SMALL and share is not None:
        lead = (
            f"This style runs small ({round(share * 100)}% of fit-related returns "
            f"were 'too small', n={n_fit})."
        )
    elif view.signal == fit.RUNS_LARGE and share is not None:
        lead = (
            f"This style runs large ({round((1 - share) * 100)}% of fit-related returns "
            f"were 'too big', n={n_fit})."
        )
    elif view.signal == fit.MIXED:
        lead = f"Returns for this style are mixed on fit (n={n_fit})."
    else:
        lead = f"ALDO has too little return data on this style for a fit signal (n={n_fit})."
    return f"{lead} {ask}"


def next_step_text(option: Mapping[str, Any]) -> str:
    """Template text for the order confirmation (weekdays come from the date)."""
    when = _eta_text(option["eta_date"])
    size, place = option["size"], option["location_name"]
    if option["type"] == TYPE_PICKUP:
        return f"Pick up size {size} at {place} from {when}. Simulated order, no payment taken."
    return (
        f"Size {size} ships from {place} and should arrive by {when}. "
        "Simulated order, no payment taken."
    )


def _option_summary(record: Mapping[str, Any], option_id: str) -> dict[str, Any]:
    """The server-rendered facts of one offered option, for proposals and the owner's card."""
    option = record["offered"][option_id]
    return {
        "product_name": record["product_name"],
        "color": record["color"],
        "option_id": option_id,
        "size": option["size"],
        "option_type": option["type"],
        "location_id": option["location_id"],
        "location_name": option["location_name"],
        "eta_date": option["eta_date"],
        "eta_text": _eta_text(option["eta_date"]),
        "meets_deadline": option.get("meets_deadline"),
        "price_cents": record["price_cents"],
        "currency": record.get("currency", money.CURRENCY),
    }


def build_purchase(
    *, record: Mapping[str, Any], option_id: str, ordered_on: date
) -> dict[str, Any]:
    """The order, its line and the response of the owner-confirmed purchase (pure, F.4 step 9).

    ``record`` is the SIZEREQ item; price, SKU, size and location come from what ALDO itself
    recorded in it. Returns ``{"order_id", "option", "items", "result"}`` where ``items`` are
    the ORDER META and LINE items with every key set (same builder as the fixtures).
    """
    option = record["offered"][option_id]
    price = money.ensure_cents(record.get("price_cents"), "price_cents", minimum=0)
    order_id = store.derived_id(store.AGENT_ORDER_PREFIX, record["size_request_id"], n=6)
    fulfillment = FulfillmentView(
        type=option["type"],
        location_id=option["location_id"],
        location_name=option["location_name"],
        eta_date=option["eta_date"],
    )
    order = {
        "order_id": order_id,
        "customer_id": record["customer_id"],
        "banner": BANNER,
        "currency": money.CURRENCY,
        "channel": CHANNEL_AGENT,
        "ordered_at": ordered_on.isoformat(),
        "delivered_at": None,
        "size_request_id": record["size_request_id"],
        "fulfillment": fulfillment.model_dump(),
        "source": "synthetic",
    }
    line = {
        "line_id": f"{order_id}-L1",
        "sku": option["sku"],
        "product_name": record["product_name"],
        "style": record["style_id"],
        "size": option["size"],
        "color": record["color"],
        "purchased_qty": 1,
        "returned_or_reserved_qty": 0,
        "unit_paid_cents": price,
        "final_sale": False,
        "source": "synthetic",
    }
    result = SizeDecisionResponse(
        order_id=order_id,
        line={"sku": option["sku"], "size": option["size"], "unit_paid_cents": price},
        fulfillment=fulfillment,
        next_step=next_step_text(option),
    )
    return {
        "order_id": order_id,
        "option": option,
        "items": store.order_items(order, [line]),
        "result": result,
    }


# ----------------------------------------------------------------- persistence helpers


def _pause() -> None:
    time.sleep(random.uniform(0.05, 0.2))


def _emit(customer_id: str, event_type: str, payload: dict[str, Any], actor: str) -> None:
    """A2A trace event; failures are logged, never raised (the business write already won)."""
    trace.emit_a2a_safe(customer_id, event_type, payload, actor=actor)


def _delegated_owner(claims: Any) -> str:
    """The customer of a verified DELEGATED token (F.3 step 1); any other scope is refused."""
    owner = getattr(claims, "customer_id", None)
    scope = getattr(claims, "scope", None)
    if scope != auth.SCOPE_DELEGATED_GATEWAY or not isinstance(owner, str) or not owner:
        raise DomainError(
            ErrorCode.NOT_AUTHORIZED, "This endpoint accepts only the delegated gateway token."
        )
    return owner


def _clean_record_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _RECORD_ID.fullmatch(value.strip()):
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"{label} has an invalid format")
    return value.strip()


def _clean_request_id(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value.strip()):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "size_request_id has an invalid format")
    return value.strip()


def _clean_option_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _OPTION_ID.fullmatch(value.strip()):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "option_id has an invalid format")
    return value.strip()


def _load_request(owner: str, request_id: str, *, actor: str) -> dict[str, Any]:
    """Consistent read of ``SIZEREQ#``: NOT_FOUND, or NOT_AUTHORIZED plus an audit event."""
    record = store.get_item(store.sizereq_key(request_id), consistent=True)
    if record is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Size request not found.")
    if record.get("customer_id") != owner:
        audit.access_denied(actor, "size_request", request_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Size request not found for this customer.")
    return record


def _is_expired(record: Mapping[str, Any]) -> bool:
    return clock.now_utc() > clock.parse_iso(str(record.get("expires_at")))


def _require_unexpired(record: Mapping[str, Any]) -> None:
    if _is_expired(record):
        raise DomainError(
            ErrorCode.REQUEST_EXPIRED, "This size request expired. Ask for sizes again."
        )


def _require_stock(option: Mapping[str, Any]) -> None:
    """F.4 (8): a consistent read of the INV item; the transaction repeats it as a condition."""
    row = store.get_item(store.inventory_key(option["sku"], option["location_id"]), consistent=True)
    if row is None or _atp(row) < 1:
        raise DomainError(
            ErrorCode.STOCK_CHANGED, "That option is no longer in stock. Ask for sizes again."
        )


def _style_products(style_id: str, color: str) -> list[dict[str, Any]]:
    items = store.query(f"STYLE#{style_id}#{color}", index="GSI1")
    products = [
        item
        for item in items
        if isinstance(item.get("sku"), str) and isinstance(item.get("size"), str)
    ]
    if not products:
        raise DomainError(ErrorCode.NOT_FOUND, "No stock data for this style.")
    if len({item.get("price_cents") for item in products}) != 1:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "the style has inconsistent prices")
    return products


def _locations_with_stock(
    inventory: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, dict[str, Any]]:
    wanted = sorted({_location_id(row) for rows in inventory.values() for row in rows if _atp(row) >= 1})
    found: dict[str, dict[str, Any]] = {}
    for location_id in wanted:
        item = store.get_item(store.location_key(location_id))
        if item is not None:
            found[location_id] = item
    return found


# ------------------------------------------------------------------------- size_assist


def size_assist(claims: auth.TokenClaims, request: Mapping[str, Any]) -> SizeAssistResponse:
    """``POST /api/gateway/size-assist`` (addendum 5.2, F.3): fit signal, stock and options.

    Needs a DELEGATED token (else NOT_AUTHORIZED); ``request`` is validated with
    ``extra="forbid"`` (``customer_id``, ``owner_fit_hint`` ... give VALIDATION_ERROR). Persists
    ``SIZEREQ#{id}`` and emits ``size_assist_requested``, ``fit_signal_shared`` and
    ``size_requested``. Changes no stock. UNKNOWN_STYLE lists ``candidates``.
    """
    owner = _delegated_owner(claims)
    body = _validate(SizeAssistRequest, request)
    deadline = date.fromisoformat(body.deadline_date) if body.deadline_date else None
    product_name, style_id, color = resolve_style(body.product_query, body.color)

    customer = store.get_item(store.customer_key(owner))
    if customer is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Customer not found.")
    products = _style_products(style_id, color)
    inventory = {
        product["sku"]: store.query(f"INV#{product['sku']}", sk_begins_with="LOC#")
        for product in products
    }
    offered = build_offered_options(
        products=products,
        inventory=inventory,
        locations=_locations_with_stock(inventory),
        home_region=customer.get("home_region"),
        today=clock.demo_today(),
        deadline=deadline,
    )

    price = money.ensure_cents(products[0].get("price_cents"), "price_cents", minimum=0)
    signal = fit_signal_view(fit.get_fit_insight(product_name))
    question = question_text(signal)
    sizes = group_sizes(offered)
    created = clock.now_utc()
    expires_at = clock.iso(created + timedelta(seconds=config.get_settings().size_request_ttl_seconds))
    query_text = " ".join(body.product_query.split())

    request_id = ""
    for _ in range(3):  # a random id can only collide with an existing request by accident
        request_id = store.new_id(REQUEST_ID_PREFIX)
        pk, sk = store.sizereq_key(request_id)
        item = {
            "pk": pk,
            "sk": sk,
            "size_request_id": request_id,
            "customer_id": owner,
            "status": STATUS_OPEN,
            "style_id": style_id,
            "product_name": product_name,
            "color": color,
            "price_cents": price,
            "currency": money.CURRENCY,
            "size_system": SIZE_SYSTEM,
            "product_query": query_text,
            "deadline_date": body.deadline_date,
            "channel_pref": body.channel_pref,
            "fit_signal": signal.model_dump(),
            "question": question,
            "offered": {
                option["option_id"]: {
                    key: option[key]
                    for key in (
                        "size",
                        "type",
                        "sku",
                        "location_id",
                        "location_name",
                        "eta_date",
                        "meets_deadline",
                        "atp",
                        "inventory_version",
                    )
                }
                for option in offered
            },
            "created_at": clock.iso(created),
            "expires_at": expires_at,
            "source": "synthetic",
        }
        try:
            store.put_item(item, condition="attribute_not_exists(#pk)")
            break
        except store.ConditionFailed:
            continue
    else:
        raise DomainError(ErrorCode.TRANSACTION_CONFLICT, "Could not create a size request; retry.")

    _emit(
        owner,
        "size_assist_requested",
        {
            "size_request_id": request_id,
            "product_query": query_text,
            "color": body.color,
            "deadline_date": body.deadline_date,
            "channel_pref": body.channel_pref,
        },
        ACTOR_PERSONAL_AGENT,
    )
    _emit(
        owner,
        "fit_signal_shared",
        {
            "size_request_id": request_id,
            "style_id": style_id,
            "product_name": product_name,
            **signal.model_dump(),
        },
        ACTOR_GATEWAY,
    )
    _emit(
        owner,
        "size_requested",
        {
            "size_request_id": request_id,
            "sizes": [choice.size for choice in sizes],
            "option_count": len(offered),
            "question": question,
            "expires_at": expires_at,
        },
        ACTOR_GATEWAY,
    )
    return SizeAssistResponse(
        size_request_id=request_id,
        style_id=style_id,
        product_name=product_name,
        color=color,
        price_cents=price,
        fit_signal=signal,
        sizes=sizes,
        question=question,
        expires_at=expires_at,
    )


# ------------------------------------------------------------------------ decide_size


def _decision_denied(actor: str, request_id: str, code: str) -> None:
    audit.write_audit(
        actor,
        "decision_denied",
        "denied",
        {"resource_type": "size_request", "resource_id": request_id, "code": code},
    )


def _decided_proposal(record: Mapping[str, Any], option_id: str, size: str) -> SizeProposalResponse:
    """F.4 (4): the request is already decided. Same choice: no writes, no events."""
    if record.get("chosen_option_id") != option_id or record.get("chosen_size") != size:
        raise DomainError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "This size request was already decided with a different option or size.",
        )
    return SizeProposalResponse(
        size_request_id=record["size_request_id"],
        status=STATUS_DECIDED,
        awaiting=None,
        expires_at=record["expires_at"],
        summary=_option_summary(record, option_id),
        order_id=record.get("order_id"),
        replayed=True,
    )


def decide_size(claims: auth.TokenClaims, request: Mapping[str, Any]) -> SizeProposalResponse:
    """``POST /api/gateway/size-decision`` (addendum 10.1, F.4 checks 1-8): record a PROPOSAL.

    Checks, before any write: (1) delegated scope; (2) the body; (3) the request exists and is
    this customer's (NOT_FOUND, NOT_AUTHORIZED + audit); (4) already decided: same choice
    returns the order id without writes, another choice is IDEMPOTENCY_CONFLICT; (5) not past
    ``expires_at`` (REQUEST_EXPIRED); (6) ``option_id`` offered and its size equals
    ``chosen_size`` (INVALID_SIZE_CHOICE + audit); (7) the server-recorded price within
    ``claims.max_amount_cents`` (DELEGATION_LIMIT_EXCEEDED + audit); (8) the option still in
    stock (STOCK_CHANGED). Then ONE conditional write sets ``status = proposed`` and the choice.
    No order, no stock change. Emits ``size_recommended``.
    """
    owner = _delegated_owner(claims)
    body = _validate(SizeDecisionRequest, request)
    request_id, option_id, size = body.size_request_id, body.option_id, body.chosen_size

    for _ in range(2):
        record = _load_request(owner, request_id, actor=ACTOR_PERSONAL_AGENT)
        if record.get("status") == STATUS_DECIDED:
            return _decided_proposal(record, option_id, size)
        _require_unexpired(record)
        offered = record.get("offered") or {}
        option = offered.get(option_id)
        if option is None or option.get("size") != size:
            _decision_denied(ACTOR_PERSONAL_AGENT, request_id, ErrorCode.INVALID_SIZE_CHOICE)
            raise DomainError(
                ErrorCode.INVALID_SIZE_CHOICE,
                "option_id is not an offered option of this request, or chosen_size does not match it.",
            )
        cap = getattr(claims, "max_amount_cents", None)
        price = money.ensure_cents(record.get("price_cents"), "price_cents", minimum=0)
        if cap is not None and price > cap:
            _decision_denied(ACTOR_PERSONAL_AGENT, request_id, ErrorCode.DELEGATION_LIMIT_EXCEEDED)
            raise DomainError(
                ErrorCode.DELEGATION_LIMIT_EXCEEDED,
                f"The purchase is above the delegated limit of {money.format_cad(cap)}.",
            )
        _require_stock(option)

        proposed_at = store.now_iso()
        try:
            store.update_item(
                store.sizereq_key(request_id),
                set={
                    "status": STATUS_PROPOSED,
                    "proposed_option_id": option_id,
                    "proposed_size": size,
                    "rationale_public": body.rationale_public,
                    "confidence": body.confidence,
                    "proposed_at": proposed_at,
                },
                condition="#status = :open OR #status = :proposed",
                values={":open": STATUS_OPEN, ":proposed": STATUS_PROPOSED},
            )
        except store.ConditionFailed:
            continue  # decided (or removed) since the read: look again
        _emit(
            owner,
            "size_recommended",
            {
                "size_request_id": request_id,
                "option_id": option_id,
                "chosen_size": size,
                "option_type": option["type"],
                "location_name": option["location_name"],
                "eta_date": option["eta_date"],
                "rationale_public": body.rationale_public,
                "confidence": body.confidence,
            },
            ACTOR_PERSONAL_AGENT,
        )
        return SizeProposalResponse(
            size_request_id=request_id,
            expires_at=record["expires_at"],
            summary=_option_summary(record, option_id),
        )
    raise DomainError(ErrorCode.TRANSACTION_CONFLICT, "The size request changed; try again.")


# ------------------------------------------------------------- owner view and confirmation


def get_proposal(customer_id: str, size_request_id: str) -> dict[str, Any]:
    """The owner's purchase card (``GET /api/purchases/{id}``, session token, owner-checked).

    Server data only: product, color, size, option type, location, ETA, deadline flag, price,
    the fit headline, the bot's public rationale and confidence, status and expiry. A request
    with no proposal yet is NOT_FOUND; another customer's is NOT_AUTHORIZED plus an audit event.
    """
    owner = _clean_record_id(customer_id, "customer_id")
    request_id = _clean_request_id(size_request_id)
    record = _load_request(owner, request_id, actor=owner)
    status = record.get("status")
    if status == STATUS_DECIDED:
        option_id = record["chosen_option_id"]
    elif status == STATUS_PROPOSED:
        option_id = record["proposed_option_id"]
    else:
        raise DomainError(ErrorCode.NOT_FOUND, "No purchase has been proposed for this request yet.")
    view = {
        "size_request_id": request_id,
        "status": status,
        **_option_summary(record, option_id),
        "fit_signal": record.get("fit_signal"),
        "rationale_public": record.get("rationale_public", ""),
        "confidence": record.get("confidence"),
        "proposed_at": record.get("proposed_at"),
        "expires_at": record["expires_at"],
        "expired": status != STATUS_DECIDED and _is_expired(record),
        "order_id": record.get("order_id"),
        "next_step": (record.get("result") or {}).get("next_step"),
    }
    return store.json_safe(view)


def _replay_decision(record: Mapping[str, Any]) -> SizeDecisionResponse:
    result = record.get("result")
    if not isinstance(result, Mapping):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "the size request has no recorded order")
    return SizeDecisionResponse.model_validate({**result, "replayed": True})


def _confirm_ops(
    record: Mapping[str, Any], option_id: str, plan: Mapping[str, Any], decided_at: str
) -> list[Any]:
    option = plan["option"]
    meta, line = plan["items"]
    return [
        store.TxUpdate(
            key=store.sizereq_key(record["size_request_id"]),
            set={
                "status": STATUS_DECIDED,
                "decided_at": decided_at,
                "order_id": plan["order_id"],
                "chosen_option_id": option_id,
                "chosen_size": option["size"],
                "approved_by": APPROVED_BY,
                "result": plan["result"].model_dump(mode="json"),
            },
            condition="#status = :proposed AND #proposed_option_id = :option",
            values={":proposed": STATUS_PROPOSED, ":option": option_id},
            label="request",
        ),
        store.TxPut(item=meta, condition="attribute_not_exists(#pk)", label="order"),
        store.TxPut(item=line, condition="attribute_not_exists(#pk)", label="line"),
        store.TxUpdate(
            key=store.inventory_key(option["sku"], option["location_id"]),
            add={"atp": -1, "version": 1},
            condition="#atp >= :one",
            values={":one": 1},
            label="inventory",
        ),
    ]


def confirm_purchase(
    customer_id: str, size_request_id: str, option_id: str | None = None
) -> SizeDecisionResponse:
    """The owner's click (``POST /api/purchases/{id}/confirm``, session token): create the order.

    No proposal yet: APPROVAL_REQUIRED. ``option_id`` (the card's option) must be the proposed
    one, else IDEMPOTENCY_CONFLICT; past ``expires_at``: REQUEST_EXPIRED; stock gone:
    STOCK_CHANGED with nothing written. Then ONE ``TransactWriteItems`` (module docstring).
    Idempotent: an already decided request returns its order with ``replayed: true``, no writes,
    no events. Emits ``size_confirmed`` (``approved_by: owner_click``).
    """
    owner = _clean_record_id(customer_id, "customer_id")
    request_id = _clean_request_id(size_request_id)
    expected = _clean_option_id(option_id)

    for attempt in range(1, _TRANSACTION_ATTEMPTS + 1):
        record = _load_request(owner, request_id, actor=owner)
        status = record.get("status")
        if status == STATUS_DECIDED:
            if expected is not None and expected != record.get("chosen_option_id"):
                raise DomainError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "This purchase was already confirmed with a different option.",
                )
            return _replay_decision(record)
        if status != STATUS_PROPOSED:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Nothing has been proposed for this size request yet.",
            )
        proposed = record["proposed_option_id"]
        expected = expected or proposed  # pin what the owner reviewed across retries
        if expected != proposed:
            raise DomainError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "The proposal changed after you reviewed it. Review it again.",
            )
        _require_unexpired(record)
        if proposed not in (record.get("offered") or {}):
            raise DomainError(ErrorCode.INTERNAL_ERROR, "the proposed option is not on the request")
        _require_stock(record["offered"][proposed])

        decided_at = store.now_iso()
        plan = build_purchase(record=record, option_id=proposed, ordered_on=clock.demo_today())
        try:
            store.transact_write(_confirm_ops(record, proposed, plan, decided_at))
        except store.TransactionCanceled as exc:
            failed = set(exc.failed())
            if failed & {"request", "order", "line"}:  # another call decided it first: re-read
                continue
            if "inventory" in failed:
                raise DomainError(
                    ErrorCode.STOCK_CHANGED,
                    "That option is no longer in stock. Ask for sizes again.",
                ) from None
            if exc.transient() and attempt < _TRANSACTION_ATTEMPTS:
                _pause()
                continue
            raise DomainError(
                ErrorCode.TRANSACTION_CONFLICT, "The purchase hit a conflict; try again."
            ) from exc

        result: SizeDecisionResponse = plan["result"]
        option = plan["option"]
        _emit(
            owner,
            "size_confirmed",
            {
                "size_request_id": request_id,
                "order_id": result.order_id,
                "status": result.status,
                "size": option["size"],
                "location_name": option["location_name"],
                "eta_date": option["eta_date"],
                "price_cents": record["price_cents"],
                "fulfillment": result.fulfillment.model_dump(),
                "next_step": result.next_step,
                "approved_by": APPROVED_BY,
            },
            ACTOR_GATEWAY,
        )
        return result
    raise DomainError(ErrorCode.TRANSACTION_CONFLICT, "The purchase hit a conflict; try again.")


# ------------------------------------------------------------------ demo controls (5.7)


def simulate_delivery(customer_id: str, order_id: str) -> dict[str, Any]:
    """Demo control: mark an order created by ``confirm_purchase`` as delivered on the DEMO_NOW date.

    ORDER_NOT_FOUND; NOT_AUTHORIZED + audit for another customer's order; VALIDATION_ERROR for
    any order whose channel is not ``agent`` (fixture orders such as DEMO-1007 keep their null
    date). Returns ``{"order_id", "delivered_at", "already_delivered"}``; a repeat writes nothing.
    """
    owner = _clean_record_id(customer_id, "customer_id")
    oid = _clean_record_id(order_id, "order_id")
    key = store.order_key(oid)
    order = store.get_item(key, consistent=True)
    if order is None:
        raise DomainError(ErrorCode.ORDER_NOT_FOUND, "Order not found.")
    if order.get("customer_id") != owner:
        audit.access_denied(owner, "order", oid)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Order not found for this customer.")
    if order.get("channel") != CHANNEL_AGENT:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "Only orders placed through the gateway can be marked delivered.",
        )
    delivered = order.get("delivered_at")
    if delivered:
        return {"order_id": oid, "delivered_at": delivered, "already_delivered": True}
    delivered = clock.demo_today().isoformat()
    try:
        store.update_item(
            key,
            set={"delivered_at": delivered},
            condition="#channel = :agent",
            values={":agent": CHANNEL_AGENT},
        )
    except store.ConditionFailed:
        raise DomainError(ErrorCode.ORDER_NOT_FOUND, "Order not found.") from None
    return {"order_id": oid, "delivered_at": delivered, "already_delivered": False}


# ---------------------------------------------------------------------- write-back (5.4)


def _txn_value(txn: Mapping[str, Any], name: str) -> Any:
    if name in txn:
        return txn[name]
    nested = txn.get("option")
    return nested.get(name) if isinstance(nested, Mapping) else None


def _policy_direction(txn: Mapping[str, Any]) -> str | None:
    """``size_direction`` of the transaction's policy check, best effort (None when unknown)."""
    try:
        check_id = _txn_value(txn, "policy_check_id")
        if not check_id and _txn_value(txn, "quote_id"):
            quote = store.get_item(store.quote_key(txn["quote_id"]))
            check_id = quote.get("policy_check_id") if quote else None
        if not check_id:
            return None
        check = store.get_item(store.pchk_key(check_id))
        direction = check.get("size_direction") if check else None
        return direction if isinstance(direction, str) else None
    except Exception:
        return None


def _exchange_reason(txn: Mapping[str, Any], original_size: str, new_size: str) -> str:
    """F.5: the policy check's size direction, else the sizes (larger = too small), else OTHER."""
    direction = _txn_value(txn, "size_direction") or _policy_direction(txn)
    if direction == "too_small":
        return "TOO_SMALL"
    if direction == "too_big":
        return "TOO_BIG"
    try:
        old, new = Decimal(original_size), Decimal(new_size)
    except (InvalidOperation, TypeError):
        return "OTHER"
    if new > old:
        return "TOO_SMALL"
    if new < old:
        return "TOO_BIG"
    return "OTHER"


def build_memory_update(
    txn: Mapping[str, Any], *, outcome: str = OUTCOME_CREATED, provisional: bool | None = None
) -> dict[str, Any] | None:
    """The write-back payload (addendum 5.4) of an exchange transaction; None for a return.

    A thin wrapper: it gathers the order line and the reason, then ``memory_updates.build_update``
    builds the payload and its ``update_id``. ``exchange_created`` is always provisional (ALDO
    cannot know the owner keeps the pair); ``exchange_completed_kept`` is not. The payload has
    no typical size, no hint, no other retailer or event.
    """
    option_type = _txn_value(txn, "option_type")
    if not (isinstance(option_type, str) and option_type.startswith("exchange_")):
        return None
    customer_id, order_id, line_id = txn.get("customer_id"), txn.get("order_id"), txn.get("line_id")
    new_size = _txn_value(txn, "replacement_size")
    line = store.get_item(store.line_key(order_id, line_id), consistent=True)
    if line is None or not isinstance(new_size, str):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "the exchange has no order line or new size")
    original_size = line["size"]
    return memory_updates.build_update(
        customer_id=customer_id,
        order_id=order_id,
        line=line,
        txn=txn,
        original_size=original_size,
        new_size=new_size,
        reason=_exchange_reason(txn, original_size, new_size),
        outcome=outcome,
        provisional=(outcome == OUTCOME_CREATED) if provisional is None else provisional,
    )


def enqueue_memory_update(update: Mapping[str, Any]) -> bool:
    """Idempotent outbox put; False when the update was already queued (or delivered)."""
    item = memory_updates.outbox_item(update, update["owner_ref"])
    try:
        store.put_item(item, condition="attribute_not_exists(#pk)")
    except store.ConditionFailed:
        return False
    return True


def _outbox_update(item: Mapping[str, Any]) -> Mapping[str, Any]:
    update = item.get("payload", item.get("update"))
    if not isinstance(update, Mapping) or not isinstance(update.get("update_id"), str):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "an outbox item has no update")
    return update


def _error_text(exc: BaseException) -> str:
    """A short, secret-free description of a delivery failure for the outbox record."""
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def _emit_memory_updated(customer_id: str, update: Mapping[str, Any]) -> None:
    fields = (
        "update_id",
        "style_id",
        "product_name",
        "original_size",
        "new_size",
        "reason",
        "outcome",
        "provisional",
    )
    _emit(
        customer_id,
        "memory_updated",
        {field: update[field] for field in fields if field in update},
        ACTOR_PERSONAL_AGENT,
    )


def _won_delivery(update_id: str) -> bool:
    """Mark the item delivered; True only for the caller that made the pending -> delivered change."""
    try:
        return memory_updates.mark_delivered(update_id) is not False
    except store.ConditionFailed:
        return False


def flush_outbox(
    customer_id: str | None = None, limit: int = 10, client: PersonalAgentClient | None = None
) -> int:
    """Send pending write-backs to the personal agent; returns the delivered count. NEVER raises.

    ``memory_updates.list_pending`` supplies the items, ``client`` (default: the transport of
    ``GATEWAY_TRANSPORT``) sends each one, ``mark_delivered`` / ``mark_failed`` record the
    outcome (attempts are counted there). The receiver answers ``applied`` or ``duplicate``;
    either way the item is delivered, and only the caller that wins the ``pending -> delivered``
    change emits ``memory_updated``, so the event appears exactly once. A transport that cannot
    be built (not configured) leaves the items pending without burning attempts.
    """
    delivered = 0
    try:
        bound = max(1, min(int(limit), PENDING_LIMIT_CAP))
        sender = client if client is not None else get_client()
        pending = list(memory_updates.list_pending(customer_id, limit=bound))
    except Exception:
        logger.warning("write-back outbox could not be read or the client built", exc_info=True)
        return 0

    for item in pending:
        update_id = str(item.get("update_id") or "")
        try:
            update = _outbox_update(item)
            update_id = update["update_id"]
            sender.send_memory_update(dict(update))
            if _won_delivery(update_id):
                owner = item.get("customer_id") or update.get("owner_ref")
                if isinstance(owner, str) and owner:
                    _emit_memory_updated(owner, update)
            delivered += 1
        except Exception as exc:
            logger.warning("write-back delivery failed: %s", type(exc).__name__)
            try:
                memory_updates.mark_failed(update_id, _error_text(exc))
            except Exception:
                logger.warning("write-back failure could not be recorded", exc_info=True)
    return delivered


def simulate_pickup(customer_id: str, transaction_id: str) -> dict[str, Any]:
    """Demo control: the owner collected and kept the exchanged pair (non-provisional write-back).

    NOT_FOUND; NOT_AUTHORIZED + audit for another customer's transaction; VALIDATION_ERROR for a
    return. Queues the ``exchange_completed_kept`` update (``provisional: false``) in the
    outbox, marks the transaction ``pickup_confirmed_at`` once, and flushes the outbox. A repeat
    returns the same ``update_id`` with ``already_done: true`` and queues nothing new.
    """
    owner = _clean_record_id(customer_id, "customer_id")
    tid = _clean_record_id(transaction_id, "transaction_id")
    key = store.txn_key(tid)
    txn = store.get_item(key, consistent=True)
    if txn is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Transaction not found.")
    if txn.get("customer_id") != owner:
        audit.access_denied(owner, "transaction", tid)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Transaction not found for this customer.")
    update = build_memory_update(txn, outcome=OUTCOME_KEPT, provisional=False)
    if update is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "Only an exchange can be picked up.")

    enqueue_memory_update(update)  # idempotent: the update id derives from the transaction
    try:
        store.update_item(
            key,
            set={"pickup_confirmed_at": store.now_iso()},
            condition="attribute_not_exists(#pickup_confirmed_at)",
        )
        already_done = False
    except store.ConditionFailed:
        already_done = True
    flush_outbox(owner, limit=3)
    return {
        "transaction_id": tid,
        "update_id": update["update_id"],
        "outcome": OUTCOME_KEPT,
        "already_done": already_done,
    }


# --------------------------------------------------------------------------- agent card


def agent_card(base_url: str) -> dict[str, Any]:
    """The public A2A-style agent card (served without authentication, contracts.md F.5).

    Skills ``size_assist``, ``size_decision`` and ``memory_writeback``; the bearer scheme with
    the delegated scope; provider and label say it is a prototype on simulated data.
    """
    if not isinstance(base_url, str) or not base_url.strip().startswith(("http://", "https://")):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "base_url must start with http:// or https://")
    base = base_url.strip().rstrip("/")
    settings = config.get_settings()
    cap = money.format_cad(settings.delegation_max_cents)
    minutes = max(1, settings.delegation_ttl_seconds // 60)
    modes = ["application/json"]
    return {
        "name": "ALDO Fit Gateway",
        "description": (
            "Purchase-time sizing for personal agents such as ALDO bot: the ALDO_CA fit signal "
            "for a style, live stock and delivery options per size, then a proposed purchase "
            "that only the owner can confirm. After an exchange ALDO pushes the size that "
            "worked back to the agent. Synthetic demo data; orders are simulated and no "
            "payment is taken."
        ),
        "url": f"{base}/api/gateway",
        "version": "1.0.0",
        "provider": {"organization": "ALDO (prototype, simulated data)", "url": base},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": modes,
        "defaultOutputModes": modes,
        "securitySchemes": {
            "delegatedBearer": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Short-lived delegated token minted by the owner's session "
                    f"(scope delegated_gateway, {minutes} minutes, {cap} cap). "
                    "Session tokens are rejected."
                ),
            }
        },
        "security": [{"delegatedBearer": []}],
        "skills": [
            {
                "id": "size_assist",
                "name": "Size assist",
                "description": (
                    "POST /api/gateway/size-assist. Returns the ALDO_CA fit signal for a "
                    "style, the sizes in stock with delivery options and a deadline flag, and "
                    "a question for the personal agent."
                ),
                "tags": ["sizing", "fit", "stock"],
                "examples": ["Help me size this style; my owner needs it by Saturday."],
                "inputModes": modes,
                "outputModes": modes,
            },
            {
                "id": "size_decision",
                "name": "Size decision",
                "description": (
                    "POST /api/gateway/size-decision. The personal agent proposes one offered "
                    "size and option; ALDO checks it and records a proposal. The owner confirms "
                    "it in the app, and only then is a simulated order created."
                ),
                "tags": ["purchase", "owner-confirmation"],
                "examples": ["Propose size 8.0 for store pickup."],
                "inputModes": modes,
                "outputModes": modes,
            },
            {
                "id": "memory_writeback",
                "name": "Fit memory write-back",
                "description": (
                    "ALDO pushes the size that worked after an exchange to the personal agent "
                    "(POST /api/personal-agent/memory-updates), signed with X-ALDO-Signature "
                    "(HMAC-SHA256 of the body). Provisional until the owner keeps the pair; "
                    "idempotent on update_id."
                ),
                "tags": ["memory", "fit"],
                "examples": [],
                "inputModes": modes,
                "outputModes": modes,
            },
        ],
        "label": "Prototype, simulated ALDO data, not official policy",
        "provenance": "synthetic demo",
    }
