"""Run trace: ordered, redacted events per run, plus the per-owner agent-to-agent (A2A) stream.

``emit`` bumps the run's atomic ``seq`` counter and then writes ``EVT#{seq:06d}``. Those are two
writes, so event N+1 can become visible before N; readers therefore get only the CONTIGUOUS
prefix after their cursor (``next_after`` = last returned seq) and never skip a gap (G-11).
Events are redacted before they are stored: no tokens, secrets, e-mail addresses or customer
identifiers, every string capped. The A2A stream (``RUN#A2A-{customer_id}``) is one long-lived
run per owner that carries only public fields and is cleared by a demo reset.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Mapping

from pydantic import BaseModel

from . import config, store
from .errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

EVENT_TYPES: tuple[str, ...] = (
    "run_started",
    "tool_call",
    "tool_result",
    "decision",
    "quote_created",
    "approval_received",
    "transaction_created",
    "case_created",
    "assistant_message",
    "run_completed",
    "warning",
    # Fit Gateway pivot (PIVOT_ADDENDUM 5.6)
    "size_assist_requested",
    "fit_signal_shared",
    "size_requested",
    "size_recommended",
    "size_confirmed",
    "memory_updated",
    # Demo v2 (PIVOT_ADDENDUM 11.4): reviews, one-chat handoff, store desk, perks, delivery
    "reviews_shared",
    "handoff_to_returns",
    "handback",
    "store_notified",
    "benefit_applied",
    "delivered",
    # Addendum 12.3: the return workflow used the owner's remembered preferences (counts only)
    "memory_consulted",
)

#: ``aldo_store`` is the simulated store desk ("ALDO Store Desk" in the UI): it is notified when an
#: in-store exchange or return is approved (D7).
ACTORS: tuple[str, ...] = ("personal_agent", "aldo_gateway", "aldo_agent", "system", "aldo_store")
DEFAULT_ACTOR = "aldo_agent"

#: The ONLY keys an event of the agent-to-agent stream may carry (PIVOT_ADDENDUM C3, contracts.md
#: F.6, widened by the orchestrator so the UI and eval can read ``size_request_id``, ``approved_by``
#: and the sizes, plus the demo v2 types of addendum 11.4). ``emit_a2a`` drops everything else, so
#: no private rationale, owner reference, retailer name, review text or token can reach the
#: middle timeline even if a caller passes it, and the redactor still removes any token, secret,
#: e-mail or customer-id key inside the allowed fields.
A2A_PUBLIC_FIELDS: dict[str, frozenset[str]] = {
    "size_assist_requested": frozenset(
        {"size_request_id", "product_query", "color", "deadline_date", "channel_pref"}
    ),
    "fit_signal_shared": frozenset(
        {
            "size_request_id",
            "style_id",
            "product_name",
            "signal",
            "too_small_share",
            "n_fit",
            "scope",
            "provenance",
            "data_source",
            "query_execution_ids",
        }
    ),
    "size_requested": frozenset(
        {"size_request_id", "sizes", "option_count", "question", "expires_at"}
    ),
    "size_recommended": frozenset(
        {
            "size_request_id",
            "option_id",
            "chosen_size",
            "option_type",
            "location_name",
            "eta_date",
            "rationale_public",
            "confidence",
        }
    ),
    "size_confirmed": frozenset(
        {
            "size_request_id",
            "order_id",
            "status",
            "fulfillment",
            "next_step",
            "size",
            "location_name",
            "eta_date",
            "price_cents",
            "approved_by",  # "owner_click": the purchase exists only after the owner's confirm click
        }
    ),
    "memory_updated": frozenset(
        {
            "update_id",
            "style_id",
            "product_name",
            "original_size",
            "new_size",
            "reason",
            "outcome",
            "provisional",
            "fit_feedback",  # "size_ok" on a PREFERENCE (colour) exchange
        }
    ),
    "transaction_created": frozenset(
        {
            "transaction_id",
            "state",
            "option_type",
            "order_id",
            "replayed",
            "original_size",
            "new_size",
            "location_id",
            "fee_cents",
        }
    ),
    # Demo v2 (PIVOT_ADDENDUM 11.4). No customer id and no review text in any of them.
    "quote_created": frozenset({"quote_id", "primary_option_type", "option_types"}),
    "reviews_shared": frozenset(
        {
            "style_id",
            "product_name",
            "n_reviews",
            "n_flagged",  # reviews the server flagged as instruction-like text (the bot excludes them)
            "flagged_review_ids",
            "provenance",
        }
    ),
    # The switch between the two workflows (personal agent purchase, ALDO agent return) is internal routing (addendum 12.1): `mode` is
    # the conversation mode afterwards ("returns" / "bot"); no field names a second agent.
    "handoff_to_returns": frozenset({"order_id", "product_name", "size", "color", "mode"}),
    "handback": frozenset({"order_id", "outcome", "transaction_id", "case_id", "mode"}),
    # Addendum 12.3: only HOW MUCH memory was used and which kinds of fields, never their values.
    "memory_consulted": frozenset({"n_entries", "fields"}),
    "store_notified": frozenset({"store_name", "eta_minutes", "summary"}),
    "benefit_applied": frozenset(
        {
            "type",  # the benefit kind, e.g. "free_return_shipping" (same value as "benefit")
            "label",  # "Platinum free return shipping"
            "benefit",
            "tier_label",
            "saved_cents",
            "fee_cents",
            "used",
            "per_month",
            "period",
            "order_id",
            "transaction_id",
        }
    ),
    "delivered": frozenset({"order_id", "delivered_at", "product_name", "size"}),
}

#: Keys whose name says "credential or identity". Matched on snake_case words, so
#: ``input_tokens`` and ``output_tokens`` (metrics) survive while ``token``, ``access_token``,
#: ``idToken`` (camelCase), ``customer_id``, ``owner_ref``, ``email`` and ``signature`` do not.
REDACT_KEYS = re.compile(
    r"(?<![a-z0-9])"
    r"(token|secret|authorization|password|passwd|api[_\-]?key|admin[_\-]?key|customer[_\-]?id"
    r"|owner[_\-]?ref|owner[_\-]?id|e?mail|signature|credentials?)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Values that must not appear even inside free text (a model can echo them).
_TOKEN_SHAPE = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")
_CUSTOMER_ID_SHAPE = re.compile(r"\bDEMO-CUST-[A-Za-z0-9-]+")

ASSISTANT_TEXT_CAP = 1500
MAX_DEPTH = 8
MAX_LIST_ITEMS = 50
EVENT_PAGE_LIMIT = 100


class EventPage(BaseModel):
    run_id: str
    status: str
    events: list[dict[str, Any]]
    next_after: int


# ---------------------------------------------------------------------------- redaction


def is_sensitive_key(key: object) -> bool:
    return bool(REDACT_KEYS.search(_CAMEL_BOUNDARY.sub("_", str(key))))


def redact(payload: Any, *, caps: Mapping[str, int] | None = None) -> Any:
    """Drop sensitive keys, scrub token- and customer-id-shaped text, cap strings, make JSON-safe.

    Strings are capped at ``Settings.event_string_cap`` (500); ``caps`` overrides the cap for
    named keys (``emit`` uses it for the 1500-character assistant text). Lists are limited to
    50 items and nesting to 8 levels. Never raises.
    """
    default_cap = config.get_settings().event_string_cap
    try:
        return _redact(store.json_safe(payload), default_cap, caps or {}, 0)
    except Exception:  # redaction must never take a run down
        logger.warning("trace redaction fell back to an empty payload", exc_info=True)
        return {}


def _redact(value: Any, cap: int, caps: Mapping[str, int], depth: int) -> Any:
    if depth > MAX_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if is_sensitive_key(key):
                continue
            out[str(key)] = _redact(item, caps.get(str(key), cap), caps, depth + 1)
        return out
    if isinstance(value, list):
        return [_redact(item, cap, caps, depth + 1) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, str):
        text = _CUSTOMER_ID_SHAPE.sub("[customer]", _TOKEN_SHAPE.sub("[redacted]", value))
        return text if len(text) <= cap else text[: max(cap - 1, 0)] + "…"
    return value


# ------------------------------------------------------------------------------- emitting


def _check(type_: object, actor: object) -> None:
    if type_ not in EVENT_TYPES:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"unknown event type {type_!r}")
    if actor not in ACTORS:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"actor must be one of: {', '.join(ACTORS)}")


def emit(run_id: str, type: str, payload: dict, *, actor: str = DEFAULT_ACTOR) -> int:  # noqa: A002
    """Append an event to a run and return its seq. Unknown types or actors raise VALIDATION_ERROR.

    The run must exist (``runs.create_run``); a missing run raises NOT_FOUND. The payload is
    redacted first; ``actor`` is one of personal_agent, aldo_gateway, aldo_agent, system.
    """
    _check(type, actor)
    caps = {"text": ASSISTANT_TEXT_CAP} if type == "assistant_message" else None
    body = redact(payload, caps=caps)
    try:
        counter = store.update_item(store.run_key(run_id), add={"seq": 1})
    except store.ConditionFailed:
        raise DomainError(ErrorCode.NOT_FOUND, "Run not found.") from None
    seq = int(counter["seq"])
    pk, sk = store.event_key(run_id, seq)
    item = {"pk": pk, "sk": sk, "seq": seq, "type": type, "actor": actor, "ts": store.now_iso(), "payload": body}
    for attempt in range(3):
        try:
            store.put_item(item)
            break
        except DomainError as exc:
            if not exc.retryable or attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
    return seq


def emit_safe(run_id: str, type: str, payload: dict, *, actor: str = DEFAULT_ACTOR) -> int | None:  # noqa: A002
    """``emit`` for callers that already committed their business write: failures are logged."""
    try:
        return emit(run_id, type, payload, actor=actor)
    except Exception:
        logger.warning("trace emit failed", extra={"run_id": run_id, "event_type": type}, exc_info=True)
        return None


def list_events(run_id: str, after: int = 0, limit: int = EVENT_PAGE_LIMIT) -> EventPage:
    """Events with ``seq > after`` as the contiguous prefix (stops at the first gap)."""
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "after must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "limit must be between 1 and 500")
    events = _contiguous_events(run_id, after, limit)
    meta = store.get_item(store.run_key(run_id))  # read after the events: see the module note
    if meta is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Run not found.")
    return EventPage(
        run_id=run_id,
        status=str(meta.get("status", "unknown")),
        events=events,
        next_after=events[-1]["seq"] if events else after,
    )


def _contiguous_events(run_id: str, after: int, limit: int) -> list[dict[str, Any]]:
    rows = store.query(
        store.run_key(run_id)[0],
        sk_between=(store.event_key(run_id, after + 1)[1], store.event_key(run_id, 999_999)[1]),
        limit=limit,
    )
    events: list[dict[str, Any]] = []
    expected = after + 1
    for row in rows:
        if row.get("seq") != expected:
            break  # a gap: a later event is visible but an earlier one is not (yet)
        events.append(
            {
                "seq": row["seq"],
                "type": row["type"],
                "ts": row["ts"],
                "actor": row.get("actor", DEFAULT_ACTOR),
                "payload": row.get("payload", {}),
            }
        )
        expected += 1
    return events


# ------------------------------------------------------------------------- A2A stream


def a2a_run_id(customer_id: str) -> str:
    """Run id of the owner's A2A stream: item ``RUN#A2A-{customer_id}/META``."""
    return "A2A-" + customer_id


