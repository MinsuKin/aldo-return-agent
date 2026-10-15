"""ALDO bot routes (Powertools ``Router``; the API Lambda includes it with ``include_router``).

Registered WITHOUT the ``/api`` prefix (the resolver strips it), like every route of the API Lambda:

* ``POST /personal-agent/conversations``                      session token -> ``201 {"conv_id"}``
* ``POST /personal-agent/conversations/<conv>/messages``       session token -> ``202 {"run_id", "conv_id"}``
* ``GET  /personal-agent/memory``                              session token -> ``200 OwnerMemory``
* ``GET  /personal-agent/runs/<run>/events?after=n``           session token -> ``200 {run_id, status, events, next_after}``
* ``POST /personal-agent/memory-updates``                      HMAC ``X-ALDO-Signature`` ONLY (no bearer)

Auth rules: the session routes call ``auth.customer_id_from_authorization`` with the default
``session`` scope, so a delegated (gateway) token is refused with 403 NOT_AUTHORIZED; the
customer id comes only from the verified token. ``memory-updates`` takes no bearer at all: the
signature is the hex HMAC-SHA256 of the RAW body under the write-back key
(``auth.verify_writeback_signature``) and is checked BEFORE the body is parsed.

``POST .../messages`` validates 1..1000 characters, allows one active run per conversation
(409 ``RUN_IN_PROGRESS``, enforced by a lease in the personal-agent table), creates the run and
starts it through the injected ``enqueue(payload)`` (the API wires the worker; the payload is
``{"agent": "personal", "run_id", "conv_id", "customer_id", "message", "kind": "customer"}``).

Errors use the contracts.md C.1 body ``{"error": {"code", "message", "retryable", "request_id"}}``;
this module renders them itself so it also works in an app without the API's global middleware.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import re
import uuid
from typing import Any, Callable

from aws_lambda_powertools.event_handler import Response, content_types
from aws_lambda_powertools.event_handler.api_gateway import Router

from personal_agent import memory, settings
from personal_agent.log import logger
from returns_domain import auth
from returns_domain.errors import DomainError, ErrorCode

MAX_BODY_BYTES = 16 * 1024
GENERIC_ERROR = "Something went wrong on our side. Please try again."
BUSY_MESSAGE = "ALDO bot is still working on your last message. Wait for it to finish."
ENQUEUE_FAILED = "ALDO bot could not start. Please try again."
SIGNATURE_HEADER = "x-aldo-signature"

_PATH_PARAM = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_INT_QUERY = re.compile(r"^\d{1,9}$")

Enqueue = Callable[[dict[str, Any]], None]
BeforeMemoryRead = Callable[[str], None]


# --------------------------------------------------------------------------------- helpers


def default_enqueue(payload: dict[str, Any]) -> None:
    """Start the worker through the API's ``worker.enqueue`` (lazy: that package belongs to the API)."""
    try:
        from worker.enqueue import enqueue_async
    except ImportError as exc:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR, "the ALDO bot worker is not configured", retryable=False
        ) from exc
    enqueue_async(payload)


def events_for(owner_id: str, run_id: str, after: int = 0) -> dict[str, Any]:
    """Event page of an ALDO bot run for ``GET /api/runs/<run>/events`` when the id starts with ``PRUN-``."""
    return memory.get_repo().list_events(owner_id, run_id, after)


def _request_id(router: Router) -> str:
    try:
        return str(router.current_event.request_context.request_id or uuid.uuid4())
    except Exception:
        return str(uuid.uuid4())


def _json(status: int, body: Any, request_id: str) -> Response:
    return Response(
        status_code=status,
        content_type=content_types.APPLICATION_JSON,
        body=json.dumps(memory.jsonable(body), separators=(",", ":"), allow_nan=False),
        headers={"Cache-Control": "no-store", "X-Request-Id": request_id},
    )


def _error(exc: DomainError, request_id: str) -> Response:
    error: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
        "request_id": request_id,
    }
    if exc.details:
        error["details"] = exc.details
    return _json(exc.http_status, {"error": error}, request_id)


def _undo_start(repo: memory.MemoryRepo, owner: str, conv_id: str, run_id: str) -> None:
    """The worker could not be started: mark the run failed and free the conversation (best effort)."""
    try:
        repo.finish_run(owner, run_id, "error", {"error_code": ErrorCode.UPSTREAM_TIMEOUT.value})
        repo.release_lease(owner, conv_id, run_id)
    except Exception as exc:
        logger.warning("could not undo a failed start", extra={"run_id": run_id, "error": type(exc).__name__})


def _path_param(value: str) -> str:
    if not isinstance(value, str) or not _PATH_PARAM.match(value):
        raise DomainError(ErrorCode.NOT_FOUND, "Not found.")
    return value


def _raw_body(router: Router) -> bytes:
    event = router.current_event
    body = event.body
    if body is None:
        return b""
    if event.is_base64_encoded:
        try:
            raw = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "request body could not be decoded") from None
    else:
        raw = body.encode("utf-8")
    if len(raw) > MAX_BODY_BYTES:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "request body is too large")
    return raw


def _json_object(raw: bytes, *, required: bool) -> dict[str, Any]:
    if not raw.strip():
        if required:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "a JSON request body is required")
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "request body must be valid JSON") from None
    if not isinstance(data, dict):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "request body must be a JSON object")
    return data


