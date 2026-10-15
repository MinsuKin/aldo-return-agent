"""GatewayClient: local, http and a2a transports (contracts.md F.8; PIVOT_ADDENDUM C4, C7)."""

from __future__ import annotations

import io
import json
import socket
import sys
import types
import urllib.error
from typing import Any

import pytest

from personal_agent import gateway_client
from personal_agent.gateway_client import (
    GatewayCallError,
    HttpGatewayClient,
    LocalGatewayClient,
    build_gateway_client,
)
from returns_domain import auth
from returns_domain.errors import DomainError

from .canned import DEMO_TOKEN, CannedGateway, load_assist, proposed_response

AVA = "DEMO-CUST-AVA"
BASE = "https://d111.cloudfront.net"
REQUEST = {"intent": "size_assist", "product_query": "Stessy", "size_system": "US_W", "deadline_date": "2026-10-17"}


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeOpener:
    """Plays a script of outcomes: ``(status, dict)``, or an exception to raise."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls: list[tuple[Any, float | None]] = []

    def open(self, request: Any, timeout: float | None = None) -> FakeResponse:
        self.calls.append((request, timeout))
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        status, body = outcome
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        if status >= 400 or 300 <= status < 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, io.BytesIO(raw))
        return FakeResponse(status, raw)


def http_client(opener: FakeOpener, sleeps: list[float] | None = None, **kwargs: Any) -> HttpGatewayClient:
    record = sleeps if sleeps is not None else []
    return HttpGatewayClient(BASE, DEMO_TOKEN, opener=opener, sleep=record.append, **kwargs)


def error_body(code: str, message: str = "nope", **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": False, "request_id": "req-1", **extra}}


# ------------------------------------------------------------------------------------ http


def test_http_posts_json_with_the_delegated_bearer_token_and_a_20_second_timeout() -> None:
    opener = FakeOpener((200, load_assist("default")))
    answer = http_client(opener).size_assist(REQUEST)
    assert answer["size_request_id"] == "SZR-3F9A1C"
    request, timeout = opener.calls[0]
    assert request.full_url == f"{BASE}/api/gateway/size-assist"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {DEMO_TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == REQUEST
    assert timeout == 20.0


def test_http_size_decision_uses_its_own_path() -> None:
    opener = FakeOpener((200, proposed_response()))
    http_client(opener).size_decision({"size_request_id": "SZR-3F9A1C"})
    assert opener.calls[0][0].full_url == f"{BASE}/api/gateway/size-decision"


def test_the_token_is_never_visible_in_repr_or_errors() -> None:
    client = http_client(FakeOpener((403, error_body("NOT_AUTHORIZED", "wrong scope"))))
    assert DEMO_TOKEN not in repr(client)
    with pytest.raises(GatewayCallError) as caught:
        client.size_assist(REQUEST)
    exposed = f"{caught.value} {caught.value.to_dict()} {caught.value.args}"
    assert DEMO_TOKEN not in exposed
    assert DEMO_TOKEN not in repr(LocalGatewayClient(DEMO_TOKEN))


def test_one_retry_on_a_5xx_answer() -> None:
    sleeps: list[float] = []
    opener = FakeOpener((503, {"message": "busy"}), (200, load_assist("default")))
    assert http_client(opener, sleeps).size_assist(REQUEST)["style_id"] == "DEMO-STYLE-HERO"
    assert len(opener.calls) == 2 and len(sleeps) == 1


@pytest.mark.parametrize("failure", [TimeoutError(), socket.timeout(), urllib.error.URLError(TimeoutError())])
def test_one_retry_on_a_timeout(failure: BaseException) -> None:
    opener = FakeOpener(failure, (200, load_assist("default")))
    assert http_client(opener).size_assist(REQUEST)["style_id"] == "DEMO-STYLE-HERO"
    assert len(opener.calls) == 2


def test_two_timeouts_end_as_a_retryable_upstream_timeout() -> None:
    opener = FakeOpener(TimeoutError(), TimeoutError())
    with pytest.raises(GatewayCallError) as caught:
        http_client(opener).size_assist(REQUEST)
    assert (caught.value.code, caught.value.retryable) == ("UPSTREAM_TIMEOUT", True)
    assert len(opener.calls) == 2


def test_two_server_errors_end_as_a_retryable_upstream_timeout() -> None:
    opener = FakeOpener((500, {"x": 1}), (502, b"<html>bad gateway</html>"))
    with pytest.raises(GatewayCallError) as caught:
        http_client(opener).size_assist(REQUEST)
    assert (caught.value.code, caught.value.retryable, caught.value.status) == ("UPSTREAM_TIMEOUT", True, 502)


def test_connection_failures_are_mapped_without_the_cause() -> None:
    opener = FakeOpener(urllib.error.URLError("secret host detail"))
    with pytest.raises(GatewayCallError) as caught:
        http_client(opener).size_assist(REQUEST)
    assert caught.value.code == "UPSTREAM_TIMEOUT" and "secret host detail" not in str(caught.value)
    assert len(opener.calls) == 1  # a refused connection is not a timeout: no retry


def test_a_4xx_answer_is_never_retried_and_keeps_the_gateway_code() -> None:
    opener = FakeOpener((422, error_body("INVALID_SIZE_CHOICE", "that size was not offered")))
    with pytest.raises(GatewayCallError) as caught:
        http_client(opener).size_decision({})
    assert (caught.value.code, caught.value.status, caught.value.retryable) == ("INVALID_SIZE_CHOICE", 422, False)
    assert caught.value.message == "that size was not offered"
    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    "code,status",
    [
        ("STOCK_CHANGED", 409),
        ("REQUEST_EXPIRED", 409),
        ("DELEGATION_LIMIT_EXCEEDED", 403),
        ("IDEMPOTENCY_CONFLICT", 409),
        ("NOT_AUTHORIZED", 403),
        ("NOT_AUTHENTICATED", 401),
        ("VALIDATION_ERROR", 422),
    ],
)
def test_gateway_error_codes_map_to_domain_error_codes(code: str, status: int) -> None:
    with pytest.raises(DomainError) as caught:
        http_client(FakeOpener((status, error_body(code)))).size_decision({})
    assert caught.value.code == code


def test_unknown_style_keeps_its_candidates() -> None:
    candidates = [{"style_id": "DEMO-STYLE-HERO", "product_name": "Stessy2.0", "colors": ["BLACK"]}]
    opener = FakeOpener((422, error_body("UNKNOWN_STYLE", "no match", candidates=candidates)))
    with pytest.raises(GatewayCallError) as caught:
        http_client(opener).size_assist(REQUEST)
    assert caught.value.code == "UNKNOWN_STYLE" and caught.value.candidates == candidates


@pytest.mark.parametrize(
    "status,expected",
    [(401, "NOT_AUTHENTICATED"), (403, "NOT_AUTHORIZED"), (404, "NOT_FOUND"), (422, "VALIDATION_ERROR"), (429, "UPSTREAM_TIMEOUT")],
)
def test_statuses_without_a_usable_body_map_to_a_code(status: int, expected: str) -> None:
    with pytest.raises(GatewayCallError) as caught:
        http_client(FakeOpener((status, b"not json"))).size_assist(REQUEST)
    assert caught.value.code == expected


def test_an_error_body_with_a_malformed_code_is_not_trusted() -> None:
    with pytest.raises(GatewayCallError) as caught:
        http_client(FakeOpener((418, error_body("lowercase code!")))).size_assist(REQUEST)
    assert caught.value.code == "INTERNAL_ERROR"


def test_a_redirect_is_an_error_and_is_never_followed() -> None:
    opener = FakeOpener((302, b""))
    with pytest.raises(GatewayCallError) as caught:
        http_client(opener).size_assist(REQUEST)
    assert caught.value.code == "INTERNAL_ERROR" and len(opener.calls) == 1
    handlers = gateway_client._DEFAULT_OPENER.handlers
    assert any(isinstance(h, gateway_client._NoRedirects) for h in handlers)
    assert not any(type(h).__name__ == "ProxyHandler" and h.proxies for h in handlers)


def test_a_non_object_or_oversized_success_body_is_rejected() -> None:
    with pytest.raises(GatewayCallError):
        http_client(FakeOpener((200, b"[1, 2]"))).size_assist(REQUEST)
    with pytest.raises(GatewayCallError):
        http_client(FakeOpener((200, b"x" * (gateway_client.MAX_RESPONSE_BYTES + 5)))).size_assist(REQUEST)


# ------------------------------------------------------------------------- base URL handling


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "ftp://example.com", "file:///etc/passwd", "https://", "example.com", "", None],
)
def test_unsafe_or_missing_base_urls_are_rejected(url: Any) -> None:
    with pytest.raises(DomainError):
        gateway_client.checked_base_url(url)


@pytest.mark.parametrize("url", ["https://d111.cloudfront.net/", "http://localhost:8080", "http://127.0.0.1:3001"])
def test_https_and_loopback_http_base_urls_are_accepted(url: str) -> None:
    assert gateway_client.checked_base_url(url) == url.rstrip("/")


def test_a_base_url_ending_in_api_is_not_doubled() -> None:
    opener = FakeOpener((200, load_assist("default")))
    HttpGatewayClient("http://localhost:3001/api", DEMO_TOKEN, opener=opener).size_assist(REQUEST)
    assert opener.calls[0][0].full_url == "http://localhost:3001/api/gateway/size-assist"


class FakeSsm:
    def __init__(self, value: str) -> None:
        self.value = value
        self.names: list[str] = []

    def get_parameter(self, Name: str) -> dict[str, Any]:  # noqa: N803 (boto3 casing)
        self.names.append(Name)
        return {"Parameter": {"Value": self.value}}


def test_base_url_comes_from_env_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://env.example.net/")
    ssm = FakeSsm("https://ssm.example.net")
    assert gateway_client.resolve_base_url(ssm) == "https://env.example.net"
    assert ssm.names == []


def test_base_url_falls_back_to_ssm_once_and_is_cached() -> None:
    ssm = FakeSsm("https://ssm.example.net/")
    assert gateway_client.resolve_base_url(ssm) == "https://ssm.example.net"
    assert gateway_client.resolve_base_url(ssm) == "https://ssm.example.net"
    assert ssm.names == ["/aldo-demo/gateway-url"]


def test_the_http_client_resolves_the_url_lazily_through_ssm() -> None:
    ssm = FakeSsm("https://ssm.example.net")
    opener = FakeOpener((200, load_assist("default")))
    HttpGatewayClient(None, DEMO_TOKEN, opener=opener, ssm_client=ssm).size_assist(REQUEST)
    assert opener.calls[0][0].full_url == "https://ssm.example.net/api/gateway/size-assist"


def test_a_missing_ssm_parameter_is_a_clear_configuration_error() -> None:
    class Broken:
        def get_parameter(self, Name: str) -> None:  # noqa: N803
            raise RuntimeError("ParameterNotFound for arn:aws:ssm:...")

    with pytest.raises(GatewayCallError) as caught:
        gateway_client.resolve_base_url(Broken())
    assert caught.value.code == "INTERNAL_ERROR" and "arn:aws" not in str(caught.value)


def test_an_unsafe_ssm_value_is_rejected_and_not_cached() -> None:
    with pytest.raises(DomainError):
        gateway_client.resolve_base_url(FakeSsm("http://evil.example.com"))
    assert gateway_client.resolve_base_url(FakeSsm("https://good.example.net")) == "https://good.example.net"


# ----------------------------------------------------------------------------------- local


def delegated_token(customer_id: str = AVA, **kwargs: Any) -> str:
    return auth.sign_token(customer_id, scope="delegated_gateway", actor="personal_agent", **kwargs).token


def test_local_verifies_the_delegated_token_and_passes_claims_to_the_gateway() -> None:
    backend = CannedGateway("default")
    client = LocalGatewayClient(delegated_token(max_amount_cents=25000), backend=backend)
    answer = client.size_assist(REQUEST)
    assert answer["fit_signal"]["signal"] == "runs_small"
    claims = backend.claims_seen[0]
    assert (claims.customer_id, claims.scope, claims.actor, claims.max_amount_cents) == (
        AVA, "delegated_gateway", "personal_agent", 25000,
    )


def test_local_decision_is_a_proposal_that_creates_no_order() -> None:
    backend = CannedGateway("default")
    client = LocalGatewayClient(delegated_token(), backend=backend)
    proposal = client.size_decision(
        {
            "size_request_id": "SZR-3F9A1C",
            "option_id": "SO-10",
            "chosen_size": "8.0",
            "rationale_public": "Runs small per ALDO fit signal",
            "confidence": "high",
        }
    )
    assert (proposal["status"], proposal["awaiting"]) == ("proposed", "owner_confirmation")
    assert "order_id" not in proposal and backend.proposals[0]["option_id"] == "SO-10"


def test_local_rejects_a_session_token() -> None:
    session = auth.sign_token(AVA).token
    with pytest.raises(GatewayCallError) as caught:
        LocalGatewayClient(session, backend=CannedGateway()).size_assist(REQUEST)
    assert caught.value.code == "NOT_AUTHORIZED"


def test_local_rejects_a_forged_token() -> None:
    with pytest.raises(GatewayCallError) as caught:
        LocalGatewayClient("not.a-token", backend=CannedGateway()).size_assist(REQUEST)
    assert caught.value.code == "NOT_AUTHENTICATED"


def test_local_maps_domain_errors_and_keeps_unknown_style_candidates() -> None:
    client = LocalGatewayClient(delegated_token(), backend=CannedGateway("default"))
    with pytest.raises(GatewayCallError) as caught:
        client.size_assist({**REQUEST, "product_query": "Nonexistent"})
    assert caught.value.code == "UNKNOWN_STYLE"
    assert caught.value.candidates == [
        {"style_id": "DEMO-STYLE-HERO", "product_name": "Stessy2.0", "colors": ["BLACK"]}
    ]


def test_local_forbids_extra_request_fields_like_the_real_gateway() -> None:
    client = LocalGatewayClient(delegated_token(), backend=CannedGateway())
    with pytest.raises(GatewayCallError) as caught:
        client.size_assist({**REQUEST, "owner_fit_hint": "7.5"})
    assert caught.value.code == "VALIDATION_ERROR"


def test_local_hides_unexpected_exceptions() -> None:
    class Exploding:
        def size_assist(self, claims: Any, request: Any) -> None:
            raise RuntimeError("table aldo-returns-demo exploded: customer DEMO-CUST-AVA")

    with pytest.raises(GatewayCallError) as caught:
        LocalGatewayClient(delegated_token(), backend=Exploding()).size_assist(REQUEST)
    assert caught.value.code == "INTERNAL_ERROR" and "exploded" not in str(caught.value)


def test_local_accepts_pydantic_style_results() -> None:
    class Model:
        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {"size_request_id": "SZR-AAAAAA"}

    class Backend:
        def size_assist(self, claims: Any, request: Any) -> Model:
            return Model()

    assert LocalGatewayClient(delegated_token(), backend=Backend()).size_assist(REQUEST) == {
        "size_request_id": "SZR-AAAAAA"
    }


def test_local_lazy_imports_the_real_gateway_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import returns_domain

    fake = types.ModuleType("returns_domain.gateway")
    fake.size_assist = lambda claims, request: {"size_request_id": "SZR-LAZY01", "caller": claims.customer_id}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "returns_domain.gateway", fake)
    monkeypatch.setattr(returns_domain, "gateway", fake, raising=False)
    answer = LocalGatewayClient(delegated_token()).size_assist(REQUEST)
    assert answer == {"size_request_id": "SZR-LAZY01", "caller": AVA}


# --------------------------------------------------------------------------------- factory


def test_a2a_transport_is_not_implemented_yet() -> None:
    with pytest.raises(NotImplementedError):
        build_gateway_client(DEMO_TOKEN, transport="a2a")


def test_factory_builds_each_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(build_gateway_client(DEMO_TOKEN, transport="local", backend=CannedGateway()), LocalGatewayClient)
    assert isinstance(build_gateway_client(DEMO_TOKEN, transport="http", base_url=BASE), HttpGatewayClient)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "http")
    monkeypatch.setenv("GATEWAY_BASE_URL", BASE)
    assert isinstance(build_gateway_client(DEMO_TOKEN), HttpGatewayClient)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    assert isinstance(build_gateway_client(DEMO_TOKEN), LocalGatewayClient)


def test_factory_rejects_an_unknown_transport() -> None:
    with pytest.raises(DomainError):
        build_gateway_client(DEMO_TOKEN, transport="carrier-pigeon")
