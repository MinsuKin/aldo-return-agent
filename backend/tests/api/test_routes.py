"""API routes: sign-in, reset, impact, the response envelope and the auth sweeps.

Events are HTTP API v2 payloads with ``rawPath="/api/..."`` (CloudFront forwards the prefix);
the table is moto DynamoDB with the demo fixtures.
"""

from __future__ import annotations

import json
import logging

import pytest

from api import http, providers
from returns_domain import audit, auth, clock, store

from .conftest import ADMIN_KEY, AVA, BEN, CHLOE, delegated_token, session_token
from .helpers import FakeContext, v2_event

HERO_SKU_75 = "DEMO-SKU-HERO-BLACK-075"
DC = "DEMO-DC-QC"
ADMIN = {"X-Demo-Admin": ADMIN_KEY}

#: Routes that need no bearer token (their own rules apply).
PUBLIC_ROUTES = {
    ("GET", "/demo/personas"),
    ("POST", "/demo/sessions"),
    ("GET", "/impact"),
    ("GET", "/gateway/.well-known/agent-card.json"),
    ("GET", "/.well-known/agent-card.json"),
}


def registered_routes(app) -> list[tuple[str, str]]:
    """Every (method, template) the resolver knows, including routers that were included."""
    resolver = app.resolver
    return [(r.method, r.path) for r in resolver._static_routes + resolver._dynamic_routes]


def concrete(template: str) -> str:
    """A callable path for a route template; the ``/api`` prefix is added unless it is root-level."""
    path = template
    for part in [p for p in template.split("/") if p.startswith("<")]:
        path = path.replace(part, "x")
    return path if path.startswith("/.well-known") else f"/api{path}"


# ------------------------------------------------------------------------------ sign-in


def test_personas_are_public_and_carry_no_personal_data(client, seeded):
    reply = client.get("/api/demo/personas")
    assert reply.status == 200
    personas = reply.json["personas"]
    assert {p["customer_id"] for p in personas} == {AVA, BEN, CHLOE}
    assert all(set(p) == {"customer_id", "display_name", "description"} for p in personas)


def test_session_issues_a_session_scoped_token_with_the_profile(client, seeded):
    reply = client.post("/api/demo/sessions", {"customer_id": AVA})
    assert reply.status == 200
    body = reply.json
    assert set(body) == {"token", "expires_at", "customer"}
    assert body["customer"] == {"customer_id": AVA, "display_name": "Ava", "home_region": "QC"}
    claims = auth.verify_claims(body["token"], "session")
    assert claims.customer_id == AVA and claims.scope == "session"
    assert claims.actor is None and claims.max_amount_cents is None
    lifetime = clock.parse_iso(body["expires_at"]) - claims.issued_at
    assert lifetime.total_seconds() == 8 * 3600


def test_session_body_cannot_request_another_scope_or_cap(client, seeded):
    reply = client.post(
        "/api/demo/sessions",
        {"customer_id": BEN, "scope": "delegated_gateway", "max_amount_cents": 1, "actor": "x"},
    )
    assert reply.status == 200
    claims = auth.verify_claims(reply.json["token"], "session")
    assert (claims.customer_id, claims.scope, claims.max_amount_cents) == (BEN, "session", None)


def test_session_rejects_an_unknown_persona(client, seeded):
    reply = client.post("/api/demo/sessions", {"customer_id": "DEMO-CUST-NOBODY"})
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '"text"', '{"customer_id": 5}'])
def test_session_needs_a_json_object_with_a_string_id(client, seeded, raw):
    reply = client.call("POST", "/api/demo/sessions", raw_body=raw)
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"


def test_validation_messages_never_echo_the_input(client, seeded):
    reply = client.post("/api/demo/sessions", {"customer_id": ["secret-value-12345"]})
    assert reply.status == 422
    assert "secret-value-12345" not in reply.text


def test_base64_bodies_are_decoded(client, seeded):
    reply = client.call("POST", "/api/demo/sessions", body={"customer_id": CHLOE}, base64_body=True)
    assert reply.status == 200 and reply.json["customer"]["customer_id"] == CHLOE


# -------------------------------------------------------------------------------- reset


def test_reset_needs_the_admin_secret_and_a_bearer_token_is_not_enough(client, seeded):
    body = {"variant": "default"}
    missing = client.post("/api/demo/reset", body)
    assert missing.status == 401 and missing.code == "NOT_AUTHENTICATED"
    wrong = client.post("/api/demo/reset", body, headers={"X-Demo-Admin": "not-the-secret-at-all"})
    assert wrong.status == 401 and wrong.code == "NOT_AUTHENTICATED"
    for token in (session_token(), delegated_token()):
        assert client.post("/api/demo/reset", body, token=token).status == 401


