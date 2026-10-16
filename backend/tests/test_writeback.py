"""ALDO -> ALDO bot write-back: signature, transports, outbox flush, memory_updated (F.5, F.12).

Unit tests use a local HTTP receiver and fakes; the integration tests use the real store,
trace, outbox and exchange flow on moto (fixtures ``ddb`` / ``seeded`` from ``conftest.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from returns_domain import (
    auth,
    clock,
    config,
    conversations,
    execution,
    gateway,
    memory_updates,
    options,
    orders,
    quotes,
    runs,
    store,
    trace,
)
from returns_domain import personal_agent_client as pac
from returns_domain.errors import DomainError, ErrorCode

AVA, BEN = "DEMO-CUST-AVA", "DEMO-CUST-BEN"
KEY = b"k" * 32


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic configuration for the tests that do not need a table."""
    for name in (
        "DEMO_WRITEBACK_KEY",
        "SECRETS_ARN",
        "GATEWAY_TRANSPORT",
        "GATEWAY_BASE_URL",
        "DEMO_NOW",
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEMO_TOKEN_KEY", "test-only-token-key-0123456789")
    config.clear_secret_cache()
    pac.clear_cache()
    pac.register_local_handler(None)
    yield
    pac.register_local_handler(None)
    pac.clear_cache()
    config.clear_secret_cache()


def update_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "update_id": "MU-5C0A11",
        "owner_ref": AVA,
        "order_id": "DEMO-A2A-81B20E",
        "style_id": "DEMO-STYLE-HERO",
        "product_name": "Stessy2.0",
        "color": "BLACK",
        "original_size": "8.0",
        "new_size": "8.5",
        "reason": "TOO_SMALL",
        "outcome": "exchange_created",
        "provisional": True,
        "source": "ALDO_CA",
        "ts": "2026-10-16T16:05:00+00:00",
    }
    body.update(overrides)
    return body


# ================================================================ signature


def test_sign_body_is_lowercase_hex_hmac_sha256(env):
    body = b'{"a":1}'
    expected = hmac.new(KEY, body, hashlib.sha256).hexdigest()
    assert pac.sign_body(body, KEY) == expected
    assert expected == expected.lower() and len(expected) == 64
    # a fixed vector, so a change of algorithm or encoding is caught
    assert pac.sign_body(b"abc", b"k" * 16) == hmac.new(b"k" * 16, b"abc", hashlib.sha256).hexdigest()


def test_signature_depends_on_every_byte_of_the_body_and_the_key(env):
    base = pac.sign_body(b'{"a":1}', KEY)
    assert pac.sign_body(b'{"a":2}', KEY) != base
    assert pac.sign_body(b'{"a":1} ', KEY) != base
    assert pac.sign_body(b'{"a":1}', b"j" * 32) != base


def test_verify_signature_accepts_the_right_header_and_rejects_everything_else(env):
    body = b'{"update_id":"MU-1"}'
    good = pac.sign_body(body, KEY)
    pac.verify_signature(body, good, KEY)
    pac.verify_signature(body, good.upper(), KEY)  # hex case does not matter
    pac.verify_signature(body, f"  {good} ", KEY)
    for bad in (None, "", "   ", "0" * 64, good[:-1], good + "0", "sha256=" + good):
        with pytest.raises(DomainError) as exc:
            pac.verify_signature(body, bad, KEY)
        assert exc.value.code == ErrorCode.NOT_AUTHENTICATED
    with pytest.raises(DomainError) as exc:
        pac.verify_signature(body + b" ", good, KEY)
    assert exc.value.code == ErrorCode.NOT_AUTHENTICATED
    with pytest.raises(DomainError):
        pac.verify_signature(body, good, b"j" * 32)


def test_sign_body_refuses_a_weak_key_and_a_non_bytes_body(env):
    for key in (b"", b"short", "a string key", None):
        with pytest.raises(DomainError) as exc:
            pac.sign_body(b"x", key)  # type: ignore[arg-type]
        assert exc.value.code == ErrorCode.INTERNAL_ERROR
    with pytest.raises(DomainError) as exc:
        pac.sign_body("text", KEY)  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_the_client_signature_is_the_one_auth_verifies(env):
    body = b'{"update_id":"MU-1"}'
    key = config.get_writeback_key()
    assert pac.sign_body(body, key) == auth.sign_writeback(body)
    auth.verify_writeback_signature(body, pac.sign_body(body, key))
    pac.verify_signature(body, auth.sign_writeback(body), key)


def test_the_writeback_key_is_derived_from_the_token_key_not_a_new_secret(env, monkeypatch):
    first = config.get_writeback_key()
    assert len(first) == 32
    monkeypatch.setenv("DEMO_TOKEN_KEY", "another-token-key-0123456789")
    config.clear_secret_cache()
    assert config.get_writeback_key() != first


# ============================================================ local transport


def test_local_client_calls_the_injected_handler(env):
    seen: list[Any] = []

    def handler(update):
        seen.append(dict(update))
        return {"update_id": update["update_id"], "applied": True, "duplicate": False}

    client = pac.LocalPersonalAgentClient(handler)
    assert client.send_memory_update(update_body()) == {
        "update_id": "MU-5C0A11",
        "applied": True,
        "duplicate": False,
    }
    assert seen == [update_body()]


