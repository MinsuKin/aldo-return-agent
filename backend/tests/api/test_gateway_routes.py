"""Gateway-facing routes: token scopes, purchases, agent card, A2A events, demo controls.

``returns_domain.gateway`` is replaced by a small fake through ``providers.override`` (the
provider seam), so these tests pin what the API layer does: which token it accepts, whose
customer id it passes, how errors map to HTTP, and that it never decides anything itself.
"""

from __future__ import annotations

import json

import boto3
import pytest
from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.event_handler.api_gateway import Router

from api import handler as entry
from api import http, providers
from returns_domain import auth, clock, personal_agent_client, trace
from returns_domain.errors import DomainError

from .conftest import AVA, BEN, delegated_token, session_token
from .fakes import FakeGateway, GatewayError
from .helpers import DOMAIN_NAME
from .test_routes import PUBLIC_ROUTES, concrete, registered_routes

SIZE_ASSIST = {
    "intent": "size_assist",
    "product_query": "Stessy",
    "color": "BLACK",
    "size_system": "US_W",
    "deadline_date": "2026-10-17",
    "channel_pref": None,
}
PROPOSAL = {
    "size_request_id": "SZR-3F9A1C",
    "option_id": "SO-3",
    "chosen_size": "8.0",
    "rationale_public": "runs small per ALDO signal; a half size up",
    "confidence": "high",
}


@pytest.fixture
def gateway(client) -> FakeGateway:
    fake = FakeGateway()
    providers.override("gateway", fake)
    return fake


# ------------------------------------------------------------------ token scopes (C4)

GATEWAY_CALLS = [
    ("/api/gateway/size-assist", SIZE_ASSIST),
    ("/api/gateway/size-decision", PROPOSAL),
]


@pytest.mark.parametrize("path,body", GATEWAY_CALLS)
def test_session_token_rejected_at_gateway(client, gateway, path, body):
    reply = client.post(path, body, token=session_token())
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED"
    assert gateway.calls == []


@pytest.mark.parametrize("path,body", GATEWAY_CALLS)
def test_gateway_needs_a_token_and_rejects_a_bad_one(client, gateway, path, body):
    assert client.post(path, body).status == 401
    assert client.post(path, body, headers={"Authorization": "Bearer junk"}).status == 401
    expired = delegated_token(ttl_seconds=1, now=clock.now_utc().replace(year=2020))
    reply = client.post(path, body, token=expired)
    assert reply.status == 401 and reply.code == "NOT_AUTHENTICATED"
    assert gateway.calls == []


PURCHASE_CALLS = [
    ("GET", "/api/purchases/SZR-3F9A1C", None),
    ("POST", "/api/purchases/SZR-3F9A1C/confirm", {"option_id": "SO-3"}),
]


@pytest.mark.parametrize("method,path,body", PURCHASE_CALLS)
def test_delegated_token_cannot_reach_the_owners_purchase_routes(client, gateway, method, path, body):
    """No agent can place an order: the owner's click needs a SESSION token."""
    reply = client.call(method, path, body=body, token=delegated_token())
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED"
    assert gateway.calls == []


# Every route that is neither public, nor delegated-only, nor the admin reset, nor the HMAC
# write-back: these must be session-token routes and refuse the ALDO bot's delegated token.
NOT_SESSION_ROUTES = PUBLIC_ROUTES | {
    ("POST", "/gateway/size-assist"),
    ("POST", "/gateway/size-decision"),
    ("POST", "/demo/reset"),
    ("POST", "/personal-agent/memory-updates"),
}
SESSION_ROUTE_TABLE = sorted(
    r for r in registered_routes(entry.app) if r not in NOT_SESSION_ROUTES
)


def test_the_route_table_to_sweep_is_not_empty():
    assert len(SESSION_ROUTE_TABLE) >= 14
    assert ("POST", "/demo/simulate-pickup") in SESSION_ROUTE_TABLE
    assert ("GET", "/a2a/events") in SESSION_ROUTE_TABLE
    assert ("POST", "/purchases/<size_request_id>/confirm") in SESSION_ROUTE_TABLE


