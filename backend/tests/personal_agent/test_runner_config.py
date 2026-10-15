"""How the Strands agent is built: model parameters, tools, executor, hooks, delegated token (F.12)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from personal_agent import memory, runner, settings, tools
from personal_agent.memory import MemoryRepo
from personal_agent.tools import PersonalRunContext
from returns_domain import auth

from .canned import FakeGateway

AVA = "DEMO-CUST-AVA"
SONNET = "us.anthropic.claude-sonnet-5-5"
OPUS = "us.anthropic.claude-opus-5-5"


def context(repo: MemoryRepo) -> PersonalRunContext:
    conv = repo.create_conversation(AVA)
    run_id = memory.new_run_id()
    repo.create_run(AVA, conv, run_id)
    return PersonalRunContext(run_id=run_id, conv_id=conv, owner_id=AVA, gateway=FakeGateway(), repo=repo)


# ------------------------------------------------------------------------------ the model


def test_the_model_gets_only_id_region_and_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class SpyModel:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    monkeypatch.setattr("strands.models.BedrockModel", SpyModel)
    runner.build_model(SONNET)
    assert seen == {"model_id": SONNET, "region_name": "us-east-1", "max_tokens": 2048}
    for rejected in ("temperature", "top_p", "top_k", "tool_choice", "toolChoice", "stop_sequences"):
        assert rejected not in seen  # Sonnet and Opus 5.5 reject them


def test_the_real_bedrock_model_config_has_no_sampling_parameters() -> None:
    model = runner.build_model(SONNET)
    config = model.get_config()
    assert config["model_id"] == SONNET and config["max_tokens"] == 2048
    assert not {"temperature", "top_p", "top_k", "stop_sequences", "additional_request_fields"} & set(config)
    assert model.client.meta.region_name == "us-east-1"  # never the Strands us-west-2 fallback


def test_model_ids_follow_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.model_id() == SONNET and settings.fallback_model_id() == OPUS
    monkeypatch.setenv("BEDROCK_MODEL_ID", "custom.primary")
    assert settings.model_id() == "custom.primary"
    monkeypatch.setenv("PERSONAL_AGENT_MODEL_ID", "custom.bot")
    assert settings.model_id() == "custom.bot"
    monkeypatch.setenv("BEDROCK_FALLBACK_MODEL_ID", "custom.fallback")
    assert settings.fallback_model_id() == "custom.fallback"


# ------------------------------------------------------------------------------ the agent


def test_the_agent_has_exactly_four_tools_a_sequential_executor_and_no_structured_output(repo: MemoryRepo) -> None:
    ctx = context(repo)
    agent = runner.default_agent_factory(ctx, SONNET, [], "system prompt text")
    assert sorted(agent.tool_names) == sorted(tools.TOOL_NAMES)
    assert type(agent.tool_executor).__name__ == "SequentialToolExecutor"
    assert agent.system_prompt == "system prompt text"
    assert agent.model.get_config()["model_id"] == SONNET
    assert not {"temperature", "top_p", "top_k"} & set(agent.model.get_config())
    assert getattr(agent, "_default_structured_output_model", None) is None


def test_model_retries_are_short_so_the_fallback_is_not_delayed(repo: MemoryRepo) -> None:
    agent = runner.default_agent_factory(context(repo), SONNET, [], "p")
    assert agent._retry_strategy._max_attempts == 2
    assert agent._retry_strategy._max_delay <= 4


def test_prior_messages_are_loaded_into_the_agent(repo: MemoryRepo) -> None:
    prior = [
        {"role": "user", "content": [{"text": "hello"}]},
        {"role": "assistant", "content": [{"text": "hi"}]},
    ]
    agent = runner.default_agent_factory(context(repo), SONNET, list(prior), "p")
    assert list(agent.messages) == prior


# ------------------------------------------------------------------------- the round-limit hook


def test_before_model_call_counts_rounds_and_does_not_cancel_normally(repo: MemoryRepo) -> None:
    ctx = context(repo)
    event = SimpleNamespace(cancel=False)
    runner.before_model_call(ctx, event)
    runner.before_model_call(ctx, event)
    assert ctx.rounds == 2 and event.cancel is False and ctx.hard_stopped is False


def test_a_runaway_tool_loop_is_cancelled_after_limit_plus_three(repo: MemoryRepo) -> None:
    ctx = context(repo)
    ctx.tool_calls = ctx.max_tool_calls + 3  # still allowed
    event = SimpleNamespace(cancel=False)
    runner.before_model_call(ctx, event)
    assert event.cancel is False
    ctx.tool_calls += 1
    runner.before_model_call(ctx, event)
    assert event.cancel == runner.HARD_STOP_REPLY and ctx.hard_stopped is True


# --------------------------------------------------------------------- the delegated token


def test_the_delegated_token_is_minted_server_side_for_the_owner() -> None:
    token = runner.mint_delegated_token(AVA)
    claims = auth.verify_claims(token, "delegated_gateway")
    assert (claims.customer_id, claims.scope, claims.actor, claims.max_amount_cents) == (
        AVA, "delegated_gateway", "personal_agent", 25000,
    )
    assert int((claims.expires_at - claims.issued_at).total_seconds()) == 600


def test_the_delegated_token_cannot_be_used_as_a_session_token() -> None:
    from returns_domain.errors import DomainError

    with pytest.raises(DomainError) as caught:
        auth.verify_token(runner.mint_delegated_token(AVA), "session")
    assert caught.value.code == "NOT_AUTHORIZED"


def test_the_delegation_cap_and_ttl_follow_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DELEGATION_MAX_CENTS", "10000")
    monkeypatch.setenv("DELEGATION_TTL_SECONDS", "120")
    claims = auth.verify_claims(runner.mint_delegated_token(AVA), "delegated_gateway")
    assert claims.max_amount_cents == 10000
    assert int((claims.expires_at - claims.issued_at).total_seconds()) == 120
