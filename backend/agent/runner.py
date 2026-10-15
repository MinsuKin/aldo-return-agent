"""One agent run: payload in, trace events and a persisted conversation out (contracts B.1, C.6, C.7).

Steps (all mandatory, in this order):

1. ``runs.mark_agent_started`` (conditional): a duplicate invocation returns ``duplicate``.
2. ``run_started`` event.
3. Rehydrate the conversation from DynamoDB (``conversations.load_messages``).
4. Build the Strands agent: ``BedrockModel(model_id, region_name, max_tokens)`` and NOTHING
   else (Sonnet and Opus 5.5 reject temperature, top_p, top_k and toolChoice other than auto,
   spike.md 0.1), ``SequentialToolExecutor`` (G-11), the ten tools, the round-limit hooks and
   a retry strategy of at most 2 model attempts (the Strands default of 6 attempts with up
   to 240 s back-off would delay the fallback by minutes, G-07).
5. The user turn is a list of content blocks: the text (customer text, or a server-authored
   ``[server] ...`` text built from the structured payload fields) and, when an order is
   selected, ``[selected_order_id: ...]`` as a separate block.
6. Run. If the FIRST model call fails before any tool call exists, retry once with the
   fallback model; otherwise the run ends in error (G-07).
7. Save the conversation (non-fatal: a failure only writes a ``warning`` event), emit
   ``assistant_message``, then ``runs.finish_run`` which emits ``run_completed``.

``run`` never raises. The chain of thought is never requested or stored.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping

from strands import Agent, ModelRetryStrategy
from strands.models import BedrockModel
from strands.tools.executors import SequentialToolExecutor
from strands.types.exceptions import ModelThrottledException

from returns_domain import config
from returns_domain.errors import DomainError, ErrorCode

from .context import RUN_KINDS, RoundLimitExceeded, RunContext
from .envelope import emit_event
from .hooks import RunHooks
from .log import logger
from .prompt import PROMPT_VERSION, SERVER_MARKER, build_system_prompt
from .services import Services
from .tools import build_tools

#: The sandbox denies regional calls outside us-east-1, so the region is never inferred.
BEDROCK_REGION = "us-east-1"
MAX_OUTPUT_TOKENS = 2048
MODEL_MAX_ATTEMPTS = 2

CUSTOMER_ERROR_TEXT = "Something went wrong on our side. Please try again."
EMPTY_REPLY_TEXT = (
    "I have finished the steps I can take. Please check the options shown, "
    "or tell me what else you need."
)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

APPROVAL_TEXT = (
    SERVER_MARKER
    + " Customer approved option {option_id} on quote {quote_id} in the authenticated UI. "
    "Execute it and verify."
)
REQUOTE_TEXT = (
    SERVER_MARKER
    + " Quote {quote_id} expired. Create fresh options for the same request and a new quote; "
    "the customer must approve again."
)


# ----------------------------------------------------------------------- factories


def make_model(model_id: str) -> BedrockModel:
    """The only place a Bedrock model is built. No sampling parameters, no structured output."""
    return BedrockModel(model_id=model_id, region_name=BEDROCK_REGION, max_tokens=MAX_OUTPUT_TOKENS)


def make_agent(ctx: RunContext, model: Any, messages: list[dict[str, Any]]) -> Agent:
    return Agent(
        model=model,
        system_prompt=build_system_prompt(max_rounds=ctx.max_rounds),
        tools=build_tools(ctx),
        messages=messages,
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
        hooks=[RunHooks(ctx)],
        retry_strategy=ModelRetryStrategy(max_attempts=MODEL_MAX_ATTEMPTS, initial_delay=1, max_delay=4),
    )


# ------------------------------------------------------------------------- payload


def _text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _identifier(payload: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    value = _text(payload, key)
    if value is None:
        if required:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"payload field {key} is required")
        return None
    if not _ID_PATTERN.match(value.strip()):
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"payload field {key} is not a valid id")
    return value.strip()


def context_from_payload(payload: Any, services: Services | None = None) -> RunContext:
    """Validate the worker payload (C.6) and build the run context. Raises VALIDATION_ERROR."""
    if not isinstance(payload, Mapping):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "payload must be a JSON object")
    kind = payload.get("kind") or "customer"
    if kind not in RUN_KINDS:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "payload kind is not supported")
    quote_id = _identifier(payload, "quote_id", required=kind in ("approval", "requote"))
    option_id = _identifier(payload, "option_id", required=kind == "approval")
    return RunContext(
        run_id=_identifier(payload, "run_id", required=True) or "",
        conv_id=_identifier(payload, "conv_id", required=True) or "",
        customer_id=_identifier(payload, "customer_id", required=True) or "",
        order_id=_identifier(payload, "order_id", required=False),
        kind=kind,
        max_rounds=config.get_settings().max_rounds,
        quote_id=quote_id,
        option_id=option_id,
        approval_id=_identifier(payload, "approval_id", required=False),
        services=services or Services(),
    )


def build_user_content(ctx: RunContext, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """The user turn as content blocks (B.1 step 5)."""
    if ctx.kind == "approval":
        text = APPROVAL_TEXT.format(option_id=ctx.option_id, quote_id=ctx.quote_id)
    elif ctx.kind == "requote":
        text = REQUOTE_TEXT.format(quote_id=ctx.quote_id)
    else:
        text = payload.get("message")
        if not isinstance(text, str) or not text.strip():
            raise DomainError(ErrorCode.VALIDATION_ERROR, "message is required")
        text = text.strip()
        limit = config.get_settings().max_message_chars
        if len(text) > limit:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"message is longer than {limit} characters")
        if text.lower().startswith(SERVER_MARKER):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "customer text cannot start with the server marker")
    blocks = [{"text": text}]
    if ctx.order_id:
        blocks.append({"text": f"[selected_order_id: {ctx.order_id}]"})
    return blocks


# ----------------------------------------------------------------------------- run


@dataclass
class _Outcome:
    status: str  # completed | error
    text: str
    error_code: str | None = None
    messages: list[dict[str, Any]] | None = None  # None: do not save the conversation


def run(payload: dict[str, Any], services: Services | None = None) -> dict[str, Any]:
    """Execute one run. ``payload`` is the worker payload (contracts C.6). Never raises."""
    try:
        ctx = context_from_payload(payload, services)
    except DomainError as exc:
        logger.warning("payload_rejected", code=exc.code)
        return {"run_id": _safe_run_id(payload), "status": "error", "metrics": {"error_code": exc.code}}
    except Exception:  # noqa: BLE001 - run() never raises
        logger.exception("payload_unreadable")
        return {
            "run_id": _safe_run_id(payload),
            "status": "error",
            "metrics": {"error_code": ErrorCode.INTERNAL_ERROR.value},
        }

    try:
        if not ctx.services.runs.mark_agent_started(ctx.run_id):
            logger.info("run_duplicate", run_id=ctx.run_id)
            return {"run_id": ctx.run_id, "status": "duplicate", "metrics": {}}
    except Exception:  # noqa: BLE001 - the worker marks the run failed when we cannot start
        logger.exception("mark_agent_started_failed", run_id=ctx.run_id)
        return {
            "run_id": ctx.run_id,
            "status": "error",
            "metrics": {"error_code": ErrorCode.INTERNAL_ERROR.value},
        }

    try:
        outcome = _converse(ctx, payload)
    except Exception as exc:  # noqa: BLE001 - last line of defence; the run must still finish
        logger.exception("run_failed", run_id=ctx.run_id)
        outcome = _Outcome("error", CUSTOMER_ERROR_TEXT, _error_code(exc))
    return _finish(ctx, outcome)


def _safe_run_id(payload: Any) -> str | None:
    try:
        if isinstance(payload, Mapping) and isinstance(payload.get("run_id"), str):
            return payload["run_id"][:64]
    except Exception:  # noqa: BLE001 - only used to label an error result
        pass
    return None


def _converse(ctx: RunContext, payload: Mapping[str, Any]) -> _Outcome:
    settings = config.get_settings()
    ctx.model_id = settings.bedrock_model_id
    try:
        content = build_user_content(ctx, payload)
    except DomainError as exc:
        return _Outcome("error", CUSTOMER_ERROR_TEXT, exc.code)

    emit_event(
        ctx,
        "run_started",
        {
            "kind": ctx.kind,
            "conv_id": ctx.conv_id,
            "order_id": ctx.order_id,
            "model_id": ctx.model_id,
            "max_rounds": ctx.max_rounds,
        },
    )
    logger.info(
        "run_started",
        run_id=ctx.run_id,
        kind=ctx.kind,
        model_id=ctx.model_id,
        prompt_version=PROMPT_VERSION,
    )

    prior = ctx.services.conversations.load_messages(ctx.conv_id)
    fallback_id = settings.bedrock_fallback_model_id
    model_id = ctx.model_id
    while True:
        ctx.model_id = model_id
        ctx.rounds = 0
        ctx.seen_message_count = -1
        agent: Agent | None = None
        try:
            agent = make_agent(ctx, make_model(model_id), copy.deepcopy(prior))
            result = agent(content)
            return _Outcome("completed", _final_text(result), messages=list(agent.messages))
        except Exception as exc:  # noqa: BLE001
            if agent is not None and _is_hard_stop(exc):
                return _hard_stop(ctx, agent)
            if ctx.tool_calls == 0 and not ctx.fallback_used and fallback_id and fallback_id != model_id:
                logger.warning(
                    "model_fallback",
                    run_id=ctx.run_id,
                    model_id=model_id,
                    error_type=type(exc).__name__,
                )
                ctx.fallback_used = True
                model_id = fallback_id
                continue
            logger.exception("agent_failed", run_id=ctx.run_id, model_id=model_id)
            return _Outcome("error", CUSTOMER_ERROR_TEXT, _error_code(exc))
        finally:
            if agent is not None:
                _add_usage(ctx, agent)


def _final_text(result: Any) -> str:
    message = getattr(result, "message", None) or {}
    parts = [
        block["text"]
        for block in message.get("content", [])
        if isinstance(block, Mapping) and isinstance(block.get("text"), str)
    ]
    return "\n".join(part.strip() for part in parts if part.strip()) or EMPTY_REPLY_TEXT


def _is_hard_stop(exc: BaseException) -> bool:
    """True if ``exc`` is RoundLimitExceeded, possibly wrapped by Strands."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, RoundLimitExceeded):
            return True
        seen.add(id(current))
        current = getattr(current, "original_exception", None) or current.__cause__ or current.__context__
    return False


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, DomainError):
        return exc.code
    if isinstance(exc, ModelThrottledException):
        return ErrorCode.UPSTREAM_TIMEOUT.value
    return ErrorCode.INTERNAL_ERROR.value


