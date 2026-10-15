"""Tool result envelope and trace emission shared by the tools and the hooks.

Envelope (contracts B.3), always JSON-safe, never raised to the model:

    {"ok": true,  "data": {...}, "source_refs": [...], "provenance": "synthetic"}
    {"ok": false, "error": {"code", "message", "retryable"}, "source_refs": []}

Trace emission is best effort: a failing trace write must never break a tool, and the
events never carry tokens, secrets or customer ids (``trace.emit`` redacts as a second line
of defence; the tools do not put those fields in payloads in the first place).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from returns_domain.errors import ErrorCode

from .context import RunContext
from .log import logger

MAX_SOURCE_REFS = 20

LIMIT_MESSAGE = (
    "The round limit for this run was reached, so this tool is not available. "
    "Call escalate_case once with reason_code LIMIT_REACHED and then stop."
)


def ok(data: Any, *, source_refs: Iterable[str] = (), provenance: str = "synthetic") -> dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "source_refs": list(source_refs)[:MAX_SOURCE_REFS],
        "provenance": provenance,
    }


def fail(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": str(code), "message": message, "retryable": bool(retryable)},
        "source_refs": [],
    }


def limit_envelope() -> dict[str, Any]:
    return fail(ErrorCode.LIMIT_REACHED, LIMIT_MESSAGE, retryable=False)


def emit_event(ctx: RunContext, event_type: str, payload: Mapping[str, Any]) -> None:
    """``trace.emit`` that never raises."""
    try:
        ctx.services.trace.emit(ctx.run_id, event_type, dict(payload))
    except Exception as exc:  # noqa: BLE001 - tracing must not break a run
        logger.warning(
            "trace_emit_failed",
            run_id=ctx.run_id,
            event_type=event_type,
            error_type=type(exc).__name__,
        )


def emit_tool_call(ctx: RunContext, tool_name: str, args: Mapping[str, Any]) -> None:
    emit_event(ctx, "tool_call", {"tool": tool_name, "args": dict(args), "round": ctx.rounds})


def emit_tool_result(
    ctx: RunContext,
    tool_name: str,
    envelope: Mapping[str, Any],
    latency_ms: int,
    evidence: Mapping[str, Any] | None = None,
    provenance: str = "synthetic",
) -> None:
    succeeded = bool(envelope.get("ok"))
    error = envelope.get("error") or {}
    emit_event(
        ctx,
        "tool_result",
        {
            "tool": tool_name,
            "ok": succeeded,
            "code": None if succeeded else error.get("code"),
            "latency_ms": int(latency_ms),
            "evidence": dict(evidence or {}),
            "source_refs": list(envelope.get("source_refs") or [])[:MAX_SOURCE_REFS],
            "provenance": envelope.get("provenance", provenance),
            "round": ctx.rounds,
        },
    )
    logger.info(
        "tool_result",
        run_id=ctx.run_id,
        tool=tool_name,
        latency_ms=int(latency_ms),
        outcome="ok" if succeeded else str(error.get("code")),
        round=ctx.rounds,
    )
