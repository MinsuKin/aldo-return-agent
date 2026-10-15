"""ALDO bot -> ALDO Fit Gateway client (contracts.md F.8). Two calls, three transports.

``size_assist(request)`` and ``size_decision(request)`` take the C.8 request bodies (plain dicts)
and return the gateway's JSON answer as a dict. Every failure is a ``GatewayCallError``, a
``DomainError`` subclass carrying the gateway's own error code (``UNKNOWN_STYLE`` with its
candidates, ``INVALID_SIZE_CHOICE``, ``STOCK_CHANGED``, ``REQUEST_EXPIRED``,
``DELEGATION_LIMIT_EXCEEDED``, ``NOT_AUTHORIZED``, ...), so callers handle one error type.

Transports (``GATEWAY_TRANSPORT``):

* ``local``: lazy-imports ``returns_domain.gateway`` and calls ``size_assist(claims, request)`` /
  ``decide_size(claims, request)`` with the claims verified from the delegated token (tests,
  local runs, fallback). ``backend=`` injects any object with those two functions.
* ``http``: ``POST {base}/api/gateway/size-assist`` and ``/size-decision`` with
  ``Authorization: Bearer <delegated token>``, 20 s timeout, ONE retry on a 5xx answer or a
  timeout, redirects refused (a bearer token must never follow a redirect), https required unless
  the host is loopback. The base URL is ``GATEWAY_BASE_URL`` or the SSM parameter
  ``/aldo-demo/gateway-url``.
* ``a2a``: Tier 2, not built yet: raises ``NotImplementedError``.

The delegated token is held here and never leaves this module: it is not in the model prompt, tool
arguments, tool results, trace events, ``repr()`` output or error messages.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from returns_domain import auth
from returns_domain.errors import DomainError, ErrorCode

from . import settings
from .log import logger

SIZE_ASSIST_PATH = "/api/gateway/size-assist"
SIZE_DECISION_PATH = "/api/gateway/size-decision"
MAX_RESPONSE_BYTES = 1_000_000
RETRY_DELAY_SECONDS = 0.3
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class GatewayCallError(DomainError):
    """A failed gateway call: the gateway's error code, message and (for UNKNOWN_STYLE) candidates."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        status: int | None = None,
        candidates: list[Any] | None = None,
    ) -> None:
        details = {"candidates": candidates} if candidates else None
        super().__init__(code, message, retryable, details=details)
        self.status = status
        self.candidates = candidates


