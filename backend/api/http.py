"""HTTP plumbing shared by every route: auth, JSON bodies, the error envelope, logging.

* Every route is registered through ``Api.route`` with an explicit auth kind, so a route can
  never forget to authenticate (a test sweeps the registry and the resolver's route list).
* One global middleware turns every outcome into the contracts.md C.1 shape: JSON bodies,
  ``x-request-id`` and ``cache-control: no-store`` on every response, ``DomainError`` mapped
  through ``errors.HTTP_STATUS``, unexpected exceptions answered with a generic 500 (the
  traceback only goes to the log) and one structured log line per request.
* Customer identity comes only from the verified token (``Request.customer_id``). Bodies and
  path parameters never carry it.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TypeVar

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from pydantic import BaseModel, ValidationError

from returns_domain import auth
from returns_domain.errors import DomainError, ErrorCode

from . import providers

store = providers.lazy("store")

logger = Logger(service=os.environ.get("POWERTOOLS_SERVICE_NAME", "returns-api"))

PUBLIC = "public"
SESSION = "session"
DELEGATED = "delegated_gateway"
ADMIN = "admin"
AUTH_KINDS = (PUBLIC, SESSION, DELEGATED, ADMIN)

MAX_BODY_BYTES = 16 * 1024
GENERIC_ERROR_MESSAGE = "Something went wrong on our side. Please try again."

_PATH_PARAM = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_INT_QUERY = re.compile(r"^-?\d{1,10}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}(:\d{1,5})?$")

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class Reply:
    """What a route returns when the default ``200`` and a plain dict are not enough."""

    body: Any = None
    status: int = 200
    #: A body that is already JSON text (served verbatim, for example ``GET /impact``).
    raw_json: str | None = None


@dataclass(frozen=True)
class RouteSpec:
    method: str
    path: str
    auth: str


@dataclass
class Request:
    """The authenticated view of one request handed to a route function."""

    resolver: APIGatewayHttpResolver
    request_id: str
    customer_id: str | None = None
    claims: auth.TokenClaims | None = None

    @property
    def event(self) -> Any:
        return self.resolver.current_event

    def header(self, name: str) -> str | None:
        value = self.event.headers.get(name)
        return value if isinstance(value, str) else None

    def annotate(self, **fields: Any) -> None:
        """Add fields (for example ``run_id``) to this request's log line."""
        self.resolver.context.setdefault("_log", {}).update(fields)

    # ------------------------------------------------------------------------- input

    def body_dict(self, *, optional: bool = False) -> dict[str, Any]:
        """The JSON request body as an object (<= 16 KB); every failure is a VALIDATION_ERROR.

        ``optional=True`` turns an absent or blank body into ``{}``.
        """
        try:
            raw = self.event.decoded_body
        except ValueError:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "request body could not be decoded") from None
        if raw is None or not raw.strip():
            if optional:
                return {}
            raise DomainError(ErrorCode.VALIDATION_ERROR, "a JSON request body is required")
        if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "request body is too large")
        try:
            data = json.loads(raw)
        except ValueError:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "request body must be valid JSON") from None
        if not isinstance(data, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "request body must be a JSON object")
        return data

    def body_model(self, model: type[M], *, optional: bool = False) -> M:
        """The JSON body validated as ``model``; every failure is a VALIDATION_ERROR."""
        data = self.body_dict(optional=optional)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise DomainError(ErrorCode.VALIDATION_ERROR, _first_error(exc)) from None

    def query_int(self, name: str, *, default: int = 0, minimum: int = 0, maximum: int = 10**9) -> int:
        raw = self.event.query_string_parameters.get(name)
        if raw is None or raw == "":
            return default
        if not _INT_QUERY.match(raw) or not minimum <= int(raw) <= maximum:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"{name} must be an integer between {minimum} and {maximum}",
            )
        return int(raw)

    def request_origin(self) -> str:
        """The origin this request came in on, as far as a header can be trusted to say.

        Behind CloudFront this is the execute-api host, NOT the public one, so absolute URLs
        come from configuration first (see ``routes_gateway.public_base_url``). Hosts that do
        not look like host names are ignored in favour of the API Gateway domain name.
        """
        host = self.header("x-forwarded-host") or self.header("host")
        if not host or not _HOST.match(host):
            host = getattr(self.event.request_context, "domain_name", None) or "localhost"
        proto = self.header("x-forwarded-proto")
        scheme = proto if proto in ("http", "https") else "https"
        return f"{scheme}://{host}"

    # -------------------------------------------------------------------------- auth

    def authenticate(self, kind: str) -> None:
        if kind == PUBLIC:
            return
        if kind == ADMIN:
            auth.verify_admin_secret(self.header("x-demo-admin"))
            return
        scope = auth.SCOPE_SESSION if kind == SESSION else auth.SCOPE_DELEGATED_GATEWAY
        self.claims = auth.claims_from_authorization(self.header("authorization"), scope)
        self.customer_id = self.claims.customer_id

    def need_customer(self) -> str:
        if not self.customer_id:  # a route registered with the wrong auth kind: fail closed
            raise DomainError(ErrorCode.NOT_AUTHENTICATED, "authentication required")
        return self.customer_id


