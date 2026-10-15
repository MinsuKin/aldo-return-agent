"""Environment-driven settings for the returns domain.

Nothing here runs at import time: ``get_settings()`` reads the environment on every call,
so tests and local runs can change variables freely. Money rules live in ``money.py``;
the token prices below are rates (USD per million tokens), clearly labelled ``assumed``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .errors import DomainError, ErrorCode

DEFAULT_TABLE_NAME = "aldo-returns-demo"
DEFAULT_REGION = "us-east-1"
DEFAULT_DEMO_NOW = "2026-10-16T12:00:00-04:00"
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-5-5"
#: Opus per design 1/6.1 and tasks 8.4; requirements.md 15.2 says Haiku (documented discrepancy).
DEFAULT_FALLBACK_MODEL_ID = "us.anthropic.claude-opus-5-5"

# Fit Gateway pivot (PIVOT_ADDENDUM 5.9, contracts.md F.2)
DEFAULT_PERSONAL_AGENT_TABLE_NAME = "aldo-personal-agent-demo"
GATEWAY_TRANSPORTS: tuple[str, ...] = ("local", "http", "a2a")
#: Code default is "local" (no network in tests); the deployed stack sets GATEWAY_TRANSPORT=http.
DEFAULT_GATEWAY_TRANSPORT = "local"
DEFAULT_DELEGATION_MAX_CENTS = 25_000  # CAD 250.00, the cap on a delegated purchase
DEFAULT_DELEGATION_TTL_SECONDS = 600  # 10 minutes
DEFAULT_SIZE_REQUEST_TTL_SECONDS = 900  # a size request stays decidable for 15 minutes
DEFAULT_RATIONALE_MAX_CHARS = 120  # rationale_public cap (PIVOT_ADDENDUM 5.3)
DEFAULT_PERSONAL_AGENT_MAX_TOOL_CALLS = 6

MIN_KEY_CHARS = 16
#: Domain-separation labels for keys derived from the token key (one generated secret is
#: enough; every side derives the same value). The write-back label is fixed by
#: PIVOT_ADDENDUM 9.4: HMAC-SHA256(token_key, b"aldo-writeback").
_ADMIN_KEY_LABEL = b"aldo-demo/admin-key/v1"
_WRITEBACK_KEY_LABEL = b"aldo-writeback"


@dataclass(frozen=True)
class TokenPrice:
    """Assumed list price in USD per million tokens. Not a billed amount."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    currency: str = "USD"
    basis: str = "assumed"


