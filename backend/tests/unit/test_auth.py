from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from returns_domain import auth, clock, config
from returns_domain.errors import DomainError

KEY = "unit-test-signing-key-0123456789abcdef"
OTHER_KEY = "another-signing-key-0123456789abcdef"
T0 = datetime(2026, 10, 16, 16, 0, 0, tzinfo=UTC)
CUSTOMER = "DEMO-CUST-AVA"
DELEGATED = auth.SCOPE_DELEGATED_GATEWAY


# ------------------------------------------------------------------------------- helpers


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _claims_of(token: str) -> dict:
    return json.loads(_unb64(token.split(".")[0]))


def _forge(claims: object, key: str = KEY) -> str:
    """A correctly signed token with arbitrary claims, to test claim validation."""
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = hmac.new(key.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(signature)}"


def _expect(code: str, fn, *args, **kwargs) -> DomainError:
    with pytest.raises(DomainError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code, caught.value
    return caught.value


def _session(**overrides) -> auth.SessionToken:
    return auth.sign_token(CUSTOMER, key=KEY, now=T0, **overrides)


def _delegated(**overrides) -> auth.SessionToken:
    return auth.sign_token(CUSTOMER, scope=DELEGATED, key=KEY, now=T0, **overrides)


# ------------------------------------------------------------------------ session tokens


def test_sign_and_verify_round_trip():
    signed = _session()
    assert auth.verify_token(signed.token, key=KEY, now=T0) == CUSTOMER
    payload, signature = signed.token.split(".")
    assert "=" not in signed.token and signed.token.count(".") == 1
    assert len(_unb64(signature)) == 32  # HMAC-SHA256
    assert signed.customer_id == CUSTOMER
    assert signed.scope == "session" and signed.actor is None and signed.max_amount_cents is None


def test_session_token_claims_and_to_dict():
    signed = _session()
    claims = _claims_of(signed.token)
    assert claims == {
        "v": 1,
        "customer_id": CUSTOMER,
        "scope": "session",
        "iat": int(T0.timestamp()),
        "exp": int(T0.timestamp()) + 8 * 3600,
    }
    assert signed.to_dict() == {
        "token": signed.token,
        "customer_id": CUSTOMER,
        "expires_at": "2026-10-17T00:00:00+00:00",
    }


def test_session_token_lives_eight_hours_exactly():
    signed = _session()
    assert signed.expires_at - signed.issued_at == timedelta(hours=8)
    just_before = T0 + timedelta(hours=8) - timedelta(seconds=1)
    assert auth.verify_token(signed.token, key=KEY, now=just_before) == CUSTOMER
    for moment in (T0 + timedelta(hours=8), T0 + timedelta(hours=8, seconds=1), T0 + timedelta(days=2)):
        error = _expect("NOT_AUTHENTICATED", auth.verify_token, signed.token, key=KEY, now=moment)
        assert error.http_status == 401
        assert "expired" in error.message


def test_custom_ttl():
    signed = _session(ttl_seconds=60)
    assert auth.verify_token(signed.token, key=KEY, now=T0 + timedelta(seconds=59)) == CUSTOMER
    _expect("NOT_AUTHENTICATED", auth.verify_token, signed.token, key=KEY, now=T0 + timedelta(seconds=60))


@pytest.mark.parametrize("ttl", [0, -5, 1.5, True, "60"])
def test_invalid_ttl_is_rejected(ttl: object):
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, key=KEY, now=T0, ttl_seconds=ttl)


def test_session_ttl_default_follows_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOKEN_TTL_SECONDS", "120")
    assert _session().expires_at - T0 == timedelta(seconds=120)


def test_default_clock_is_the_patchable_wall_clock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(clock, "now_utc", lambda: T0)
    signed = auth.sign_token(CUSTOMER, key=KEY)
    assert signed.issued_at == T0
    assert auth.verify_token(signed.token, key=KEY) == CUSTOMER
    monkeypatch.setattr(clock, "now_utc", lambda: T0 + timedelta(hours=9))
    _expect("NOT_AUTHENTICATED", auth.verify_token, signed.token, key=KEY)