def _message_text(body: dict[str, Any]) -> str:
    text = body.get("text")
    if not isinstance(text, str):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "text is required")
    text = text.strip()
    limit = settings.max_message_chars()
    if not 1 <= len(text) <= limit:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"text must be 1 to {limit} characters")
    if text.lower().startswith("[server]"):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "text must not start with [server]")
    return text


# ------------------------------------------------------------------------------ the router


def build_router(
    *,
    enqueue: Enqueue | None = None,
    before_memory_read: BeforeMemoryRead | None = None,
    repo_provider: Callable[[], memory.MemoryRepo] | None = None,
) -> Router:
    """A fresh router with its collaborators bound (one router per resolver).

    ``enqueue`` starts a run in the worker (default: ``worker.enqueue.enqueue_async``).
    ``before_memory_read(owner_id)`` runs before ``GET /memory`` so pending write-backs can be
    delivered first (failures are logged and ignored). ``repo_provider`` returns the repository.
    """
    router = Router()
    start_run = enqueue or default_enqueue
    get_repo = repo_provider or memory.get_repo

    def guarded(handler: Callable[..., Response]) -> Callable[..., Response]:
        @functools.wraps(handler)
        def wrapper(*args: Any, **kwargs: Any) -> Response:
            request_id = _request_id(router)
            try:
                return handler(request_id, *args, **kwargs)
            except DomainError as exc:
                return _error(exc, request_id)
            except Exception:
                logger.exception("personal agent route failed", extra={"request_id": request_id})
                return _error(DomainError(ErrorCode.INTERNAL_ERROR, GENERIC_ERROR), request_id)

        return wrapper

    def owner_id() -> str:
        """Verified customer id of a SESSION token; a delegated token is refused (403)."""
        return auth.customer_id_from_authorization(
            router.current_event.get_header_value("authorization", case_sensitive=False)
        )

    @router.post("/personal-agent/conversations")
    @guarded
    def create_conversation(request_id: str) -> Response:
        owner = owner_id()
        _json_object(_raw_body(router), required=False)
        repo = get_repo()
        if repo.get_owner(owner) is None:
            raise DomainError(ErrorCode.NOT_FOUND, "no ALDO bot profile for this customer")
        return _json(201, {"conv_id": repo.create_conversation(owner)}, request_id)

    @router.post("/personal-agent/conversations/<conv>/messages")
    @guarded
    def post_message(request_id: str, conv: str) -> Response:
        owner = owner_id()
        conv_id = _path_param(conv)
        text = _message_text(_json_object(_raw_body(router), required=True))
        repo = get_repo()
        repo.get_conversation(owner, conv_id)  # NOT_FOUND also covers another owner's conversation
        run_id = memory.new_run_id()
        if not repo.try_acquire_lease(owner, conv_id, run_id):
            raise DomainError(ErrorCode.RUN_IN_PROGRESS, BUSY_MESSAGE)
        try:
            repo.create_run(owner, conv_id, run_id)
            start_run(
                {
                    "agent": "personal",
                    "run_id": run_id,
                    "conv_id": conv_id,
                    "customer_id": owner,
                    "message": text,
                    "kind": "customer",
                }
            )
        except Exception as exc:
            logger.warning(
                "personal agent run could not be started",
                extra={"run_id": run_id, "error": type(exc).__name__},
            )
            _undo_start(repo, owner, conv_id, run_id)
            raise DomainError(ErrorCode.UPSTREAM_TIMEOUT, ENQUEUE_FAILED, retryable=True) from None
        return _json(202, {"run_id": run_id, "conv_id": conv_id}, request_id)

    @router.get("/personal-agent/memory")
    @guarded
    def get_memory(request_id: str) -> Response:
        owner = owner_id()
        if before_memory_read is not None:
            try:
                before_memory_read(owner)
            except Exception as exc:  # delivering write-backs is best effort
                logger.warning("before_memory_read failed", extra={"error": type(exc).__name__})
        return _json(200, get_repo().list_memory(owner), request_id)

    @router.get("/personal-agent/runs/<run>/events")
    @guarded
    def get_run_events(request_id: str, run: str) -> Response:
        owner = owner_id()
        run_id = _path_param(run)
        raw_after = router.current_event.get_query_string_value("after", default_value="0")
        if not isinstance(raw_after, str) or not _INT_QUERY.match(raw_after):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "after must be an integer of 0 or more")
        return _json(200, get_repo().list_events(owner, run_id, int(raw_after)), request_id)

    @router.post("/personal-agent/memory-updates")
    @guarded
    def post_memory_update(request_id: str) -> Response:
        raw = _raw_body(router)
        # HMAC only, no bearer: verify the signature over the raw bytes BEFORE parsing anything.
        auth.verify_writeback_signature(
            raw, router.current_event.get_header_value(SIGNATURE_HEADER, case_sensitive=False)
        )
        result = get_repo().apply_update(_json_object(raw, required=True))
        body = {key: result[key] for key in ("update_id", "applied", "duplicate", "provisional")}
        return _json(200, {"ok": True, **body}, request_id)

    return router


#: Default router for an app that does not need to inject anything (worker resolved lazily).
router = build_router()