@pytest.mark.parametrize("method,template", SESSION_ROUTE_TABLE)
def test_delegated_token_rejected_everywhere(client, gateway, method, template):
    reply = client.call(
        method, concrete(template), body={} if method == "POST" else None, token=delegated_token()
    )
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED", (method, template)
    assert gateway.calls == []


# ----------------------------------------------------------- size assist / size decision


def test_size_assist_hands_the_verified_claims_and_the_raw_body_over(client, gateway):
    reply = client.post("/api/gateway/size-assist", SIZE_ASSIST, token=delegated_token(BEN))
    assert reply.status == 200 and reply.json["size_request_id"] == "SZR-3F9A1C"
    (name, (claims, request), kwargs), = gateway.calls
    assert name == "size_assist" and kwargs == {}
    assert claims.customer_id == BEN and claims.scope == "delegated_gateway"
    assert claims.actor == "personal_agent" and claims.max_amount_cents == 25_000
    assert request == SIZE_ASSIST  # untouched: the gateway module owns the strict schema


def test_the_customer_comes_from_the_token_even_when_the_body_names_another(client, gateway):
    body = {**SIZE_ASSIST, "customer_id": AVA, "owner_fit_hint": "usually 9"}
    client.post("/api/gateway/size-assist", body, token=delegated_token(BEN))
    (_, (claims, request), _), = gateway.calls
    assert claims.customer_id == BEN
    assert request == body  # the route does not clean it up; the gateway rejects it (422)


def test_size_decision_passes_the_delegation_cap_and_returns_a_proposal(client, gateway):
    reply = client.post("/api/gateway/size-decision", PROPOSAL, token=delegated_token(max_amount_cents=15_000))
    assert reply.status == 200
    assert reply.json["status"] == "proposed" and reply.json["awaiting"] == "owner_confirmation"
    assert "order_id" not in reply.json
    (name, (claims, request), _), = gateway.calls
    assert name == "decide_size" and claims.max_amount_cents == 15_000 and request == PROPOSAL


@pytest.mark.parametrize("raw", [None, "", "{oops", "[]", '"text"'])
def test_gateway_bodies_must_be_json_objects(client, gateway, raw):
    for path in ("/api/gateway/size-assist", "/api/gateway/size-decision"):
        reply = client.call("POST", path, raw_body=raw, token=delegated_token())
        assert reply.status == 422 and reply.code == "VALIDATION_ERROR"
    assert gateway.calls == []


def test_gateway_bodies_are_size_limited(client, gateway):
    body = {"product_query": "x" * 20_000}
    reply = client.post("/api/gateway/size-assist", body, token=delegated_token())
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"
    assert gateway.calls == []


ERROR_STATUS = [
    ("UNKNOWN_STYLE", 422),
    ("INVALID_SIZE_CHOICE", 422),
    ("VALIDATION_ERROR", 422),
    ("REQUEST_EXPIRED", 409),
    ("STOCK_CHANGED", 409),
    ("IDEMPOTENCY_CONFLICT", 409),
    ("APPROVAL_REQUIRED", 409),
    ("DELEGATION_LIMIT_EXCEEDED", 403),
    ("NOT_AUTHORIZED", 403),
    ("NOT_FOUND", 404),
    ("TRANSACTION_CONFLICT", 409),
]


@pytest.mark.parametrize("code,status", ERROR_STATUS)
def test_gateway_errors_map_to_http(client, gateway, code, status):
    gateway.failure = DomainError(code, "nope")
    for method, path, body, token in (
        ("POST", "/api/gateway/size-assist", SIZE_ASSIST, delegated_token()),
        ("POST", "/api/gateway/size-decision", PROPOSAL, delegated_token()),
        ("GET", "/api/purchases/SZR-3F9A1C", None, session_token()),
        ("POST", "/api/purchases/SZR-3F9A1C/confirm", {}, session_token()),
    ):
        reply = client.call(method, path, body=body, token=token)
        assert reply.status == status and reply.code == code, (path, reply.text)
        assert reply.error["request_id"] == reply.headers["x-request-id"]


