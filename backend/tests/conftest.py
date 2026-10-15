"""Shared pytest fixtures for the persistence and services tests (moto DynamoDB, no real AWS).

* ``aws_env``: fake credentials, the table name, a signing key, the repo fixtures directory and
  a cleared set of the variables ``returns_domain.config`` reads. Never touches real AWS.
* ``ddb``: ``aws_env`` + ``mock_aws`` + an empty table (``store.create_table``).
* ``seeded``: ``ddb`` + ``fixtures.reset("default")``.

The pure-logic tests in ``tests/unit`` have their own hermetic conftest and do not use these.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from moto import mock_aws

from returns_domain import clock, config, fit, fixtures, store

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "fixtures"

# Variables returns_domain.config (and the AWS SDK) read; cleared so a developer shell
# cannot change a result or point a test at real AWS.
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
    "WRITEBACK_KEY",
    "BEDROCK_MODEL_ID",
    "BEDROCK_FALLBACK_MODEL_ID",
    "MAX_ROUNDS",
    "MAX_MESSAGE_CHARS",
    "TOKEN_TTL_SECONDS",
    "QUOTE_TTL_SECONDS",
    "EXECUTE_WINDOW_SECONDS",
    "RUN_LEASE_SECONDS",
    "CONVERSATION_MAX_BYTES",
    "PERSONAL_AGENT_TABLE_NAME",
    "GATEWAY_TRANSPORT",
    "GATEWAY_BASE_URL",
    "DELEGATION_MAX_CENTS",
)


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("TABLE_NAME", "aldo-returns-demo")
    monkeypatch.setenv("DEMO_TOKEN_KEY", "test-only-token-key-0123456789")
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES_ROOT))
    config.clear_secret_cache()
    fit.clear_cache()
    fixtures.clear_cache()
    store.reset_clients()


@pytest.fixture
def ddb(aws_env: None) -> Iterator[None]:
    with mock_aws():
        store.reset_clients()
        store.create_table()
        yield
    store.reset_clients()


@pytest.fixture
def seeded(ddb: None) -> None:
    fixtures.reset("default")


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch):
    """Freeze ``clock.now_utc`` (the wall clock) and return a setter to move it.

    Usage: ``fixed_now.set("2026-10-16T16:00:00+00:00"); fixed_now.advance(seconds=900)``.
    The default instant is 2026-10-16T16:00:00+00:00.
    """
    from datetime import datetime, timedelta

    class _Now:
        def __init__(self) -> None:
            self.value = clock.parse_iso("2026-10-16T16:00:00+00:00")

        def set(self, text: str) -> None:
            self.value = clock.parse_iso(text)

        def advance(self, **kwargs: float) -> None:
            self.value = self.value + timedelta(**kwargs)

        def __call__(self) -> datetime:
            return self.value

    now = _Now()
    monkeypatch.setattr(clock, "now_utc", now)
    return now