def test_local_client_refuses_odd_handlers_and_answers(env):
    with pytest.raises(DomainError) as exc:
        pac.LocalPersonalAgentClient("not callable")  # type: ignore[arg-type]
    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    with pytest.raises(DomainError) as exc:
        pac.LocalPersonalAgentClient(lambda update: "applied").send_memory_update(update_body())
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


def test_local_client_lets_the_handlers_errors_through(env):
    def handler(update):
        raise DomainError(ErrorCode.NOT_FOUND, "unknown owner")

    with pytest.raises(DomainError) as exc:
        pac.LocalPersonalAgentClient(handler).send_memory_update(update_body())
    assert exc.value.code == ErrorCode.NOT_FOUND


# ======================================================================= factory


def test_get_client_picks_the_transport(env, monkeypatch):
    with pytest.raises(DomainError) as exc:  # local, nothing registered
        pac.get_client("local")
    assert exc.value.code == ErrorCode.INTERNAL_ERROR

    handler = lambda update: {"applied": True}  # noqa: E731
    pac.register_local_handler(handler)
    assert isinstance(pac.get_client("local"), pac.LocalPersonalAgentClient)
    assert isinstance(pac.get_client("LOCAL "), pac.LocalPersonalAgentClient)
    pac.register_local_handler(None)

    monkeypatch.setenv("GATEWAY_BASE_URL", "https://example.cloudfront.net")
    for name in ("http", "a2a"):  # a2a changes the purchase leg only: write-back stays the push
        client = pac.get_client(name)
        assert isinstance(client, pac.HttpPersonalAgentClient)
        assert client.url == "https://example.cloudfront.net/api/personal-agent/memory-updates"

    with pytest.raises(DomainError) as exc:
        pac.get_client("smoke-signals")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert pac.get_personal_agent_client is pac.get_client


def test_get_client_defaults_to_the_configured_transport(env, monkeypatch):
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    pac.register_local_handler(lambda update: {"applied": True})
    assert isinstance(pac.get_client(), pac.LocalPersonalAgentClient)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "http")
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://example.cloudfront.net/")
    assert pac.get_client().url == "https://example.cloudfront.net/api/personal-agent/memory-updates"


def test_get_client_takes_explicit_arguments(env):
    client = pac.get_client("http", base_url="http://127.0.0.1:9", key=KEY)
    assert client.url == "http://127.0.0.1:9/api/personal-agent/memory-updates"
    handler = lambda update: {"applied": False}  # noqa: E731
    assert pac.get_client("local", handler=handler).send_memory_update(update_body()) == {"applied": False}


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "ftp://example.com", "example.com", "", "https://", "javascript:alert(1)", None, 5],
)
def test_the_base_url_must_be_https_or_loopback(env, url):
    with pytest.raises(DomainError) as exc:
        pac.checked_base_url(url)
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    "url", ["https://example.com", "https://example.com/", "http://localhost:8080", "http://127.0.0.1:1/", "http://[::1]:9"]
)
def test_the_base_url_may_be_https_or_loopback(env, url):
    assert pac.checked_base_url(url) == url.rstrip("/")


