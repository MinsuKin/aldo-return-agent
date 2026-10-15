"""Fixtures for the API and worker tests.

Builds on the shared ``tests/conftest.py`` (moto DynamoDB: ``ddb``, ``seeded``, ``fixed_now``).
Every test gets a fresh app whose worker hand-off is a list, so "async invoke" is just an
assertion on ``enqueued``. ``providers`` overrides are cleared around each test.
"""

from __future__ import annotations

import pytest

from api import providers
from api.handler import build_app
from returns_domain import auth

from .helpers import ApiClient

ADMIN_KEY = "test-only-admin-key-0123456789"
AVA = "DEMO-CUST-AVA"
BEN = "DEMO-CUST-BEN"
CHLOE = "DEMO-CUST-CHLOE"


def session_token(customer_id: str = AVA, **kwargs) -> str:
    """A session token for a persona (what ``POST /demo/sessions`` issues)."""
    return auth.sign_token(customer_id, **kwargs).token


def delegated_token(customer_id: str = AVA, *, max_amount_cents: int = 25_000, **kwargs) -> str:
    """The ALDO bot's short-lived delegated token (minted server-side in production)."""
    return auth.sign_token(
        customer_id,
        scope="delegated_gateway",
        actor="personal_agent",
        max_amount_cents=max_amount_cents,
        **kwargs,
    ).token


@pytest.fixture(autouse=True)
def clean_provider_overrides():
    providers.clear_overrides()
    yield
    providers.clear_overrides()


@pytest.fixture
def enqueued() -> list[dict]:
    """Payloads the API handed to the worker."""
    return []


@pytest.fixture
def app(ddb, monkeypatch, enqueued):
    # ``ddb`` (and so ``aws_env``) runs first; the admin key is set after it clears the env.
    monkeypatch.setenv("DEMO_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "http")  # no in-process write-back wiring in route tests
    return build_app(enqueue=enqueued.append)


@pytest.fixture
def client(app) -> ApiClient:
    return ApiClient(app)