def test_now_must_be_timezone_aware_and_may_be_in_any_zone():
    naive = datetime(2026, 10, 16, 16, 0, 0)
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, key=KEY, now=naive)
    _expect("VALIDATION_ERROR", auth.verify_token, _session().token, key=KEY, now=naive)
    toronto = T0.astimezone(timezone(timedelta(hours=-4)))
    signed = auth.sign_token(CUSTOMER, key=KEY, now=toronto)
    assert signed.issued_at == T0
    assert auth.verify_token(signed.token, key=KEY, now=toronto + timedelta(hours=7)) == CUSTOMER


def test_demo_now_does_not_affect_token_expiry(monkeypatch: pytest.MonkeyPatch):
    signed = _session(ttl_seconds=60)
    monkeypatch.setenv("DEMO_NOW", "2030-01-01T00:00:00-05:00")
    assert auth.verify_token(signed.token, key=KEY, now=T0) == CUSTOMER


def test_signing_key_from_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_TOKEN_KEY", KEY)
    signed = auth.sign_token(CUSTOMER, now=T0)
    assert auth.verify_token(signed.token, now=T0) == CUSTOMER
    # The environment key is the same as passing it explicitly.
    assert auth.verify_token(signed.token, key=KEY, now=T0) == CUSTOMER
    _expect("NOT_AUTHENTICATED", auth.verify_token, signed.token, key=OTHER_KEY, now=T0)


def test_missing_key_is_a_configuration_error_not_an_authentication_failure():
    error = _expect("INTERNAL_ERROR", auth.sign_token, CUSTOMER, now=T0)
    assert error.http_status == 500
    _expect("INTERNAL_ERROR", auth.verify_token, _session().token, now=T0)


@pytest.mark.parametrize("short", ["", "short", "x" * 15, b"y" * 15])
def test_short_explicit_key_is_rejected(short):
    _expect("INTERNAL_ERROR", auth.sign_token, CUSTOMER, key=short, now=T0)
    _expect("INTERNAL_ERROR", auth.verify_token, _session().token, key=short, now=T0)


def test_bytes_and_str_keys_are_equivalent():
    signed = auth.sign_token(CUSTOMER, key=KEY.encode(), now=T0)
    assert auth.verify_token(signed.token, key=KEY, now=T0) == CUSTOMER


@pytest.mark.parametrize("customer_id", ["", " ", "has space", "semi;colon", "ünï", "-lead", "x" * 129, None, 7])
def test_customer_id_format_is_validated_when_signing(customer_id: object):
    _expect("VALIDATION_ERROR", auth.sign_token, customer_id, key=KEY, now=T0)


# --------------------------------------------------------------- tamper and malformed tokens


def test_tampered_payload_is_rejected():
    signed = _session()
    payload, signature = signed.token.split(".")
    claims = _claims_of(signed.token)

    other_customer = _b64(json.dumps({**claims, "customer_id": "DEMO-CUST-BEN"}).encode())
    longer_life = _b64(json.dumps({**claims, "exp": claims["exp"] + 86400}).encode())
    wider_scope = _b64(json.dumps({**claims, "scope": DELEGATED}).encode())
    flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
    for forged_payload in (other_customer, longer_life, wider_scope, flipped):
        error = _expect(
            "NOT_AUTHENTICATED", auth.verify_token, f"{forged_payload}.{signature}", key=KEY, now=T0
        )
        assert error.message == "invalid token"


def test_tampered_signature_is_rejected():
    signed = _session()
    payload, signature = signed.token.split(".")
    flipped = signature[:-1] + ("A" if signature[-1] != "A" else "B")
    for bad in (flipped, signature[:-4], signature + "AAAA", "A" * len(signature), ""):
        _expect("NOT_AUTHENTICATED", auth.verify_token, f"{payload}.{bad}", key=KEY, now=T0)