class Api:
    """The resolver plus the registry of routes and the injected collaborators."""

    def __init__(self, resolver: APIGatewayHttpResolver, *, enqueue: Callable[[dict], None]) -> None:
        self.resolver = resolver
        #: Starts a run in the worker; replaced by a fake in tests.
        self.enqueue = enqueue
        self.routes: list[RouteSpec] = []

    def route(self, method: str, path: str, *, auth: str, status: int = 200) -> Callable:
        """Register ``func(request, **path_params)``. ``auth`` is one of ``AUTH_KINDS`` (required)."""
        kind = auth  # the keyword reads well at call sites; ``auth`` is also the token module
        if kind not in AUTH_KINDS:
            raise ValueError(f"unknown auth kind {kind!r}")

        def decorator(func: Callable) -> Callable:
            def endpoint(**path_params: str) -> Response:
                for value in path_params.values():
                    if not _PATH_PARAM.match(value):
                        raise DomainError(ErrorCode.NOT_FOUND, "Not found.")
                request = Request(self.resolver, self.resolver.context["_request_id"])
                request.authenticate(kind)
                return to_response(func(request, **path_params), default_status=status)

            endpoint.__name__ = func.__name__
            endpoint.__doc__ = func.__doc__
            self.resolver.route(path, method)(endpoint)
            self.routes.append(RouteSpec(method, path, kind))
            return func

        return decorator

    def get(self, path: str, *, auth: str, status: int = 200) -> Callable:
        return self.route("GET", path, auth=auth, status=status)

    def post(self, path: str, *, auth: str, status: int = 200) -> Callable:
        return self.route("POST", path, auth=auth, status=status)


# ------------------------------------------------------------------------- responses


def to_response(result: Any, *, default_status: int = 200) -> Response:
    if isinstance(result, Response):
        return result
    if isinstance(result, Reply):
        if result.raw_json is not None:
            return _json_response(result.status, result.raw_json)
        return _json_response(result.status, _dumps(result.body))
    return _json_response(default_status, _dumps(result))


def _dumps(body: Any) -> str:
    return json.dumps(store.json_safe(body), separators=(",", ":"), allow_nan=False)


def _json_response(status: int, text: str) -> Response:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=text)


def error_response(status: int, error: Mapping[str, Any], request_id: str) -> Response:
    """contracts.md C.1 error body: ``{"error": {code, message, retryable, request_id, ...}}``.

    ``error`` is a ``DomainError.to_dict()`` (``GatewayError`` adds ``candidates`` to it); its
    content is small, JSON-safe and never secret.
    """
    body = {"error": {**error, "request_id": request_id}}
    return _json_response(status, json.dumps(body, separators=(",", ":"), default=str))


def simple_error(status: int, code: str, message: str, request_id: str, *, retryable: bool = False) -> Response:
    return error_response(
        status, {"code": code, "message": message, "retryable": retryable}, request_id
    )


def _first_error(exc: ValidationError) -> str:
    """One safe sentence for a pydantic failure: where and what, never the offending value."""
    problems = exc.errors(include_input=False, include_url=False, include_context=False)
    if not problems:
        return "request body is invalid"
    problem = problems[0]
    where = ".".join(str(part) for part in problem.get("loc", ())) or "body"
    return f"{where}: {problem.get('msg', 'is invalid')}"


# ------------------------------------------------------------------------ middleware


def request_id_for(resolver: APIGatewayHttpResolver) -> str:
    lambda_context = getattr(resolver, "lambda_context", None)
    request_id = getattr(lambda_context, "aws_request_id", None)
    if request_id:
        return str(request_id)
    context = resolver.current_event.get("requestContext") or {}
    return str(context.get("requestId") or uuid.uuid4())


def observe(resolver: APIGatewayHttpResolver, next_middleware: Callable[..., Response]) -> Response:
    """Global middleware: error envelope, standard headers and the structured access log."""
    started = time.perf_counter()
    request_id = request_id_for(resolver)
    fields: dict[str, Any] = {"request_id": request_id}
    resolver.append_context(_request_id=request_id, _log=fields)
    try:
        response = next_middleware(resolver)
    except DomainError as exc:
        response = error_response(exc.http_status, exc.to_dict(), request_id)
        fields["outcome"] = exc.code
    except Exception:
        # The traceback goes to the log only; the caller gets a generic message.
        logger.exception("unhandled_error", request_id=request_id)
        response = simple_error(
            500, ErrorCode.INTERNAL_ERROR.value, GENERIC_ERROR_MESSAGE, request_id
        )
        fields["outcome"] = ErrorCode.INTERNAL_ERROR.value

    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "no-store"
    status = response.status_code
    fields.setdefault("outcome", "ok" if status < 400 else f"http_{status}")
    matched = resolver.context.get("_route")
    log = logger.warning if status >= 500 else logger.info
    log(
        "request",
        route=f"{matched.method} {matched.path}"[:160] if matched is not None else "unmatched",
        status=status,
        latency_ms=int((time.perf_counter() - started) * 1000),
        **fields,
    )
    return response