def test_unknown_style_carries_its_candidates_next_to_the_code(client, gateway):
    candidates = [{"style_id": "DEMO-STYLE-HERO", "product_name": "Stessy2.0", "colors": ["BLACK"]}]
    gateway.failure = GatewayError("UNKNOWN_STYLE", "Which style?", extra={"candidates": candidates})
    reply = client.post("/api/gateway/size-assist", SIZE_ASSIST, token=delegated_token())
    assert reply.status == 422
    assert reply.error["code"] == "UNKNOWN_STYLE" and reply.error["candidates"] == candidates
    assert reply.error["retryable"] is False and "request_id" in reply.error


def test_a_crashing_gateway_does_not_leak_internals(client, gateway):
    gateway.failure = RuntimeError("SIZEREQ#SZR-1 table aldo-returns-demo exploded")
    reply = client.post("/api/gateway/size-assist", SIZE_ASSIST, token=delegated_token())
    assert reply.status == 500 and "exploded" not in reply.text


# ---------------------------------------------------------------------------- purchases


def test_the_owner_reads_the_proposal_with_a_session_token(client, gateway):
    reply = client.get("/api/purchases/SZR-3F9A1C", token=session_token(BEN))
    assert reply.status == 200 and reply.json["status"] == "proposed"
    assert gateway.calls == [("get_proposal", (BEN, "SZR-3F9A1C"), {})]


def test_confirm_is_the_owners_click_and_forwards_the_option(client, gateway):
    reply = client.post("/api/purchases/SZR-3F9A1C/confirm", {"option_id": "SO-3"}, token=session_token(AVA))
    assert reply.status == 200 and reply.json["order_id"] == "DEMO-A2A-81B20E"
    assert gateway.calls == [("confirm_purchase", (AVA, "SZR-3F9A1C"), {"option_id": "SO-3"})]


def test_confirm_works_without_a_body_and_ignores_unknown_keys(client, gateway):
    assert client.call("POST", "/api/purchases/SZR-3F9A1C/confirm", token=session_token()).status == 200
    client.post(
        "/api/purchases/SZR-3F9A1C/confirm",
        {"option_id": "SO-2", "customer_id": BEN, "price_cents": 1},
        token=session_token(AVA),
    )
    assert [c[2] for c in gateway.calls] == [{"option_id": None}, {"option_id": "SO-2"}]
    assert gateway.calls[1][1][0] == AVA  # the body's customer_id is ignored


def test_confirm_rejects_a_non_string_option(client, gateway):
    reply = client.post("/api/purchases/SZR-3F9A1C/confirm", {"option_id": 3}, token=session_token())
    assert reply.status == 422 and gateway.calls == []


# ----------------------------------------------------------------------------- agent card


@pytest.mark.parametrize("path", ["/api/gateway/.well-known/agent-card.json", "/.well-known/agent-card.json"])
def test_agent_card_is_public_on_both_paths(client, gateway, path):
    reply = client.get(path)
    assert reply.status == 200
    assert reply.json["name"] == "ALDO Fit Gateway"
    assert reply.json["url"] == f"https://{DOMAIN_NAME}/api/gateway"


@pytest.fixture(autouse=True)
def no_cached_base_url():
    """The SSM-resolved public URL is cached per process; keep tests independent."""
    personal_agent_client.clear_cache()
    yield
    personal_agent_client.clear_cache()


@pytest.mark.parametrize("path", ["/api/gateway/.well-known/agent-card.json", "/.well-known/agent-card.json"])
def test_agent_card_uses_the_configured_public_url_not_the_request_host(client, gateway, monkeypatch, path):
    """Behind CloudFront the Lambda sees the execute-api Host header; the card must not use it."""
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://d111.cloudfront.net/")
    reply = client.get(path, headers={"Host": DOMAIN_NAME, "X-Forwarded-Host": "other.example.com"})
    assert reply.json["url"] == "https://d111.cloudfront.net/api/gateway"  # no trailing slash, no Host


