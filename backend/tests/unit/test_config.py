from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from returns_domain import config
from returns_domain.errors import DomainError

TOKEN_KEY = "unit-test-token-key-0123456789abcdef"


def _expect_config_error(fn, *args, **kwargs) -> DomainError:
    with pytest.raises(DomainError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == "INTERNAL_ERROR", caught.value
    return caught.value


# ------------------------------------------------------------------------------- settings


def test_defaults():
    settings = config.get_settings({})
    assert settings.table_name == "aldo-returns-demo"
    assert settings.region == "us-east-1"
    assert settings.demo_now == "2026-10-16T12:00:00-04:00"
    assert settings.bedrock_model_id == "us.anthropic.claude-sonnet-5-5"
    assert settings.bedrock_fallback_model_id == "us.anthropic.claude-opus-5-5"
    assert settings.max_rounds == 8
    assert settings.max_message_chars == 1000
    assert settings.token_ttl_seconds == 8 * 3600
    assert settings.quote_ttl_seconds == 15 * 60
    assert settings.execute_window_seconds == 5 * 60
    assert settings.run_lease_seconds == 330
    assert settings.tool_retries == 2
    assert settings.tool_retry_delays_ms == (100, 300)
    assert settings.max_summary_chars == 800
    assert settings.max_decision_chars == 240
    assert settings.event_string_cap == 500
    assert settings.conversation_max_bytes == 300_000


def test_environment_overrides():
    settings = config.get_settings(
        {
            "TABLE_NAME": "other-table",
            "AWS_REGION": "us-east-2",
            "DEMO_NOW": "2026-12-01T09:00:00-05:00",
            "BEDROCK_MODEL_ID": "model-a",
            "BEDROCK_FALLBACK_MODEL_ID": "model-b",
            "MAX_ROUNDS": "12",
            "MAX_MESSAGE_CHARS": "500",
            "TOKEN_TTL_SECONDS": "60",
            "QUOTE_TTL_SECONDS": "30",
            "EXECUTE_WINDOW_SECONDS": "20",
            "RUN_LEASE_SECONDS": "10",
            "TOOL_RETRIES": "0",
        }
    )
    assert settings.table_name == "other-table"
    assert settings.region == "us-east-2"
    assert settings.demo_now == "2026-12-01T09:00:00-05:00"
    assert (settings.bedrock_model_id, settings.bedrock_fallback_model_id) == ("model-a", "model-b")
    assert (settings.max_rounds, settings.max_message_chars) == (12, 500)
    assert (settings.token_ttl_seconds, settings.quote_ttl_seconds) == (60, 30)
    assert (settings.execute_window_seconds, settings.run_lease_seconds) == (20, 10)
    assert settings.tool_retries == 0


def test_region_falls_back_to_the_default_region_variable():
    assert config.get_settings({"AWS_DEFAULT_REGION": "us-west-2"}).region == "us-west-2"
    assert config.get_settings({"AWS_REGION": "us-east-2", "AWS_DEFAULT_REGION": "x"}).region == "us-east-2"


def test_blank_values_use_the_default():
    settings = config.get_settings({"TABLE_NAME": "  ", "MAX_ROUNDS": "", "BEDROCK_MODEL_ID": ""})
    assert settings.table_name == "aldo-returns-demo"
    assert settings.max_rounds == 8
    assert settings.bedrock_model_id == "us.anthropic.claude-sonnet-5-5"


def test_get_settings_reads_the_process_environment_on_every_call(monkeypatch: pytest.MonkeyPatch):
    assert config.get_settings().max_rounds == 8
    monkeypatch.setenv("MAX_ROUNDS", "12")
    assert config.get_settings().max_rounds == 12
    monkeypatch.delenv("MAX_ROUNDS")
    assert config.get_settings().max_rounds == 8


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_ROUNDS", "abc"),
        ("MAX_ROUNDS", "0"),
        ("MAX_ROUNDS", "8.5"),
        ("QUOTE_TTL_SECONDS", "-1"),
        ("TOKEN_TTL_SECONDS", "0"),
        ("TOOL_RETRIES", "-1"),
        ("PRICE_INPUT_PER_MTOK_USD", "cheap"),
        ("PRICE_OUTPUT_PER_MTOK_USD", "-1"),
        ("USD_TO_CAD", "NaN"),
        ("USD_TO_CAD", "Infinity"),
        ("DELEGATION_MAX_CENTS", "0"),
        ("DELEGATION_MAX_CENTS", "250.00"),
        ("DELEGATION_TTL_SECONDS", "0"),
        ("SIZE_REQUEST_TTL_SECONDS", "0"),
        ("RATIONALE_MAX_CHARS", "0"),
        ("PERSONAL_AGENT_MAX_TOOL_CALLS", "0"),
        ("GATEWAY_TRANSPORT", "carrier-pigeon"),
    ],
)
def test_invalid_values_fail_fast_with_the_variable_name(name: str, value: str):
    error = _expect_config_error(config.get_settings, {name: value})
    assert name in error.message