def test_the_base_url_comes_from_the_environment_first_then_ssm_cached(env, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_ssm(name: str, region: str) -> str:
        calls.append((name, region))
        return "https://from-ssm.cloudfront.net/"

    monkeypatch.setattr(pac, "_read_ssm_parameter", fake_ssm)
    monkeypatch.setenv("GATEWAY_BASE_URL", "https://from-env.cloudfront.net")
    assert pac.resolve_base_url() == "https://from-env.cloudfront.net"
    assert calls == []

    monkeypatch.delenv("GATEWAY_BASE_URL")
    assert pac.resolve_base_url() == "https://from-ssm.cloudfront.net"
    assert pac.resolve_base_url() == "https://from-ssm.cloudfront.net"
    assert calls == [("/aldo-demo/gateway-url", "us-east-1")]  # asked once, then cached

    pac.clear_cache()
    pac.resolve_base_url()
    assert len(calls) == 2


def test_an_unreadable_or_unsafe_ssm_value_is_a_configuration_error_without_details(env, monkeypatch):
    def broken(name: str, region: str) -> str:
        raise RuntimeError("AccessDenied for arn:aws:ssm:us-east-1:123456789012:parameter/secret-ish")

    monkeypatch.setattr(pac, "_read_ssm_parameter", broken)
    with pytest.raises(DomainError) as exc:
        pac.resolve_base_url()
    assert exc.value.code == ErrorCode.INTERNAL_ERROR
    assert "123456789012" not in exc.value.message and "AccessDenied" not in exc.value.message

    monkeypatch.setattr(pac, "_read_ssm_parameter", lambda name, region: "http://not-loopback.example")
    with pytest.raises(DomainError) as exc:
        pac.resolve_base_url()
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


# ================================================================ http transport


class Receiver:
    """A scripted loopback HTTP server that records every request."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        self.replies: list[tuple[int, bytes, dict[str, str], float]] = []
        self.base_url = ""

    def reply(
        self, status: int = 200, body: bytes | dict | None = None, headers: dict[str, str] | None = None, delay: float = 0
    ) -> None:
        payload = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
        self.replies.append((status, payload, headers or {}, delay))

    def next(self) -> tuple[int, bytes, dict[str, str], float]:
        if self.replies:
            return self.replies.pop(0)
        return 200, b'{"update_id":"MU-5C0A11","applied":true,"duplicate":false}', {}, 0


@pytest.fixture
def receiver(env) -> Iterator[Receiver]:
    script = Receiver()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(size)
            script.seen.append(
                {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
            )
            status, payload, headers, delay = script.next()
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass  # the client gave up (timeout test)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield script
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_client_posts_the_signed_canonical_body(receiver):
    client = pac.HttpPersonalAgentClient(receiver.base_url, config.get_writeback_key())
    update = update_body()
    answer = client.send_memory_update(update)
    assert answer == {"update_id": "MU-5C0A11", "applied": True, "duplicate": False}

    (request,) = receiver.seen
    assert request["path"] == "/api/personal-agent/memory-updates"
    assert request["headers"]["content-type"] == "application/json"
    expected_body = json.dumps(update, sort_keys=True, separators=(",", ":")).encode()
    assert request["body"] == expected_body
    expected_signature = hmac.new(config.get_writeback_key(), expected_body, hashlib.sha256).hexdigest()
    assert request["headers"]["x-aldo-signature"] == expected_signature
    # the receiving side verifies the RAW body it got with the same derived key
    pac.verify_signature(request["body"], request["headers"]["x-aldo-signature"], config.get_writeback_key())
    auth.verify_writeback_signature(request["body"], request["headers"]["x-aldo-signature"])
    # no bearer token and no cookie ever travels with the write-back
    assert "authorization" not in request["headers"] and "cookie" not in request["headers"]


def test_http_client_makes_exactly_one_attempt(receiver):
    receiver.reply(503)
    client = pac.HttpPersonalAgentClient(receiver.base_url, KEY)
    with pytest.raises(DomainError) as exc:
        client.send_memory_update(update_body())
    assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT and exc.value.retryable
    assert len(receiver.seen) == 1  # retries belong to the outbox attempts counter


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (500, ErrorCode.UPSTREAM_TIMEOUT, True),
        (502, ErrorCode.UPSTREAM_TIMEOUT, True),
        (504, ErrorCode.UPSTREAM_TIMEOUT, True),
        (408, ErrorCode.UPSTREAM_TIMEOUT, True),
        (429, ErrorCode.UPSTREAM_TIMEOUT, True),
        (401, ErrorCode.NOT_AUTHENTICATED, False),
        (403, ErrorCode.NOT_AUTHENTICATED, False),
        (400, ErrorCode.VALIDATION_ERROR, False),
        (404, ErrorCode.VALIDATION_ERROR, False),
        (422, ErrorCode.VALIDATION_ERROR, False),
    ],
)
def test_http_client_maps_the_receivers_answer(receiver, status, code, retryable):
    receiver.reply(status, {"error": {"code": "WHATEVER", "message": "x" * 50}})
    client = pac.HttpPersonalAgentClient(receiver.base_url, KEY)
    with pytest.raises(DomainError) as exc:
        client.send_memory_update(update_body())
    assert (exc.value.code, exc.value.retryable) == (code, retryable)
    assert exc.value.details == {"status": status}
    assert "x" * 50 not in exc.value.message  # the receiver's text is never echoed


def test_http_client_does_not_follow_redirects(receiver):
    receiver.reply(302, headers={"Location": f"{receiver.base_url}/elsewhere"})
    client = pac.HttpPersonalAgentClient(receiver.base_url, KEY)
    with pytest.raises(DomainError) as exc:
        client.send_memory_update(update_body())
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert [r["path"] for r in receiver.seen] == ["/api/personal-agent/memory-updates"]  # no second request


def test_http_client_times_out_and_reports_a_retryable_error(receiver):
    receiver.reply(200, {}, delay=1.5)
    client = pac.HttpPersonalAgentClient(receiver.base_url, KEY, timeout_s=0.3)
    started = time.monotonic()
    with pytest.raises(DomainError) as exc:
        client.send_memory_update(update_body())
    assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT and exc.value.retryable
    assert time.monotonic() - started < 1.2


def test_http_client_default_timeout_is_five_seconds():
    assert pac.DEFAULT_TIMEOUT_SECONDS == 5.0
    assert pac.HttpPersonalAgentClient("https://example.com", KEY)._timeout == 5.0


def test_http_client_reports_a_closed_port_as_retryable_without_details(env):
    client = pac.HttpPersonalAgentClient("http://127.0.0.1:9", KEY, timeout_s=1)
    with pytest.raises(DomainError) as exc:
        client.send_memory_update(update_body())
    assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT and exc.value.retryable
    assert "127.0.0.1" not in exc.value.message


def test_http_client_ignores_environment_proxies(receiver, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    client = pac.HttpPersonalAgentClient(receiver.base_url, KEY)
    assert client.send_memory_update(update_body())["applied"] is True
    assert len(receiver.seen) == 1


@pytest.mark.parametrize("body", [b"", b"   ", b"not json", b"[1, 2]", b"null"])
def test_a_2xx_with_an_odd_body_still_counts_as_delivered(receiver, body):
    receiver.reply(200, body)
    assert pac.HttpPersonalAgentClient(receiver.base_url, KEY).send_memory_update(update_body()) == {}


def test_http_client_refuses_a_body_that_is_not_json(receiver):
    client = pac.HttpPersonalAgentClient(receiver.base_url, KEY)
    with pytest.raises(DomainError) as exc:
        client.send_memory_update({"update_id": "MU-1", "bad": {1, 2}})
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert receiver.seen == []


def test_http_client_repr_hides_the_key():
    assert "kkkk" not in repr(pac.HttpPersonalAgentClient("https://example.com", KEY))


def test_http_client_validates_its_arguments(env):
    with pytest.raises(DomainError):
        pac.HttpPersonalAgentClient("http://public.example", KEY)
    with pytest.raises(DomainError):
        pac.HttpPersonalAgentClient("https://example.com", b"short")


def test_the_client_module_never_imports_the_personal_agent():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pac))
    imported = {
        alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert "personal_agent" not in imported


# =============================================================== flush (fakes)


class FakeUpdates:
    """A stand-in for ``memory_updates`` that records what the gateway asks of it."""

    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = items or []
        self.calls: list[tuple[Any, ...]] = []
        self.delivered_result: dict[str, Any] = {}
        self.list_error: Exception | None = None
        self.delivered_error: Exception | None = None
        self.failed_error: Exception | None = None

    def list_pending(self, customer_id=None, *, limit=10):
        self.calls.append(("list", customer_id, limit))
        if self.list_error:
            raise self.list_error
        return list(self.items)

    def mark_delivered(self, update_id):
        self.calls.append(("delivered", update_id))
        if self.delivered_error:
            raise self.delivered_error
        return self.delivered_result.get(update_id, True)

    def mark_failed(self, update_id, error):
        self.calls.append(("failed", update_id, error))
        if self.failed_error:
            raise self.failed_error


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any], str]] = []
        self.error: Exception | None = None

    def emit_a2a_safe(self, customer_id, event_type, payload, *, actor):
        if self.error:
            raise self.error
        self.events.append((customer_id, event_type, dict(payload), actor))


class FakeClient:
    def __init__(self, *outcomes: Any) -> None:
        self.sent: list[dict[str, Any]] = []
        self.outcomes = list(outcomes)

    def send_memory_update(self, update):
        self.sent.append(dict(update))
        outcome = self.outcomes.pop(0) if self.outcomes else {"applied": True, "duplicate": False}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def outbox_item(update_id: str, *, key: str = "payload", customer: str = AVA, **overrides: Any) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "customer_id": customer,
        key: update_body(update_id=update_id, **overrides),
        "status": "pending",
        "attempts": 0,
    }


@pytest.fixture
def fakes(monkeypatch):
    updates, events = FakeUpdates(), FakeTrace()
    monkeypatch.setattr(gateway, "memory_updates", updates)
    monkeypatch.setattr(gateway, "trace", events)
    return SimpleNamespace(updates=updates, trace=events)


def test_flush_sends_each_pending_update_and_emits_memory_updated_once(fakes):
    fakes.updates.items = [outbox_item("MU-000001"), outbox_item("MU-000002", outcome="exchange_completed_kept", provisional=False)]
    client = FakeClient({"applied": True, "duplicate": False}, {"applied": False, "duplicate": True})
    assert gateway.flush_outbox(AVA, limit=5, client=client) == 2
    assert [u["update_id"] for u in client.sent] == ["MU-000001", "MU-000002"]
    assert fakes.updates.calls[0] == ("list", AVA, 5)
    assert [c for c in fakes.updates.calls if c[0] == "delivered"] == [("delivered", "MU-000001"), ("delivered", "MU-000002")]
    # the event appears whether the receiver said applied or duplicate: it follows the delivery win
    assert [(e[0], e[1], e[3]) for e in fakes.trace.events] == [
        (AVA, "memory_updated", "personal_agent"),
        (AVA, "memory_updated", "personal_agent"),
    ]
    allowed = trace.A2A_PUBLIC_FIELDS["memory_updated"]
    for _, _, payload, _ in fakes.trace.events:
        assert set(payload) <= allowed
        assert "owner_ref" not in payload and AVA not in json.dumps(payload)
    assert fakes.trace.events[0][2]["provisional"] is True and fakes.trace.events[1][2]["provisional"] is False


def test_flush_reads_the_older_update_key_too(fakes):
    fakes.updates.items = [outbox_item("MU-000001", key="update")]
    assert gateway.flush_outbox(AVA, client=FakeClient()) == 1


def test_flush_emits_nothing_when_another_caller_won_the_delivery(fakes):
    fakes.updates.items = [outbox_item("MU-000001")]
    fakes.updates.delivered_result["MU-000001"] = False
    assert gateway.flush_outbox(AVA, client=FakeClient()) == 1  # it was sent
    assert fakes.trace.events == []  # the winner of pending -> delivered owns the event


def test_flush_records_a_failure_and_keeps_going(fakes):
    fakes.updates.items = [outbox_item("MU-000001"), outbox_item("MU-000002")]
    client = FakeClient(DomainError(ErrorCode.UPSTREAM_TIMEOUT, "the personal agent could not be reached"))
    assert gateway.flush_outbox(AVA, client=client) == 1
    failed = [c for c in fakes.updates.calls if c[0] == "failed"]
    assert len(failed) == 1 and failed[0][1] == "MU-000001"
    assert failed[0][2].startswith("DomainError: ") and len(failed[0][2]) <= 200
    assert [c for c in fakes.updates.calls if c[0] == "delivered"] == [("delivered", "MU-000002")]
    assert len(fakes.trace.events) == 1


def test_flush_error_text_is_short(fakes):
    fakes.updates.items = [outbox_item("MU-000001")]
    gateway.flush_outbox(AVA, client=FakeClient(RuntimeError("x" * 5000)))
    (failed,) = [c for c in fakes.updates.calls if c[0] == "failed"]
    assert len(failed[2]) < 200


@pytest.mark.parametrize(
    "break_it",
    [
        lambda f: setattr(f.updates, "list_error", RuntimeError("index down")),
        lambda f: setattr(f.updates, "delivered_error", RuntimeError("table down")),
        lambda f: setattr(f.updates, "failed_error", RuntimeError("table down")),
        lambda f: setattr(f.trace, "error", RuntimeError("trace down")),
        lambda f: f.updates.items.append({"update_id": "MU-BAD", "customer_id": AVA}),  # no payload
        lambda f: f.updates.items.append("not even a mapping"),
    ],
)
def test_flush_never_raises(fakes, break_it):
    fakes.updates.items = [outbox_item("MU-000001")]
    break_it(fakes)
    client = FakeClient(RuntimeError("receiver down"), {"applied": True}, {"applied": True})
    assert isinstance(gateway.flush_outbox(AVA, client=client), int)


def test_flush_leaves_items_alone_when_the_transport_is_not_configured(fakes, monkeypatch):
    fakes.updates.items = [outbox_item("MU-000001")]

    def not_configured():
        raise DomainError(ErrorCode.INTERNAL_ERROR, "no local personal agent handler is registered")

    monkeypatch.setattr(gateway, "get_client", not_configured)
    assert gateway.flush_outbox(AVA) == 0
    assert [c[0] for c in fakes.updates.calls] == []  # nothing listed, no attempt burned


def test_flush_uses_the_configured_transport_by_default(fakes, monkeypatch):
    fakes.updates.items = [outbox_item("MU-000001")]
    client = FakeClient()
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    assert gateway.flush_outbox() == 1
    assert fakes.updates.calls[0] == ("list", None, 10)  # every owner, default batch
    assert len(client.sent) == 1


@pytest.mark.parametrize(("limit", "expected"), [(0, 1), (-5, 1), (3, 3), (1000, 50), ("7", 7)])
def test_flush_clamps_its_batch_size(fakes, limit, expected):
    gateway.flush_outbox(AVA, limit=limit, client=FakeClient())
    assert fakes.updates.calls[0] == ("list", AVA, expected)


def test_flush_survives_a_nonsense_limit(fakes):
    assert gateway.flush_outbox(AVA, limit="many", client=FakeClient()) == 0  # type: ignore[arg-type]


# ======================================================== the update body (5.4, C2, C3)


def txn_item(**overrides: Any) -> dict[str, Any]:
    """The snapshot ``execution.execute`` keeps on a stored exchange ``TXN#`` item."""
    item = {
        "transaction_id": "DEMO-RMA-3F9A1C",
        "customer_id": AVA,
        "option_type": "exchange_store_pickup",
        "order_id": "DEMO-A2A-81B20E",
        "line_id": "DEMO-A2A-81B20E-L1",
        "style": "DEMO-STYLE-HERO",
        "product_name": "Stessy2.0",
        "color": "BLACK",
        "original_size": "8.0",
        "replacement_size": "8.5",
        "memory_reason": "TOO_SMALL",
    }
    item.update(overrides)
    return item


def test_the_update_for_a_created_exchange_is_provisional_and_has_the_5_4_body(env):
    update = gateway.build_memory_update(txn_item())
    assert update is not None
    assert update["update_id"] == store.derived_id("MU-", "DEMO-RMA-3F9A1C", "exchange_created", n=6)
    assert {k: v for k, v in update.items() if k != "ts"} == {
        "update_id": update["update_id"],
        "owner_ref": AVA,
        "order_id": "DEMO-A2A-81B20E",
        "style_id": "DEMO-STYLE-HERO",
        "product_name": "Stessy2.0",
        "color": "BLACK",
        "original_size": "8.0",
        "new_size": "8.5",
        "reason": "TOO_SMALL",
        "outcome": "exchange_created",
        "provisional": True,
        "source": "ALDO_CA",
    }
    assert isinstance(update["ts"], str) and clock.parse_iso(update["ts"])


def test_only_the_pickup_makes_a_final_update(env):
    created = gateway.build_memory_update(txn_item())
    kept = gateway.build_memory_update(txn_item(), outcome="exchange_completed_kept", provisional=False)
    assert (kept["outcome"], kept["provisional"]) == ("exchange_completed_kept", False)
    assert kept["update_id"] != created["update_id"]
    assert gateway.build_memory_update(txn_item(), outcome="exchange_completed_kept")["provisional"] is False
    for outcome, provisional in (("exchange_created", False), ("exchange_completed_kept", True)):
        with pytest.raises(DomainError) as exc:  # ALDO cannot know the owner keeps the pair at creation
            gateway.build_memory_update(txn_item(), outcome=outcome, provisional=provisional)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(DomainError):
        gateway.build_memory_update(txn_item(), outcome="exchange_kept_maybe")


def test_the_update_id_is_stable_for_a_transaction_and_outcome(env):
    first = gateway.build_memory_update(txn_item())
    again = gateway.build_memory_update(txn_item())
    other = gateway.build_memory_update(txn_item(transaction_id="DEMO-RMA-AAAAAA"))
    assert first["update_id"] == again["update_id"] != other["update_id"]


def test_the_update_never_carries_the_owners_profile_or_other_retailers(env):
    text = json.dumps(gateway.build_memory_update(txn_item()))
    for forbidden in ("typical", "owner_fit_hint", "retailer", "Retailer", "memory", "kept_after", "rationale"):
        assert forbidden not in text
    assert set(gateway.build_memory_update(txn_item())) == {
        "update_id", "owner_ref", "order_id", "style_id", "product_name", "color", "original_size",
        "new_size", "reason", "outcome", "provisional", "source", "ts"}


def test_a_return_has_no_update_and_a_bare_item_is_an_internal_error(env):
    assert gateway.build_memory_update(txn_item(option_type="return_in_store")) is None
    assert gateway.build_memory_update(txn_item(option_type="return_mail")) is None
    bare = {"transaction_id": "DEMO-RMA-3F9A1C", "customer_id": AVA, "option_type": "exchange_store_pickup"}
    with pytest.raises(DomainError) as exc:
        gateway.build_memory_update(bare)
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


# ===================================================== outbox, real tables (moto)


def local_pa() -> SimpleNamespace:
    """A stand-in personal agent: idempotent on update_id, like ``apply_memory_update``."""
    box = SimpleNamespace(received={}, sends=0, fail_with=None)

    def handler(update):
        box.sends += 1
        if box.fail_with is not None:
            raise box.fail_with
        duplicate = update["update_id"] in box.received
        box.received.setdefault(update["update_id"], dict(update))
        return {"update_id": update["update_id"], "applied": not duplicate, "duplicate": duplicate}

    box.handler = handler
    return box


@pytest.fixture
def pa(seeded, monkeypatch) -> Iterator[SimpleNamespace]:
    box = local_pa()
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    pac.register_local_handler(box.handler)
    yield box
    pac.register_local_handler(None)


def memory_events(customer: str = AVA) -> list[dict[str, Any]]:
    return [e for e in trace.list_a2a_events(customer).events if e["type"] == "memory_updated"]


def pending_update(**overrides: Any) -> dict[str, Any]:
    return update_body(**overrides)


def test_enqueue_is_idempotent(ddb):
    update = pending_update()
    assert gateway.enqueue_memory_update(update) is True
    assert gateway.enqueue_memory_update(update) is False
    item = store.get_item(store.outbox_key("MU-5C0A11"))
    assert (item["status"], item["attempts"], item["customer_id"]) == ("pending", 0, AVA)
    assert item["payload"] == update


def test_flush_delivers_through_the_real_outbox_and_emits_exactly_once(pa):
    gateway.enqueue_memory_update(pending_update())
    assert gateway.flush_outbox(AVA) == 1
    item = store.get_item(store.outbox_key("MU-5C0A11"))
    assert item["status"] == "delivered" and "gsi1pk" not in item
    assert pa.received["MU-5C0A11"]["owner_ref"] == AVA
    (event,) = memory_events()
    assert event["actor"] == "personal_agent"
    assert set(event["payload"]) <= trace.A2A_PUBLIC_FIELDS["memory_updated"]
    assert event["payload"]["provisional"] is True and "owner_ref" not in event["payload"]
    # nothing is left to do, and nothing is emitted again
    assert gateway.flush_outbox(AVA) == 0
    assert pa.sends == 1 and len(memory_events()) == 1


def test_memory_updated_comes_from_the_winner_of_the_delivery_only(pa):
    """Two flushers send the same update; the personal agent answers applied / duplicate."""
    gateway.enqueue_memory_update(pending_update())

    class Racing:
        """Delivers, then another flusher marks the item delivered before this one does."""

        def send_memory_update(self, update):
            answer = pa.handler(update)
            memory_updates.mark_delivered(update["update_id"])  # the other flusher wins
            return answer

    assert gateway.flush_outbox(AVA, client=Racing()) == 1
    assert memory_events() == []  # the loser emits nothing, and the winner here did not emit either
    # a flusher that does win emits exactly once, even when the receiver says duplicate
    gateway.enqueue_memory_update(pending_update(update_id="MU-5C0A12"))
    pa.received["MU-5C0A12"] = {}
    assert gateway.flush_outbox(AVA) == 1
    assert [e["payload"]["update_id"] for e in memory_events()] == ["MU-5C0A12"]


def test_a_failing_receiver_keeps_the_item_pending_and_counts_attempts(pa):
    gateway.enqueue_memory_update(pending_update())
    pa.fail_with = DomainError(ErrorCode.UPSTREAM_TIMEOUT, "the personal agent could not be reached")
    for attempt in range(1, 5):
        assert gateway.flush_outbox(AVA) == 0
        item = store.get_item(store.outbox_key("MU-5C0A11"))
        assert (item["status"], item["attempts"]) == ("pending", attempt)
    assert gateway.flush_outbox(AVA) == 0  # the fifth failure
    item = store.get_item(store.outbox_key("MU-5C0A11"))
    assert (item["status"], item["attempts"]) == ("failed", 5)
    assert "gsi1pk" not in item and item["last_error"].startswith("DomainError")
    assert memory_events() == []
    pa.fail_with = None
    assert gateway.flush_outbox(AVA) == 0  # a failed item is not retried again
    assert pa.sends == 5


def test_flush_only_touches_the_requested_owner(pa):
    gateway.enqueue_memory_update(pending_update())
    gateway.enqueue_memory_update(pending_update(update_id="MU-5C0A99", owner_ref=BEN))
    assert gateway.flush_outbox(BEN) == 1
    assert set(pa.received) == {"MU-5C0A99"}
    assert store.get_item(store.outbox_key("MU-5C0A11"))["status"] == "pending"
    assert gateway.flush_outbox() == 1  # no owner: everyone
    assert set(pa.received) == {"MU-5C0A99", "MU-5C0A11"}


# ============================== real exchange: purchase -> return -> write-back -> pickup

HERO_DEADLINE = "2026-10-17"


class World:
    """Builders that put a real exchange in the table, the way the agent's tools would."""

    def __init__(self, customer: str = AVA) -> None:
        self.customer = customer

    def claims(self, **kw: Any) -> auth.TokenClaims:
        token = auth.sign_token(self.customer, scope="delegated_gateway", actor="personal_agent", **kw)
        return auth.verify_claims(token.token, "delegated_gateway")

    def buy(self, size: str = "8.0", option_type: str = "store_pickup") -> str:
        """size_assist -> proposal -> owner confirm; returns the DEMO-A2A order id."""
        assisted = gateway.size_assist(
            self.claims(),
            {"intent": "size_assist", "product_query": "stessy", "deadline_date": HERO_DEADLINE},
        )
        option = next(
            o for s in assisted.sizes if s.size == size for o in s.options if o.type == option_type
        )
        gateway.decide_size(
            self.claims(),
            {
                "size_request_id": assisted.size_request_id,
                "option_id": option.option_id,
                "chosen_size": size,
                "rationale_public": "runs small per ALDO",
                "confidence": "high",
            },
        )
        return gateway.confirm_purchase(self.customer, assisted.size_request_id, option.option_id).order_id

    def quote(self, order_id: str, line_id: str, replacement: str | None, direction: str = "too_small"):
        conv_id = conversations.create_conversation(self.customer, order_id)
        run_id = store.new_id("RUN-", 10)
        assert runs.try_acquire_lease(conv_id, run_id)
        assert runs.create_run(self.customer, conv_id, run_id, "customer")
        check = orders.check_policy(self.customer, run_id, order_id, line_id, 1, "FIT_SIZE", "unworn", direction)
        found = options.find_options(
            self.customer, check.policy_check_id, replacement_size=replacement, deadline_date=HERO_DEADLINE, run_id=run_id
        )
        quote = quotes.create_quote(
            self.customer, run_id, conv_id, found.candidate_set_id, [o.option_id for o in found.options]
        )
        assert runs.claim_run(run_id) and runs.finish_run(run_id, "completed", {})
        return quote

    def execute(self, quote, option_type: str):
        chosen = next(o for o in quote.options if o.type == option_type)
        approval, _ = quotes.approve(self.customer, quote.quote_id, chosen.option_id, chosen.payload_hash, "RUN-APPROVAL01")
        return execution.execute(self.customer, quote.quote_id, run_id=None)


def test_the_outbox_item_is_written_atomically_with_the_exchange(pa):
    world = World()
    order_id = world.buy()
    gateway.simulate_delivery(AVA, order_id)
    result = world.execute(world.quote(order_id, f"{order_id}-L1", "8.5"), "exchange_store_pickup")
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert result.memory_update_id == txn["memory_update_id"]
    item = store.get_item(store.outbox_key(result.memory_update_id))
    assert item is not None  # the write-back exists because the exchange does
    update = item["payload"]
    assert (update["original_size"], update["new_size"], update["reason"]) == ("8.0", "8.5", "TOO_SMALL")
    assert (update["outcome"], update["provisional"], update["order_id"]) == ("exchange_created", True, order_id)
    assert update["owner_ref"] == AVA


def test_no_exchange_means_no_outbox_item(pa):
    world = World()
    order_id = world.buy()
    gateway.simulate_delivery(AVA, order_id)
    quote = world.quote(order_id, f"{order_id}-L1", "8.5")
    chosen = next(o for o in quote.options if o.type == "exchange_store_pickup")
    quotes.approve(AVA, quote.quote_id, chosen.option_id, chosen.payload_hash, "RUN-APPROVAL01")
    store.update_item(store.inventory_key(chosen.sku, chosen.location_id), set={"atp": 0})  # sold out meanwhile
    with pytest.raises(DomainError) as exc:
        execution.execute(AVA, quote.quote_id, run_id=None)
    assert exc.value.code == ErrorCode.STOCK_CHANGED
    assert store.query("A2AOUT#pending", index="GSI1") == []
    assert not [k for k in store.scan_keys(("A2AOUT#", "TXN#"))]


def test_a_return_creates_no_write_back(pa):
    world = World()
    order_id = world.buy()
    gateway.simulate_delivery(AVA, order_id)
    result = world.execute(world.quote(order_id, f"{order_id}-L1", "8.5"), "return_in_store")
    assert result.memory_update_id is None
    assert [k for k in store.scan_keys(("A2AOUT#",))] == []
    with pytest.raises(DomainError) as exc:  # and a return cannot be "picked up"
        gateway.simulate_pickup(AVA, result.transaction_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_a_delivery_failure_never_fails_the_exchange(pa):
    pa.fail_with = DomainError(ErrorCode.UPSTREAM_TIMEOUT, "the personal agent could not be reached")
    world = World()
    order_id = world.buy()
    gateway.simulate_delivery(AVA, order_id)
    result = world.execute(world.quote(order_id, f"{order_id}-L1", "8.5"), "exchange_store_pickup")
    assert result.state == "completed" and result.replayed is False
    assert store.get_item(store.txn_key(result.transaction_id))["state"] == "completed"
    item = store.get_item(store.outbox_key(result.memory_update_id))
    assert item["status"] == "pending" and item["attempts"] >= 1  # retried later by the worker flush
    assert memory_events() == []
    pa.fail_with = None
    assert gateway.flush_outbox(limit=10) == 1  # the worker's retry
    assert store.get_item(store.outbox_key(result.memory_update_id))["status"] == "delivered"
    assert len(memory_events()) == 1


def test_the_whole_story_purchase_return_writeback_and_pickup(pa):
    world = World()
    order_id = world.buy()
    gateway.simulate_delivery(AVA, order_id)

    result = world.execute(world.quote(order_id, f"{order_id}-L1", "8.5"), "exchange_store_pickup")
    # the exchange flushed the provisional update itself
    assert [u["outcome"] for u in pa.received.values()] == ["exchange_created"]
    provisional = pa.received[result.memory_update_id]
    assert provisional["provisional"] is True
    assert (provisional["original_size"], provisional["new_size"]) == ("8.0", "8.5")
    assert len(memory_events()) == 1

    first = gateway.simulate_pickup(AVA, result.transaction_id)
    assert first["already_done"] is False and first["outcome"] == "exchange_completed_kept"
    assert first["update_id"] != result.memory_update_id  # a second, final update
    final = pa.received[first["update_id"]]
    assert final["provisional"] is False and final["outcome"] == "exchange_completed_kept"
    assert (final["order_id"], final["original_size"], final["new_size"]) == (order_id, "8.0", "8.5")
    assert [e["payload"]["provisional"] for e in memory_events()] == [True, False]
    assert store.get_item(store.outbox_key(first["update_id"]))["status"] == "delivered"
    assert "pickup_confirmed_at" in store.get_item(store.txn_key(result.transaction_id))
    sends = pa.sends

    again = gateway.simulate_pickup(AVA, result.transaction_id)
    assert again["update_id"] == first["update_id"] and again["already_done"] is True
    assert pa.sends == sends and len(memory_events()) == 2  # nothing new is created or sent
    assert len([k for k in store.scan_keys(("A2AOUT#",))]) == 2


def test_simulate_pickup_checks_ownership_and_existence(pa):
    world = World()
    order_id = world.buy()
    gateway.simulate_delivery(AVA, order_id)
    result = world.execute(world.quote(order_id, f"{order_id}-L1", "8.5"), "exchange_store_pickup")
    with pytest.raises(DomainError) as exc:
        gateway.simulate_pickup(BEN, result.transaction_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    with pytest.raises(DomainError) as exc:
        gateway.simulate_pickup(AVA, "DEMO-RMA-NOPE00")
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert "pickup_confirmed_at" not in store.get_item(store.txn_key(result.transaction_id))
    assert [e for e in store.query(f"AUDIT#{clock.now_utc().date().isoformat()}") if e["action"] == "access_denied"]


def test_a_failed_flush_after_pickup_still_queues_the_final_update(pa):
    world = World()
    order_id = world.buy()
    gateway.simulate_delivery(AVA, order_id)
    result = world.execute(world.quote(order_id, f"{order_id}-L1", "8.5"), "exchange_store_pickup")
    pa.fail_with = DomainError(ErrorCode.UPSTREAM_TIMEOUT, "down")
    outcome = gateway.simulate_pickup(AVA, result.transaction_id)
    assert outcome["already_done"] is False
    assert store.get_item(store.outbox_key(outcome["update_id"]))["status"] == "pending"
    pa.fail_with = None
    assert gateway.flush_outbox(AVA) == 1
    assert pa.received[outcome["update_id"]]["provisional"] is False


# ------------------------------------------------- the colour exchange tells the stream the size fit


def test_memory_updated_carries_fit_feedback_for_a_colour_exchange_and_nothing_extra(env, monkeypatch):
    """D11: a PREFERENCE update is shown with fit_feedback size_ok; no other update field leaks."""
    seen: list[tuple[str, str, dict, str]] = []
    monkeypatch.setattr(gateway, "_emit", lambda owner, kind, payload, actor: seen.append((owner, kind, payload, actor)))
    update = {
        "update_id": "MU-5C0A12", "owner_ref": AVA, "order_id": "DEMO-A2A-81B20E", "style_id": "DEMO-STYLE-MEN001",
        "product_name": "Toney", "color": "BLACK", "original_size": "9.5", "new_size": "9.5", "reason": "PREFERENCE",
        "outcome": "exchange_created", "provisional": True, "source": "ALDO_CA", "ts": "2026-10-16T16:05:00+00:00",
        "fit_feedback": "size_ok",
    }
    gateway._emit_memory_updated(AVA, update)
    (owner, kind, payload, actor), = seen
    assert (owner, kind, actor) == (AVA, "memory_updated", "personal_agent")
    assert payload["fit_feedback"] == "size_ok" and payload["reason"] == "PREFERENCE" and payload["new_size"] == "9.5"
    assert set(payload) <= trace.A2A_PUBLIC_FIELDS["memory_updated"]
    assert "owner_ref" not in payload and "order_id" not in payload and "ts" not in payload

    seen.clear()
    gateway._emit_memory_updated(AVA, {k: v for k, v in update.items() if k != "fit_feedback"} | {"reason": "TOO_SMALL"})
    assert "fit_feedback" not in seen[0][2]  # a size exchange has none