def test_agent_card_reads_the_public_url_from_ssm_once(client, gateway, no_cached_base_url):
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/aldo-demo/gateway-url", Value="https://d222.cloudfront.net", Type="String")
    first = client.get("/.well-known/agent-card.json", headers={"Host": DOMAIN_NAME})
    assert first.json["url"] == "https://d222.cloudfront.net/api/gateway"
    ssm.put_parameter(Name="/aldo-demo/gateway-url", Value="https://changed.cloudfront.net", Type="String", Overwrite=True)
    second = client.get("/api/gateway/.well-known/agent-card.json", headers={"Host": DOMAIN_NAME})
    assert second.json["url"] == "https://d222.cloudfront.net/api/gateway"  # cached for the process


def test_agent_card_falls_back_to_the_request_only_when_nothing_is_configured(client, gateway, no_cached_base_url):
    reply = client.get("/.well-known/agent-card.json")  # no env, no SSM parameter (local run)
    assert reply.json["url"] == f"https://{DOMAIN_NAME}/api/gateway"


def test_agent_card_base_url_follows_a_forwarded_host_but_not_a_hostile_one(client, gateway, no_cached_base_url):
    ok = client.get("/.well-known/agent-card.json", headers={"X-Forwarded-Host": "demo.example.com", "X-Forwarded-Proto": "https"})
    assert ok.json["url"] == "https://demo.example.com/api/gateway"
    hostile = client.get("/.well-known/agent-card.json", headers={"Host": "evil.com/\"><script>"})
    assert hostile.json["url"] == f"https://{DOMAIN_NAME}/api/gateway"


# ------------------------------------------------------------------------- A2A events


def emit_size_assist(customer_id: str) -> int:
    return trace.emit_a2a(
        customer_id,
        "size_assist_requested",
        {"size_request_id": "SZR-3F9A1C", "product_query": "Stessy"},
        actor="personal_agent",
    )


def test_a2a_events_belong_to_the_signed_in_owner(client, seeded):
    assert emit_size_assist(AVA) == 1
    emit_size_assist(AVA)
    mine = client.get("/api/a2a/events", query={"after": "0"}, token=session_token(AVA))
    assert mine.status == 200
    assert [e["seq"] for e in mine.json["events"]] == [1, 2]
    assert mine.json["events"][0]["type"] == "size_assist_requested"
    assert mine.json["events"][0]["actor"] == "personal_agent"
    assert mine.json["next_after"] == 2
    assert AVA not in mine.text  # the stream carries no customer identifier
    theirs = client.get("/api/a2a/events", token=session_token(BEN))
    assert theirs.status == 200 and theirs.json["events"] == []


def test_a2a_events_honour_the_after_cursor_and_start_empty(client, seeded):
    empty = client.get("/api/a2a/events", token=session_token(AVA))
    assert empty.status == 200 and empty.json["events"] == [] and empty.json["next_after"] == 0
    emit_size_assist(AVA)
    emit_size_assist(AVA)
    page = client.get("/api/a2a/events", query={"after": "1"}, token=session_token(AVA))
    assert [e["seq"] for e in page.json["events"]] == [2]
    assert page.json["next_after"] == 2
    caught_up = client.get("/api/a2a/events", query={"after": "2"}, token=session_token(AVA))
    assert caught_up.json["events"] == [] and caught_up.json["next_after"] == 2


@pytest.mark.parametrize("after", ["-1", "abc", "1.5", "99999999999"])
def test_a2a_events_validate_after(client, seeded, after):
    reply = client.get("/api/a2a/events", query={"after": after}, token=session_token())
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"


# ----------------------------------------------------------------------- demo controls


def test_simulate_delivery_is_for_the_signed_in_owner(client, gateway):
    reply = client.post("/api/demo/simulate-delivery", {"order_id": "DEMO-A2A-81B20E"}, token=session_token(BEN))
    assert reply.status == 200 and reply.json["delivered_at"] == "2026-10-16"
    assert gateway.calls == [("simulate_delivery", (BEN,), {"order_id": "DEMO-A2A-81B20E"})]


