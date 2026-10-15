"""Adapter for the per-owner A2A event stream (the middle panel).

The stream itself lives in the ALDO returns table (``RUN#A2A-{customer_id}``) and is owned by
``returns_domain.trace``. This package writes only ONE event type to it: ``memory_updated``, when
an ALDO write-back is applied. Everything else on the A2A stream is emitted by the gateway.

The default emitter lazily resolves ``returns_domain.trace.emit_a2a``. Assumed signature::

    emit_a2a(customer_id: str, type: str, payload: dict, *, actor: str = ...) -> int

If that function does not exist yet (the persistence stream may still be writing it) the event is
logged and dropped, never raised: a missing trace line must not fail a write-back. Swap the
emitter with ``set_a2a_emitter(fn)`` (same signature) or change ``_default_emitter`` in one place.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .log import logger

A2AEmitter = Callable[..., Any]

_emitter: A2AEmitter | None = None


def set_a2a_emitter(emitter: A2AEmitter | None) -> None:
    """Install ``emitter`` (or None to restore the default lazy ``returns_domain.trace`` lookup)."""
    global _emitter
    _emitter = emitter


def emit_a2a(owner_id: str, event_type: str, payload: dict[str, Any], *, actor: str = "personal_agent") -> Any:
    """Best-effort write to the owner's A2A stream. Returns the sequence number or None."""
    try:
        emitter = _emitter or _default_emitter()
        if emitter is None:
            logger.warning(
                "a2a emitter unavailable, event dropped", extra={"event_type": event_type}
            )
            return None
        return _call(emitter, owner_id, event_type, payload, actor)
    except Exception:  # best effort by design: the write-back already succeeded
        logger.exception("a2a emit failed", extra={"event_type": event_type})
        return None


def _call(emitter: A2AEmitter, owner_id: str, event_type: str, payload: dict[str, Any], actor: str) -> Any:
    try:
        parameters = inspect.signature(emitter).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_actor = "actor" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )
    if accepts_actor:
        return emitter(owner_id, event_type, payload, actor=actor)
    return emitter(owner_id, event_type, payload)


def _default_emitter() -> A2AEmitter | None:
    try:
        from returns_domain import trace  # lazy: pulls in the returns-table store
    except Exception:
        return None
    return getattr(trace, "emit_a2a", None)
