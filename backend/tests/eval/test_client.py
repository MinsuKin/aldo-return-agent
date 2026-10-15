"""ApiClient (throttling, error bodies, no secrets in output) and the run-completion wait."""

from __future__ import annotations

import json

import pytest


class Script:
    """Transport that replays a list of (status, body) answers and records the calls."""

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        status, payload = self.answers.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw, {}


def test_429_is_retried_then_succeeds(ev):
    naps = []
    script = Script([(429, {"message": "Too Many Requests"}), (429, {"message": "Too Many Requests"}), (200, {"ok": True})])
    api = ev.ApiClient("https://x.invalid/", script, sleep=naps.append)
    resp = api.get("/api/demo/personas")
    assert resp.ok and resp.body == {"ok": True} and len(script.calls) == 3 and len(naps) == 2


def test_429_gives_up_after_the_retry_budget(ev):
    script = Script([(429, {"message": "slow down"})] * 3)
    resp = ev.ApiClient("https://x.invalid", script, max_429_retries=2, sleep=lambda _s: None).get("/api/x")
    assert resp.status == 429 and len(script.calls) == 3


def test_error_body_is_parsed_and_brief_is_safe(ev):
    err = {"error": {"code": "QUOTE_EXPIRED", "message": "Quote expired.", "retryable": False}}
    resp = ev.ApiClient("https://x.invalid", Script([(409, err)])).post("/api/quotes/q/approve", {"option_id": "OPT-1"}, token="secret-token-value")
    assert resp.error_code == "QUOTE_EXPIRED" and "QUOTE_EXPIRED" in resp.brief() and "secret-token-value" not in resp.brief()


def test_non_json_error_page_does_not_crash(ev):
    resp = ev.ApiClient("https://x.invalid", Script([(502, b"<html>Bad gateway</html>")])).get("/api/x")
    assert resp.body is None and resp.error_code is None and "502" in resp.brief()


def test_bearer_token_and_json_content_type_are_sent(ev):
    script = Script([(200, {})])
    ev.ApiClient("https://x.invalid", script).post("/api/y", {"a": 1}, token="tok")
    _method, url, headers, body = script.calls[0]
    assert url == "https://x.invalid/api/y" and headers["Authorization"] == "Bearer tok" and headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"a": 1}


def test_get_retries_network_errors_but_post_does_not(ev):
    calls = []

    def flaky(method, url, headers, body, timeout):
        calls.append(method)
        raise ev.TransportError("boom")

    api = ev.ApiClient("https://x.invalid", flaky, sleep=lambda _s: None)
    with pytest.raises(ev.TransportError):
        api.get("/api/x")
    assert calls == ["GET", "GET", "GET"]
    calls.clear()
    with pytest.raises(ev.TransportError):
        api.post("/api/x", {})
    assert calls == ["POST"]


def event(seq, type_, **payload):
    return {"seq": seq, "type": type_, "ts": "t", "payload": payload}


def test_wait_for_run_polls_until_run_completed_then_reads_the_full_trace(ev):
    clock = ev.FakeClock()
    pages = [
        {"status": "queued", "events": [], "next_after": 0},
        {"status": "running", "events": [event(1, "run_started")], "next_after": 1},
        {"status": "completed", "events": [event(2, "assistant_message", text="hi"), event(3, "run_completed", status="completed", input_tokens=10)], "next_after": 3},
        {"status": "completed", "events": [event(1, "run_started"), event(2, "assistant_message"), event(3, "run_completed", status="completed", input_tokens=10)], "next_after": 3},
        {"status": "completed", "events": [], "next_after": 3},
    ]
    api = ev.ApiClient("https://x.invalid", Script([(200, p) for p in pages]), sleep=clock.sleep)
    res = ev.wait_for_run(api, "tok", "RUN-1", sleep=clock.sleep, monotonic=clock.monotonic)
    assert res.status == "completed" and res.types == ["run_started", "assistant_message", "run_completed"] and res.completed_is_last
    assert res.completed["input_tokens"] == 10 and clock.now == 2.0  # two sleeps between three polls


def test_wait_for_run_times_out(ev):
    clock = ev.FakeClock()
    api = ev.ApiClient("https://x.invalid", Script([(200, {"status": "running", "events": [], "next_after": 0})] * 50), sleep=clock.sleep)
    with pytest.raises(ev.RunTimeout):
        ev.wait_for_run(api, "tok", "RUN-1", timeout_s=5, poll_s=1.0, sleep=clock.sleep, monotonic=clock.monotonic)


def test_terminal_status_without_run_completed_gives_up_after_a_grace_period(ev):
    clock = ev.FakeClock()
    page = {"status": "error", "events": [event(1, "run_started")], "next_after": 1}
    api = ev.ApiClient("https://x.invalid", Script([(200, page)] * 60), sleep=clock.sleep)
    res = ev.wait_for_run(api, "tok", "RUN-1", grace_s=3.0, sleep=clock.sleep, monotonic=clock.monotonic)
    assert res.completed is None and not res.completed_is_last and res.status == "error"


def test_record_llm_run_flags_a_run_that_did_not_complete(ev):
    rec = ev.Recorder("A", 1, "default")
    res = ev.RunResult("RUN-1", "error", [event(1, "run_completed", status="error", error_code="UPSTREAM_TIMEOUT")], {"status": "error", "error_code": "UPSTREAM_TIMEOUT"}, 1.0)
    ev.record_llm_run(rec, res, "customer_run", "aldo")
    assert [c.ok for c in rec.checks] == [False, True]
    assert rec.llm_runs[0]["tokens_reported"] is False and rec.llm_runs[0]["error_code"] == "UPSTREAM_TIMEOUT"