def test_token_signed_with_another_key_is_rejected():
    foreign = auth.sign_token(CUSTOMER, key=OTHER_KEY, now=T0)
    _expect("NOT_AUTHENTICATED", auth.verify_token, foreign.token, key=KEY, now=T0)


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        " ",
        "abc",
        ".",
        "..",
        "a.b.c",
        "a.b",
        "not base64!.also not",
        "ünï.cödé",
        "a b.c d",
        "x" * 2000,
        123,
        b"bytes.token",
        ["a", "b"],
    ],
)
def test_malformed_tokens_are_not_authenticated(token: object):
    error = _expect("NOT_AUTHENTICATED", auth.verify_token, token, key=KEY, now=T0)
    assert error.http_status == 401


@pytest.mark.parametrize(
    "claims",
    [
        [],
        "just a string",
        {},
        {"v": 2, "customer_id": CUSTOMER, "iat": 1, "exp": 4_000_000_000},
        {"v": 1, "iat": 1, "exp": 4_000_000_000},
        {"v": 1, "customer_id": 7, "iat": 1, "exp": 4_000_000_000},
        {"v": 1, "customer_id": "bad id", "iat": 1, "exp": 4_000_000_000},
        {"v": 1, "customer_id": CUSTOMER, "iat": 1},
        {"v": 1, "customer_id": CUSTOMER, "iat": 1, "exp": "4000000000"},
        {"v": 1, "customer_id": CUSTOMER, "iat": 1, "exp": True},
        {"v": 1, "customer_id": CUSTOMER, "iat": 1.5, "exp": 4_000_000_000},
        {"v": 1, "customer_id": CUSTOMER, "iat": 10**30, "exp": 10**31},
        # scope rules for correctly signed tokens
        {"v": 1, "customer_id": CUSTOMER, "iat": 1, "exp": 4_000_000_000, "scope": "admin"},
        {"v": 1, "customer_id": CUSTOMER, "iat": 1, "exp": 4_000_000_000, "scope": ["session"]},
        {"v": 1, "customer_id": CUSTOMER, "iat": 1, "exp": 4_000_000_000, "scope": "session", "actor": "personal_agent"},
        {"v": 1, "customer_id": CUSTOMER, "iat": 1, "exp": 4_000_000_000, "scope": "session", "max_amount_cents": 100},
    ],
)
def test_signed_but_invalid_claims_are_not_authenticated(claims: object):
    _expect("NOT_AUTHENTICATED", auth.verify_token, _forge(claims), key=KEY, now=T0)


def test_signed_payload_that_is_not_json_is_not_authenticated():
    payload = _b64(b"\xff\xfenot json")
    signature = _b64(hmac.new(KEY.encode(), payload.encode(), hashlib.sha256).digest())
    _expect("NOT_AUTHENTICATED", auth.verify_token, f"{payload}.{signature}", key=KEY, now=T0)


def test_token_without_a_scope_claim_is_a_session_token():
    legacy = _forge({"v": 1, "customer_id": CUSTOMER, "iat": int(T0.timestamp()), "exp": int(T0.timestamp()) + 60})
    claims = auth.verify_claims(legacy, key=KEY, now=T0)
    assert claims.scope == "session" and claims.actor is None and claims.max_amount_cents is None
    _expect("NOT_AUTHORIZED", auth.verify_token, legacy, DELEGATED, key=KEY, now=T0)


def test_signature_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[type, type]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    signed = _session()
    assert auth.verify_token(signed.token, key=KEY, now=T0) == CUSTOMER
    assert calls == [(bytes, bytes)]


# ---------------------------------------------------------------------- delegated tokens


def test_token_claims_fields_match_contracts_f1():
    assert [field.name for field in dataclasses.fields(auth.TokenClaims)] == [
        "customer_id",
        "scope",
        "actor",
        "max_amount_cents",
        "issued_at",
        "expires_at",
    ]
    assert (auth.SCOPE_SESSION, auth.SCOPE_DELEGATED_GATEWAY) == ("session", "delegated_gateway")
    claims = auth.TokenClaims("DEMO-CUST-AVA", "delegated_gateway", "personal_agent", 25000, T0, T0)
    claims.check_amount(25000)  # positional construction works as the contract spells it