# ---------------------------------------------------------------------------- fixtures dir


def test_fixtures_dir_from_environment(tmp_path: Path):
    settings = config.get_settings({"FIXTURES_DIR": str(tmp_path)})
    assert settings.fixtures_dir == tmp_path
    assert settings.fit_insights_path == tmp_path / "derived" / "fit_insights.json"


def test_fixtures_dir_default_is_the_bundle_or_the_repo_fixtures_folder():
    fixtures_dir = config.get_settings({}).fixtures_dir
    backend_dir = Path(config.__file__).resolve().parent.parent
    bundle = backend_dir / "fixtures_bundle"
    assert fixtures_dir == (bundle if bundle.is_dir() else backend_dir.parent / "fixtures")
    assert fixtures_dir.is_absolute()


# ------------------------------------------------------------------------------- prices


def test_assumed_prices_and_exchange_rate():
    settings = config.get_settings({})
    assert settings.price_primary == config.TokenPrice(Decimal("3.00"), Decimal("15.00"))
    assert settings.price_fallback == config.TokenPrice(Decimal("5.00"), Decimal("25.00"))
    assert settings.price_primary.basis == "assumed" and settings.price_fallback.basis == "assumed"
    assert settings.price_primary.currency == "USD"
    assert settings.usd_to_cad == Decimal("1.38")
    assert isinstance(settings.usd_to_cad, Decimal)


def test_price_overrides_are_decimals_not_floats():
    settings = config.get_settings(
        {
            "PRICE_INPUT_PER_MTOK_USD": "0.1",
            "PRICE_OUTPUT_PER_MTOK_USD": "0.7",
            "FALLBACK_PRICE_INPUT_PER_MTOK_USD": "1",
            "FALLBACK_PRICE_OUTPUT_PER_MTOK_USD": "2.50",
            "USD_TO_CAD": "1.4",
        }
    )
    assert settings.price_primary.input_per_mtok == Decimal("0.1")
    assert settings.price_primary.output_per_mtok == Decimal("0.7")
    assert settings.price_fallback == config.TokenPrice(Decimal("1"), Decimal("2.50"))
    assert settings.usd_to_cad == Decimal("1.4")


def test_token_price_depends_on_the_model_actually_used():
    settings = config.get_settings({})
    assert settings.token_price("us.anthropic.claude-sonnet-5-5") is settings.price_primary
    assert settings.token_price("us.anthropic.claude-opus-5-5") is settings.price_fallback
    assert settings.token_price("some-other-model") is settings.price_primary
    assert settings.token_price(None) is settings.price_primary
    # Primary and fallback set to the same id: the primary price applies.
    same = config.get_settings({"BEDROCK_FALLBACK_MODEL_ID": "us.anthropic.claude-sonnet-5-5"})
    assert same.token_price("us.anthropic.claude-sonnet-5-5") is same.price_primary