class GatewayClient(Protocol):
    """What the tools need from the gateway. Implementations raise ``GatewayCallError`` only."""

    def size_assist(self, request: Mapping[str, Any]) -> dict[str, Any]: ...

    def size_decision(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


# ----------------------------------------------------------------------------- local transport


class LocalGatewayClient:
    """In-process transport: verified claims + request dict into ``returns_domain.gateway``."""

    def __init__(self, token: str, *, backend: Any = None) -> None:
        self._token = token
        self._backend_override = backend

    def __repr__(self) -> str:
        return "LocalGatewayClient(token=***)"

    def _backend(self) -> Any:
        if self._backend_override is not None:
            return self._backend_override
        try:
            from returns_domain import gateway  # lazy: pulls in the ALDO returns store
        except ImportError as exc:
            raise GatewayCallError(
                ErrorCode.INTERNAL_ERROR, "the local ALDO gateway is not available"
            ) from exc
        return gateway

    def size_assist(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("size_assist", request)

    def size_decision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("decide_size", request)

    def _call(self, function: str, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            claims = auth.verify_claims(self._token, settings.DELEGATED_SCOPE)
            result = getattr(self._backend(), function)(claims, dict(request))
        except GatewayCallError:
            raise
        except DomainError as exc:
            raise _from_domain_error(exc) from None
        except Exception as exc:  # the cause may quote request data: log the type only
            logger.error("local gateway call failed", extra={"function": function, "error": type(exc).__name__})
            raise GatewayCallError(ErrorCode.INTERNAL_ERROR, "the local ALDO gateway failed") from None
        return _as_dict(result)


def _from_domain_error(exc: DomainError) -> GatewayCallError:
    body = exc.to_dict() if hasattr(exc, "to_dict") else {}
    candidates = body.get("candidates")
    if candidates is None and isinstance(getattr(exc, "details", None), Mapping):
        candidates = exc.details.get("candidates")
    return GatewayCallError(
        exc.code,
        exc.message,
        retryable=exc.retryable,
        candidates=candidates if isinstance(candidates, list) else None,
    )


def _as_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if not isinstance(result, Mapping):
        raise GatewayCallError(ErrorCode.INTERNAL_ERROR, "the ALDO gateway returned an unexpected answer")
    return dict(result)


# ------------------------------------------------------------------------------ http transport


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx surfaces as an HTTPError instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


#: No environment proxies and no redirects: the bearer token goes straight to the gateway.
_DEFAULT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)

_URL_CACHE: dict[str, str] = {}
_URL_LOCK = threading.Lock()


def clear_url_cache() -> None:
    """Forget the SSM-resolved base URL (tests, or after the stack changes URL)."""
    with _URL_LOCK:
        _URL_CACHE.clear()


def checked_base_url(value: object) -> str:
    """A base URL that is safe to send a bearer token to: https, or http on a loopback host."""
    text = value.strip().rstrip("/") if isinstance(value, str) else ""
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return text
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        return text
    raise GatewayCallError(
        ErrorCode.INTERNAL_ERROR,
        "the gateway base URL must be https (http is only allowed for localhost)",
    )


def resolve_base_url(ssm_client: Any = None) -> str:
    """``GATEWAY_BASE_URL``, else the SSM parameter ``/aldo-demo/gateway-url`` (cached)."""
    configured = settings.gateway_base_url()
    if configured:
        return checked_base_url(configured)
    param = settings.gateway_url_param()
    with _URL_LOCK:
        cached = _URL_CACHE.get(param)
    if cached:
        return cached
    try:
        client = ssm_client
        if client is None:
            import boto3  # lazy: keeps the module importable without AWS clients
            from botocore.config import Config

            client = boto3.client(
                "ssm",
                region_name=settings.region(),
                config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}),
            )
        value = client.get_parameter(Name=param)["Parameter"]["Value"]
    except Exception as exc:  # never echo the cause: it can quote request details
        raise GatewayCallError(
            ErrorCode.INTERNAL_ERROR, "the gateway base URL is not configured"
        ) from exc
    checked = checked_base_url(value)
    with _URL_LOCK:
        _URL_CACHE[param] = checked
    return checked


