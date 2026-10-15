"""The ALDO returns flow routes: orders, conversations, runs, quotes, approve, transactions, cases.

Run lifecycle (contracts.md C.5, design-gap G-05/G-06): the API creates the run as ``queued`` and
holds the conversation lease, the worker claims it (``running``) and exactly one side finishes
it. The approve route generates its ``run_id`` BEFORE the conditional approval put, so replays
return the same approval and the same run.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable, Mapping

from returns_domain import config
from returns_domain.errors import DomainError, ErrorCode

from . import providers
from .http import SESSION, Api, Reply, Request, logger
from .models import ApproveBody, ConversationBody, MessageBody, RequoteBody

cases = providers.lazy("cases")
conversations = providers.lazy("conversations")
execution = providers.lazy("execution")
orders = providers.lazy("orders")
quotes = providers.lazy("quotes")
runs = providers.lazy("runs")
store = providers.lazy("store")
trace = providers.lazy("trace")

#: Customer text may never carry the server's own marker (G-13), on any line, in any width.
_SERVER_MARKER = re.compile(r"^\s*\[server\]", re.IGNORECASE | re.MULTILINE)

#: Runs of the ALDO bot (personal agent) live in its own table; their ids start with this.
PERSONAL_RUN_PREFIX = "PRUN-"

RUN_BUSY_MESSAGE = "Another request is still running in this conversation. Please wait a moment."
ENQUEUE_FAILED_MESSAGE = "We could not start that request. Please try again."


def register(api: Api) -> None:
    @api.get("/orders", auth=SESSION)
    def list_orders(req: Request) -> dict[str, Any]:
        return {"orders": orders.list_orders(req.need_customer())}

    @api.post("/conversations", auth=SESSION, status=201)
    def create_conversation(req: Request) -> dict[str, Any]:
        customer_id = req.need_customer()
        body = req.body_model(ConversationBody, optional=True)
        # An order that is not the caller's is ORDER_NOT_FOUND / NOT_AUTHORIZED (+ audit),
        # checked inside create_conversation.
        return {"conv_id": conversations.create_conversation(customer_id, body.order_id)}

    @api.post("/conversations/<conv>/messages", auth=SESSION, status=202)
    def post_message(req: Request, conv: str) -> dict[str, Any]:
        customer_id = req.need_customer()
        body = req.body_model(MessageBody)
        conversation = conversations.get_conversation(customer_id, conv)
        text = _checked_text(body.text)
        order_id = body.order_id or conversation.get("selected_order_id")
        if body.order_id is not None:
            orders.get_order(customer_id, body.order_id)
            if body.order_id != conversation.get("selected_order_id"):
                conversations.set_selected_order(conv, body.order_id)
        run_id = _start_run(
            api,
            req,
            customer_id=customer_id,
            conv_id=conv,
            kind="customer",
            order_id=order_id,
            message=text,
        )
        return {"run_id": run_id, "conv_id": conv}

    @api.post("/conversations/<conv>/requote", auth=SESSION, status=202)
    def requote(req: Request, conv: str) -> dict[str, Any]:
        customer_id = req.need_customer()
        body = req.body_model(RequoteBody)
        conversations.get_conversation(customer_id, conv)
        quote = quotes.get_quote(customer_id, body.quote_id)
        if _field(quote, "conv_id") != conv:
            raise DomainError(ErrorCode.NOT_FOUND, "That quote does not belong to this conversation.")
        if _field(quote, "status") != "expired":
            raise DomainError(ErrorCode.VALIDATION_ERROR, "Only an expired quote can be re-quoted.")
        run_id = _start_run(
            api,
            req,
            customer_id=customer_id,
            conv_id=conv,
            kind="requote",
            order_id=_field(quote, "order_id"),
            quote_id=body.quote_id,
        )
        return {"run_id": run_id, "conv_id": conv}

    @api.get("/runs/<run>/events", auth=SESSION)
    def run_events(req: Request, run: str) -> dict[str, Any]:
        """Both run families share this route and this response shape (contracts.md C.8)."""
        customer_id = req.need_customer()
        after = req.query_int("after")
        if run.startswith(PERSONAL_RUN_PREFIX):
            return event_page(_personal_run_events(customer_id, run, after))
        runs.get_run(customer_id, run)  # NOT_FOUND / NOT_AUTHORIZED (+ audit)
        return event_page(trace.list_events(run, after))

    @api.get("/quotes/<quote_id>", auth=SESSION)
    def get_quote(req: Request, quote_id: str) -> Any:
        return quotes.get_quote(req.need_customer(), quote_id)

    @api.post("/quotes/<quote_id>/approve", auth=SESSION, status=202)
    def approve(req: Request, quote_id: str) -> Reply:
        return _approve(api, req, quote_id)

    @api.get("/transactions/<transaction_id>", auth=SESSION)
    def get_transaction(req: Request, transaction_id: str) -> Any:
        return execution.get_status(req.need_customer(), transaction_id)

    @api.get("/cases", auth=SESSION)
    def list_cases(req: Request) -> dict[str, Any]:
        return {"cases": cases.list_cases(req.need_customer())}


# ---------------------------------------------------------------------------- shared


def event_page(page: Any) -> dict[str, Any]:
    """The ``{run_id, status, events, next_after}`` body of an ``EventPage`` (or dict)."""
    return {
        "run_id": _field(page, "run_id"),
        "status": _field(page, "status"),
        "events": _field(page, "events"),
        "next_after": _field(page, "next_after"),
    }


def _field(obj: Any, name: str) -> Any:
    """Read ``name`` from a domain pydantic model or a plain dict."""
    return obj[name] if isinstance(obj, dict) else getattr(obj, name)


def _personal_run_events(customer_id: str, run_id: str, after: int) -> Any:
    """Event page of an ALDO bot run, read from the personal-agent table.

    The owner is the token's customer, so another owner's run id is NOT_FOUND / NOT_AUTHORIZED
    inside the personal-agent module. Without that package there are no such runs.
    """
    memory = providers.optional("personal_agent.memory")
    if memory is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Run not found.")
    reader = getattr(memory, "list_run_events", None) or memory.list_events
    return reader(customer_id, run_id, after)


def _marker_view(text: str) -> str:
    """NFKC text, invisible characters removed, every line break as ``\\n`` (marker check only)."""
    lines = unicodedata.normalize("NFKC", text).splitlines()
    return "\n".join(
        "".join(ch for ch in line if unicodedata.category(ch) not in ("Cf", "Cc")) for line in lines
    )


def _checked_text(raw: str) -> str:
    text = raw.strip()
    limit = config.get_settings().max_message_chars
    if not text or len(text) > limit:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"text must be 1 to {limit} characters")
    if _SERVER_MARKER.search(_marker_view(text)):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "text must not start with [server]")
    return text


#: The owner's text checked the same way for every chat route (length, no server marker on any line).
checked_text = _checked_text


def run_payload(
    *,
    run_id: str,
    conv_id: str,
    customer_id: str,
    kind: str,
    order_id: str | None,
    message: str = "",
    quote_id: str | None = None,
    option_id: str | None = None,
    approval_id: str | None = None,
    memory_context: Mapping[str, Any] | None = None,
    selected_order_id: str | None = None,
) -> dict[str, Any]:
    """contracts.md C.6 worker payload for the ALDO return workflow.

    EVERY run of a hand-off's conversation (the single chat: each owner message, the approval, a
    requote) adds two trusted server fields; both keys are absent on every other run:
    ``memory_context`` (addendum 12.3, the summary of the owner's ALDO bot memory) and
    ``selected_order_id`` (the order ``hand_off_to_returns`` named, the same value as ``order_id``;
    without it the return workflow looks up the owner's recent delivered orders).
    """
    payload = {
        "run_id": run_id,
        "conv_id": conv_id,
        "customer_id": customer_id,
        "order_id": order_id,
        "kind": kind,
        "message": message,
        "quote_id": quote_id,
        "option_id": option_id,
        "approval_id": approval_id,
        "agent": "aldo",
    }
    if memory_context:
        payload["memory_context"] = dict(memory_context)
    if selected_order_id:
        payload["selected_order_id"] = selected_order_id
    return payload


def _enqueue(api: Api, payload: dict[str, Any]) -> None:
    """Hand a payload to the worker; every failure becomes a retryable UPSTREAM_TIMEOUT."""
    enqueue_payload(api.enqueue, payload)


def enqueue_payload(enqueue: Callable[[dict[str, Any]], None], payload: dict[str, Any]) -> None:
    """Hand a payload to the worker through ``enqueue``; every failure is a retryable UPSTREAM_TIMEOUT."""
    try:
        enqueue(payload)
    except DomainError:
        raise
    except Exception as exc:
        logger.exception("enqueue_failed", run_id=payload.get("run_id"))
        raise DomainError(ErrorCode.UPSTREAM_TIMEOUT, ENQUEUE_FAILED_MESSAGE, retryable=True) from exc


def _start_run(
    api: Api,
    req: Request,
    *,
    customer_id: str,
    conv_id: str,
    kind: str,
    order_id: str | None,
    message: str = "",
    quote_id: str | None = None,
) -> str:
    """lease -> create run -> enqueue. Undoes what it did when a later step fails."""
    handoff = _handoff_fields(customer_id, conv_id, order_id)
    return start_customer_run(
        api.enqueue,
        customer_id=customer_id,
        conv_id=conv_id,
        kind=kind,
        order_id=order_id,
        message=message,
        quote_id=quote_id,
        memory_context=handoff.get("memory_context"),
        selected_order_id=handoff.get("selected_order_id"),
        annotate=req.annotate,
    )


def _handoff_fields(customer_id: str, conv_id: str, order_id: str | None) -> dict[str, Any]:
    """``memory_context`` and ``selected_order_id`` when the conversation belongs to a hand-off of the single chat.

    Every run of that conversation carries them (an owner message, the approval, a requote); a
    conversation that is not a hand-off gets nothing.
    """
    from . import returns_chat  # late: returns_chat imports this module

    return returns_chat.handoff_fields(customer_id, conv_id, order_id)


def start_customer_run(
    enqueue: Callable[[dict[str, Any]], None],
    *,
    customer_id: str,
    conv_id: str,
    kind: str,
    order_id: str | None,
    message: str = "",
    quote_id: str | None = None,
    memory_context: Mapping[str, Any] | None = None,
    selected_order_id: str | None = None,
    annotate: Callable[..., None] | None = None,
) -> str:
    """lease -> create run -> enqueue the return workflow's run. Undoes what it did when a step fails.

    RUN_IN_PROGRESS when the ALDO-side conversation already has an active run. The one function
    behind ``POST /conversations/<conv>/messages`` and the hand-off messages of the single chat.
    """
    run_id = store.new_id("RUN-", 10)
    if not runs.try_acquire_lease(conv_id, run_id):
        raise DomainError(ErrorCode.RUN_IN_PROGRESS, RUN_BUSY_MESSAGE)
    try:
        runs.create_run(customer_id, conv_id, run_id, kind)
    except Exception:
        _release_lease(conv_id, run_id)
        raise
    if annotate is not None:
        annotate(run_id=run_id)
    payload = run_payload(
        run_id=run_id,
        conv_id=conv_id,
        customer_id=customer_id,
        kind=kind,
        order_id=order_id,
        message=message,
        quote_id=quote_id,
        memory_context=memory_context,
        selected_order_id=selected_order_id,
    )
    try:
        enqueue_payload(enqueue, payload)
    except DomainError:
        _abandon_unstarted_run(run_id, conv_id)
        raise
    return run_id


def _release_lease(conv_id: str, run_id: str) -> None:
    try:
        runs.release_lease(conv_id, run_id)
    except Exception:
        logger.exception("release_lease_failed", run_id=run_id)


def _abandon_unstarted_run(run_id: str, conv_id: str) -> None:
    """The worker never got the run: close it as an error so the lease is released.

    The run is claimed first. If a worker already owns it (an ambiguous enqueue failure, for
    example a timeout after Lambda accepted the call) ``claim_run`` is False and the run is left
    alone, so a run that is really executing is never closed underneath its agent.
    """
    try:
        if runs.claim_run(run_id):
            runs.finish_run(run_id, "error", {"error_code": ErrorCode.UPSTREAM_TIMEOUT.value})
    except Exception:
        logger.exception("abandon_run_failed", run_id=run_id)
        _release_lease(conv_id, run_id)


# ---------------------------------------------------------------------------- approve


def _approve(api: Api, req: Request, quote_id: str) -> Reply:
    """contracts.md C.4, in this exact order."""
    customer_id = req.need_customer()
    body = req.body_model(ApproveBody)
    run_id = store.new_id("RUN-", 10)  # before the conditional put, so replays share it
    quote = quotes.get_quote(customer_id, quote_id)
    conv_id = _field(quote, "conv_id")

    def guard() -> None:
        # Only runs for a NEW approval (replays return earlier inside quotes.approve), so a
        # blocked approve leaves no side effects at all.
        if runs.lease_active(conv_id):
            raise DomainError(ErrorCode.RUN_IN_PROGRESS, RUN_BUSY_MESSAGE)

    approval, created = quotes.approve(
        customer_id, quote_id, body.option_id, body.payload_hash, run_id, before_create=guard
    )
    approval_run_id = _field(approval, "run_id")
    req.annotate(run_id=approval_run_id)
    if created:
        _launch_approval_run(api, customer_id, quote, approval)
        return Reply(_approval_body(approval, replayed=False), status=202)
    _heal_replayed_approval(api, customer_id, quote, approval)
    return Reply(_approval_body(approval, replayed=True), status=200)


def _approval_body(approval: Any, *, replayed: bool) -> dict[str, Any]:
    return {
        "approval_id": _field(approval, "approval_id"),
        "run_id": _field(approval, "run_id"),
        "quote_id": _field(approval, "quote_id"),
        "option_id": _field(approval, "option_id"),
        "replayed": replayed,
    }


def _approval_payload(customer_id: str, quote: Any, approval: Any) -> dict[str, Any]:
    order_id = _field(quote, "order_id")
    handoff = _handoff_fields(customer_id, _field(quote, "conv_id"), order_id)
    return run_payload(
        run_id=_field(approval, "run_id"),
        conv_id=_field(quote, "conv_id"),
        customer_id=customer_id,
        kind="approval",
        order_id=order_id,
        quote_id=_field(approval, "quote_id"),
        option_id=_field(approval, "option_id"),
        approval_id=_field(approval, "approval_id"),
        memory_context=handoff.get("memory_context"),
        selected_order_id=handoff.get("selected_order_id"),
    )


def _launch_approval_run(api: Api, customer_id: str, quote: Any, approval: Any) -> None:
    """The ``created=True`` steps: lease, RUN item, ``approval_received`` as seq 1, enqueue.

    The approval is already stored. When a later step fails the approval stays, and the
    client's retry takes the replay path, which finishes the job (``_heal_replayed_approval``).
    """
    conv_id = _field(quote, "conv_id")
    run_id = _field(approval, "run_id")
    if not runs.try_acquire_lease(conv_id, run_id):
        raise DomainError(ErrorCode.RUN_IN_PROGRESS, RUN_BUSY_MESSAGE)
    if not runs.create_run(customer_id, conv_id, run_id, "approval"):
        return  # a concurrent request created the run first; it emits the event and enqueues
    trace.emit(
        run_id,
        "approval_received",
        {
            "approval_id": _field(approval, "approval_id"),
            "quote_id": _field(approval, "quote_id"),
            "option_id": _field(approval, "option_id"),
        },
    )
    _enqueue(api, _approval_payload(customer_id, quote, approval))


def _heal_replayed_approval(api: Api, customer_id: str, quote: Any, approval: Any) -> None:
    """A replayed approve: finish whatever the first request did not (G-06)."""
    run_id = _field(approval, "run_id")
    try:
        status = runs.get_run(customer_id, run_id)["status"]
    except DomainError as exc:
        if exc.code != ErrorCode.NOT_FOUND.value:
            raise
        _launch_approval_run(api, customer_id, quote, approval)  # the RUN item never existed
        return
    if status == "queued":
        _enqueue(api, _approval_payload(customer_id, quote, approval))  # worker claim dedups
