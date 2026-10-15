"""Runs one turn of ALDO bot (PIVOT_ADDENDUM 5.5, 10.1; contracts.md F.9).

``run(payload)`` takes the worker payload ``{run_id, conv_id, customer_id, message, agent: "personal"}``
(``customer_id`` is trusted: it comes from the verified session token via the API) and returns
``{"run_id", "status": "completed" | "error" | "duplicate", "metrics": {...}}``.

Steps: once-only start guard -> ``run_started`` -> mint a short-lived delegated token for the owner
(server side; the model never sees or supplies it) and build the gateway client -> rehydrate the
conversation -> Strands agent on Bedrock (sequential tools, 6 tool calls, one fallback model before any
proposal, no sampling parameters) -> save the conversation (non-fatal) -> ``assistant_message`` ->
``finish_run`` which emits ``run_completed`` with the metrics -> release the conversation lease.

The final reply is checked against the user's rule: it must say the proposal is ready for the
owner's confirmation and must never claim an order was placed. Otherwise a deterministic reply built
from the proposal is used (``metrics.reply_guard`` records what happened).
"""

from __future__ import annotations

import copy
import re
import time
from decimal import Decimal
from typing import Any, Callable, Mapping

from botocore.exceptions import ClientError

from returns_domain import auth
from returns_domain import config as domain_config
from returns_domain.errors import DomainError, ErrorCode

from . import settings
from .gateway_client import GatewayClient, build_gateway_client
from .log import logger
from .memory import MemoryRepo, get_repo
from .prompt import build_system_prompt, owner_message
from .sanitize import count_words, drop_quoting_sentences, limit_words, quotes_any, strip_markdown
from .tools import PersonalRunContext, build_tools, clean_orders

SAFE_ERROR_REPLY = (
    "ALDO bot couldn't finish that just now. Nothing was ordered. Please try again in a moment."
)
HARD_STOP_REPLY = (
    "ALDO bot ran out of steps before it could finish. Nothing was ordered. Please try again."
)
CONFIRM_SUFFIX = "Please confirm it in the app; nothing is ordered until you do."
#: The one sentence after a hand-off to the return flow: it names nobody else (addendum 12.1).
HANDOFF_REPLY = "Sure, let's sort out your return."
HANDOFF_MIN_WORDS = 3  # "Done." is not a sentence the owner can follow
HANDOFF_MAX_WORDS = 25
#: Words that would introduce somebody else to the owner. The reply after a hand-off must not have any.
_OTHER_AGENT = re.compile(
    r"\b(?:agents?|assistants?|specialists?|colleagues?|teams?|departments?|systems?|humans?)\b"
    r"|\b(?:hand(?:ing|ed|s)?[\s-]*(?:you\s+|this\s+|it\s+)?(?:over|off)|hand-?off|transfer\w*|redirect\w*"
    r"|escalat\w*|bring(?:ing)?\s+in|connect(?:ing)?\s+you|pass(?:ing)?\s+(?:you|this)\s+(?:on|over|to))\b"
    r"|\b(?:another|other|different|separate|dedicated)\s+(?:bot|person|desk)\b",
    re.IGNORECASE,
)

#: Model errors worth ONE retry on the fallback model (throttling and access problems).
_FALLBACK_CODES = frozenset(
    {
        "ThrottlingException",
        "throttlingException",
        "AccessDeniedException",
        "ServiceQuotaExceededException",
        "ModelNotReadyException",
        "ServiceUnavailableException",
    }
)

#: Claims that the order is placed. Deliberately first-person/active so that "nothing is ordered
#: until you confirm" is NOT flagged.
_PLACED_CLAIM = re.compile(
    r"\bI(?:'ve|\s+have)?\s+(?:just\s+)?(?:placed|ordered|bought|purchased)\b"
    r"|\b(?:your\s+|the\s+)?(?:order|purchase)\s+(?:has\s+been|is|was)\s+(?:placed|complete|completed|confirmed|done)\b"
    r"|\bit(?:'s|\s+is|\s+has\s+been)\s+(?:been\s+)?(?:ordered|purchased|bought)\b"
    r"|\bsuccessfully\s+(?:ordered|purchased|placed)\b",
    re.IGNORECASE,
)
_CONFIRM_WORD = re.compile(r"\bconfirm", re.IGNORECASE)
_ORDER_WORD = re.compile(r"\border", re.IGNORECASE)