def test_delegated_token_defaults():
    signed = _delegated()
    assert signed.scope == DELEGATED
    assert signed.actor == "personal_agent"
    assert signed.max_amount_cents == 25000
    assert signed.expires_at - signed.issued_at == timedelta(minutes=10)
    assert _claims_of(signed.token) == {
        "v": 1,
        "customer_id": CUSTOMER,
        "scope": DELEGATED,
        "actor": "personal_agent",
        "max_amount_cents": 25000,
        "iat": int(T0.timestamp()),
        "exp": int(T0.timestamp()) + 600,
    }
    claims = auth.verify_claims(signed.token, DELEGATED, key=KEY, now=T0)
    assert claims.customer_id == CUSTOMER
    assert claims.scope == DELEGATED
    assert claims.actor == "personal_agent"
    assert claims.max_amount_cents == 25000
    assert claims.issued_at == T0
    assert claims.expires_at == T0 + timedelta(minutes=10)
    assert auth.verify_token(signed.token, DELEGATED, key=KEY, now=T0) == CUSTOMER


def test_delegated_token_expires_after_ten_minutes():
    signed = _delegated()
    assert auth.verify_token(signed.token, DELEGATED, key=KEY, now=T0 + timedelta(seconds=599)) == CUSTOMER
    for moment in (T0 + timedelta(seconds=600), T0 + timedelta(hours=1)):
        error = _expect("NOT_AUTHENTICATED", auth.verify_token, signed.token, DELEGATED, key=KEY, now=moment)
        assert "expired" in error.message


def test_delegated_ttl_can_be_shorter_but_never_longer_than_ten_minutes():
    short = _delegated(ttl_seconds=30)
    assert short.expires_at - short.issued_at == timedelta(seconds=30)
    assert _delegated(ttl_seconds=600).expires_at - T0 == timedelta(seconds=600)
    for too_long in (601, 3600, 8 * 3600):
        error = _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, scope=DELEGATED, key=KEY, now=T0, ttl_seconds=too_long)
        assert "600" in error.message


def test_delegated_ttl_follows_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DELEGATION_TTL_SECONDS", "90")
    assert _delegated().expires_at - T0 == timedelta(seconds=90)
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, scope=DELEGATED, key=KEY, now=T0, ttl_seconds=91)


def test_delegated_cap_defaults_to_the_configured_limit_and_can_be_lower():
    assert _delegated().max_amount_cents == 25000
    lower = _delegated(max_amount_cents=5000)
    assert lower.max_amount_cents == 5000
    assert auth.verify_claims(lower.token, DELEGATED, key=KEY, now=T0).max_amount_cents == 5000
    assert _delegated(max_amount_cents=25000).max_amount_cents == 25000


@pytest.mark.parametrize("cap", [0, -1, 25001, 10**9, 12.5, True, "25000"])
def test_delegated_cap_must_be_a_positive_int_within_the_limit(cap: object):
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, scope=DELEGATED, key=KEY, now=T0, max_amount_cents=cap)


def test_delegation_limit_follows_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DELEGATION_MAX_CENTS", "10000")
    assert _delegated().max_amount_cents == 10000
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, scope=DELEGATED, key=KEY, now=T0, max_amount_cents=10001)


def test_delegated_tokens_are_only_issued_to_the_personal_agent():
    assert auth.ACTOR_PERSONAL_AGENT == "personal_agent"
    assert _delegated().actor == "personal_agent"
    assert _delegated(actor="personal_agent").actor == "personal_agent"
    for bad in ("partner_agent", "", "Personal Agent", "agent;drop", "x" * 65, "admin", 5):
        _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, scope=DELEGATED, key=KEY, now=T0, actor=bad)


def test_session_tokens_cannot_carry_delegation_claims():
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, key=KEY, now=T0, actor="personal_agent")
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, key=KEY, now=T0, max_amount_cents=100)


@pytest.mark.parametrize("scope", ["admin", "", "SESSION", None, "gateway"])
def test_unknown_scope_cannot_be_signed(scope: object):
    _expect("VALIDATION_ERROR", auth.sign_token, CUSTOMER, scope=scope, key=KEY, now=T0)


