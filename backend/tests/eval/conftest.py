"""Shared setup for the eval tests: the script as a module, moto tables, and the in-memory simulator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "eval_scenarios.py"

HERMETIC_ENV = (
    "DEMO_NOW", "FIXTURES_DIR", "SECRETS_ARN", "DEMO_TOKEN_KEY", "DEMO_ADMIN_KEY", "DEMO_WRITEBACK_KEY", "DEMO_BASE_URL",
    "TABLE_NAME", "PERSONAL_AGENT_TABLE_NAME", "BEDROCK_MODEL_ID", "BEDROCK_FALLBACK_MODEL_ID", "USD_TO_CAD",
    "PRICE_INPUT_PER_MTOK_USD", "PRICE_OUTPUT_PER_MTOK_USD", "FALLBACK_PRICE_INPUT_PER_MTOK_USD",
    "FALLBACK_PRICE_OUTPUT_PER_MTOK_USD", "DELEGATION_MAX_CENTS", "STACK_NAME",
)


@pytest.fixture(scope="session")
def ev():
    """scripts/eval_scenarios.py imported as a module (it lives outside the package tree)."""
    spec = importlib.util.spec_from_file_location("eval_scenarios", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_scenarios"] = module  # dataclasses need the module registered
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    for name in HERMETIC_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def aws(ev):
    """Dummy credentials and moto; nothing can reach a real account."""
    with ev.dry_run_environment(), mock_aws():
        yield


@pytest.fixture
def build(ev, aws):
    """build(faults=()) -> (ctx, system): fresh moto tables and the scripted simulator."""
    def _build(faults=()):
        return ev.build_dry_ctx(frozenset(faults))
    return _build


@pytest.fixture
def run_scenario(ev, build):
    """run_scenario("A", faults=(), runs=1) -> (list of run dicts, ctx, system)."""
    def _run(sid, faults=(), runs=1):
        ctx, system = build(faults)
        rows = ev.run_suite(ctx, [sid], runs, ev.Pricing.from_config(), out=lambda _line: None)
        return rows, ctx, system
    return _run