class HttpGatewayClient:
    """HTTPS transport with a delegated bearer token (stdlib urllib, no extra dependency)."""

    def __init__(
        self,
        base_url: str | None,
        token: str,
        timeout_s: float = settings.GATEWAY_TIMEOUT_SECONDS,
        *,
        retries: int = 1,
        opener: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        ssm_client: Any = None,
    ) -> None:
        self._base_url = checked_base_url(base_url) if base_url else None
        self._token = token
        self._timeout = timeout_s
        self._retries = max(0, retries)
        self._opener = opener or _DEFAULT_OPENER
        self._sleep = sleep
        self._ssm_client = ssm_client

    def __repr__(self) -> str:
        return f"HttpGatewayClient(base_url={self._base_url!r}, token=***)"

    def size_assist(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(SIZE_ASSIST_PATH, request)

    def size_decision(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(SIZE_DECISION_PATH, request)

    def _url(self, path: str) -> str:
        base = self._base_url or resolve_base_url(self._ssm_client)
        if base.endswith("/api") and path.startswith("/api/"):
            path = path[len("/api") :]
        return f"{base}{path}"

    def _post(self, path: str, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            body = json.dumps(dict(request), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            raise GatewayCallError(ErrorCode.VALIDATION_ERROR, "the request is not JSON serializable") from None
        url = self._url(path)
        last_status: int | None = None
        for attempt in range(self._retries + 1):
            last_attempt = attempt >= self._retries
            try:
                status, raw = self._send(url, body)
            except (TimeoutError, socket.timeout):
                if last_attempt:
                    raise GatewayCallError(
                        ErrorCode.UPSTREAM_TIMEOUT, "ALDO did not answer in time", retryable=True
                    ) from None
                self._sleep(RETRY_DELAY_SECONDS)
                continue
            except (urllib.error.URLError, ConnectionError, OSError) as exc:
                if _is_timeout(exc) and not last_attempt:
                    self._sleep(RETRY_DELAY_SECONDS)
                    continue
                logger.warning("gateway call failed", extra={"path": path, "error": type(exc).__name__})
                raise GatewayCallError(
                    ErrorCode.UPSTREAM_TIMEOUT, "ALDO could not be reached", retryable=True
                ) from None
            last_status = status
            if status >= 500 and not last_attempt:
                self._sleep(RETRY_DELAY_SECONDS)
                continue
            if 200 <= status < 300:
                return _parse_success(raw, status)
            raise _error_from_response(status, raw)
        raise GatewayCallError(  # unreachable in practice: the loop always returns or raises
            ErrorCode.UPSTREAM_TIMEOUT, "ALDO did not answer", retryable=True, status=last_status
        )

    def _send(self, url: str, body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "aldo-bot/1.0",
            },
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return int(getattr(response, "status", 200)), response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:  # non-2xx, and 3xx because redirects are refused
            try:
                return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
            finally:
                exc.close()


def _is_timeout(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (TimeoutError, socket.timeout))


def _parse_success(raw: bytes, status: int) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GatewayCallError(ErrorCode.INTERNAL_ERROR, "the ALDO gateway answer is too large", status=status)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise GatewayCallError(
            ErrorCode.INTERNAL_ERROR, "the ALDO gateway returned an unexpected answer", status=status
        )
    return parsed


def _error_from_response(status: int, raw: bytes) -> GatewayCallError:
    """Map ``{"error": {"code", "message", "retryable", ...}}`` (or a bare status) to an error."""
    error: Mapping[str, Any] = {}
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        if isinstance(parsed, Mapping) and isinstance(parsed.get("error"), Mapping):
            error = parsed["error"]
    except ValueError:
        pass
    code = error.get("code")
    if isinstance(code, str) and _CODE_PATTERN.match(code):
        message = error.get("message")
        retryable = error.get("retryable")
        candidates = error.get("candidates")
        return GatewayCallError(
            code,
            _short(message) if isinstance(message, str) and message.strip() else f"ALDO answered HTTP {status}",
            retryable=retryable if isinstance(retryable, bool) else None,
            status=status,
            candidates=candidates if isinstance(candidates, list) else None,
        )
    return GatewayCallError(*_fallback_code(status), status=status)


def _fallback_code(status: int) -> tuple[str, str]:
    if status == 401:
        return ErrorCode.NOT_AUTHENTICATED, "ALDO did not accept the delegated token"
    if status == 403:
        return ErrorCode.NOT_AUTHORIZED, "ALDO refused this request"
    if status == 404:
        return ErrorCode.NOT_FOUND, "ALDO could not find that"
    if status in (400, 422):
        return ErrorCode.VALIDATION_ERROR, "ALDO rejected the request"
    if status == 429 or status >= 500:
        return ErrorCode.UPSTREAM_TIMEOUT, f"ALDO is unavailable (HTTP {status})"
    return ErrorCode.INTERNAL_ERROR, f"ALDO answered HTTP {status}"


def _short(text: str, limit: int = 200) -> str:
    cleaned = " ".join(text.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


# ------------------------------------------------------------------------------------- factory


def build_gateway_client(
    token: str,
    *,
    transport: str | None = None,
    base_url: str | None = None,
    **options: Any,
) -> GatewayClient:
    """The client for ``transport`` (default ``GATEWAY_TRANSPORT``) holding the delegated ``token``."""
    name = (transport or settings.gateway_transport()).strip().lower()
    if name == "local":
        return LocalGatewayClient(token, **options)
    if name == "http":
        return HttpGatewayClient(base_url or settings.gateway_base_url(), token, **options)
    if name == "a2a":
        raise NotImplementedError(
            "the a2a gateway transport is Tier 2 and not built yet; use GATEWAY_TRANSPORT=http or local"
        )
    raise DomainError(ErrorCode.INTERNAL_ERROR, "GATEWAY_TRANSPORT must be one of: local, http, a2a")