# ----------------------------------------------------------------------------- model + agent


def build_model(model_id: str) -> Any:
    """Bedrock model with ONLY model id, region and max tokens.

    Sonnet and Opus 5.5 reject ``temperature`` other than 1.0, ``top_p``, ``top_k`` and a
    ``toolChoice`` of any/tool (spike.md 0.1), so none is passed and no structured output is used.
    The region is explicit: Strands would otherwise fall back to us-west-2, which the sandbox denies.
    """
    from strands.models import BedrockModel

    return BedrockModel(
        model_id=model_id,
        region_name=settings.BEDROCK_REGION,
        max_tokens=settings.MAX_MODEL_TOKENS,
    )


def before_model_call(ctx: PersonalRunContext, event: Any) -> None:
    """Strands ``BeforeModelCallEvent`` hook: count model calls and hard-stop a runaway tool loop."""
    ctx.rounds += 1
    if ctx.tool_calls > ctx.max_tool_calls + settings.HARD_STOP_EXTRA_CALLS:
        ctx.hard_stopped = True
        event.cancel = HARD_STOP_REPLY


def default_agent_factory(
    ctx: PersonalRunContext, model_id: str, prior_messages: list[dict[str, Any]], system_prompt: str
) -> Any:
    from strands import Agent, ModelRetryStrategy
    from strands.hooks import BeforeModelCallEvent
    from strands.tools.executors import SequentialToolExecutor

    agent = Agent(
        model=build_model(model_id),
        system_prompt=system_prompt,
        tools=build_tools(ctx),
        messages=prior_messages,
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
        # The Strands default (6 attempts, 4 s up to 240 s) would delay the fallback by minutes.
        retry_strategy=ModelRetryStrategy(max_attempts=2, initial_delay=1, max_delay=4),
    )
    agent.hooks.add_callback(BeforeModelCallEvent, lambda event: before_model_call(ctx, event))
    return agent


AgentFactory = Callable[[PersonalRunContext, str, list, str], Any]


def mint_delegated_token(owner_id: str) -> str:
    """Short-lived delegated token for the gateway (scope delegated_gateway, actor personal_agent).

    Minted here, server side, from the trusted owner id. The model never sees or supplies it.
    """
    limits = domain_config.get_settings()
    issued = auth.sign_token(
        owner_id,
        scope=settings.DELEGATED_SCOPE,
        actor=settings.DELEGATED_ACTOR,
        max_amount_cents=limits.delegation_max_cents,
        ttl_seconds=limits.delegation_ttl_seconds,
    )
    return issued.token


# --------------------------------------------------------------------------------- run entry


