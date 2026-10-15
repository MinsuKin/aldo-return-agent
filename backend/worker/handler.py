"""Worker Lambda: one run per asynchronous invocation (contracts.md C.5/C.6, PIVOT_ADDENDUM 9.6/10.3).

* ``agent == "aldo"`` (default): claim the run (``queued -> running``, conditional), invoke the
  AgentCore runtime with the conversation's session id (or POST to ``AGENT_LOCAL_URL`` when
  ``AGENT_MODE=local``), and make sure the run ends: if the runtime fails, or returns while the run
  is still ``running``, the run is closed as an error with a customer-safe message.
* ``agent == "personal"``: the ALDO bot's run is executed in process by
  ``personal_agent.runner.run``; its own once-only start guard makes a repeat harmless.

Every invocation starts by flushing the write-back outbox (``gateway.flush_outbox(limit=10)``),
which is the retry path for size write-backs the personal agent could not receive earlier.

The client never retries (``max_attempts=1``) and the Lambda is configured with no async retries
(design-gap G-05): an invoke is not idempotent, and a duplicate would run the agent twice.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from api import providers, returns_chat, wiring
from returns_domain import config
from returns_domain.errors import DomainError, ErrorCode

from .enqueue import LOCAL_MODE, agent_mode
from .log import logger

runs = providers.lazy("runs")
conversations = providers.lazy("conversations")
trace = providers.lazy("trace")

AGENT_ALDO = "aldo"
AGENT_PERSONAL = "personal"
AGENTS = (AGENT_ALDO, AGENT_PERSONAL)

DEFAULT_RUNTIME_ARN_PARAM = "/aldo-demo/runtime-arn"
DEFAULT_LOCAL_URL = "http://localhost:8080/invocations"
LOCAL_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
#: infra may set AGENT_RUNTIME_ARN to a syntactically valid stand-in before the runtime exists.
PLACEHOLDER_MARKER = "placeholder"

OUTBOX_FLUSH_LIMIT = 10
DEFAULT_BUDGET_MS = 300_000  # worker timeout, used when no Lambda context is available
MIN_READ_TIMEOUT_S = 30
READ_TIMEOUT_MARGIN_S = 15  # leave time to close the run before the Lambda is killed
CONNECT_TIMEOUT_S = 5

GENERIC_FAILURE_TEXT = "Something went wrong on our side. Please try again."
PERSONAL_FAILURE_TEXT = "ALDO bot ran into a problem on our side. Please try again."

#: AgentCore errors worth reporting as "upstream trouble, try again" (the rest is INTERNAL_ERROR).
_TRANSIENT_CLIENT_CODES = frozenset(
    {
        "ThrottlingException",
        "RetryableConflictException",
        "ServiceQuotaExceededException",
        "InternalServerException",
        "ServiceUnavailableException",
        "RequestTimeoutException",
    }
)
_TIMEOUT_ERRORS = (
    ReadTimeoutError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ConnectionClosedError,
    TimeoutError,
    socket.timeout,
    urllib.error.URLError,
)

_runtime_arn: str | None = None


# ----------------------------------------------------------------------------- entry points


@logger.inject_lambda_context(clear_state=True)
def handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point (``worker.handler.handler``)."""
    return process(event, remaining_ms=_remaining_ms(context))


lambda_handler = handler


