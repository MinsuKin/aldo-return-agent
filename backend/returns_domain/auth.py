"""HMAC-signed demo tokens (requirement 1, PIVOT_ADDENDUM C4/C7/5.1), the demo admin secret
check and the write-back body signature.

Token format: ``b64url(payload_json) + "." + b64url(HMAC-SHA256(key, b64url(payload_json)))``.
Payload claims: ``v`` (1), ``customer_id``, ``scope``, ``iat``, ``exp`` (epoch seconds) and,
for delegated tokens only, ``actor`` and ``max_amount_cents``. The signature covers the
payload segment exactly as transmitted, so there is no JSON canonicalization to get wrong.

Two scopes keep the browser session and the personal agent apart:

* ``session``: minted by ``POST /demo/sessions``, 8 hours, accepted by every non-gateway route.
* ``delegated_gateway``: minted server-side for the personal agent, 10 minutes, carries
  ``actor`` and a ``max_amount_cents`` cap, accepted ONLY by gateway routes.

Verification order is signature, expiry, then scope: bad signature, malformed or expired
tokens raise ``NOT_AUTHENTICATED`` (HTTP 401); a genuine token of the wrong scope raises
``NOT_AUTHORIZED`` (HTTP 403). ``required_scope`` defaults to ``session`` so a caller that
forgets it can never accept a delegated token. A missing signing key raises INTERNAL_ERROR,
so a misconfiguration is never mistaken for a bad token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import clock, config, money
from .errors import DomainError, ErrorCode

TOKEN_VERSION = 1
MAX_TOKEN_CHARS = 1024

SCOPE_SESSION = "session"
SCOPE_DELEGATED_GATEWAY = "delegated_gateway"
SCOPES: tuple[str, ...] = (SCOPE_SESSION, SCOPE_DELEGATED_GATEWAY)
ACTOR_PERSONAL_AGENT = "personal_agent"

_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_CUSTOMER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class SessionToken:
    """A freshly signed token (session or delegated)."""

    token: str
    customer_id: str
    issued_at: datetime
    expires_at: datetime
    scope: str = SCOPE_SESSION
    actor: str | None = None
    max_amount_cents: int | None = None

    def to_dict(self) -> dict[str, str]:
        """Body of ``POST /demo/sessions`` (the API adds the customer profile)."""
        return {
            "token": self.token,
            "customer_id": self.customer_id,
            "expires_at": clock.iso(self.expires_at),
        }


@dataclass(frozen=True)
class TokenClaims:
    """The verified claims of a token (field order is part of contracts.md F.1)."""

    customer_id: str
    scope: str  # "session" | "delegated_gateway"
    actor: str | None  # "personal_agent" for delegated tokens, else None
    max_amount_cents: int | None  # delegated tokens only
    issued_at: datetime
    expires_at: datetime

    def check_amount(self, amount_cents: int) -> None:
        """Refuse an amount above this token's cap with DELEGATION_LIMIT_EXCEEDED (HTTP 403).

        Session tokens have no cap. A delegated token always has one (it is required to
        verify), so an amount equal to the cap passes and anything above it is refused.
        """
        amount = money.ensure_cents(amount_cents, "amount_cents", minimum=0)
        if self.max_amount_cents is not None and amount > self.max_amount_cents:
            raise DomainError(
                ErrorCode.DELEGATION_LIMIT_EXCEEDED,
                "The purchase is above the delegated limit of "
                f"{money.format_cad(self.max_amount_cents)}.",
            )


# -------------------------------------------------------------------------------- signing


def sign_token(
    customer_id: str,
    *,
    scope: str = SCOPE_SESSION,
    actor: str | None = None,
    max_amount_cents: int | None = None,
    ttl_seconds: int | None = None,
    key: bytes | str | None = None,
    now: datetime | None = None,
) -> SessionToken:
    """Sign a token for ``customer_id``.

    ``session`` (default): 8 hours, no ``actor`` or cap allowed. ``delegated_gateway``:
    ``DELEGATION_TTL_SECONDS`` (10 minutes) at most, ``actor`` must be ``personal_agent``
    (the default) and ``max_amount_cents`` defaults to ``DELEGATION_MAX_CENTS``; a lower
    cap is allowed, a higher one is refused. Only server code calls this: no tool, request
    body or prompt ever mints or carries a token.
    """
    if not isinstance(customer_id, str) or not _CUSTOMER_ID.fullmatch(customer_id):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "customer_id has an invalid format")
    settings = config.get_settings()

    extra_claims: dict[str, Any] = {}
    max_ttl: int | None = None
    if scope == SCOPE_SESSION:
        if actor is not None or max_amount_cents is not None:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "session tokens do not carry actor or max_amount_cents",
            )
        default_ttl = settings.token_ttl_seconds
    elif scope == SCOPE_DELEGATED_GATEWAY:
        actor = ACTOR_PERSONAL_AGENT if actor is None else actor
        if actor != ACTOR_PERSONAL_AGENT:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"delegated tokens are only issued to the actor '{ACTOR_PERSONAL_AGENT}'",
            )
        cap = settings.delegation_max_cents if max_amount_cents is None else max_amount_cents
        if not _is_int(cap) or not 1 <= cap <= settings.delegation_max_cents:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "max_amount_cents must be between 1 and the configured delegation limit",
            )
        max_amount_cents = cap
        extra_claims = {"actor": actor, "max_amount_cents": cap}
        default_ttl = max_ttl = settings.delegation_ttl_seconds
    else:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"scope must be one of: {', '.join(SCOPES)}"
        )

    ttl = default_ttl if ttl_seconds is None else ttl_seconds
    if not _is_int(ttl) or ttl < 1 or (max_ttl is not None and ttl > max_ttl):
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "ttl_seconds must be a positive integer"
            + (f" of at most {max_ttl} for delegated tokens" if max_ttl is not None else ""),
        )

    secret = _key_bytes(key)
    issued = int(_current_time(now).timestamp())
    claims = {
        "v": TOKEN_VERSION,
        "customer_id": customer_id,
        "scope": scope,
        "iat": issued,
        "exp": issued + ttl,
        **extra_claims,
    }
    payload = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signature = _b64url_encode(_digest(payload.encode("ascii"), secret))
    return SessionToken(
        token=f"{payload}.{signature}",
        customer_id=customer_id,
        issued_at=datetime.fromtimestamp(issued, UTC),
        expires_at=datetime.fromtimestamp(issued + ttl, UTC),
        scope=scope,
        actor=actor if scope == SCOPE_DELEGATED_GATEWAY else None,
        max_amount_cents=max_amount_cents if scope == SCOPE_DELEGATED_GATEWAY else None,
    )


#: Name used by contracts.md A.0 for the same function.
issue_token = sign_token


# ----------------------------------------------------------------------------- verification


def verify_claims(
    token: str | None,
    required_scope: str = SCOPE_SESSION,
    *,
    key: bytes | str | None = None,
    now: datetime | None = None,
) -> TokenClaims:
    """Verified claims of ``token``.

    NOT_AUTHENTICATED: malformed, bad signature or expired. NOT_AUTHORIZED: a genuine,
    unexpired token whose scope is not ``required_scope``.
    """
    _require_known_scope(required_scope)
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        raise _unauthenticated("missing or malformed token")
    parts = token.split(".")
    if len(parts) != 2 or not all(_SEGMENT.fullmatch(part) for part in parts):
        raise _unauthenticated("invalid token")
    payload, signature = parts
    secret = _key_bytes(key)
    try:
        provided = _b64url_decode(signature)
    except ValueError:
        raise _unauthenticated("invalid token") from None
    if not hmac.compare_digest(_digest(payload.encode("ascii"), secret), provided):
        raise _unauthenticated("invalid token")

    claims = _read_claims(payload)
    current = _current_time(now).timestamp()
    if current >= claims.expires_at.timestamp():
        raise _unauthenticated("token expired")
    if claims.scope != required_scope:
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "This token cannot be used for this request.")
    return claims


def verify_token(
    token: str | None,
    required_scope: str = SCOPE_SESSION,
    *,
    key: bytes | str | None = None,
    now: datetime | None = None,
) -> str:
    """The ``customer_id`` of a valid token of ``required_scope`` (see ``verify_claims``)."""
    return verify_claims(token, required_scope, key=key, now=now).customer_id


def claims_from_authorization(
    header: str | None,
    required_scope: str = SCOPE_SESSION,
    *,
    key: bytes | str | None = None,
    now: datetime | None = None,
) -> TokenClaims:
    """``Authorization: Bearer <token>`` -> verified claims (design-gap G-25)."""
    if not isinstance(header, str) or not header.strip():
        raise _unauthenticated("missing bearer token")
    scheme, _, token = header.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthenticated("expected 'Authorization: Bearer <token>'")
    return verify_claims(token.strip(), required_scope, key=key, now=now)


def customer_id_from_authorization(
    header: str | None,
    required_scope: str = SCOPE_SESSION,
    *,
    key: bytes | str | None = None,
    now: datetime | None = None,
) -> str:
    """``Authorization: Bearer <token>`` -> verified ``customer_id``."""
    return claims_from_authorization(header, required_scope, key=key, now=now).customer_id


def verify_admin_secret(candidate: str | None, *, expected: str | None = None) -> None:
    """Constant-time check of the ``X-Demo-Admin`` header value; raises NOT_AUTHENTICATED."""
    if not isinstance(candidate, str) or not candidate:
        raise _unauthenticated("admin secret required")
    wanted = config.get_admin_key() if expected is None else expected
    if not hmac.compare_digest(candidate.encode("utf-8"), wanted.encode("utf-8")):
        raise _unauthenticated("invalid admin secret")


# ----------------------------------------------------------------------- write-back signature


def sign_writeback(body: bytes, *, key: bytes | str | None = None) -> str:
    """Hex HMAC-SHA256 of the raw request body: the ``X-ALDO-Signature`` header value."""
    return hmac.new(_writeback_key_bytes(key), _body_bytes(body), hashlib.sha256).hexdigest()


def verify_writeback_signature(
    body: bytes, signature: str | None, *, key: bytes | str | None = None
) -> None:
    """Constant-time check of ``X-ALDO-Signature`` over the raw body; NOT_AUTHENTICATED if bad."""
    if not isinstance(signature, str) or not signature.strip():
        raise _unauthenticated("missing signature")
    expected = sign_writeback(body, key=key)
    if not hmac.compare_digest(
        expected.encode("ascii"), signature.strip().lower().encode("utf-8")
    ):
        raise _unauthenticated("invalid signature")


# ----------------------------------------------------------------------------- internals


def _unauthenticated(message: str) -> DomainError:
    return DomainError(ErrorCode.NOT_AUTHENTICATED, message)


def _current_time(now: datetime | None) -> datetime:
    """The wall clock (patchable via ``clock.now_utc``) or an explicit timezone-aware ``now``."""
    if now is None:
        return clock.now_utc()
    if now.utcoffset() is None:  # a naive time would be read as local time: refuse it
        raise DomainError(ErrorCode.VALIDATION_ERROR, "now must be timezone-aware")
    return now


def _require_known_scope(scope: object) -> None:
    if scope not in SCOPES:  # a coding error, not a client error
        raise ValueError(f"required_scope must be one of {SCOPES}, got {scope!r}")


def _explicit_key(key: bytes | str, label: str) -> bytes:
    raw = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if len(raw) < config.MIN_KEY_CHARS:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR, f"{label} must be at least {config.MIN_KEY_CHARS} bytes"
        )
    return raw


def _key_bytes(key: bytes | str | None) -> bytes:
    return config.get_token_key() if key is None else _explicit_key(key, "token signing key")


def _writeback_key_bytes(key: bytes | str | None) -> bytes:
    return config.get_writeback_key() if key is None else _explicit_key(key, "write-back key")


def _body_bytes(body: object) -> bytes:
    if not isinstance(body, (bytes, bytearray)):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "the signed body must be raw bytes")
    return bytes(body)


def _digest(message: bytes, key: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_claims(payload_segment: str) -> TokenClaims:
    """Parse the claims of a token whose signature already verified."""
    try:
        raw = json.loads(_b64url_decode(payload_segment))
    except ValueError:  # includes binascii.Error and json.JSONDecodeError
        raise _unauthenticated("invalid token") from None
    if (
        not isinstance(raw, dict)
        or raw.get("v") != TOKEN_VERSION
        or not isinstance(raw.get("customer_id"), str)
        or not _CUSTOMER_ID.fullmatch(raw["customer_id"])
        or not _is_int(raw.get("iat"))
        or not _is_int(raw.get("exp"))
    ):
        raise _unauthenticated("invalid token")

    scope = raw.get("scope", SCOPE_SESSION)  # tokens without a scope claim are session tokens
    actor = raw.get("actor")
    cap = raw.get("max_amount_cents")
    if scope == SCOPE_SESSION:
        if actor is not None or cap is not None:
            raise _unauthenticated("invalid token")
    elif scope == SCOPE_DELEGATED_GATEWAY:
        # A delegated token without the agent actor or a cap must never verify as "unlimited".
        if actor != ACTOR_PERSONAL_AGENT or not _is_int(cap) or cap < 1:
            raise _unauthenticated("invalid token")
    else:
        raise _unauthenticated("invalid token")

    try:
        issued_at = datetime.fromtimestamp(raw["iat"], UTC)
        expires_at = datetime.fromtimestamp(raw["exp"], UTC)
    except (OverflowError, OSError, ValueError):
        raise _unauthenticated("invalid token") from None
    return TokenClaims(
        customer_id=raw["customer_id"],
        scope=scope,
        actor=actor,
        max_amount_cents=cap,
        issued_at=issued_at,
        expires_at=expires_at,
    )
