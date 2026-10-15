"""Fixtures for the ALDO bot tests: moto DynamoDB for the personal-agent table only, never real AWS.

* ``pa_env`` (autouse): fake credentials, a signing key, the repo fixtures directory, the personal
  agent variables cleared, and the process-wide singletons reset.
* ``repo``: ``pa_env`` + ``mock_aws`` + an empty personal-agent table seeded from
  ``fixtures/personal_agent/memory.json`` and installed as the default repository.
* ``fixed_now`` comes from ``tests/conftest.py`` (freezes ``clock.now_utc``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from moto import mock_aws

from personal_agent import a2a, gateway_client, memory
from returns_domain import config, fit

REPO_ROOT = Path(__file__).resolve().parents[3]

_CLEARED = (
    "AWS_PROFILE",
    "AWS_SESSION_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_DYNAMODB",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "DEMO_NOW",
    "SECRETS_ARN",
    "DEMO_ADMIN_KEY",
    "DEMO_WRITEBACK_KEY",
    "BEDROCK_MODEL_ID",
    "BEDROCK_FALLBACK_MODEL_ID",
    "PERSONAL_AGENT_MODEL_ID",
    "PERSONAL_AGENT_MAX_TOOL_CALLS",
    "PERSONAL_AGENT_TABLE_NAME",
    "PERSONAL_AGENT_EMIT_A2A",
    "GATEWAY_TRANSPORT",
    "GATEWAY_BASE_URL",
    "GATEWAY_BASE_URL_PARAM",
    "DELEGATION_MAX_CENTS",
    "DELEGATION_TTL_SECONDS",
    "MAX_MESSAGE_CHARS",
    "CONVERSATION_MAX_BYTES",
    "RUN_LEASE_SECONDS",
    "WORKER_FUNCTION_NAME",
    "AGENT_MODE",
)


@pytest.fixture(autouse=True)
def pa_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("DEMO_TOKEN_KEY", "test-only-token-key-0123456789")
    monkeypatch.setenv("FIXTURES_DIR", str(REPO_ROOT / "fixtures"))
    config.clear_secret_cache()
    fit.clear_cache()
    gateway_client.clear_url_cache()
    a2a.set_a2a_emitter(None)
    memory.set_repo(None)
    yield
    a2a.set_a2a_emitter(None)
    memory.set_repo(None)


@pytest.fixture
def repo(pa_env: None) -> Iterator[memory.MemoryRepo]:
    with mock_aws():
        table = memory.MemoryRepo()
        table.create_table()
        table.seed()
        memory.set_repo(table)
        yield table
