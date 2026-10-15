"""ALDO -> ALDO bot write-back transport (PIVOT_ADDENDUM 5.4, 10.3; contracts.md F.5).

ALDO pushes a size write-back to the owner's personal agent through ``PersonalAgentClient``.
The dependency points one way: this module never imports ``personal_agent``. The ``local``
transport receives a handler callable from its caller (tests and the worker bootstrap register
``personal_agent.memory.apply_memory_update`` with ``register_local_handler``), and the ``http``
transport talks to the deployed route.

Transports (``GATEWAY_TRANSPORT``, deployed default ``http``):

* ``local``: calls the registered handler in process.
* ``http`` (and ``a2a``): ``POST {base_url}/api/personal-agent/memory-updates``. The body is
  ``json.dumps(update, sort_keys=True, separators=(",", ":"))`` and the header
  ``X-ALDO-Signature`` is ``sign_body(body, key)``, the lowercase hex HMAC-SHA256 of exactly
  those bytes under ``config.get_writeback_key()`` (derived from the token key, no extra
  secret). One attempt, 5 second timeout, no retry here: the outbox attempts counter in
  ``memory_updates`` owns retries. Redirects and environment proxies are refused, so a signed
  body can only reach the configured host. The base URL must be https (http only on loopback).
  ``a2a`` changes only how the personal agent calls the gateway; the write-back stays this push.

``GATEWAY_BASE_URL`` falls back to the SSM parameter ``/aldo-demo/gateway-url``, resolved at
runtime (cached) so the CDK stack needs no reference to its own CloudFront URL.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import config
from .errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

TRANSPORTS: tuple[str, ...] = config.GATEWAY_TRANSPORTS  # local | http | a2a
SIGNATURE_HEADER = "X-ALDO-Signature"
MEMORY_UPDATES_PATH = "/api/personal-agent/memory-updates"
SSM_GATEWAY_URL_PARAM = "/aldo-demo/gateway-url"
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 64 * 1024
MIN_KEY_BYTES = 16

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
#: HTTP statuses worth another attempt from the outbox; every other non-2xx is a rejection.
_TRANSIENT_STATUSES = frozenset({408, 425, 429})


class PersonalAgentClient(Protocol):
    """ALDO -> the owner's personal agent. Raises on failure, returns the receiver's answer."""

    def send_memory_update(self, update: Mapping[str, Any]) -> dict:
        """Deliver one write-back (addendum 5.4). The receiver is idempotent on ``update_id``.

        Returns ``{"update_id", "applied": bool, "duplicate": bool}``; ``duplicate`` is true
        when the receiver already had this ``update_id``. A transport failure raises
        ``DomainError(UPSTREAM_TIMEOUT)`` (retryable).
        """
        ...


# ------------------------------------------------------------------------------ signature


def _key_bytes(key: object) -> bytes:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"the write-back key must be at least {MIN_KEY_BYTES} bytes",
        )
    return bytes(key)


def _body_bytes(raw_body: object) -> bytes:
    if not isinstance(raw_body, (bytes, bytearray)):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "the signed body must be raw bytes")
    return bytes(raw_body)


def sign_body(raw_body: bytes, key: bytes) -> str:
    """Lowercase hex HMAC-SHA256 of the raw body: the ``X-ALDO-Signature`` value (no prefix)."""
    return hmac.new(_key_bytes(key), _body_bytes(raw_body), hashlib.sha256).hexdigest()


def verify_signature(raw_body: bytes, header_value: str | None, key: bytes) -> None:
    """Constant-time check of ``X-ALDO-Signature`` over the RAW body; NOT_AUTHENTICATED if bad."""
    if not isinstance(header_value, str) or not header_value.strip():
        raise DomainError(ErrorCode.NOT_AUTHENTICATED, "missing signature")
    expected = sign_body(raw_body, key)
    provided = header_value.strip().lower()
    if not hmac.compare_digest(expected.encode("ascii"), provided.encode("utf-8")):
        raise DomainError(ErrorCode.NOT_AUTHENTICATED, "invalid signature")


# ------------------------------------------------------------------------------- local


class LocalPersonalAgentClient:
    """In-process delivery through an injected handler (``apply_memory_update``)."""

    def __init__(self, handler: Callable[[Mapping[str, Any]], dict]) -> None:
        if not callable(handler):
            raise DomainError(ErrorCode.INTERNAL_ERROR, "the local handler must be callable")
        self._handler = handler

    def send_memory_update(self, update: Mapping[str, Any]) -> dict:
        result = self._handler(update)
        if not isinstance(result, Mapping):
            raise DomainError(
                ErrorCode.INTERNAL_ERROR, "the personal agent returned an unexpected answer"
            )
        return dict(result)


_LOCAL_HANDLER: Callable[[Mapping[str, Any]], dict] | None = None


def register_local_handler(handler: Callable[[Mapping[str, Any]], dict] | None) -> None:
    """Install (or, with None, remove) the handler the ``local`` transport calls.

    Tests and the worker bootstrap pass ``personal_agent.memory.apply_memory_update``.
    """
    global _LOCAL_HANDLER
    _LOCAL_HANDLER = handler


# --------------------------------------------------------------------------------- http


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a 3xx surfaces as an HTTPError instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


#: No environment proxies and no redirects: the signed body goes straight to our own endpoint.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)


def checked_base_url(value: object) -> str:
    """A base URL the signed write-back may be sent to: https, or http on a loopback host."""
    text = value.strip().rstrip("/") if isinstance(value, str) else ""
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return text
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        return text
    raise DomainError(
        ErrorCode.INTERNAL_ERROR,
        "the gateway base URL must be https (http is only allowed for localhost)",
    )


_URL_CACHE: dict[str, str] = {}
_URL_LOCK = threading.Lock()


def clear_cache() -> None:
    """Forget the SSM-resolved base URL (tests, or after the stack changes URL)."""
    with _URL_LOCK:
        _URL_CACHE.clear()


def _read_ssm_parameter(name: str, region: str) -> str:
    import boto3  # lazy: keeps the module importable without AWS clients
    from botocore.config import Config

    client = boto3.client(
        "ssm",
        region_name=region,
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}),
    )
    return client.get_parameter(Name=name)["Parameter"]["Value"]


def resolve_base_url() -> str:
    """``GATEWAY_BASE_URL``, else the SSM parameter ``/aldo-demo/gateway-url`` (cached)."""
    settings = config.get_settings()
    if settings.gateway_base_url:
        return checked_base_url(settings.gateway_base_url)
    with _URL_LOCK:
        cached = _URL_CACHE.get(SSM_GATEWAY_URL_PARAM)
    if cached:
        return cached
    try:
        value = checked_base_url(_read_ssm_parameter(SSM_GATEWAY_URL_PARAM, settings.region))
    except DomainError:
        raise
    except Exception as exc:  # never echo the cause: it can quote request details
        raise DomainError(
            ErrorCode.INTERNAL_ERROR, "the gateway base URL is not configured"
        ) from exc
    with _URL_LOCK:
        _URL_CACHE[SSM_GATEWAY_URL_PARAM] = value
    return value


def _encode(update: Mapping[str, Any]) -> bytes:
    """The raw body: exactly these bytes are signed and sent."""
    try:
        return json.dumps(update, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, "the memory update is not JSON serializable"
        ) from None


def _answer(raw: bytes) -> dict[str, Any]:
    """The receiver's JSON object; a 2xx with an empty or odd body still counts as delivered."""
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rejected(status: int) -> DomainError:
    if status in (401, 403):
        return DomainError(
            ErrorCode.NOT_AUTHENTICATED,
            f"the personal agent refused the signature (HTTP {status})",
            details={"status": status},
        )
    transient = status in _TRANSIENT_STATUSES or status >= 500
    return DomainError(
        ErrorCode.UPSTREAM_TIMEOUT if transient else ErrorCode.VALIDATION_ERROR,
        f"the personal agent answered HTTP {status}",
        retryable=transient,
        details={"status": status},
    )