@dataclass(frozen=True)
class Settings:
    table_name: str
    region: str
    demo_now: str
    bedrock_model_id: str
    bedrock_fallback_model_id: str
    fixtures_dir: Path

    # Limits (requirements 7.1, 12.2, 12.3; design 6.2, 7; design-gap G-02, G-06, G-08, G-12)
    max_rounds: int
    max_message_chars: int
    token_ttl_seconds: int
    quote_ttl_seconds: int
    execute_window_seconds: int
    run_lease_seconds: int
    tool_retries: int
    tool_retry_delays_ms: tuple[int, ...]
    max_summary_chars: int
    max_decision_chars: int
    event_string_cap: int
    conversation_max_bytes: int

    # Assumed prices (labelled "assumed" wherever they are shown)
    price_primary: TokenPrice
    price_fallback: TokenPrice
    usd_to_cad: Decimal

    # Fit Gateway pivot: personal agent and delegated gateway access
    personal_agent_table_name: str
    personal_agent_model_id: str
    personal_agent_max_tool_calls: int
    gateway_transport: str  # local | http | a2a
    gateway_base_url: str | None  # env only; the callers resolve SSM when unset
    delegation_max_cents: int
    delegation_ttl_seconds: int
    size_request_ttl_seconds: int
    rationale_max_chars: int

    # Deliberately absent: temperature, top_p, top_k and tool_choice. Sonnet and Opus 5.5
    # reject them (spike.md 0.1), so there is no default to pass to the model.

    @property
    def fit_insights_path(self) -> Path:
        return self.fixtures_dir / "derived" / "fit_insights.json"

    def token_price(self, model_id: str | None) -> TokenPrice:
        """Price for ``model_id``; the fallback price applies only to the fallback model."""
        if (
            model_id
            and model_id == self.bedrock_fallback_model_id
            and model_id != self.bedrock_model_id
        ):
            return self.price_fallback
        return self.price_primary

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        model_id = _text(source, "BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
        return cls(
            table_name=_text(source, "TABLE_NAME", DEFAULT_TABLE_NAME),
            region=_region(source),
            demo_now=demo_now_setting(source),
            bedrock_model_id=model_id,
            bedrock_fallback_model_id=_text(
                source, "BEDROCK_FALLBACK_MODEL_ID", DEFAULT_FALLBACK_MODEL_ID
            ),
            fixtures_dir=_fixtures_dir(source),
            max_rounds=_int(source, "MAX_ROUNDS", 8, minimum=1),
            max_message_chars=_int(source, "MAX_MESSAGE_CHARS", 1000, minimum=1),
            token_ttl_seconds=_int(source, "TOKEN_TTL_SECONDS", 8 * 3600, minimum=1),
            quote_ttl_seconds=_int(source, "QUOTE_TTL_SECONDS", 15 * 60, minimum=1),
            execute_window_seconds=_int(source, "EXECUTE_WINDOW_SECONDS", 5 * 60, minimum=1),
            run_lease_seconds=_int(source, "RUN_LEASE_SECONDS", 330, minimum=1),
            tool_retries=_int(source, "TOOL_RETRIES", 2, minimum=0),
            tool_retry_delays_ms=(100, 300),
            max_summary_chars=_int(source, "MAX_SUMMARY_CHARS", 800, minimum=1),
            max_decision_chars=_int(source, "MAX_DECISION_CHARS", 240, minimum=1),
            event_string_cap=_int(source, "EVENT_STRING_CAP", 500, minimum=1),
            conversation_max_bytes=_int(source, "CONVERSATION_MAX_BYTES", 300_000, minimum=1),
            price_primary=TokenPrice(
                input_per_mtok=_decimal(source, "PRICE_INPUT_PER_MTOK_USD", "3.00"),
                output_per_mtok=_decimal(source, "PRICE_OUTPUT_PER_MTOK_USD", "15.00"),
            ),
            price_fallback=TokenPrice(
                input_per_mtok=_decimal(source, "FALLBACK_PRICE_INPUT_PER_MTOK_USD", "5.00"),
                output_per_mtok=_decimal(source, "FALLBACK_PRICE_OUTPUT_PER_MTOK_USD", "25.00"),
            ),
            usd_to_cad=_decimal(source, "USD_TO_CAD", "1.38"),
            personal_agent_table_name=_text(
                source, "PERSONAL_AGENT_TABLE_NAME", DEFAULT_PERSONAL_AGENT_TABLE_NAME
            ),
            personal_agent_model_id=_text(source, "PERSONAL_AGENT_MODEL_ID", model_id),
            personal_agent_max_tool_calls=_int(
                source,
                "PERSONAL_AGENT_MAX_TOOL_CALLS",
                DEFAULT_PERSONAL_AGENT_MAX_TOOL_CALLS,
                minimum=1,
            ),
            gateway_transport=_choice(
                source, "GATEWAY_TRANSPORT", DEFAULT_GATEWAY_TRANSPORT, GATEWAY_TRANSPORTS
            ),
            gateway_base_url=_base_url(source, "GATEWAY_BASE_URL"),
            delegation_max_cents=_int(
                source, "DELEGATION_MAX_CENTS", DEFAULT_DELEGATION_MAX_CENTS, minimum=1
            ),
            delegation_ttl_seconds=_int(
                source, "DELEGATION_TTL_SECONDS", DEFAULT_DELEGATION_TTL_SECONDS, minimum=1
            ),
            size_request_ttl_seconds=_int(
                source, "SIZE_REQUEST_TTL_SECONDS", DEFAULT_SIZE_REQUEST_TTL_SECONDS, minimum=1
            ),
            rationale_max_chars=_int(
                source, "RATIONALE_MAX_CHARS", DEFAULT_RATIONALE_MAX_CHARS, minimum=1
            ),
        )


def get_settings(env: Mapping[str, str] | None = None) -> Settings:
    return Settings.from_env(env)


def demo_now_setting(env: Mapping[str, str] | None = None) -> str:
    """Raw DEMO_NOW text. Kept separate so the clock does not depend on unrelated settings."""
    source = os.environ if env is None else env
    return _text(source, "DEMO_NOW", DEFAULT_DEMO_NOW)


# --------------------------------------------------------------------------- parsing helpers


def _config_error(name: str, problem: str) -> DomainError:
    return DomainError(ErrorCode.INTERNAL_ERROR, f"invalid configuration: {name} {problem}")


def _text(source: Mapping[str, str], name: str, default: str) -> str:
    value = source.get(name)
    return value.strip() if value and value.strip() else default


def _int(source: Mapping[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise _config_error(name, "must be an integer") from None
    if value < minimum:
        raise _config_error(name, f"must be at least {minimum}")
    return value


def _choice(source: Mapping[str, str], name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _text(source, name, default).lower()
    if value not in allowed:
        raise _config_error(name, f"must be one of: {', '.join(allowed)}")
    return value


def _base_url(source: Mapping[str, str], name: str) -> str | None:
    """Trimmed value without a trailing slash, or None. The scheme is not validated here:
    ``get_settings()`` runs on every request, so one odd optional URL must not break
    unrelated routes; the code that uses the URL checks it (pa_client does)."""
    value = _text(source, name, "").rstrip("/")
    return value or None


def _region(source: Mapping[str, str]) -> str:
    return source.get("AWS_REGION") or source.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def _decimal(source: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = source.get(name)
    text = raw.strip() if raw and raw.strip() else default
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise _config_error(name, "must be a decimal number") from None
    if not value.is_finite() or value < 0:
        raise _config_error(name, "must be a non-negative number")
    return value


def _fixtures_dir(source: Mapping[str, str]) -> Path:
    """FIXTURES_DIR, else backend/fixtures_bundle (deployed bundle), else repo fixtures/."""
    configured = source.get("FIXTURES_DIR")
    if configured and configured.strip():
        return Path(configured.strip()).expanduser()
    backend_dir = Path(__file__).resolve().parent.parent
    bundle = backend_dir / "fixtures_bundle"
    if bundle.is_dir():
        return bundle
    return backend_dir.parent / "fixtures"


# ------------------------------------------------------------------------------- secrets
#
# Token signing key, demo admin key and write-back key. Order of lookup:
#   1. environment (DEMO_TOKEN_KEY / DEMO_ADMIN_KEY / DEMO_WRITEBACK_KEY), tests and local runs
#   2. token and admin key: the Secrets Manager secret named by SECRETS_ARN
#      (JSON: token_key, optional admin_key)
#   3. admin and write-back keys: derived from the token key (HMAC with a fixed label), so
#      one generated secret is enough
# There is deliberately no built-in default key: a missing token key fails closed.

_SECRET_CACHE: dict[str, dict[str, Any]] = {}


def clear_secret_cache() -> None:
    _SECRET_CACHE.clear()


def _load_secret(secret_id: str, region: str) -> dict[str, Any]:
    cached = _SECRET_CACHE.get(secret_id)
    if cached is not None:
        return cached
    import boto3  # lazy: the pure-logic modules stay importable without AWS clients

    try:
        client = boto3.client("secretsmanager", region_name=region)
        data = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    except Exception as exc:  # never echo the cause: it could quote request details
        raise DomainError(ErrorCode.INTERNAL_ERROR, "could not load the signing secrets") from exc
    if not isinstance(data, dict):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "signing secrets must be a JSON object")
    _SECRET_CACHE[secret_id] = data
    return data


def _secret_field(source: Mapping[str, str], field: str) -> str | None:
    secret_id = source.get("SECRETS_ARN")
    if not secret_id or not secret_id.strip():
        return None
    value = _load_secret(secret_id.strip(), _region(source)).get(field)
    return value if isinstance(value, str) and value else None


def _checked_key(raw: str | None, label: str) -> str:
    if not raw or len(raw) < MIN_KEY_CHARS:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR,
            f"{label} is not configured (need at least {MIN_KEY_CHARS} characters)",
        )
    return raw


def get_token_key(env: Mapping[str, str] | None = None) -> bytes:
    source = os.environ if env is None else env
    raw = source.get("DEMO_TOKEN_KEY") or _secret_field(source, "token_key")
    return _checked_key(raw, "token signing key").encode("utf-8")


def get_admin_key(env: Mapping[str, str] | None = None) -> str:
    """The value the ``X-Demo-Admin`` header must carry for ``POST /demo/reset``."""
    source = os.environ if env is None else env
    raw = source.get("DEMO_ADMIN_KEY") or _secret_field(source, "admin_key")
    if raw:
        return _checked_key(raw, "admin key")
    return hmac.new(get_token_key(source), _ADMIN_KEY_LABEL, hashlib.sha256).hexdigest()


def get_writeback_key(env: Mapping[str, str] | None = None) -> bytes:
    """HMAC key for the ALDO -> personal agent write-back signature (``X-ALDO-Signature``).

    PIVOT_ADDENDUM 9.4: derived, not a new secret: ``HMAC-SHA256(token_key, b"aldo-writeback")``,
    so both sides that can read the token key agree. A ``writeback_key`` field in the secret
    JSON is ignored on purpose. ``DEMO_WRITEBACK_KEY`` overrides it for tests and local runs
    only (like ``DEMO_TOKEN_KEY``); it is never set in deployed environments.
    """
    source = os.environ if env is None else env
    override = source.get("DEMO_WRITEBACK_KEY")
    if override:
        return _checked_key(override, "write-back key").encode("utf-8")
    return hmac.new(get_token_key(source), _WRITEBACK_KEY_LABEL, hashlib.sha256).digest()
