"""API side of the hand-off: start a run in the worker without waiting for it.

Deployed: ``lambda:InvokeFunction`` with ``InvocationType="Event"`` on ``WORKER_FUNCTION_NAME``.
Local (``AGENT_MODE=local``): the worker runs inline in a daemon thread, so the HTTP request
still returns ``202`` immediately.

Every failure is reported as a retryable ``UPSTREAM_TIMEOUT`` ``DomainError``; the caller
decides what to undo (the run it just created). The cause is logged, never returned.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

import boto3
from botocore.config import Config

from returns_domain import config
from returns_domain.errors import DomainError, ErrorCode

from .log import logger

LOCAL_MODE = "local"
ENQUEUE_FAILED = "The run could not be started. Please try again."

_lambda_client: Any = None


def agent_mode() -> str:
    """``agentcore`` (default) or ``local``; read from the environment on every call."""
    return (os.environ.get("AGENT_MODE") or "agentcore").strip().lower()


def _client() -> Any:
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client(
            "lambda",
            region_name=config.get_settings().region,
            # One attempt: a duplicate invoke is harmless (claim_run) but a hung call is not.
            config=Config(
                connect_timeout=3,
                read_timeout=10,
                retries={"mode": "standard", "max_attempts": 1},
            ),
        )
    return _lambda_client


def reset_client() -> None:
    """Forget the cached Lambda client (tests that start a fresh moto session)."""
    global _lambda_client
    _lambda_client = None


def enqueue_async(payload: dict[str, Any]) -> None:
    """Start the worker for ``payload`` (contracts.md C.6, plus ``agent``) and return."""
    if agent_mode() == LOCAL_MODE:
        _start_inline(payload)
        return
    function_name = os.environ.get("WORKER_FUNCTION_NAME", "").strip()
    if not function_name:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "WORKER_FUNCTION_NAME is not configured")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        response = _client().invoke(FunctionName=function_name, InvocationType="Event", Payload=body)
        accepted = response.get("StatusCode") == 202
    except Exception as exc:
        logger.exception("worker_invoke_failed", run_id=payload.get("run_id"))
        raise DomainError(ErrorCode.UPSTREAM_TIMEOUT, ENQUEUE_FAILED, retryable=True) from exc
    if not accepted:
        logger.error("worker_invoke_not_accepted", run_id=payload.get("run_id"))
        raise DomainError(ErrorCode.UPSTREAM_TIMEOUT, ENQUEUE_FAILED, retryable=True)


def _start_inline(payload: dict[str, Any]) -> None:
    thread = threading.Thread(
        target=_run_inline,
        args=(dict(payload),),
        name=f"worker-{payload.get('run_id', 'run')}",
        daemon=True,
    )
    thread.start()


def _run_inline(payload: dict[str, Any]) -> None:
    from .handler import process  # imported late: handler imports this module's helpers

    try:
        process(payload)
    except Exception:  # a daemon thread must never die silently
        logger.exception("inline_worker_failed", run_id=payload.get("run_id"))