def _ensure_a2a_meta(customer_id: str) -> None:
    try:
        pk, sk = store.run_key(a2a_run_id(customer_id))
        store.put_item(
            {
                "pk": pk,
                "sk": sk,
                "run_id": a2a_run_id(customer_id),
                "customer_id": customer_id,
                "kind": "a2a",
                "status": "open",
                "seq": 0,
                "created_at": store.now_iso(),
            },
            condition="attribute_not_exists(#pk)",
        )
    except store.ConditionFailed:
        pass  # another writer created it first


def emit_a2a(customer_id: str, type: str, payload: Mapping[str, Any], *, actor: str) -> int:  # noqa: A002
    """Append to the owner's A2A stream; only the public fields of ``A2A_PUBLIC_FIELDS`` are kept.

    ``type`` must be a key of ``A2A_PUBLIC_FIELDS`` (else VALIDATION_ERROR); keys outside its
    allowlist are dropped before redaction, so the middle timeline can never carry a private
    rationale, a retailer name, an owner reference or a token. The stream's META (seq 0, status
    ``open``) is created on first use.
    """
    allowed = A2A_PUBLIC_FIELDS.get(type)
    if allowed is None:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"{type!r} is not an agent-to-agent event type ({', '.join(A2A_PUBLIC_FIELDS)})",
        )
    if not isinstance(payload, Mapping):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "payload must be an object")
    filtered = {key: value for key, value in payload.items() if key in allowed}
    run_id = a2a_run_id(customer_id)
    try:
        return emit(run_id, type, filtered, actor=actor)
    except DomainError as exc:
        if exc.code != ErrorCode.NOT_FOUND:
            raise
    _ensure_a2a_meta(customer_id)
    return emit(run_id, type, filtered, actor=actor)


def emit_a2a_safe(customer_id: str, type: str, payload: Mapping[str, Any], *, actor: str) -> int | None:  # noqa: A002
    """``emit_a2a`` for callers that already committed their business write: failures are logged."""
    try:
        return emit_a2a(customer_id, type, payload, actor=actor)
    except Exception:
        logger.warning("a2a trace emit failed", extra={"event_type": type}, exc_info=True)
        return None


def list_a2a_events(customer_id: str, after: int = 0, limit: int = EVENT_PAGE_LIMIT) -> EventPage:
    """The owner's A2A events after ``after`` (same contiguous-prefix rule as ``list_events``).

    A stream that has no events yet is an empty page with status ``open``. The page's
    ``run_id`` is the constant ``"A2A"`` so the response carries no customer identifier.
    """
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "after must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "limit must be between 1 and 500")
    run_id = a2a_run_id(customer_id)
    events = _contiguous_events(run_id, after, limit)
    meta = store.get_item(store.run_key(run_id))
    return EventPage(
        run_id="A2A",
        status=str(meta.get("status", "open")) if meta else "open",
        events=events,
        next_after=events[-1]["seq"] if events else after,
    )


#: Name used by the PIVOT_ADDENDUM 9.1 text.
read_a2a = list_a2a_events