# ---------------------------------------------------------------- sampling parameters (spike)


def test_no_sampling_parameters_are_configurable():
    """Sonnet and Opus 5.5 reject temperature != 1.0, top_p, top_k and toolChoice any/tool
    (spike.md 0.1), so config must not offer a default for any of them."""
    banned = ("temperature", "top_p", "topp", "top_k", "topk", "tool_choice", "toolchoice", "sampling")
    names = {field.name for field in dataclasses.fields(config.Settings)}
    assert not [name for name in names if any(word in name.lower() for word in banned)]
    assert not [
        name for name in dir(config) if any(word in name.lower() for word in banned)
    ]


# ---------------------------------------------------------------------------- fit gateway


def test_fit_gateway_defaults():
    settings = config.get_settings({})
    assert settings.personal_agent_table_name == "aldo-personal-agent-demo"
    assert settings.gateway_transport == "local"  # contracts F.2; the deployed stack sets http
    assert settings.gateway_base_url is None
    assert settings.delegation_max_cents == 25000
    assert settings.delegation_ttl_seconds == 600
    assert settings.size_request_ttl_seconds == 900
    assert settings.rationale_max_chars == 120
    assert settings.personal_agent_max_tool_calls == 6
    assert config.GATEWAY_TRANSPORTS == ("local", "http", "a2a")


def test_personal_agent_model_defaults_to_the_primary_model():
    assert config.get_settings({}).personal_agent_model_id == "us.anthropic.claude-sonnet-5-5"
    follows = config.get_settings({"BEDROCK_MODEL_ID": "model-a"})
    assert follows.personal_agent_model_id == "model-a"
    explicit = config.get_settings({"BEDROCK_MODEL_ID": "model-a", "PERSONAL_AGENT_MODEL_ID": "model-c"})
    assert (explicit.bedrock_model_id, explicit.personal_agent_model_id) == ("model-a", "model-c")


def test_fit_gateway_overrides():
    settings = config.get_settings(
        {
            "PERSONAL_AGENT_TABLE_NAME": "aldo-personal-agent-other",
            "GATEWAY_TRANSPORT": "HTTP",
            "GATEWAY_BASE_URL": " https://abc.cloudfront.net/ ",
            "DELEGATION_MAX_CENTS": "10000",
            "DELEGATION_TTL_SECONDS": "120",
            "SIZE_REQUEST_TTL_SECONDS": "60",
            "RATIONALE_MAX_CHARS": "80",
            "PERSONAL_AGENT_MAX_TOOL_CALLS": "4",
        }
    )
    assert settings.personal_agent_table_name == "aldo-personal-agent-other"
    assert settings.gateway_transport == "http"
    assert settings.gateway_base_url == "https://abc.cloudfront.net"
    assert settings.delegation_max_cents == 10000
    assert settings.delegation_ttl_seconds == 120
    assert settings.size_request_ttl_seconds == 60
    assert settings.rationale_max_chars == 80
    assert settings.personal_agent_max_tool_calls == 4
    for transport in config.GATEWAY_TRANSPORTS:
        assert config.get_settings({"GATEWAY_TRANSPORT": transport}).gateway_transport == transport


def test_gateway_base_url_is_trimmed_but_not_judged_by_settings():
    # get_settings() runs on every request; the code that uses the URL validates the scheme.
    for raw, expected in (
        ("https://abc.cloudfront.net/", "https://abc.cloudfront.net"),
        (" http://localhost:3001// ", "http://localhost:3001"),
        ("abc.cloudfront.net", "abc.cloudfront.net"),
        ("", None),
        ("   ", None),
    ):
        assert config.get_settings({"GATEWAY_BASE_URL": raw}).gateway_base_url == expected