def test_reset_restores_stock_and_clears_runtime_items(client, seeded):
    key = store.inventory_key(HERO_SKU_75, DC)
    store.update_item(key, set={"atp": 0})
    store.put_item({"pk": "RUN#RUN-LEFTOVER", "sk": "META", "status": "completed"})
    reply = client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN)
    assert reply.status == 200
    body = reply.json
    assert body["ok"] is True and body["variant"] == "default"
    assert body["deleted"] >= 1 and body["written"] > 0
    assert store.get_item(key)["atp"] == 5 and store.get_item(key)["version"] == 1
    assert store.get_item(("RUN#RUN-LEFTOVER", "META")) is None


def test_reset_applies_the_requested_variant_and_defaults_to_default(client, seeded):
    key = store.inventory_key(HERO_SKU_75, DC)
    assert client.post("/api/demo/reset", {"variant": "dc_stockout"}, headers=ADMIN).status == 200
    assert store.get_item(key)["atp"] == 0
    default = client.call("POST", "/api/demo/reset", headers=ADMIN)  # no body at all
    assert default.status == 200 and default.json["variant"] == "default"
    assert store.get_item(key)["atp"] == 5


def test_reset_rejects_an_unknown_variant_without_deleting_anything(client, seeded):
    store.put_item({"pk": "RUN#RUN-KEEP", "sk": "META", "status": "running"})
    reply = client.post("/api/demo/reset", {"variant": "everything"}, headers=ADMIN)
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"
    assert store.get_item(("RUN#RUN-KEEP", "META")) is not None


def test_reset_writes_an_audit_event_after_the_wipe(client, seeded):
    audit.write_audit("someone", "execute", "allowed", {})  # wiped by the reset below
    assert client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN).status == 200
    assert audit.list_audit(action="execute") == []
    resets = audit.list_audit(action="reset")
    assert len(resets) == 1 and resets[0]["outcome"] == "allowed"


class FakeMemory:
    """Stands in for ``personal_agent.memory``."""

    def __init__(self) -> None:
        self.resets = 0

    def reset_memory(self) -> dict:
        self.resets += 1
        return {"owners": 3, "entries": 5}


def test_reset_also_resets_the_personal_agent_memory(client, seeded):
    memory = FakeMemory()
    providers.override("personal_agent.memory", memory)
    assert client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN).status == 200
    assert memory.resets == 1


def test_reset_uses_the_older_reset_name_when_that_is_all_there_is(client, seeded):
    class OldMemory:
        resets = 0

        def reset(self):
            OldMemory.resets += 1

    providers.override("personal_agent.memory", OldMemory())
    assert client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN).status == 200
    assert OldMemory.resets == 1


def test_reset_works_without_the_personal_agent_package(client, seeded):
    providers.override("personal_agent.memory", None)  # "not installed"
    assert client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN).status == 200


def test_a_failing_personal_reset_is_not_reported_as_ok(client, seeded):
    class Broken:
        def reset_memory(self):
            raise RuntimeError("table missing")

    providers.override("personal_agent.memory", Broken())
    reply = client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN)
    assert reply.status == 500 and reply.code == "INTERNAL_ERROR"
    assert "table missing" not in reply.text


# ------------------------------------------------------------------------------- impact


def test_impact_is_served_verbatim_from_the_configured_file(client, tmp_path, monkeypatch):
    text = '{"b": 1.50,\n "a": [1, 2.25]}\n'
    path = tmp_path / "impact-model.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("IMPACT_JSON_PATH", str(path))
    reply = client.get("/api/impact")
    assert reply.status == 200 and reply.text == text
    assert reply.headers["content-type"].startswith("application/json")


def test_impact_falls_back_to_the_bundled_copy_next_to_the_fixtures(client, tmp_path, monkeypatch):
    (tmp_path / "impact-model.json").write_text('{"bundled": true}', encoding="utf-8")
    monkeypatch.delenv("IMPACT_JSON_PATH", raising=False)
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path))
    assert client.get("/api/impact").json == {"bundled": True}


def test_impact_missing_is_a_json_404(client, tmp_path, monkeypatch):
    monkeypatch.setenv("IMPACT_JSON_PATH", str(tmp_path / "nope.json"))
    reply = client.get("/api/impact")
    assert reply.status == 404 and reply.code == "NOT_FOUND"


def test_a_broken_impact_file_fails_loudly(client, tmp_path, monkeypatch):
    path = tmp_path / "impact-model.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("IMPACT_JSON_PATH", str(path))
    assert client.get("/api/impact").status == 500


# ---------------------------------------------------------------------------- envelope


def test_unknown_routes_and_methods_are_json_404s(client, seeded):
    for method, path in (("GET", "/api/nope"), ("POST", "/api/demo/personas"), ("DELETE", "/api/orders")):
        reply = client.call(method, path)
        assert reply.status == 404 and reply.code == "NOT_FOUND", (method, path)
        assert reply.error["request_id"] == reply.headers["x-request-id"]


def test_every_response_is_json_no_store_and_carries_the_request_id(client, seeded):
    replies = [
        client.get("/api/demo/personas"),
        client.get("/api/orders"),  # 401
        client.get("/api/nope"),  # 404
        client.post("/api/demo/sessions", {"customer_id": "nobody"}),  # 422
    ]
    for reply in replies:
        assert reply.headers["content-type"].startswith("application/json")
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["x-request-id"].startswith("req-test-")
    assert replies[1].error["request_id"] == replies[1].headers["x-request-id"]