def test_simulate_pickup_is_for_the_signed_in_owner(client, gateway):
    reply = client.post("/api/demo/simulate-pickup", {"transaction_id": "DEMO-RMA-3F9A1C"}, token=session_token(AVA))
    assert reply.status == 200 and reply.json["update_id"] == "MU-5C0A11"
    assert gateway.calls == [("simulate_pickup", (AVA,), {"transaction_id": "DEMO-RMA-3F9A1C"})]


@pytest.mark.parametrize("path,body", [
    ("/api/demo/simulate-delivery", {}),
    ("/api/demo/simulate-delivery", {"order_id": ""}),
    ("/api/demo/simulate-pickup", {"transaction_id": 7}),
    ("/api/demo/simulate-pickup", {}),
])
def test_simulate_controls_validate_their_body(client, gateway, path, body):
    reply = client.post(path, body, token=session_token())
    assert reply.status == 422 and gateway.calls == []


def test_a_foreign_order_is_the_gateways_call_to_refuse(client, gateway):
    gateway.failure = DomainError("NOT_AUTHORIZED", "Order not found for this customer.")
    reply = client.post("/api/demo/simulate-delivery", {"order_id": "DEMO-1001"}, token=session_token(BEN))
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED"


# --------------------------------------------------------- personal-agent run events


class FakePersonalMemory:
    def __init__(self) -> None:
        self.reads: list[tuple] = []

    def list_run_events(self, owner_id, run_id, after=0, limit=100):
        self.reads.append((owner_id, run_id, after))
        if owner_id != AVA:
            raise DomainError("NOT_FOUND", "run not found")
        return {
            "run_id": run_id,
            "status": "completed",
            "events": [{"seq": 1, "type": "run_started", "ts": "t", "actor": "system", "payload": {}}],
            "next_after": 1,
        }


def test_personal_run_ids_are_read_from_the_personal_agent_table(client, seeded):
    memory = FakePersonalMemory()
    providers.override("personal_agent.memory", memory)
    reply = client.get("/api/runs/PRUN-0123456789/events", query={"after": "0"}, token=session_token(AVA))
    assert reply.status == 200
    assert reply.json == {
        "run_id": "PRUN-0123456789",
        "status": "completed",
        "events": [{"seq": 1, "type": "run_started", "ts": "t", "actor": "system", "payload": {}}],
        "next_after": 1,
    }
    assert memory.reads == [(AVA, "PRUN-0123456789", 0)]  # the owner is the token's customer


def test_another_owners_personal_run_is_not_found(client, seeded):
    providers.override("personal_agent.memory", FakePersonalMemory())
    reply = client.get("/api/runs/PRUN-0123456789/events", token=session_token(BEN))
    assert reply.status == 404 and reply.code == "NOT_FOUND"


def test_personal_run_ids_without_the_package_are_not_found(client, seeded):
    providers.override("personal_agent.memory", None)
    reply = client.get("/api/runs/PRUN-0123456789/events", token=session_token(AVA))
    assert reply.status == 404 and reply.code == "NOT_FOUND"


def test_a_delegated_token_cannot_read_run_events(client, seeded):
    providers.override("personal_agent.memory", FakePersonalMemory())
    reply = client.get("/api/runs/PRUN-0123456789/events", token=delegated_token())
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED"


# ------------------------------------------------------- the personal-agent router


