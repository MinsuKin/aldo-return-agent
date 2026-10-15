"""RunContext limits, the late-bound Services handle and the model preflight (no AWS calls)."""

from __future__ import annotations

from typing import Any

import pytest

from agent import preflight
from agent import tools as agent_tools
from agent.context import ALWAYS_EXEMPT_TOOLS, HARD_STOP_EXTRA_ROUNDS, RunContext
from agent.services import SERVICE_NAMES, Services

# ----------------------------------------------------------------------- RunContext


def make_ctx(**overrides: Any) -> RunContext:
    return RunContext(run_id="RUN-1", conv_id="CONV-1", customer_id="DEMO-CUST-AVA", **overrides)


def test_limit_boundaries() -> None:
    ctx = make_ctx(max_rounds=8)
    ctx.rounds = 8
    assert ctx.is_over_limit("get_order_context") is False  # the 8th round may still use tools
    ctx.rounds = 9
    assert ctx.is_over_limit("get_order_context") is True
    assert ctx.hard_stop_due() is False
    ctx.rounds = 8 + HARD_STOP_EXTRA_ROUNDS
    assert ctx.hard_stop_due() is False
    ctx.rounds += 1
    assert ctx.hard_stop_due() is True


def test_exemptions() -> None:
    ctx = make_ctx(max_rounds=2)
    ctx.rounds = 3
    assert ALWAYS_EXEMPT_TOOLS == {"record_decision"}
    assert ctx.is_over_limit("record_decision") is False
    assert ctx.is_over_limit("escalate_case") is False
    ctx.escalated = True
    assert ctx.is_over_limit("escalate_case") is True
    assert ctx.is_over_limit("execute_approved_resolution") is True


def test_defaults_match_the_contract() -> None:
    ctx = make_ctx()
    assert (ctx.kind, ctx.max_rounds, ctx.rounds, ctx.tool_calls, ctx.escalated) == ("customer", 8, 0, 0, False)
    assert (ctx.model_id, ctx.fallback_used, ctx.input_tokens, ctx.output_tokens) == ("", False, 0, 0)
    assert ctx.order_id is None and ctx.quote_id is None and ctx.option_id is None and ctx.approval_id is None
    assert ctx.elapsed_ms() >= 0


# ------------------------------------------------------------------------- Services


def test_services_serve_overrides_without_importing_anything() -> None:
    fake = object()
    services = Services(orders=fake)
    assert services.orders is fake


def test_services_import_the_real_module_lazily_and_cache_it() -> None:
    services = Services()
    from returns_domain import store

    assert services.store is store
    assert services.store is services.store


def test_services_refuse_names_outside_the_whitelist() -> None:
    with pytest.raises(ValueError):
        Services(os=object())  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        Services().os  # noqa: B018 - must not become a general import hook
    with pytest.raises(AttributeError):
        Services()._private  # noqa: B018
    assert {"orders", "options", "quotes", "execution", "cases", "trace", "runs", "conversations", "gateway"} <= SERVICE_NAMES


def test_override_returns_a_copy_and_available_reports_missing_modules() -> None:
    base = Services(orders="a")
    changed = base.override(orders="b", cases="c")
    assert (base.orders, changed.orders, changed.cases) == ("a", "b", "c")
    assert base.available("orders") is True


# ---------------------------------------------------------------------- preflight


def test_preflight_request_sends_only_max_tokens_and_no_tool_choice() -> None:
    request = preflight.request("us.anthropic.claude-sonnet-5-5", "hello", "DEMO-A2A-81B20E")
    assert request["inferenceConfig"] == {"maxTokens": 512}
    assert "toolChoice" not in request["toolConfig"] and "toolChoice" not in request
    assert [t["toolSpec"]["name"] for t in request["toolConfig"]["tools"]] == list(agent_tools.TOOL_NAMES)
    assert request["messages"][0]["content"] == [{"text": "hello"}, {"text": "[selected_order_id: DEMO-A2A-81B20E]"}]
    assert "Today (demo clock)" in request["system"][0]["text"]


def test_preflight_reads_a_tool_use_answer() -> None:
    class FakeClient:
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["modelId"] == "m1"
            return {
                "output": {"message": {"content": [{"toolUse": {"name": "get_order_context", "input": {"order_id": "X"}}}]}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 6000, "outputTokens": 50},
            }

    result = preflight.check_model(FakeClient(), "m1", "hi", None)
    assert result["first_tool"] == "get_order_context" and result["first_tool_input"] == {"order_id": "X"}
    assert result["stop_reason"] == "tool_use" and result["input_tokens"] == 6000


def test_preflight_main_reports_each_model_and_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import boto3

    class FakeClient:
        def converse(self, **kwargs: Any) -> dict[str, Any]:
            if kwargs["modelId"] == "bad":
                raise RuntimeError("AccessDeniedException: no")
            return {"output": {"message": {"content": [{"text": "Quick check first."}]}}, "stopReason": "end_turn",
                    "usage": {"inputTokens": 1, "outputTokens": 2}}

    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: FakeClient())
    assert preflight.main(["--models", "good"]) == 0
    assert preflight.main(["--models", "good,bad"]) == 1
    out = capsys.readouterr().out
    assert "OK   good stop=end_turn" in out and "FAIL bad RuntimeError" in out