def test_the_error_body_has_the_documented_shape(client, seeded):
    error = client.get("/api/orders").error
    assert set(error) == {"code", "message", "retryable", "request_id"}
    assert error["code"] == "NOT_AUTHENTICATED" and error["retryable"] is False


def test_retryable_errors_say_so(client, seeded, enqueued):
    class Boom:
        def persona_list(self):
            raise http.DomainError("UPSTREAM_TIMEOUT", "try again")

    providers.override("fixtures", Boom())
    reply = client.get("/api/demo/personas")
    assert reply.status == 504 and reply.error["retryable"] is True


def test_unexpected_errors_are_generic_and_leak_nothing(client, seeded):
    class Boom:
        def persona_list(self):
            raise RuntimeError("db password=hunter2 at 10.0.0.7")

    providers.override("fixtures", Boom())
    reply = client.get("/api/demo/personas")
    assert reply.status == 500 and reply.code == "INTERNAL_ERROR"
    assert "hunter2" not in reply.text and "10.0.0.7" not in reply.text
    assert reply.error["message"] == http.GENERIC_ERROR_MESSAGE


def test_path_parameters_with_odd_characters_never_reach_a_route(client, seeded):
    reply = client.get("/api/quotes/x%00y", token=session_token())
    assert reply.status == 404
    assert client.get("/api/quotes/" + "a" * 300, token=session_token()).status == 404


def test_money_never_becomes_a_string_in_json(client, seeded):
    class Priced:
        def persona_list(self):
            from decimal import Decimal

            return [{"customer_id": "x", "display_name": "y", "description": Decimal("12999")}]

    providers.override("fixtures", Priced())
    assert client.get("/api/demo/personas").json["personas"][0]["description"] == 12999


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_one_structured_log_line_per_request_without_credentials(client, seeded):
    capture = Capture()
    target = logging.getLogger(http.logger.service)
    target.addHandler(capture)
    try:
        token = session_token()
        client.get("/api/orders", token=token)
        client.get("/api/demo/personas")
    finally:
        target.removeHandler(capture)
    lines = [r for r in capture.records if r.getMessage() == "request"]
    assert len(lines) == 2
    orders_line, personas_line = lines
    assert orders_line.route == "GET /orders" and personas_line.route == "GET /demo/personas"
    assert personas_line.status == 200 and personas_line.outcome == "ok"
    assert personas_line.request_id == "req-test-0002"
    assert isinstance(personas_line.latency_ms, int)
    everything = json.dumps([r.__dict__ for r in capture.records], default=str)
    assert token not in everything and "Bearer" not in everything


# ------------------------------------------------------------------------- auth sweeps


def test_every_registered_route_is_in_the_registry_and_vice_versa(app):
    ours = {(spec.method, spec.path) for spec in app.routes}
    theirs = set(registered_routes(app))
    assert ours <= theirs
    assert {r for r in theirs - ours if not r[1].startswith("/personal-agent")} == set()


def test_every_route_rejects_a_request_without_credentials(client, app, seeded):
    checked = 0
    for method, template in registered_routes(app):
        reply = client.call(method, concrete(template))
        if (method, template) in PUBLIC_ROUTES:
            assert reply.status not in (401, 403), (method, template)
        else:
            assert reply.status == 401, (method, template, reply.status)
        checked += 1
    assert checked >= 20


def test_a_malformed_or_expired_token_is_a_401_everywhere(client, app, seeded):
    expired = auth.sign_token(AVA, ttl_seconds=1, now=clock.now_utc().replace(year=2020)).token
    bad_values = [
        "Bearer not-a-token",
        f"Token {session_token()}",  # wrong scheme
        f"Bearer {expired}",
        f"Bearer {session_token()}x",  # signature no longer matches
    ]
    for header in bad_values:
        reply = client.get("/api/orders", headers={"Authorization": header})
        assert reply.status == 401 and reply.code == "NOT_AUTHENTICATED", header


def test_session_routes_reject_delegated_tokens(client, app, seeded):
    """A delegated token (the ALDO bot's) must be refused by every non-gateway route."""
    token = delegated_token()
    session_routes = [
        (spec.method, spec.path) for spec in app.routes if spec.auth == http.SESSION
    ]
    assert len(session_routes) >= 12
    for method, template in session_routes:
        reply = client.call(method, concrete(template), token=token, body={} if method == "POST" else None)
        assert reply.status == 403 and reply.code == "NOT_AUTHORIZED", (method, template)


def test_the_envelope_survives_the_lambda_entry_point(ddb, monkeypatch):
    from api import handler as entry

    monkeypatch.setenv("DEMO_ADMIN_KEY", ADMIN_KEY)
    event = v2_event("GET", "/api/demo/personas")
    result = entry.handler(event, FakeContext())
    assert result["statusCode"] == 200
    assert result["headers"]["X-Request-Id"] == "req-test-0001"