@pytest.mark.parametrize(
    "extra",
    [
        {},  # no actor, no cap
        {"actor": "personal_agent"},  # no cap: must never verify as unlimited
        {"max_amount_cents": 25000},  # no actor
        {"actor": "personal_agent", "max_amount_cents": 0},
        {"actor": "personal_agent", "max_amount_cents": -5},
        {"actor": "personal_agent", "max_amount_cents": True},
        {"actor": "personal_agent", "max_amount_cents": "25000"},
        {"actor": "Personal Agent", "max_amount_cents": 25000},
        {"actor": "partner_agent", "max_amount_cents": 25000},  # only the personal agent holds one
        {"actor": 7, "max_amount_cents": 25000},
    ],
)
def test_signed_delegated_token_missing_actor_or_cap_is_not_authenticated(extra: dict):
    claims = {"v": 1, "customer_id": CUSTOMER, "scope": DELEGATED, "iat": 1, "exp": 4_000_000_000, **extra}
    _expect("NOT_AUTHENTICATED", auth.verify_token, _forge(claims), DELEGATED, key=KEY, now=T0)


# ------------------------------------------------------------------------- scope separation


def test_default_required_scope_is_session_so_delegated_tokens_are_refused():
    delegated = _delegated()
    error = _expect("NOT_AUTHORIZED", auth.verify_token, delegated.token, key=KEY, now=T0)
    assert error.http_status == 403
    _expect("NOT_AUTHORIZED", auth.verify_claims, delegated.token, key=KEY, now=T0)
    _expect("NOT_AUTHORIZED", auth.customer_id_from_authorization, f"Bearer {delegated.token}", key=KEY, now=T0)
    _expect("NOT_AUTHORIZED", auth.claims_from_authorization, f"Bearer {delegated.token}", key=KEY, now=T0)


def test_session_routes_reject_delegated_tokens_and_gateway_routes_accept_only_delegated():
    session, delegated = _session(), _delegated()
    assert auth.verify_token(session.token, "session", key=KEY, now=T0) == CUSTOMER
    assert auth.verify_token(delegated.token, "delegated_gateway", key=KEY, now=T0) == CUSTOMER
    _expect("NOT_AUTHORIZED", auth.verify_token, delegated.token, "session", key=KEY, now=T0)
    _expect("NOT_AUTHORIZED", auth.verify_token, session.token, "delegated_gateway", key=KEY, now=T0)


def test_scope_is_only_judged_for_genuine_unexpired_tokens():
    delegated, session = _delegated(), _session()
    # Expired: authentication fails first, even though the scope also mismatches.
    _expect("NOT_AUTHENTICATED", auth.verify_token, delegated.token, "session", key=KEY, now=T0 + timedelta(hours=1))
    # Tampered: signature fails first.
    payload, signature = session.token.split(".")
    upgraded = _b64(json.dumps({**_claims_of(session.token), "scope": DELEGATED}).encode())
    _expect("NOT_AUTHENTICATED", auth.verify_token, f"{upgraded}.{signature}", "delegated_gateway", key=KEY, now=T0)
    # Another key: signature fails first.
    foreign = auth.sign_token(CUSTOMER, scope=DELEGATED, key=OTHER_KEY, now=T0)
    _expect("NOT_AUTHENTICATED", auth.verify_token, foreign.token, "session", key=KEY, now=T0)


def test_a_session_token_cannot_be_upgraded_by_forging_claims_without_the_key():
    forged = _forge(
        {"v": 1, "customer_id": CUSTOMER, "scope": DELEGATED, "actor": "personal_agent",
         "max_amount_cents": 25000, "iat": int(T0.timestamp()), "exp": int(T0.timestamp()) + 600},
        key=OTHER_KEY,
    )
    _expect("NOT_AUTHENTICATED", auth.verify_token, forged, DELEGATED, key=KEY, now=T0)


@pytest.mark.parametrize("scope", [None, "", "admin", "SESSION", 3])
def test_unknown_required_scope_is_a_coding_error(scope: object):
    with pytest.raises(ValueError):
        auth.verify_token(_session().token, scope, key=KEY, now=T0)  # type: ignore[arg-type]