class FakeRouterModule:
    """Stands in for ``api.personal_agent_routes`` (owned by the personal-agent stream)."""

    def __init__(self) -> None:
        self.enqueue = None
        self.before_memory_read = None

    def build_router(self, *, enqueue, before_memory_read=None) -> Router:
        self.enqueue, self.before_memory_read = enqueue, before_memory_read
        router = Router()

        @router.get("/personal-agent/memory")
        def memory() -> dict:
            owner = auth.customer_id_from_authorization(router.current_event.headers.get("authorization"))
            if before_memory_read:
                before_memory_read(owner)
            return {"owner_id": owner}

        @router.post("/personal-agent/conversations/<conv>/messages")
        def send(conv: str) -> Response:
            owner = auth.customer_id_from_authorization(router.current_event.headers.get("authorization"))
            enqueue({"run_id": "PRUN-0123456789", "conv_id": conv, "customer_id": owner, "agent": "personal"})
            return Response(202, "application/json", json.dumps({"run_id": "PRUN-0123456789"}))

        @router.get("/personal-agent/boom")
        def boom() -> dict:
            raise DomainError("RUN_IN_PROGRESS", "busy")

        return router


@pytest.fixture
def router_module():
    """Install the fake router module BEFORE the app is built (list it first in a test)."""
    module = FakeRouterModule()
    providers.override("api.personal_agent_routes", module)
    return module


@pytest.fixture
def no_router():
    providers.override("api.personal_agent_routes", None)  # "module not present"


@pytest.fixture
def broken_router():
    class Broken:
        def build_router(self, **_):
            raise RuntimeError("boom")

    providers.override("api.personal_agent_routes", Broken())


def bare_api() -> http.Api:
    from aws_lambda_powertools.event_handler import APIGatewayHttpResolver

    resolver = APIGatewayHttpResolver(strip_prefixes=["/api"], enable_validation=False)
    return http.Api(resolver, enqueue=lambda payload: None)


def test_include_reports_whether_the_router_was_added(router_module):
    assert entry.include_personal_agent_routes(bare_api()) is True
    providers.override("api.personal_agent_routes", None)
    assert entry.include_personal_agent_routes(bare_api()) is False


def test_the_personal_router_is_included_with_the_worker_hand_off(router_module, client, gateway, enqueued):
    reply = client.post("/api/personal-agent/conversations/PCONV-1/messages", {}, token=session_token(AVA))
    assert reply.status == 202
    assert enqueued == [
        {"run_id": "PRUN-0123456789", "conv_id": "PCONV-1", "customer_id": AVA, "agent": "personal"}
    ]


def test_the_router_hand_off_follows_a_replaced_enqueue(router_module, app, client, gateway):
    seen: list[dict] = []
    app.enqueue = seen.append
    client.post("/api/personal-agent/conversations/PCONV-1/messages", {}, token=session_token(AVA))
    assert len(seen) == 1


def test_reading_memory_flushes_the_write_back_outbox_first(router_module, client, gateway):
    reply = client.get("/api/personal-agent/memory", token=session_token(BEN))
    assert reply.status == 200 and reply.json == {"owner_id": BEN}
    assert gateway.calls == [("flush_outbox", (BEN,), {"limit": 10})]


def test_a_failing_flush_never_blocks_reading_memory(router_module, client, gateway):
    gateway.failure = RuntimeError("receiver down")
    reply = client.get("/api/personal-agent/memory", token=session_token(BEN))
    assert reply.status == 200


def test_router_routes_get_the_same_envelope_and_scope_rules(router_module, client, gateway):
    delegated = client.get("/api/personal-agent/memory", token=delegated_token())
    assert delegated.status == 403 and delegated.code == "NOT_AUTHORIZED"
    assert client.get("/api/personal-agent/memory").status == 401
    busy = client.get("/api/personal-agent/boom", token=session_token())
    assert busy.status == 409 and busy.error["retryable"] is True
    assert busy.headers["cache-control"] == "no-store" and "request_id" in busy.error


def test_the_api_works_without_the_router_module(no_router, client, seeded):
    assert client.get("/api/demo/personas").status == 200
    assert client.get("/api/personal-agent/memory", token=session_token()).status == 404


def test_the_api_works_when_the_router_module_is_broken(broken_router, client, seeded):
    assert client.get("/api/demo/personas").status == 200
    assert client.get("/api/personal-agent/memory", token=session_token()).status == 404


def test_the_route_table_lists_the_included_router(router_module, app):
    assert ("GET", "/personal-agent/memory") in registered_routes(app)
