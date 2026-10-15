"""Audit log: who attempted what and whether it was allowed (requirement 1.4).

Items are ``AUDIT#{utc date}/{iso ts}#{uuid}``; the date comes from ``clock.now_utc()`` (the
real wall clock, monkeypatchable). Writing an audit event is best effort: a failure is logged
and swallowed, because an audit problem must never turn a denial into a crash or an allowed
operation into a failure.

Action vocabulary used by the services: ``access_denied`` (ownership failures, refs
``resource_type`` and ``resource_id``), ``execute_denied`` (APPROVAL_REQUIRED paths),
``approve``, ``execute``, ``reset`` and ``case_created``. The gateway adds its own actions.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Mapping

from . import clock, store, trace

logger = logging.getLogger(__name__)

OUTCOMES: tuple[str, ...] = ("allowed", "denied")


def write_audit(actor: str, action: str, outcome: str, refs: Mapping[str, Any] | None = None) -> None:
    """Record one audit event. Never raises."""
    try:
        now = clock.now_utc()
        ts = clock.iso(now)
        # Time-ordered unique suffix: events of the same second still list in the order they happened.
        uid = f"{time.time_ns():016x}{uuid.uuid4().hex[:8]}"
        pk, sk = store.audit_key(now.date().isoformat(), ts, uid)
        store.put_item(
            {
                "pk": pk,
                "sk": sk,
                "actor": str(actor)[:128],
                "action": str(action)[:64],
                "outcome": str(outcome)[:16],
                "refs": trace.redact(dict(refs or {})),
                "ts": ts,
            }
        )
    except Exception:
        logger.warning("audit write failed", extra={"audit_action": action}, exc_info=True)


#: Alias used by contracts.md A.8 (``audit.write``).
write = write_audit


def access_denied(
    actor: str, resource_type: str, resource_id: str, run_id: str | None = None
) -> None:
    """The standard audit event for an ownership failure (refs: resource type and id, run id)."""
    refs: dict[str, Any] = {"resource_type": resource_type, "resource_id": resource_id}
    if run_id:
        refs["run_id"] = run_id
    write_audit(actor, "access_denied", "denied", refs)


def list_audit(
    day: str | None = None,
    *,
    action: str | None = None,
    outcome: str | None = None,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    """Audit events of a UTC day (default today), oldest first, optionally filtered.

    For tests and the eval script, which assert that a denial left an audit event.
    """
    date_text = day or clock.now_utc().date().isoformat()
    rows = store.query(f"AUDIT#{date_text}")
    return [
        row
        for row in rows
        if (action is None or row.get("action") == action)
        and (outcome is None or row.get("outcome") == outcome)
        and (actor is None or row.get("actor") == actor)
    ]