def test_invalid_unrelated_setting_does_not_break_the_clock_or_secrets(monkeypatch: pytest.MonkeyPatch):
    # The clock and the secret lookups read only the variables they need.
    monkeypatch.setenv("GATEWAY_TRANSPORT", "carrier-pigeon")
    monkeypatch.setenv("DEMO_TOKEN_KEY", TOKEN_KEY)
    assert config.demo_now_setting() == "2026-10-16T12:00:00-04:00"
    assert config.get_token_key() == TOKEN_KEY.encode()


# -------------------------------------------------------------------------------- secrets


def test_token_key_from_environment():
    assert config.get_token_key({"DEMO_TOKEN_KEY": TOKEN_KEY}) == TOKEN_KEY.encode()


@pytest.mark.parametrize("value", [None, "", "short", "x" * 15])
def test_missing_or_short_token_key_fails_closed(value: str | None):
    env = {} if value is None else {"DEMO_TOKEN_KEY": value}
    error = _expect_config_error(config.get_token_key, env)
    assert not value or value not in error.message  # a bad key is never echoed back


def test_admin_key_from_environment_or_derived_from_the_token_key():
    explicit = config.get_admin_key({"DEMO_TOKEN_KEY": TOKEN_KEY, "DEMO_ADMIN_KEY": "explicit-admin-key-0123456789"})
    assert explicit == "explicit-admin-key-0123456789"

    derived = config.get_admin_key({"DEMO_TOKEN_KEY": TOKEN_KEY})
    assert derived == hmac.new(TOKEN_KEY.encode(), b"aldo-demo/admin-key/v1", hashlib.sha256).hexdigest()
    assert derived == config.get_admin_key({"DEMO_TOKEN_KEY": TOKEN_KEY})  # stable
    assert derived != config.get_admin_key({"DEMO_TOKEN_KEY": "a-different-token-key-0123456789"})
    assert TOKEN_KEY not in derived and len(derived) == 64


def test_admin_key_needs_either_its_own_value_or_a_token_key():
    _expect_config_error(config.get_admin_key, {})
    _expect_config_error(config.get_admin_key, {"DEMO_ADMIN_KEY": "short"})
    assert config.get_admin_key({"DEMO_ADMIN_KEY": "explicit-admin-key-0123456789"})


def test_writeback_key_is_derived_from_the_token_key():
    """PIVOT_ADDENDUM 9.4: HMAC-SHA256(token_key, b"aldo-writeback"), no separate secret."""
    derived = config.get_writeback_key({"DEMO_TOKEN_KEY": TOKEN_KEY})
    assert derived == hmac.new(TOKEN_KEY.encode(), b"aldo-writeback", hashlib.sha256).digest()
    assert isinstance(derived, bytes) and len(derived) == 32
    assert derived == config.get_writeback_key({"DEMO_TOKEN_KEY": TOKEN_KEY})  # stable
    assert derived != config.get_token_key({"DEMO_TOKEN_KEY": TOKEN_KEY})
    assert derived != config.get_writeback_key({"DEMO_TOKEN_KEY": "a-different-token-key-0123456789"})
    assert derived.hex() != config.get_admin_key({"DEMO_TOKEN_KEY": TOKEN_KEY})  # separate label
    _expect_config_error(config.get_writeback_key, {})  # no token key: fails closed


def test_writeback_key_environment_override_is_for_tests_and_local_runs():
    override = config.get_writeback_key({"DEMO_WRITEBACK_KEY": "explicit-writeback-key-0123456789"})
    assert override == b"explicit-writeback-key-0123456789"
    both = config.get_writeback_key(
        {"DEMO_TOKEN_KEY": TOKEN_KEY, "DEMO_WRITEBACK_KEY": "explicit-writeback-key-0123456789"}
    )
    assert both == override
    _expect_config_error(config.get_writeback_key, {"DEMO_WRITEBACK_KEY": "short"})
    # The old, unprefixed name is not read.
    _expect_config_error(config.get_writeback_key, {"WRITEBACK_KEY": "explicit-writeback-key-0123456789"})