def process(payload: Mapping[str, Any], *, remaining_ms: int | None = None) -> dict[str, Any]:
    """Run one payload to the end. Also the body of the inline thread in ``AGENT_MODE=local``."""
    started = time.perf_counter()
    problem = _payload_problem(payload)
    if problem:
        logger.error("worker_payload_rejected", problem=problem)
        return {"status": "error", "error_code": ErrorCode.VALIDATION_ERROR.value}

    wiring.register_local_write_back()
    wiring.flush_outbox_quietly(limit=OUTBOX_FLUSH_LIMIT)

    agent = payload.get("agent") or AGENT_ALDO
    if agent == AGENT_PERSONAL:
        result = _run_personal(payload)
    else:
        result = _run_aldo(payload, remaining_ms)
    logger.info(
        "worker_run",
        run_id=payload["run_id"],
        agent=agent,
        kind=payload.get("kind"),
        outcome=result.get("status"),
        error_code=result.get("error_code"),
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return result


def _payload_problem(payload: object) -> str | None:
    if not isinstance(payload, Mapping):
        return "payload must be an object"
    for name in ("run_id", "conv_id", "customer_id"):
        if not isinstance(payload.get(name), str) or not payload[name]:
            return f"{name} is required"
    if (payload.get("agent") or AGENT_ALDO) not in AGENTS:
        return "unknown agent"
    return None


def _remaining_ms(context: Any) -> int | None:
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(getter):
        return None
    try:
        return int(getter())
    except Exception:
        return None


# ------------------------------------------------------------------------------ ALDO agent


def _run_aldo(payload: Mapping[str, Any], remaining_ms: int | None) -> dict[str, Any]:
    run_id, customer_id = payload["run_id"], payload["customer_id"]
    if not runs.claim_run(run_id):
        return {"run_id": run_id, "status": "duplicate"}
    try:
        session_id = conversations.get_conversation(customer_id, payload["conv_id"])[
            "runtime_session_id"
        ]
        result = invoke_runtime(dict(payload), session_id, remaining_ms)
    except Exception as exc:
        code = error_code_for(exc)
        logger.exception("runtime_invoke_failed", run_id=run_id, error_code=code)
        _fail_if_running(customer_id, run_id, code)
        returns_chat.settle_after_run(customer_id, payload["conv_id"], run_id)
        return {"run_id": run_id, "status": "error", "error_code": code}

    if result.get("status") == "duplicate":
        # Another invocation already started this run in the runtime; it owns the ending.
        return {"run_id": run_id, "status": "duplicate"}
    # The agent finishes its own run. A run that is still "running" here was left behind.
    _fail_if_running(customer_id, run_id, ErrorCode.INTERNAL_ERROR.value)
    # A return that reached a transaction or a case ends the hand-off: the chat goes back to ALDO bot.
    returns_chat.settle_after_run(customer_id, payload["conv_id"], run_id)
    return {"run_id": run_id, "status": result.get("status", "completed")}


def _fail_if_running(customer_id: str, run_id: str, code: str) -> bool:
    """Close a run that is still ``running`` as an error. The winner of ``finish_run`` emits
    ``run_completed``; the apology goes first so ``run_completed`` stays the last event."""
    try:
        if runs.get_run(customer_id, run_id)["status"] != "running":
            return False
        try:
            trace.emit(run_id, "assistant_message", {"text": GENERIC_FAILURE_TEXT})
        except Exception:
            logger.exception("failure_message_not_emitted", run_id=run_id)
        return bool(runs.finish_run(run_id, "error", {"error_code": code}))
    except Exception:
        logger.exception("run_not_closed", run_id=run_id)
        return False


def error_code_for(exc: BaseException) -> str:
    """``UPSTREAM_TIMEOUT`` for timeouts, throttling and capacity trouble, else ``INTERNAL_ERROR``."""
    if isinstance(exc, DomainError):
        return (
            ErrorCode.UPSTREAM_TIMEOUT.value
            if exc.code == ErrorCode.UPSTREAM_TIMEOUT.value
            else ErrorCode.INTERNAL_ERROR.value
        )
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _TRANSIENT_CLIENT_CODES:
            return ErrorCode.UPSTREAM_TIMEOUT.value
        return ErrorCode.INTERNAL_ERROR.value
    if isinstance(exc, _TIMEOUT_ERRORS):
        return ErrorCode.UPSTREAM_TIMEOUT.value
    return ErrorCode.INTERNAL_ERROR.value


# ---------------------------------------------------------------------------- the runtime


def agentcore_config(remaining_ms: int | None = None) -> Config:
    """Client config for ``invoke_agent_runtime`` (design-gap G-05).

    Read timeout = the time left minus a margin (at least 30 s), connect timeout 5 s, and
    exactly one attempt: botocore must not re-invoke the runtime on a read timeout.
    """
    budget_s = (DEFAULT_BUDGET_MS if remaining_ms is None else remaining_ms) / 1000
    return Config(
        read_timeout=max(MIN_READ_TIMEOUT_S, int(budget_s - READ_TIMEOUT_MARGIN_S)),
        connect_timeout=CONNECT_TIMEOUT_S,
        retries={"mode": "standard", "max_attempts": 1},
    )


def runtime_arn() -> str:
    """``AGENT_RUNTIME_ARN`` (unless it is a placeholder), else the SSM parameter. Cached."""
    global _runtime_arn
    if _runtime_arn:
        return _runtime_arn
    configured = os.environ.get("AGENT_RUNTIME_ARN", "").strip()
    if configured and PLACEHOLDER_MARKER not in configured.lower():
        value = configured
    else:
        name = os.environ.get("AGENT_RUNTIME_ARN_PARAM", "").strip() or DEFAULT_RUNTIME_ARN_PARAM
        ssm = boto3.client("ssm", region_name=config.get_settings().region)
        value = ssm.get_parameter(Name=name)["Parameter"]["Value"].strip()
    if not value.startswith("arn:") or PLACEHOLDER_MARKER in value.lower():
        raise DomainError(ErrorCode.INTERNAL_ERROR, "the agent runtime ARN is not configured")
    _runtime_arn = value
    return value


def reset_runtime_arn() -> None:
    """Forget the cached runtime ARN (tests, or after the runtime is recreated)."""
    global _runtime_arn
    _runtime_arn = None


def invoke_runtime(
    payload: dict[str, Any], session_id: str, remaining_ms: int | None = None
) -> dict[str, Any]:
    """Invoke the ALDO returns agent and return its JSON answer ``{run_id, status, metrics}``."""
    if agent_mode() == LOCAL_MODE:
        return _post_local(payload, session_id, agentcore_config(remaining_ms).read_timeout)
    client = boto3.client(
        "bedrock-agentcore",
        region_name=config.get_settings().region,
        config=agentcore_config(remaining_ms),
    )
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn(),
        runtimeSessionId=session_id,
        payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    return _parse_answer(response["response"].read())


def _post_local(payload: dict[str, Any], session_id: str, timeout_s: float) -> dict[str, Any]:
    url = os.environ.get("AGENT_LOCAL_URL", "").strip() or DEFAULT_LOCAL_URL
    if not url.startswith(("http://", "https://")):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "AGENT_LOCAL_URL must start with http:// or https://")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", LOCAL_SESSION_HEADER: session_id},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost: no proxies
    with opener.open(request, timeout=timeout_s) as response:
        return _parse_answer(response.read())


