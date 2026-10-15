"""Round limit (G-02): MODEL rounds are counted, tool calls are cancelled past the limit.

Unit tests drive ``RunHooks`` with minimal event stand-ins; the integration tests run the real
Strands agent loop (``SequentialToolExecutor``, real hooks, real tool wrappers) against a
scripted model, so the exact behaviour of the pinned Strands version is covered.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from agent import runner
from agent.context import RoundLimitExceeded, RunContext
from agent.hooks import RunHooks

from .fakes import AGENT_ORDER, CUSTOMER, FakeDomain, payload
from .scripted_model import ScriptedModel, text_turn, tool_turn

LINE = f"{AGENT_ORDER}-L1"


# ----------------------------------------------------------------------------- unit


def model_event(message_count: int) -> Any:
    return SimpleNamespace(agent=SimpleNamespace(messages=[None] * message_count))


def tool_event(name: str, **arguments: Any) -> Any:
    return SimpleNamespace(
        tool_use={"name": name, "toolUseId": f"tooluse_{name}", "input": arguments}, cancel_tool=False
    )


def test_each_model_call_is_one_round(ctx: RunContext) -> None:
    hooks = RunHooks(ctx)
    for count in (1, 3, 5):  # the conversation grows by an assistant turn and a tool-result turn
        hooks.before_model_call(model_event(count))
    assert ctx.rounds == 3


def test_a_throttle_retry_of_the_same_call_is_not_a_new_round(ctx: RunContext) -> None:
    hooks = RunHooks(ctx)
    hooks.before_model_call(model_event(1))
    hooks.before_model_call(model_event(1))  # same messages: Strands retrying the model call
    assert ctx.rounds == 1


def test_tool_calls_do_not_count_as_rounds(ctx: RunContext) -> None:
    hooks = RunHooks(ctx)
    hooks.before_model_call(model_event(1))
    for name in ("get_order_context", "evaluate_return_policy", "get_fit_insight"):
        hooks.before_tool_call(tool_event(name))
    assert ctx.rounds == 1


def test_tools_run_while_rounds_are_within_the_limit(ctx: RunContext) -> None:
    ctx.rounds = ctx.max_rounds  # the 8th round may still use tools
    event = tool_event("get_order_context", order_id=AGENT_ORDER)
    RunHooks(ctx).before_tool_call(event)
    assert event.cancel_tool is False and ctx.tool_calls == 0


def test_tools_are_cancelled_with_limit_reached_past_the_limit(domain: FakeDomain, ctx: RunContext) -> None:
    ctx.rounds = ctx.max_rounds + 1
    event = tool_event("get_order_context", order_id=AGENT_ORDER)
    RunHooks(ctx).before_tool_call(event)
    envelope = json.loads(event.cancel_tool)
    assert envelope["ok"] is False and envelope["error"]["code"] == "LIMIT_REACHED"
    assert envelope["error"]["retryable"] is False
    assert ctx.tool_calls == 1
    assert domain.trace.types == ["tool_call", "tool_result"]
    assert domain.trace.of_type("tool_call")[0]["args"] == {"order_id": AGENT_ORDER}
    result = domain.trace.of_type("tool_result")[0]
    assert result["ok"] is False and result["code"] == "LIMIT_REACHED"


def test_record_decision_is_always_exempt(ctx: RunContext) -> None:
    ctx.rounds = ctx.max_rounds + 2
    for _ in range(3):
        event = tool_event("record_decision", summary="why")
        RunHooks(ctx).before_tool_call(event)
        assert event.cancel_tool is False


def test_escalate_case_is_exempt_exactly_once(ctx: RunContext) -> None:
    ctx.rounds = ctx.max_rounds + 1
    hooks = RunHooks(ctx)
    first = tool_event("escalate_case", reason_code="LIMIT_REACHED", summary="x")
    hooks.before_tool_call(first)
    assert first.cancel_tool is False
    ctx.escalated = True  # the tool flips this when the case was created
    second = tool_event("escalate_case", reason_code="LIMIT_REACHED", summary="x")
    hooks.before_tool_call(second)
    assert json.loads(second.cancel_tool)["error"]["code"] == "LIMIT_REACHED"


def test_hard_stop_raises_beyond_max_rounds_plus_three(ctx: RunContext) -> None:
    hooks = RunHooks(ctx)
    ctx.rounds = ctx.max_rounds + 3  # 11 rounds are tolerated (tools cancelled)
    ctx.seen_message_count = 20
    hooks.before_model_call(model_event(20))  # retry of the same call: still 11
    assert ctx.rounds == 11
    with pytest.raises(RoundLimitExceeded):
        hooks.before_model_call(model_event(22))  # round 12


def test_after_hook_traces_calls_strands_rejected_before_the_tool_ran(
    domain: FakeDomain, ctx: RunContext
) -> None:
    hooks = RunHooks(ctx)
    event = tool_event("evaluate_return_policy", order_id=AGENT_ORDER, reason_category="NOPE")
    hooks.before_tool_call(event)  # within the limit: not cancelled
    after = SimpleNamespace(tool_use=event.tool_use, result={"status": "error", "content": [{"text": "bad"}]})
    hooks.after_tool_call(after)
    assert ctx.tool_calls == 1
    assert domain.trace.types == ["tool_call", "tool_result"]
    assert domain.trace.of_type("tool_result")[0]["code"] == "VALIDATION_ERROR"


def test_after_hook_does_not_double_trace_calls_that_ran(domain: FakeDomain, ctx: RunContext) -> None:
    from agent import tools as agent_tools

    tools = {t.tool_spec["name"]: t for t in agent_tools.build_tools(ctx)}
    hooks = RunHooks(ctx)
    event = tool_event("get_order_context", order_id=AGENT_ORDER)
    hooks.before_tool_call(event)
    tools["get_order_context"](order_id=AGENT_ORDER)  # what Strands would run in the tool thread
    hooks.after_tool_call(SimpleNamespace(tool_use=event.tool_use, result={"status": "success", "content": []}))
    assert domain.trace.types == ["tool_call", "tool_result"] and ctx.tool_calls == 1


# ------------------------------------------------------------ with the real Strands loop


@pytest.fixture
def run_with(monkeypatch: pytest.MonkeyPatch, domain: FakeDomain):
    def go(model: ScriptedModel, **overrides: Any) -> dict[str, Any]:
        monkeypatch.setattr(runner, "make_model", lambda model_id: model)
        return runner.run(payload(**overrides), services=domain.services)

    return go


def hero_script() -> list[Any]:
    return [
        tool_turn("get_order_context", order_id=AGENT_ORDER),
        tool_turn("evaluate_return_policy", order_id=AGENT_ORDER, line_id=LINE, qty=1,
                  reason_category="FIT_SIZE", condition="unworn", size_direction="too_small"),
        tool_turn("get_fit_insight", product_name="Stessy2.0"),
        tool_turn("find_resolution_options", policy_check_id="PCHK-0000000A", deadline_date="2026-10-17"),
        tool_turn("record_decision", summary="Store pickup in Montreal is free and ready Sat Oct 17."),
        tool_turn("create_quote", candidate_set_id="CAND-0000000B", option_ids=["OPT-4", "OPT-3", "OPT-1"]),
        text_turn("Please review the option cards and press Approve."),
    ]


def test_hero_path_of_six_tool_calls_does_not_trip_the_limit(domain: FakeDomain, run_with) -> None:
    out = run_with(ScriptedModel(hero_script()))
    assert out["status"] == "completed"
    assert out["metrics"]["tool_count"] == 6 and out["metrics"]["rounds"] == 7
    results = domain.trace.of_type("tool_result")
    assert [r["ok"] for r in results] == [True] * 6
    assert all(r["code"] is None for r in results)
    assert [t for t in domain.trace.types if t == "tool_call"] == ["tool_call"] * 6


def test_round_nine_tool_calls_are_cancelled_but_the_run_can_escalate_once(
    domain: FakeDomain, run_with
) -> None:
    script = [tool_turn("get_order_context", order_id=AGENT_ORDER)] * 8 + [
        tool_turn("get_order_context", order_id=AGENT_ORDER),  # round 9: cancelled
        tool_turn("escalate_case", reason_code="LIMIT_REACHED", summary="Round limit reached."),  # exempt once
        text_turn("A person will review your case."),
    ]
    out = run_with(ScriptedModel(script))
    results = domain.trace.of_type("tool_result")
    assert [r["code"] for r in results[:8]] == [None] * 8
    assert results[8]["code"] == "LIMIT_REACHED" and results[8]["round"] == 9
    assert results[9]["ok"] is True and results[9]["tool"] == "escalate_case"
    assert out["status"] == "completed" and out["metrics"]["rounds"] == 11
    assert domain.trace.of_type("assistant_message") == [{"text": "A person will review your case."}]
    assert len(domain.calls_to("FakeCases", "escalate")) == 1


def test_record_decision_still_works_past_the_limit(domain: FakeDomain, run_with) -> None:
    script = [tool_turn("get_order_context", order_id=AGENT_ORDER)] * 8 + [
        tool_turn("record_decision", summary="Public rationale."),
        text_turn("Done."),
    ]
    run_with(ScriptedModel(script))
    last = domain.trace.of_type("tool_result")[8]
    assert last["tool"] == "record_decision" and last["ok"] is True and last["round"] == 9
    assert domain.trace.of_type("decision") == [{"summary": "Public rationale."}]


def test_a_second_escalation_past_the_limit_is_cancelled(domain: FakeDomain, run_with) -> None:
    escalate = tool_turn("escalate_case", reason_code="LIMIT_REACHED", summary="x")
    script = [tool_turn("get_order_context", order_id=AGENT_ORDER)] * 8 + [escalate, escalate, text_turn("Done.")]
    run_with(ScriptedModel(script))
    codes = [r["code"] for r in domain.trace.of_type("tool_result")[8:]]
    assert codes == [None, "LIMIT_REACHED"]
    assert len(domain.calls_to("FakeCases", "escalate")) == 1


def test_the_model_sees_the_limit_error_envelope(domain: FakeDomain, run_with) -> None:
    script = [tool_turn("get_order_context", order_id=AGENT_ORDER)] * 9 + [text_turn("ok")]
    model = ScriptedModel(script)
    run_with(model)
    last_user = model.calls[9]["messages"][-1]["content"][0]["toolResult"]
    assert last_user["status"] == "error"
    assert json.loads(last_user["content"][0]["text"])["error"]["code"] == "LIMIT_REACHED"


def test_hard_stop_creates_a_case_and_completes_the_run(domain: FakeDomain, run_with) -> None:
    model = ScriptedModel([], tail=tool_turn("get_order_context", order_id=AGENT_ORDER))
    out = run_with(model)
    assert out["status"] == "completed" and out["metrics"]["error_code"] is None
    assert len(model.calls) == 11  # the 12th model call never happens
    cases = domain.calls_to("FakeCases", "escalate")
    assert len(cases) == 1 and cases[0][0][2] == "LIMIT_REACHED"
    text = domain.trace.of_type("assistant_message")[0]["text"]
    assert "Your case ID is DEMO-CASE-" in text and "cannot promise an outcome" in text
    saved = domain.conversations.saved["CONV-TEST000001"]
    assert saved[-1]["role"] == "assistant" and saved[-1]["content"][0]["text"] == text
    assert [m["role"] for m in saved][-2] == "user"  # roles still alternate


def test_hard_stop_after_a_model_escalation_reuses_that_case(domain: FakeDomain, run_with) -> None:
    script = [tool_turn("escalate_case", reason_code="POLICY_REVIEW", summary="needs a person")]
    model = ScriptedModel(script, tail=tool_turn("get_order_context", order_id=AGENT_ORDER))
    out = run_with(model)
    assert out["status"] == "completed"
    assert len(domain.calls_to("FakeCases", "escalate")) == 1  # no second, server-side case
    assert domain.trace.of_type("tool_result")[0]["tool"] == "escalate_case"
    assert "DEMO-CASE-" in domain.trace.of_type("assistant_message")[0]["text"]


def test_max_rounds_is_configurable_through_the_environment(
    domain: FakeDomain, run_with, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_ROUNDS", "2")
    script = [tool_turn("get_order_context", order_id=AGENT_ORDER)] * 3 + [text_turn("ok")]
    run_with(ScriptedModel(script))
    codes = [r["code"] for r in domain.trace.of_type("tool_result")]
    assert codes == [None, None, "LIMIT_REACHED"]
    assert domain.trace.of_type("run_started")[0]["max_rounds"] == 2


def test_customer_id_is_never_part_of_any_event(domain: FakeDomain, run_with) -> None:
    run_with(ScriptedModel(hero_script()))
    assert CUSTOMER not in json.dumps(domain.trace.events)
