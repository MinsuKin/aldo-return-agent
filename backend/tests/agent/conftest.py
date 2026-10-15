"""Fixtures for the agent tests. The fakes live in ``fakes.py`` (see its docstring)."""

from __future__ import annotations

from typing import Any

import pytest

from agent import tools as agent_tools
from agent.context import RunContext
from returns_domain import config, fit

from .fakes import AGENT_ORDER, CUSTOMER, ENV_VARS, FakeDomain


@pytest.fixture(autouse=True)
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient settings: a developer shell must not change results."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    config.clear_secret_cache()
    fit.clear_cache()


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record tool retry delays instead of sleeping."""
    delays: list[float] = []
    monkeypatch.setattr(agent_tools, "_sleep", delays.append)
    return delays


@pytest.fixture
def domain() -> FakeDomain:
    return FakeDomain()


@pytest.fixture
def ctx(domain: FakeDomain) -> RunContext:
    return RunContext(
        run_id="RUN-TEST000001",
        conv_id="CONV-TEST000001",
        customer_id=CUSTOMER,
        order_id=AGENT_ORDER,
        kind="customer",
        max_rounds=8,
        services=domain.services,
    )


@pytest.fixture
def tools_by_name(ctx: RunContext) -> dict[str, Any]:
    return {t.tool_spec["name"]: t for t in agent_tools.build_tools(ctx)}