def _parse_answer(raw: bytes | str) -> dict[str, Any]:
    try:
        answer = json.loads(raw)
    except ValueError:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "the agent runtime returned invalid JSON") from None
    if not isinstance(answer, dict):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "the agent runtime returned an unexpected answer")
    return answer


# ------------------------------------------------------------------------ the ALDO bot


def _run_personal(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The ALDO bot (personal agent) runs in this process; its runner owns the run lifecycle."""
    run_id = payload["run_id"]
    try:
        runner = providers.optional("personal_agent.runner")
        if runner is None:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "the personal agent is not installed")
        result = runner.run(_with_orders(payload))
    except Exception as exc:
        code = error_code_for(exc)
        logger.exception("personal_run_failed", run_id=run_id, error_code=code)
        _fail_personal_run(payload, code)
        return {"run_id": run_id, "status": "error", "error_code": code}
    return result if isinstance(result, dict) else {"run_id": run_id, "status": "completed"}


def _with_orders(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The bot's payload plus the owner's recent ALDO orders, read here as trusted server data.

    The bot needs them to pick the order for a return (``hand_off_to_returns``) and to build the
    return workflow's memory context. A request can never supply them: any ``orders`` key in the
    incoming payload is replaced, and when the read fails the run simply has no order list.
    """
    enriched = {key: value for key, value in payload.items() if key != "orders"}
    try:
        enriched["orders"] = returns_chat.recent_orders(payload["customer_id"])
    except Exception:
        logger.exception("orders_context_failed", run_id=payload["run_id"])
    return enriched


def _fail_personal_run(payload: Mapping[str, Any], code: str) -> None:
    """The runner crashed before closing the run: close it so the owner is not left waiting."""
    owner_id, run_id, conv_id = payload["customer_id"], payload["run_id"], payload["conv_id"]
    try:
        memory = providers.optional("personal_agent.memory")
        if memory is None:
            return
        repo = memory.get_repo() if hasattr(memory, "get_repo") else None
        emit = getattr(memory, "emit_run_event", None) or getattr(repo, "emit", None)
        finish = getattr(memory, "finish_run", None) or getattr(repo, "finish_run", None)
        release = getattr(memory, "release_lease", None) or getattr(repo, "release_lease", None)
        if emit is not None:
            try:
                emit(owner_id, run_id, "assistant_message", {"text": PERSONAL_FAILURE_TEXT}, actor="system")
            except Exception:
                logger.exception("personal_failure_message_not_emitted", run_id=run_id)
        if finish is not None:
            finish(owner_id, run_id, "error", {"error_code": code})
        if release is not None:
            release(owner_id, conv_id, run_id)
    except Exception:
        logger.exception("personal_run_not_closed", run_id=run_id)