def _add_usage(ctx: RunContext, agent: Any) -> None:
    usage = getattr(getattr(agent, "event_loop_metrics", None), "accumulated_usage", None) or {}
    ctx.input_tokens += int(usage.get("inputTokens", 0) or 0)
    ctx.output_tokens += int(usage.get("outputTokens", 0) or 0)


def _hard_stop(ctx: RunContext, agent: Any) -> _Outcome:
    """The model kept going past max_rounds + 3: escalate server-side and close the run (B.2)."""
    case_id = ctx.case_id
    if not ctx.escalated:
        try:
            case = ctx.services.store.json_safe(
                ctx.services.cases.escalate(
                    ctx.customer_id,
                    ctx.run_id,
                    "LIMIT_REACHED",
                    "The agent reached its round limit before finishing this request.",
                    ctx.order_id,
                    None,
                )
            )
            case_id = case.get("case_id")
            ctx.escalated = True
            ctx.case_id = case_id
        except Exception:  # noqa: BLE001
            logger.exception("hard_stop_escalation_failed", run_id=ctx.run_id)
    if case_id:
        text = (
            "I could not finish this automatically, so I passed it to a person on the ALDO team. "
            f"Your case ID is {case_id}. They will review it; I cannot promise an outcome."
        )
    else:
        text = (
            "I could not finish this automatically. Please try again, "
            "or ask for a person to review it."
        )
    messages = list(agent.messages)
    messages.append({"role": "assistant", "content": [{"text": text}]})
    return _Outcome("completed", text, messages=messages)


