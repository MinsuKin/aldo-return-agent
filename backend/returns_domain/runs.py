"""Run state machine and the per-conversation lease (design-gap G-05, G-06).

A run is ``RUN#{run_id}/META`` with ``status`` ``queued -> running -> completed | error``. Every
transition is a conditional UpdateItem, so whichever side wins ``finish_run`` (the agent runner
or the worker) emits ``run_completed`` exactly once, and a duplicate async invocation cannot
start a second run (``claim_run`` and ``mark_agent_started`` are claimed once).

``finish_run`` accepts ``queued`` as well as ``running`` so the API can mark a run ``error`` when
the async invoke itself failed before any worker claimed it.

The lease on ``CONV#{conv}/META`` (``active_run_id`` + ``active_run_expires_at``) is the "one
active run per conversation" guard. It expires on its own (330 s by default), so a crashed
Lambda cannot block a conversation for longer than that.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Mapping

from . import audit, clock, config, store, trace
from .errors import DomainError, ErrorCode

RUN_STATUSES: tuple[str, ...] = ("queued", "running", "completed", "error")
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "error")

_KIND = re.compile(r"^[a-z][a-z_]{0,31}$")
_DEFAULT_METRICS: dict[str, Any] = {
    "rounds": 0,
    "tool_count": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_latency_ms": 0,
    "model_id_used": "",
    "fallback_used": False,
    "error_code": None,
}


def create_run(customer_id: str, conv_id: str, run_id: str, kind: str) -> bool:
    """Create ``RUN#{run_id}/META`` as ``queued`` (``seq`` 0). False when the run already exists.

    ``kind`` is set by server code only (customer, approval, requote); never from a request body.
    """
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "run kind has an invalid format")
    pk, sk = store.run_key(run_id)
    try:
        store.put_item(
            {
                "pk": pk,
                "sk": sk,
                "run_id": run_id,
                "customer_id": customer_id,
                "conv_id": conv_id,
                "kind": kind,
                "status": "queued",
                "seq": 0,
                "created_at": store.now_iso(),
            },
            condition="attribute_not_exists(#pk)",
        )
    except store.ConditionFailed:
        return False
    return True


def claim_run(run_id: str) -> bool:
    """Worker: ``queued -> running``. False when the run was already claimed (or is finished)."""
    try:
        store.update_item(
            store.run_key(run_id),
            set={"status": "running", "claimed_at": store.now_iso()},
            condition="#status = :queued",
            values={"queued": "queued"},
            return_new=False,
        )
    except store.ConditionFailed:
        return False
    return True


def mark_agent_started(run_id: str) -> bool:
    """Runner: set ``agent_started_at`` once. False means another invocation already started it."""
    try:
        store.update_item(
            store.run_key(run_id),
            set={"agent_started_at": store.now_iso()},
            condition="attribute_not_exists(#agent_started_at)",
            return_new=False,
        )
    except store.ConditionFailed:
        return False
    return True


def finish_run(run_id: str, status: str, metrics: Mapping[str, Any] | None = None) -> bool:
    """``queued|running -> completed|error``. Only the winner writes, emits ``run_completed``
    and releases the conversation lease; the loser returns False and writes nothing."""
    if status not in TERMINAL_STATUSES:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "status must be 'completed' or 'error'")
    safe_metrics = store.json_safe(dict(metrics or {}))
    try:
        item = store.update_item(
            store.run_key(run_id),
            set={"status": status, "metrics": safe_metrics, "finished_at": store.now_iso()},
            condition="#status IN (:queued, :running)",
            values={"queued": "queued", "running": "running"},
        )
    except store.ConditionFailed:
        return False
    payload = {"status": status, **{k: safe_metrics.get(k, v) for k, v in _DEFAULT_METRICS.items()}}
    try:
        trace.emit(run_id, "run_completed", payload)
    finally:
        conv_id = (item or {}).get("conv_id")
        if conv_id:
            release_lease(conv_id, run_id)
    return True


def get_run(customer_id: str, run_id: str) -> dict[str, Any]:
    """The run's META for its owner. NOT_FOUND, or NOT_AUTHORIZED plus an ``access_denied`` audit."""
    item = store.get_item(store.run_key(run_id))
    if item is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Run not found.")
    if item.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "run", run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Run not found for this customer.")
    return {k: v for k, v in item.items() if k not in ("pk", "sk")}


# ------------------------------------------------------------------------------- lease


def lease_active(conv_id: str) -> bool:
    """True when the conversation has a lease that has not expired."""
    item = store.get_item(store.conv_key(conv_id))
    if not item:
        return False
    run_id, expires = item.get("active_run_id"), item.get("active_run_expires_at")
    if not run_id or not expires:
        return False
    return clock.parse_iso(expires) > clock.now_utc()


def try_acquire_lease(conv_id: str, run_id: str, ttl_seconds: int | None = None) -> bool:
    """Take the lease unless another run holds an unexpired one (the same run may re-take it)."""
    ttl = config.get_settings().run_lease_seconds if ttl_seconds is None else ttl_seconds
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "ttl_seconds must be a positive integer")
    if not isinstance(run_id, str) or not run_id:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "run_id is required")
    now = clock.now_utc()
    try:
        store.update_item(
            store.conv_key(conv_id),
            set={
                "active_run_id": run_id,
                "active_run_expires_at": clock.iso(now + timedelta(seconds=ttl)),
            },
            condition=(
                "attribute_not_exists(#active_run_id) OR #active_run_expires_at <= :now"
                " OR #active_run_id = :run"
            ),
            values={"now": clock.iso(now), "run": run_id},
            return_new=False,
        )
    except store.ConditionFailed:
        return False
    return True


def release_lease(conv_id: str, run_id: str) -> None:
    """Drop the lease if ``run_id`` still holds it. Never raises for a lost or missing lease."""
    try:
        store.update_item(
            store.conv_key(conv_id),
            remove=["active_run_id", "active_run_expires_at"],
            condition="#active_run_id = :run",
            values={"run": run_id},
            return_new=False,
        )
    except store.ConditionFailed:
        pass
