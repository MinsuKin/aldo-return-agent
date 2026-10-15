"""AgentCore entrypoint (contracts B.5, spike.md 0.2 and 3.3)."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

import agent_app
from agent import runner

BACKEND = Path(__file__).resolve().parents[2]


def test_entrypoint_signature_lets_the_sdk_pass_the_request_context() -> None:
    params = list(inspect.signature(agent_app.invoke).parameters)
    assert params[:2] == ["payload", "context"]  # the SDK passes context only if it is named so
    assert agent_app.app.handlers["main"] is agent_app.invoke


def test_invocations_calls_the_runner_and_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_run(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"run_id": payload["run_id"], "status": "completed", "metrics": {"rounds": 1}}

    monkeypatch.setattr(runner, "run", fake_run)
    client = TestClient(agent_app.app)
    body = {"run_id": "RUN-APP0000001", "conv_id": "CONV-APP0000001", "customer_id": "DEMO-CUST-AVA",
            "kind": "customer", "message": "hello"}
    response = client.post(
        "/invocations", json=body,
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "conv-0123456789abcdef0123456789abcdef01234567"},
    )
    assert response.status_code == 200
    assert response.json() == {"run_id": "RUN-APP0000001", "status": "completed", "metrics": {"rounds": 1}}
    assert seen == [body]


def test_ping_is_healthy() -> None:
    assert TestClient(agent_app.app).get("/ping").status_code == 200


def test_importing_the_entrypoint_opens_no_clients_and_loads_no_data() -> None:
    """AgentCore gives the runtime 30 s to initialise: the module only builds the app object."""
    source = inspect.getsource(agent_app)
    assert "boto3" not in source and "load_insights" not in source and "Agent(" not in source


def test_requirements_are_pinned_and_cover_the_runtime_imports() -> None:
    lines = [
        line.strip() for line in (BACKEND / "requirements-agent.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines and all("==" in line for line in lines), lines
    names = {line.split("==")[0].lower() for line in lines}
    assert {"strands-agents", "bedrock-agentcore", "boto3", "pydantic", "aws-lambda-powertools"} <= names
