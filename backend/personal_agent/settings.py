"""Settings and fixed limits of the personal agent.

Nothing runs at import time: every accessor reads the environment when called, so tests and
local runs can change variables freely. Everything that has a field in
``returns_domain.config.Settings`` is read from there (one source for the table name, model ids,
delegation limits, gateway transport and base URL, the tool-call limit); only package-local
constants live here.
"""

from __future__ import annotations

import os
import re
from typing import Final

from returns_domain import config as domain_config

BEDROCK_REGION: Final = "us-east-1"  # us-east-1 only (project rule); also the explicit Strands region
DEFAULT_GATEWAY_URL_PARAM: Final = "/aldo-demo/gateway-url"

# Delegated token (PIVOT_ADDENDUM C4): short-lived, minted server-side, never seen by the model.
DELEGATED_SCOPE: Final = "delegated_gateway"
DELEGATED_ACTOR: Final = "personal_agent"

# Run limits.
HARD_STOP_EXTRA_CALLS: Final = 3  # model calls are cancelled once tool calls exceed the limit + 3
MAX_MODEL_TOKENS: Final = 2048
MAX_REPLY_WORDS: Final = 100  # owner-facing reply
MAX_QUERY_CHARS: Final = 80  # gateway: product_query is 2..80 characters
MIN_QUERY_CHARS: Final = 2
MAX_COLOR_CHARS: Final = 20  # gateway: color is at most 20 characters
GATEWAY_TIMEOUT_SECONDS: Final = 20.0

ID_REGEX: Final = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
ID_PATTERN: Final = re.compile(ID_REGEX)



def _settings() -> domain_config.Settings:
    return domain_config.get_settings()


def table_name() -> str:
    """PERSONAL_AGENT_TABLE_NAME (default ``aldo-personal-agent-demo``)."""
    return _settings().personal_agent_table_name


def region() -> str:
    return _settings().region


def model_id() -> str:
    """PERSONAL_AGENT_MODEL_ID, defaulting to BEDROCK_MODEL_ID."""
    return _settings().personal_agent_model_id


def fallback_model_id() -> str:
    return _settings().bedrock_fallback_model_id


def max_tool_calls() -> int:
    """PERSONAL_AGENT_MAX_TOOL_CALLS (default 6): the 7th tool call returns LIMIT_REACHED."""
    return _settings().personal_agent_max_tool_calls


def gateway_transport() -> str:
    """GATEWAY_TRANSPORT: ``local`` (code default, tests), ``http`` (deployed) or ``a2a``."""
    return _settings().gateway_transport


def gateway_base_url() -> str | None:
    """GATEWAY_BASE_URL, or None when the caller must resolve the SSM parameter."""
    return _settings().gateway_base_url


def gateway_url_param() -> str:
    """SSM parameter holding the public base URL (GATEWAY_BASE_URL_PARAM)."""
    return os.environ.get("GATEWAY_BASE_URL_PARAM", "").strip() or DEFAULT_GATEWAY_URL_PARAM


def delegation_max_cents() -> int:
    return _settings().delegation_max_cents


def delegation_ttl_seconds() -> int:
    return _settings().delegation_ttl_seconds


def rationale_max_chars() -> int:
    return _settings().rationale_max_chars


def max_decision_chars() -> int:
    return _settings().max_decision_chars


def max_message_chars() -> int:
    return _settings().max_message_chars


def conversation_max_bytes() -> int:
    return _settings().conversation_max_bytes


def lease_seconds() -> int:
    return _settings().run_lease_seconds


def emit_a2a_enabled() -> bool:
    """PERSONAL_AGENT_EMIT_A2A=1 makes ``apply_update`` also write ``memory_updated`` itself.

    Off by default: the ALDO side emits it exactly once when the outbox delivery succeeds
    (contracts.md F.5), so enabling this would duplicate the event.
    """
    return os.environ.get("PERSONAL_AGENT_EMIT_A2A", "").strip().lower() in ("1", "true", "yes")


def valid_id(value: object) -> bool:
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def require_id(value: object, field: str) -> str:
    from returns_domain.errors import DomainError, ErrorCode

    if not valid_id(value):
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"{field} has an invalid format")
    return str(value)