def test_customer_id_comes_only_from_the_verified_claims():
    ava = auth.sign_token("DEMO-CUST-AVA", scope=DELEGATED, key=KEY, now=T0)
    ben = auth.sign_token("DEMO-CUST-BEN", scope=DELEGATED, key=KEY, now=T0)
    assert auth.verify_token(ava.token, DELEGATED, key=KEY, now=T0) == "DEMO-CUST-AVA"
    assert auth.verify_token(ben.token, DELEGATED, key=KEY, now=T0) == "DEMO-CUST-BEN"


# ------------------------------------------------------------------------------ the cap claim


def test_check_amount_allows_up_to_and_including_the_cap():
    claims = auth.verify_claims(_delegated().token, DELEGATED, key=KEY, now=T0)
    for amount in (0, 1, 12999, 24999, 25000):
        claims.check_amount(amount)


@pytest.mark.parametrize("amount", [25001, 28900, 10**9])
def test_check_amount_refuses_above_the_cap(amount: int):
    claims = auth.verify_claims(_delegated().token, DELEGATED, key=KEY, now=T0)
    error = _expect("DELEGATION_LIMIT_EXCEEDED", claims.check_amount, amount)
    assert error.http_status == 403
    assert error.retryable is False
    assert "CAD 250.00" in error.message


def test_check_amount_uses_the_cap_inside_the_token_not_the_config(monkeypatch: pytest.MonkeyPatch):
    token = _delegated(max_amount_cents=5000).token
    monkeypatch.setenv("DELEGATION_MAX_CENTS", "999999")  # config changes after signing
    claims = auth.verify_claims(token, DELEGATED, key=KEY, now=T0)
    assert claims.max_amount_cents == 5000
    claims.check_amount(5000)
    _expect("DELEGATION_LIMIT_EXCEEDED", claims.check_amount, 5001)


def test_the_hero_purchase_fits_but_the_boot_does_not():
    claims = auth.verify_claims(_delegated().token, DELEGATED, key=KEY, now=T0)
    claims.check_amount(12999)  # hero style, CAD 129.99
    _expect("DELEGATION_LIMIT_EXCEEDED", claims.check_amount, 28900)  # DEMO-1005 boot, CAD 289.00


@pytest.mark.parametrize("bad", [-1, 12.5, True, "100", None])
def test_check_amount_validates_its_input(bad: object):
    claims = auth.verify_claims(_delegated().token, DELEGATED, key=KEY, now=T0)
    _expect("VALIDATION_ERROR", claims.check_amount, bad)


def test_session_claims_have_no_cap():
    claims = auth.verify_claims(_session().token, key=KEY, now=T0)
    assert claims.max_amount_cents is None
    claims.check_amount(10**9)


# ------------------------------------------------------------------ Authorization header


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_authorization_header_bearer_scheme_is_case_insensitive(scheme: str):
    signed = _session()
    assert auth.customer_id_from_authorization(f"{scheme} {signed.token}", key=KEY, now=T0) == CUSTOMER
    assert auth.claims_from_authorization(f"{scheme}  {signed.token} ", key=KEY, now=T0).customer_id == CUSTOMER


def test_authorization_header_with_scope():
    delegated = _delegated()
    header = f"Bearer {delegated.token}"
    assert auth.customer_id_from_authorization(header, DELEGATED, key=KEY, now=T0) == CUSTOMER
    claims = auth.claims_from_authorization(header, DELEGATED, key=KEY, now=T0)
    assert claims.max_amount_cents == 25000 and claims.actor == "personal_agent"


@pytest.mark.parametrize(
    "header",
    [None, "", "   ", "Bearer", "Bearer ", "Basic abc", "Token abc", "abc", "Bearer a b c", 7],
)
def test_bad_authorization_headers_are_not_authenticated(header: object):
    _expect("NOT_AUTHENTICATED", auth.customer_id_from_authorization, header, key=KEY, now=T0)


# ---------------------------------------------------------------------------- admin secret


def test_admin_secret_accepts_only_the_exact_value():
    auth.verify_admin_secret("correct-admin-secret", expected="correct-admin-secret")
    for bad in (None, "", "wrong", "correct-admin-secret ", "Correct-admin-secret", "ünï-secret", 5):
        error = _expect("NOT_AUTHENTICATED", auth.verify_admin_secret, bad, expected="correct-admin-secret")
        assert error.http_status == 401