def run(
    payload: Mapping[str, Any],
    *,
    repo: MemoryRepo | None = None,
    gateway: GatewayClient | None = None,
    agent_factory: AgentFactory | None = None,
    token_minter: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Run one ALDO bot turn. The keyword arguments are test and smoke seams."""
    run_id, conv_id, owner_id, message = _validate_payload(payload)
    repo = repo or get_repo()

    # A run created by the API already has these; direct callers (smoke, tests) get them here.
    repo.create_conversation(owner_id, conv_id)
    repo.create_run(owner_id, conv_id, run_id)
    if not repo.mark_started(owner_id, run_id):
        return {"run_id": run_id, "status": "duplicate", "metrics": {}}
    repo.try_acquire_lease(owner_id, conv_id, run_id)  # after the guard: a duplicate must not take it

    started = time.monotonic()
    ctx: PersonalRunContext | None = None
    try:
        profile = repo.get_owner(owner_id)
        if profile is None:
            raise DomainError(ErrorCode.NOT_FOUND, "no personal agent profile for this owner")
        if gateway is None:
            token = (token_minter or mint_delegated_token)(owner_id)
            gateway = build_gateway_client(token)
        orders = clean_orders(payload.get("orders"))  # server data from the worker, or None
        ctx = PersonalRunContext(
            run_id=run_id,
            conv_id=conv_id,
            owner_id=owner_id,
            gateway=gateway,
            repo=repo,
            size_system=profile["size_system"],
            model_id=settings.model_id(),
            message=message,
            orders=orders,
        )
        ctx.emit(
            "run_started",
            {"conv_id": conv_id, "model_id": ctx.model_id, "max_tool_calls": ctx.max_tool_calls},
        )
        prior = repo.load_messages(owner_id, conv_id)
        system_prompt = build_system_prompt(profile["display_name"], orders=orders)
        agent, result = _run_agent(ctx, agent_factory or default_agent_factory, prior, system_prompt, message)
        reply, guard, raw_words = _final_reply(ctx, _reply_text(result), profile["display_name"])
        _save_messages(ctx, agent)
        ctx.emit("assistant_message", {"text": reply, "words": count_words(reply)})
        status, error_code = "completed", None
        metrics = _metrics(ctx, result, started, error_code, guard, raw_words)
    except Exception as exc:
        code = exc.code if isinstance(exc, DomainError) else _error_code(exc)
        logger.exception("personal agent run failed", extra={"run_id": run_id, "error_code": code})
        if ctx is None:  # failed before run_started: keep the "first event is run_started" shape
            _emit_quietly(repo, owner_id, run_id, "run_started", {"conv_id": conv_id})
        _emit_quietly(repo, owner_id, run_id, "assistant_message", {"text": SAFE_ERROR_REPLY})
        status = "error"
        metrics = _metrics(ctx, None, started, code, "none", 0)
    repo.finish_run(owner_id, run_id, status, metrics)
    repo.release_lease(owner_id, conv_id, run_id)
    return {"run_id": run_id, "status": status, "metrics": metrics}


def _validate_payload(payload: Mapping[str, Any]) -> tuple[str, str, str, str]:
    if not isinstance(payload, Mapping):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "payload must be an object")
    if payload.get("agent") not in (None, "personal"):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "this runner only handles agent=personal")
    run_id = settings.require_id(payload.get("run_id"), "run_id")
    conv_id = settings.require_id(payload.get("conv_id"), "conv_id")
    owner_id = settings.require_id(payload.get("customer_id"), "customer_id")
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise DomainError(ErrorCode.VALIDATION_ERROR, "message must be a non-empty string")
    message = owner_message(message)
    if len(message) > settings.max_message_chars():
        raise DomainError(ErrorCode.VALIDATION_ERROR, "message is too long")
    return run_id, conv_id, owner_id, message


# ------------------------------------------------------------------------ agent + fallback


def _run_agent(
    ctx: PersonalRunContext,
    factory: AgentFactory,
    prior: list[dict[str, Any]],
    system_prompt: str,
    message: str,
) -> tuple[Any, Any]:
    """Run the agent; on a throttle/access error BEFORE any proposal, retry once on the fallback model."""
    fallback = settings.fallback_model_id()
    attempts = [ctx.model_id] + ([fallback] if fallback and fallback != ctx.model_id else [])
    for index, model_id in enumerate(attempts):
        ctx.model_id = model_id
        ctx.fallback_used = index > 0
        agent = factory(ctx, model_id, copy.deepcopy(prior), system_prompt)
        try:
            return agent, agent(message)
        except Exception as exc:
            can_fall_back = index + 1 < len(attempts) and not ctx.proposal_sent and _is_fallback_error(exc)
            if not can_fall_back:
                raise
            logger.warning(
                "model call failed, retrying once on the fallback model",
                extra={"run_id": ctx.run_id, "error": type(exc).__name__},
            )
            ctx.emit(
                "warning",
                {"code": "MODEL_FALLBACK", "message": "The primary model was unavailable; retrying once on the fallback model."},
            )
    raise DomainError(ErrorCode.INTERNAL_ERROR, "no model attempt ran")  # pragma: no cover


def _causes(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = getattr(current, "original_exception", None) or current.__cause__ or current.__context__
    return chain


def _is_fallback_error(exc: BaseException) -> bool:
    for cause in _causes(exc):
        if type(cause).__name__ == "ModelThrottledException":
            return True
        if isinstance(cause, ClientError) and cause.response.get("Error", {}).get("Code") in _FALLBACK_CODES:
            return True
    return False


def _error_code(exc: BaseException) -> str:
    if _is_fallback_error(exc):
        return ErrorCode.UPSTREAM_TIMEOUT.value
    return ErrorCode.INTERNAL_ERROR.value


# ------------------------------------------------------------------------------ the reply


def _reply_text(result: Any) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, Mapping):
        parts = [
            block["text"]
            for block in message.get("content", [])
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        ]
        text = " ".join(part.strip() for part in parts if part.strip())
        if text:
            return text
    return str(result).strip() if result is not None else ""


def compose_proposal_reply(ctx: PersonalRunContext, owner_name: str) -> str:
    """Deterministic reply from the proposal (used when the model's reply breaks the rules)."""
    proposal = ctx.proposal or {}
    size = proposal.get("size", "")
    product = " ".join(part for part in (proposal.get("product_name"), proposal.get("color")) if part)
    where = proposal.get("location_name") or "ALDO"
    when = proposal.get("eta_text") or proposal.get("eta_date") or ""
    how = {
        "store_pickup": f"pickup at {where}",
        "ship_from_store": f"shipping from {where}",
        "ship_from_dc": f"shipping from {where}",
    }.get(proposal.get("option_type"), f"from {where}")
    late = " It will miss your deadline." if proposal.get("meets_deadline") is False else ""
    return (
        f"{owner_name}, I have a proposal ready: size {size}"
        f"{' ' + product if product else ''}, {how}{' on ' + when if when else ''}.{late} "
        f"{CONFIRM_SUFFIX}"
    )


def handoff_reply(text: str) -> tuple[str, str]:
    """``(reply, guard)`` after a hand-off: one short sentence that names nobody else.

    The model's own sentence is kept when it is short and mentions no agent, assistant, team or
    hand-over; anything else becomes the fixed sentence ``HANDOFF_REPLY``.
    """
    text = (text or "").strip()
    if HANDOFF_MIN_WORDS <= count_words(text) <= HANDOFF_MAX_WORDS and not _OTHER_AGENT.search(text):
        return text, "none"
    return HANDOFF_REPLY, "replaced"


#: Closing question of the multi-item hand-off reply (demo script: "shows the 10 items and asks which three and why").
HANDOFF_LIST_QUESTION = "Which ones are you returning, and why?"


def owner_named_an_item(ctx: PersonalRunContext) -> bool:
    """The owner's message already names an item of the handed-off order (its product name, or colour and kind)."""
    message = (ctx.message or "").lower()
    order = next((o for o in (ctx.orders or []) if o.get("order_id") == ctx.handoff_order_id), None)
    for line in (order or {}).get("lines") or []:
        name = str(line.get("product_name") or "").strip().lower()
        color = str(line.get("color") or "").strip().lower()
        kind = str(line.get("category") or "").strip().lower()
        if name and name in message:
            return True
        if color and kind and color in message and kind in message:
            return True
    return False


def handoff_list_reply(text: str, items: list[str]) -> str:
    """After a hand-off of an order with several items: one short sentence, the numbered items exactly as the
    server formatted them (never the model's copy), then one question. The model's first sentence is the lead
    when it is short, names nobody else and is not itself a question; otherwise the fixed sentence."""
    stripped = (text or "").strip()
    lead = re.split(r"(?<=[.!?])\s+", stripped, maxsplit=1)[0] if stripped else ""
    if (
        not HANDOFF_MIN_WORDS <= count_words(lead) <= HANDOFF_MAX_WORDS
        or _OTHER_AGENT.search(lead)
        or lead.rstrip().endswith("?")
    ):
        lead = HANDOFF_REPLY
    return "\n".join([lead, *items, HANDOFF_LIST_QUESTION])


def _final_reply(ctx: PersonalRunContext, text: str, owner_name: str) -> tuple[str, str, int]:
    """(reply, guard, raw_words): enforce 'proposal, confirm in the app, never claim an order'.

    Also: nothing quoted from a flagged review leaves in the reply, and after a hand-off the reply
    is one short sentence that does not mention another agent.
    """
    text = strip_markdown(text)
    raw_words = count_words(text)
    if ctx.hard_stopped:
        return HARD_STOP_REPLY, "hard_stop", raw_words
    if ctx.handed_off:
        reply, guard = handoff_reply(text)
        if ctx.handoff_items and not owner_named_an_item(ctx):
            return handoff_list_reply(text, ctx.handoff_items), "list_added", raw_words
        return reply, guard, raw_words
    guard = "none"
    if ctx.flagged_texts and quotes_any(text, ctx.flagged_texts):
        text = drop_quoting_sentences(text, ctx.flagged_texts)
        guard = "dropped_quote"
    if ctx.proposal_sent:
        if not text or _PLACED_CLAIM.search(text):
            return compose_proposal_reply(ctx, owner_name), "replaced", raw_words
        if not _CONFIRM_WORD.search(text):
            room = settings.MAX_REPLY_WORDS - count_words(CONFIRM_SUFFIX)
            return f"{limit_words(text, room)} {CONFIRM_SUFFIX}", "appended_confirm", raw_words
        return limit_words(text, settings.MAX_REPLY_WORDS), guard, raw_words
    if not text:
        return SAFE_ERROR_REPLY, "empty", raw_words
    if _PLACED_CLAIM.search(text) and _ORDER_WORD.search(text):
        return "I haven't sent a proposal, so nothing is ordered. Please tell me which shoe you want and I will try again.", "replaced", raw_words
    return limit_words(text, settings.MAX_REPLY_WORDS), guard, raw_words


# ------------------------------------------------------------------------- bookkeeping


def _save_messages(ctx: PersonalRunContext, agent: Any) -> None:
    """Persist the conversation at a clean boundary; failure is non-fatal (warning event)."""
    try:
        ctx.repo.save_messages(ctx.owner_id, ctx.conv_id, list(getattr(agent, "messages", [])))
    except Exception as exc:
        logger.warning("messages not saved", extra={"run_id": ctx.run_id, "error": type(exc).__name__})
        ctx.emit("warning", {"code": "MESSAGES_NOT_SAVED", "message": "The conversation could not be saved."})


def _emit_quietly(repo: MemoryRepo, owner_id: str, run_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
    try:
        repo.emit(owner_id, run_id, event_type, payload)
    except Exception as exc:
        logger.warning("run event not written", extra={"run_id": run_id, "error": type(exc).__name__})


def _usage(result: Any) -> tuple[int, int, int]:
    """(cycles, input_tokens, output_tokens) from Strands ``result.metrics``; zeros if absent."""
    metrics = getattr(result, "metrics", None)
    if metrics is None:
        return 0, 0, 0
    usage = getattr(metrics, "accumulated_usage", None) or {}
    return (
        int(getattr(metrics, "cycle_count", 0) or 0),
        int(usage.get("inputTokens", 0) or 0),
        int(usage.get("outputTokens", 0) or 0),
    )


def _metrics(
    ctx: PersonalRunContext | None,
    result: Any,
    started: float,
    error_code: str | None,
    guard: str,
    raw_words: int,
) -> dict[str, Any]:
    cycles, tokens_in, tokens_out = _usage(result)
    if ctx is not None:
        ctx.input_tokens, ctx.output_tokens = tokens_in, tokens_out
    model_id = ctx.model_id if ctx else settings.model_id()
    price = domain_config.get_settings().token_price(model_id)
    usd = (Decimal(tokens_in) * price.input_per_mtok + Decimal(tokens_out) * price.output_per_mtok) / Decimal(1_000_000)
    cad = usd * domain_config.get_settings().usd_to_cad
    return {
        "rounds": cycles or (ctx.rounds if ctx else 0),
        "tool_count": ctx.tool_calls if ctx else 0,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "total_latency_ms": int((time.monotonic() - started) * 1000),
        "model_id_used": model_id,
        "fallback_used": bool(ctx.fallback_used) if ctx else False,
        "error_code": error_code,
        "proposal_sent": bool(ctx.proposal_sent) if ctx else False,
        "reviews_read": bool(ctx and ctx.reviews_read),
        "reviews_flagged": sum(c["n_flagged"] for c in ctx.reviews_read.values()) if ctx else 0,
        "handoff": bool(ctx and ctx.handed_off),
        "limit_reached": bool(ctx and (ctx.tool_calls > ctx.max_tool_calls or ctx.hard_stopped)),
        "reply_guard": guard,
        "reply_words_raw": raw_words,
        "est_cost_cad": f"{cad:.4f}",
        "price_basis": price.basis,
    }