@pytest.fixture
def secrets_manager(monkeypatch: pytest.MonkeyPatch):
    """moto Secrets Manager with fake credentials; nothing here can reach real AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    config.clear_secret_cache()
    with mock_aws():
        yield boto3.client("secretsmanager", region_name="us-east-1")
    config.clear_secret_cache()


def _create_secret(client, payload: object, name: str = "aldo-demo/unit-test-secrets") -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return client.create_secret(Name=name, SecretString=text)["ARN"]


def test_keys_from_secrets_manager(secrets_manager):
    arn = _create_secret(secrets_manager, {"token_key": TOKEN_KEY})
    env = {"SECRETS_ARN": arn}
    assert config.get_token_key(env) == TOKEN_KEY.encode()
    # Only token_key is stored: admin and write-back keys are derived from it.
    assert config.get_admin_key(env) == config.get_admin_key({"DEMO_TOKEN_KEY": TOKEN_KEY})
    assert config.get_writeback_key(env) == config.get_writeback_key({"DEMO_TOKEN_KEY": TOKEN_KEY})


def test_secret_may_carry_its_own_admin_key(secrets_manager):
    arn = _create_secret(
        secrets_manager, {"token_key": TOKEN_KEY, "admin_key": "admin-from-the-secret-0123456789"}
    )
    assert config.get_admin_key({"SECRETS_ARN": arn}) == "admin-from-the-secret-0123456789"


def test_a_writeback_key_field_in_the_secret_is_ignored(secrets_manager):
    # PIVOT_ADDENDUM 9.4: derived, not a new secret, even if infra stored one.
    arn = _create_secret(
        secrets_manager,
        {"token_key": TOKEN_KEY, "writeback_key": "writeback-from-the-secret-0123456789"},
    )
    assert config.get_writeback_key({"SECRETS_ARN": arn}) == hmac.new(
        TOKEN_KEY.encode(), b"aldo-writeback", hashlib.sha256
    ).digest()


def test_secret_can_be_addressed_by_name(secrets_manager):
    _create_secret(secrets_manager, {"token_key": TOKEN_KEY}, name="aldo-demo/by-name")
    assert config.get_token_key({"SECRETS_ARN": "aldo-demo/by-name"}) == TOKEN_KEY.encode()


def test_environment_wins_over_the_secret(secrets_manager):
    arn = _create_secret(secrets_manager, {"token_key": TOKEN_KEY})
    env = {"SECRETS_ARN": arn, "DEMO_TOKEN_KEY": "environment-token-key-0123456789"}
    assert config.get_token_key(env) == b"environment-token-key-0123456789"


def test_secret_is_cached_per_process_until_cleared(secrets_manager):
    arn = _create_secret(secrets_manager, {"token_key": TOKEN_KEY})
    env = {"SECRETS_ARN": arn}
    assert config.get_token_key(env) == TOKEN_KEY.encode()
    secrets_manager.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
    assert config.get_token_key(env) == TOKEN_KEY.encode()  # served from the cache
    config.clear_secret_cache()
    _expect_config_error(config.get_token_key, env)


def test_unreadable_secret_fails_closed_without_leaking_details(secrets_manager):
    error = _expect_config_error(config.get_token_key, {"SECRETS_ARN": "aldo-demo/does-not-exist"})
    assert "does-not-exist" not in error.message


@pytest.mark.parametrize("payload", ["not json at all", '["token_key"]', '{"admin_key": "only-admin-0123456789"}', '{"token_key": 7}'])
def test_malformed_secret_fails_closed(secrets_manager, payload: str):
    arn = _create_secret(secrets_manager, payload)
    _expect_config_error(config.get_token_key, {"SECRETS_ARN": arn})
