"""The worker's part of the single chat: the order list it gives the bot, and the way back to the bot.

Real ``runs`` / ``conversations`` / ``trace`` and the bot's table on moto. The bot's runner and the
return workflow's runtime are fakes that record what the worker handed them, so the tests read the
payloads exactly as the deployed Lambda would see them.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import pytest

from api import providers, returns_chat
from returns_domain import runs, trace
from worker import handler as worker

from .conftest import ADMIN_KEY, AVA
from .test_single_chat import a2a, hand_off, repo, say, start_chat
from .test_worker import PERSONAL, RUNTIME_ARN, TIMEOUT, FakeAgentCore, FakeBoto3, FakeRunner, answer

ADMIN = {"X-Demo-Admin": ADMIN_KEY}


@pytest.fixture
def world(client, seeded, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", RUNTIME_ARN)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "http")
    monkeypatch.delenv("AGENT_MODE", raising=False)
    worker.reset_runtime_arn()
    assert client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN).status == 200
    yield client
    worker.reset_runtime_arn()


def install_runtime(monkeypatch: pytest.MonkeyPatch, behavior: Callable[[dict], dict]) -> FakeAgentCore:
    fake = FakeAgentCore(behavior)
    monkeypatch.setattr(worker, "boto3", FakeBoto3(fake))
    return fake


def start_return(world, enqueued) -> tuple[str, dict[str, Any]]:
    """A chat in returns mode, and the payload the API queued for the owner's first message."""
    conv = start_chat(world)
    hand_off(AVA, conv)
    assert say(world, conv, "Too tight.").status == 202
    payload = enqueued[-1]
    assert payload["agent"] == "aldo"
    return conv, payload


def started(kwargs: dict) -> str:
    run_id = json.loads(kwargs["payload"])["run_id"]
    assert runs.mark_agent_started(run_id)
    return run_id


# ============================================================ the orders the bot is handed


def test_the_bot_is_handed_the_owners_recent_orders_read_by_the_worker(world):
    runner = FakeRunner()
    providers.override("personal_agent.runner", runner)

    worker.process(dict(PERSONAL))

    (seen,) = runner.payloads
    expected = returns_chat.recent_orders(AVA)
    assert expected and seen["orders"] == expected
    assert {key: value for key, value in seen.items() if key != "orders"} == PERSONAL


def test_a_client_cannot_supply_the_orders_the_bot_trusts(world):
    runner = FakeRunner()
    providers.override("personal_agent.runner", runner)
    forged = [{"order_id": "DEMO-9999", "channel": "agent", "delivered_at": None, "lines": []}]

    worker.process({**PERSONAL, "orders": forged})

    (seen,) = runner.payloads
    assert seen["orders"] == returns_chat.recent_orders(AVA)
    assert "DEMO-9999" not in json.dumps(seen["orders"])


def test_when_the_orders_cannot_be_read_the_bot_still_runs_without_them(world, monkeypatch):
    runner = FakeRunner()
    providers.override("personal_agent.runner", runner)

    def table_down(*args: Any, **kwargs: Any) -> list:
        raise RuntimeError("table down")

    monkeypatch.setattr(returns_chat, "recent_orders", table_down)

    result = worker.process({**PERSONAL, "orders": [{"order_id": "DEMO-9999"}]})

    assert result == runner.result
    assert "orders" not in runner.payloads[0]  # not the forged list either: "not provided"


# ============================================================ the way back, through the worker


def test_a_return_that_ends_in_a_case_is_handed_back_by_the_worker(world, enqueued, monkeypatch):
    conv, payload = start_return(world, enqueued)

    def behavior(kwargs: dict) -> dict:
        run_id = started(kwargs)
        trace.emit(run_id, "case_created", {"case_id": "DEMO-CASE-7", "reason_code": "FINAL_SALE_DEFECT"})
        trace.emit(run_id, "assistant_message", {"text": "A person will review this."})
        runs.finish_run(run_id, "completed", {})
        return answer({"run_id": run_id, "status": "completed", "metrics": {}})

    install_runtime(monkeypatch, behavior)

    assert worker.process(payload) == {"run_id": payload["run_id"], "status": "completed"}
    assert repo().get_conversation(AVA, conv)["mode"] == "bot"
    (handback,) = [e for e in a2a(world) if e["type"] == "handback"]
    assert handback["actor"] == "aldo_agent"
    assert handback["payload"]["outcome"] == "escalated" and handback["payload"]["case_id"] == "DEMO-CASE-7"


def test_a_timeout_after_the_exchange_was_made_still_hands_the_chat_back(world, enqueued, monkeypatch):
    """The transaction exists, so the return is over even though the runtime never answered."""
    conv, payload = start_return(world, enqueued)

    def behavior(kwargs: dict) -> dict:
        run_id = started(kwargs)
        trace.emit(run_id, "transaction_created", {"transaction_id": "DEMO-RMA-AB12CD", "option_type": "exchange_store_pickup"})
        raise TIMEOUT

    install_runtime(monkeypatch, behavior)

    result = worker.process(payload)

    assert result["status"] == "error" and result["error_code"] == "UPSTREAM_TIMEOUT"
    assert runs.get_run(AVA, payload["run_id"])["status"] == "error"  # the worker closed the run
    assert repo().get_conversation(AVA, conv)["mode"] == "bot"
    (handback,) = [e for e in a2a(world) if e["type"] == "handback"]
    assert handback["payload"]["outcome"] == "completed"
    assert handback["payload"]["transaction_id"] == "DEMO-RMA-AB12CD"


def test_a_failed_return_run_without_an_outcome_keeps_the_chat_in_returns_mode(world, enqueued, monkeypatch):
    """Nothing was decided, so the owner can simply send the message again."""
    conv, payload = start_return(world, enqueued)

    def behavior(kwargs: dict) -> dict:
        started(kwargs)
        raise TIMEOUT

    install_runtime(monkeypatch, behavior)

    result = worker.process(payload)

    assert result["status"] == "error" and result["error_code"] == "UPSTREAM_TIMEOUT"
    assert repo().get_conversation(AVA, conv)["mode"] == "returns"
    assert [e for e in a2a(world) if e["type"] == "handback"] == []
    again = say(world, conv, "Hello? Are you there?")  # the lease was released with the failed run
    assert again.status == 202 and again.json["agent"] == "returns"


def test_a_return_that_only_quotes_keeps_the_chat_in_returns_mode(world, enqueued, monkeypatch):
    conv, payload = start_return(world, enqueued)

    def behavior(kwargs: dict) -> dict:
        run_id = started(kwargs)
        trace.emit(run_id, "assistant_message", {"text": "Brown is in stock in your size."})
        runs.finish_run(run_id, "completed", {})
        return answer({"run_id": run_id, "status": "completed", "metrics": {}})

    install_runtime(monkeypatch, behavior)

    assert worker.process(payload)["status"] == "completed"
    assert repo().get_conversation(AVA, conv)["mode"] == "returns"
