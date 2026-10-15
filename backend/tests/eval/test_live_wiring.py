"""The LIVE code path (urllib transport, keys from returns_domain.config, boto3 table reads) against a loopback
HTTP server that serves the simulator. No AWS, no model: it proves the wiring that dry-run skips."""

from __future__ import annotations

import http.server
import threading

import pytest


def serve(system):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _serve(self):
            size = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(size) if size else b""
            status, payload, _headers = system.transport(self.command, f"http://127.0.0.1{self.path}", dict(self.headers.items()), body, 5)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = _serve

        def log_message(self, *args):  # keep test output clean
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def loopback(ev, build, monkeypatch):
    ctx, system = build()
    system.reset("default")  # personas exist before any scenario resets
    server = serve(system)
    monkeypatch.setenv("DEMO_TOKEN_KEY", system.token_key.decode())
    monkeypatch.setenv("DEMO_ADMIN_KEY", system.admin_key)
    monkeypatch.setenv("DEMO_WRITEBACK_KEY", system.writeback_key.decode())
    yield f"http://127.0.0.1:{server.server_address[1]}", system
    server.shutdown()


def test_urllib_transport_parses_success_and_error_bodies(ev, loopback):
    base, _system = loopback
    api = ev.ApiClient(base)
    ok = api.post("/api/demo/sessions", {"customer_id": "DEMO-CUST-AVA"})
    assert ok.status == 200 and ok.body["token"]
    bad = api.post("/api/demo/sessions", {"customer_id": "NOBODY"})
    assert bad.status == 422 and bad.error_code == "VALIDATION_ERROR"
    missing = api.get("/api/orders")
    assert missing.status == 401 and missing.error_code == "NOT_AUTHENTICATED"
    refused = api.reset("wrong-admin-key", "default")
    assert refused.status == 401 and refused.error_code == "NOT_AUTHENTICATED"


def test_live_mode_runs_scenarios_over_http_with_keys_from_config(ev, loopback, tmp_path):
    base, _system = loopback
    lines: list[str] = []
    code = ev.main(["--base-url", base, "--scenarios", "C,P3", "--runs", "1", "--poll-interval", "0", "--out-dir", str(tmp_path)], out=lines.append)
    assert code == 0, "\n".join(lines)
    assert lines[0].startswith("LIVE: http://127.0.0.1:")
    results = (tmp_path / "results.json").read_text()
    assert '"mode": "live"' in results and '"simulated": false' in results
    assert "fake-token-key" not in results and "fake-admin-key" not in results and "Bearer" not in results  # no secrets in outputs
    assert not any("fake-token-key" in line or "fake-admin-key" in line for line in lines)


def test_missing_signing_secret_is_a_config_error_not_a_crash(ev, loopback, monkeypatch):
    base, _system = loopback
    monkeypatch.delenv("DEMO_TOKEN_KEY")
    monkeypatch.setattr(ev, "ensure_secret_source", lambda region: None)  # no stack lookup in tests
    lines: list[str] = []
    assert ev.main(["--base-url", base, "--scenarios", "C"], out=lines.append) == 2
    assert "signing secrets" in lines[0]