class HttpPersonalAgentClient:
    """Signed ``POST`` of the update to the personal agent's memory-updates route."""

    def __init__(
        self, base_url: str, key: bytes, timeout_s: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._base_url = checked_base_url(base_url)
        self._key = _key_bytes(key)
        self._timeout = timeout_s

    def __repr__(self) -> str:
        return f"HttpPersonalAgentClient(base_url={self._base_url!r}, key=***)"

    @property
    def url(self) -> str:
        return f"{self._base_url}{MEMORY_UPDATES_PATH}"

    def send_memory_update(self, update: Mapping[str, Any]) -> dict:
        body = _encode(update)
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                SIGNATURE_HEADER: sign_body(body, self._key),
            },
        )
        try:
            with _OPENER.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as exc:  # non-2xx, and 3xx because redirects are refused
            status = exc.code
            exc.close()
            raise _rejected(status) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            logger.warning("write-back delivery failed: %s", type(exc).__name__)
            raise DomainError(
                ErrorCode.UPSTREAM_TIMEOUT, "the personal agent could not be reached"
            ) from exc
        return _answer(raw)


# --------------------------------------------------------------------------------- factory


def get_client(
    transport: str | None = None,
    *,
    base_url: str | None = None,
    key: bytes | None = None,
    handler: Callable[[Mapping[str, Any]], dict] | None = None,
) -> PersonalAgentClient:
    """The write-back client for ``transport`` (default: ``GATEWAY_TRANSPORT``).

    ``local`` uses ``handler`` or the registered one (none: INTERNAL_ERROR); ``http`` and
    ``a2a`` use ``base_url`` or ``resolve_base_url()`` and ``key`` or the derived write-back key.
    """
    name = config.get_settings().gateway_transport if transport is None else transport
    name = name.strip().lower() if isinstance(name, str) else ""
    if name == "local":
        chosen = handler if handler is not None else _LOCAL_HANDLER
        if chosen is None:
            raise DomainError(
                ErrorCode.INTERNAL_ERROR, "no local personal agent handler is registered"
            )
        return LocalPersonalAgentClient(chosen)
    if name in ("http", "a2a"):
        return HttpPersonalAgentClient(
            base_url if base_url is not None else resolve_base_url(),
            key if key is not None else config.get_writeback_key(),
        )
    raise DomainError(
        ErrorCode.VALIDATION_ERROR, f"transport must be one of: {', '.join(TRANSPORTS)}"
    )


#: Name used by contracts.md F.5.
get_personal_agent_client = get_client