def _finish(ctx: RunContext, outcome: _Outcome) -> dict[str, Any]:
    """Save the conversation (non-fatal), emit the final message, close the run."""
    if outcome.messages is not None:
        try:
            ctx.services.conversations.save_messages(ctx.conv_id, outcome.messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning("messages_not_saved", run_id=ctx.run_id, error_type=type(exc).__name__)
            emit_event(
                ctx,
                "warning",
                {"code": "MESSAGES_NOT_SAVED", "message": "The conversation could not be saved."},
            )
    emit_event(ctx, "assistant_message", {"text": outcome.text})

    metrics: dict[str, Any] = {
        "rounds": ctx.rounds,
        "tool_count": ctx.tool_calls,
        "input_tokens": ctx.input_tokens,
        "output_tokens": ctx.output_tokens,
        "total_latency_ms": ctx.elapsed_ms(),
        "model_id_used": ctx.model_id,
        "fallback_used": ctx.fallback_used,
        "error_code": outcome.error_code,
    }
    try:
        ctx.services.runs.finish_run(ctx.run_id, outcome.status, metrics)
    except Exception:  # noqa: BLE001 - the worker closes a run that is still running
        logger.exception("finish_run_failed", run_id=ctx.run_id)
    logger.info(
        "run_completed",
        run_id=ctx.run_id,
        status=outcome.status,
        outcome=outcome.error_code or "ok",
        latency_ms=metrics["total_latency_ms"],
        rounds=ctx.rounds,
        tool_count=ctx.tool_calls,
        model_id=ctx.model_id,
    )
    return {"run_id": ctx.run_id, "status": outcome.status, "metrics": metrics}