def test_admin_secret_comes_from_config_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_ADMIN_KEY", "admin-key-from-the-environment")
    auth.verify_admin_secret("admin-key-from-the-environment")
    _expect("NOT_AUTHENTICATED", auth.verify_admin_secret, "something else")


def test_admin_secret_derived_from_the_token_key_works_end_to_end(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_TOKEN_KEY", KEY)
    derived = config.get_admin_key()
    auth.verify_admin_secret(derived)
    _expect("NOT_AUTHENTICATED", auth.verify_admin_secret, KEY)  # the token key is not the admin key


def test_admin_check_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[type, type]] = []
    real = hmac.compare_digest
    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    auth.verify_admin_secret("s" * 20, expected="s" * 20)
    assert calls == [(bytes, bytes)]


# ------------------------------------------------------------------- write-back signature


BODY = b'{"update_id":"MU-5C0A11","outcome":"exchange_created"}'


def test_writeback_signature_round_trip():
    signature = auth.sign_writeback(BODY, key=KEY)
    assert len(signature) == 64 and signature == signature.lower()
    assert signature == hmac.new(KEY.encode(), BODY, hashlib.sha256).hexdigest()
    auth.verify_writeback_signature(BODY, signature, key=KEY)
    auth.verify_writeback_signature(BODY, f"  {signature.upper()} ", key=KEY)


def test_writeback_signature_rejects_changes():
    signature = auth.sign_writeback(BODY, key=KEY)
    other_signature = auth.sign_writeback(BODY, key=OTHER_KEY)
    assert signature != other_signature
    cases = [
        (BODY + b" ", signature, KEY),
        (BODY.replace(b"created", b"completed"), signature, KEY),
        (BODY, other_signature, KEY),
        (BODY, signature[:-1] + ("0" if signature[-1] != "0" else "1"), KEY),
        (BODY, signature[:-2], KEY),
        (BODY, "ünï" * 10, KEY),
        (BODY, "", KEY),
        (BODY, None, KEY),
        (BODY, 12345, KEY),
    ]
    for body, candidate, key in cases:
        error = _expect("NOT_AUTHENTICATED", auth.verify_writeback_signature, body, candidate, key=key)
        assert error.http_status == 401


def test_writeback_body_must_be_raw_bytes():
    _expect("VALIDATION_ERROR", auth.sign_writeback, "a string body", key=KEY)
    _expect("VALIDATION_ERROR", auth.verify_writeback_signature, {"a": 1}, "abc", key=KEY)
    assert auth.sign_writeback(bytearray(BODY), key=KEY) == auth.sign_writeback(BODY, key=KEY)


def test_writeback_key_comes_from_config_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_WRITEBACK_KEY", "writeback-key-from-the-environment")
    signature = auth.sign_writeback(BODY)
    auth.verify_writeback_signature(BODY, signature)
    assert signature == auth.sign_writeback(BODY, key="writeback-key-from-the-environment")
    _expect("INTERNAL_ERROR", auth.sign_writeback, BODY, key="short")


def test_writeback_key_derived_from_the_token_key_matches_on_both_sides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_TOKEN_KEY", KEY)
    signature = auth.sign_writeback(BODY)
    auth.verify_writeback_signature(BODY, signature)
    # PIVOT_ADDENDUM 9.4: the key is HMAC-SHA256(token_key, b"aldo-writeback").
    derived = hmac.new(KEY.encode(), b"aldo-writeback", hashlib.sha256).digest()
    assert signature == hmac.new(derived, BODY, hashlib.sha256).hexdigest()
    # The token key itself is not the write-back key.
    assert signature != auth.sign_writeback(BODY, key=KEY)
    _expect("NOT_AUTHENTICATED", auth.verify_writeback_signature, BODY, auth.sign_writeback(BODY, key=KEY))


def test_issue_token_is_the_a0_name_of_sign_token():
    assert auth.issue_token is auth.sign_token
    issued = auth.issue_token(CUSTOMER, key=KEY, now=T0, ttl_seconds=120)
    assert auth.verify_token(issued.token, key=KEY, now=T0) == CUSTOMER
