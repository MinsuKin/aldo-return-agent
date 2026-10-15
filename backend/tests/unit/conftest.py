"""Hermetic setup for the pure-logic unit tests: no ambient env, no shared caches."""

from __future__ import annotations

import pytest

from returns_domain import config, fit

# Every variable returns_domain.config reads. Cleared so a developer shell cannot change results.
CONFIG_ENV_VARS = (
    "TABLE_NAME",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "DEMO_NOW",
    "BEDROCK_MODEL_ID",
    "BEDROCK_FALLBACK_MODEL_ID",
    "FIXTURES_DIR",
    "MAX_ROUNDS",
    "MAX_MESSAGE_CHARS",
    "TOKEN_TTL_SECONDS",
    "QUOTE_TTL_SECONDS",
    "EXECUTE_WINDOW_SECONDS",
    "RUN_LEASE_SECONDS",
    "TOOL_RETRIES",
    "MAX_SUMMARY_CHARS",
    "MAX_DECISION_CHARS",
    "EVENT_STRING_CAP",
    "CONVERSATION_MAX_BYTES",
    "PRICE_INPUT_PER_MTOK_USD",
    "PRICE_OUTPUT_PER_MTOK_USD",
    "FALLBACK_PRICE_INPUT_PER_MTOK_USD",
    "FALLBACK_PRICE_OUTPUT_PER_MTOK_USD",
    "USD_TO_CAD",
    "DEMO_TOKEN_KEY",
    "DEMO_ADMIN_KEY",
    "DEMO_WRITEBACK_KEY",
    "SECRETS_ARN",
    "PERSONAL_AGENT_TABLE_NAME",
    "PERSONAL_AGENT_MODEL_ID",
    "PERSONAL_AGENT_MAX_TOOL_CALLS",
    "GATEWAY_TRANSPORT",
    "GATEWAY_BASE_URL",
    "DELEGATION_MAX_CENTS",
    "DELEGATION_TTL_SECONDS",
    "SIZE_REQUEST_TTL_SECONDS",
    "RATIONALE_MAX_CHARS",
)


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config.clear_secret_cache()
    fit.clear_cache()
