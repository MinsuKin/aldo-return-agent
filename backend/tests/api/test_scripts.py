"""scripts/local_api.py and scripts/checkpoint2.py, run for real against the API on moto.

The Checkpoint 2 script talks HTTP to the local server, which is the deployed handler; the worker
runs inline and POSTs to a fake agent (``AGENT_MODE=local``) that does what the real agent's tools
do. The negative tests prove the script's table assertions are not vacuous.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from api import handler as entry
from returns_domain import execution, options, orders, personal_agent_client, quotes, runs, store, trace

from .conftest import ADMIN_KEY, AVA
from .test_worker import LocalAgent

REPO = Path(__file__).resolve().parents[3]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


local_api = load_script("local_api")
checkpoint2 = load_script("checkpoint2")


@contextmanager
def serving(handler):
    server = local_api.make_server(handler, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def fetch(url: str, *, method: str = "GET", body=None, headers=None) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), json.loads(exc.read() or b"null")


# ------------------------------------------------------------------ the fake returns agent


def hero_agent(*, skip_quote=False, skip_execute=False, extra_txn=False, extra_atp_drop=False):
    """What the runtime does for the hero flow, with switches that break it on purpose."""

    def agent(payload: dict, headers: dict) -> dict:
        run_id, customer_id = payload["run_id"], payload["customer_id"]
        assert runs.mark_agent_started(run_id)
        if payload["kind"] == "customer":
            if not skip_quote:
                check = orders.check_policy(customer_id, run_id, payload["order_id"], "DEMO-1001-L1", 1, "FIT_SIZE", "unworn", "too_small")
                found = options.find_options(customer_id, check.policy_check_id, deadline_date="2026-10-17", run_id=run_id)
                quotes.create_quote(customer_id, run_id, payload["conv_id"], found.candidate_set_id, [o.option_id for o in found.options])
            trace.emit(run_id, "assistant_message", {"text": "Here are your options."})
        else:
            if not skip_execute:
                result = execution.execute(customer_id, payload["quote_id"], payload["option_id"], run_id=run_id)
                if extra_txn:
                    store.put_item({"pk": "TXN#DEMO-RMA-DOUBLE", "sk": "META", "customer_id": customer_id})
                if extra_atp_drop:
                    view = execution.get_status(customer_id, result.transaction_id)
                    store.update_item(
                        store.inventory_key(view.sku, view.location_id), add={"atp": -1}, return_new=False
                    )
            trace.emit(run_id, "assistant_message", {"text": "Your exchange is created (simulated)."})
        runs.finish_run(run_id, "completed", {"rounds": 2, "tool_count": 3})
        return {"run_id": run_id, "status": "completed", "metrics": {}}

    return agent


@pytest.fixture
def stack(ddb, personal_table, monkeypatch):
    """The API on a real socket, the worker inline, a fake agent behind AGENT_LOCAL_URL."""
    monkeypatch.setenv("DEMO_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("AGENT_MODE", "local")
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")

    @contextmanager
    def running(agent):
        with LocalAgent(lambda body, headers: agent(body, headers)) as fake, serving(entry.handler) as url:
            monkeypatch.setenv("AGENT_LOCAL_URL", fake.url)
            yield url, fake

    yield running
    personal_agent_client.register_local_handler(None)  # the worker registered the bot's handler


def run_script(url: str, **overrides):
    table = checkpoint2.Table("aldo-returns-demo")
    return checkpoint2.run_checkpoint(
        checkpoint2.Http(url, timeout_s=10),
        table,
        admin_key=ADMIN_KEY,
        variant=overrides.pop("variant", "dc_stockout"),
        poll_s=0.05,
        timeout_s=overrides.pop("timeout_s", 60),
        **overrides,
    )


def failed(report) -> list[str]:
    return [name for name, ok, _ in report.rows if not ok]


# ------------------------------------------------------------------- checkpoint 2 passes


def test_checkpoint_2_passes_against_the_working_system(stack, personal_table, capsys):
    with stack(hero_agent()) as (url, fake):
        report = run_script(url)

    assert failed(report) == [], report.rows
    assert report.passed and len(report.rows) >= 14
    assert report.facts["option_type"] == "exchange_store_pickup"  # the only option that meets Saturday
    assert report.facts["transaction_id"].startswith("DEMO-RMA-")
    kinds = [body["kind"] for body, _, _ in fake.requests]
    assert kinds.count("customer") == 1 and kinds.count("approval") == 1  # the double click ran the agent once
    out = capsys.readouterr().out
    assert "[  ok] exactly one transaction in the table" in out
    assert ADMIN_KEY not in out and "Bearer" not in out and "Authorization" not in out
    # the exchange also wrote back to the ALDO bot (provisional, once)
    updates = personal_table.get_repo().list_memory(AVA)["updates"]
    assert len(updates) == 1 and updates[0]["provisional"] is True


def test_the_command_line_wrapper_reports_and_exits_zero(stack, monkeypatch, capsys):
    monkeypatch.setenv("DEMO_ADMIN_KEY", ADMIN_KEY)
    with stack(hero_agent()) as (url, _):
        code = checkpoint2.main(["--base-url", url, "--variant", "dc_stockout", "--timeout", "60", "--json"])
    out = capsys.readouterr().out
    assert code == 0 and "PASS:" in out
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["passed"] is True and summary["facts"]["option_type"] == "exchange_store_pickup"
    assert ADMIN_KEY not in out


def test_the_default_variant_picks_the_pickup_too(stack):
    with stack(hero_agent()) as (url, _):
        report = run_script(url, variant="default")
    assert report.passed, failed(report)
    assert report.facts["option_type"] == "exchange_store_pickup"


# ------------------------------------------- checkpoint 2 fails when the system misbehaves


def test_checkpoint_2_fails_without_a_quote(stack):
    with stack(hero_agent(skip_quote=True)) as (url, _):
        report = run_script(url)
    assert not report.passed and failed(report) == ["run produced a quote"]


def test_checkpoint_2_fails_when_nothing_was_executed(stack):
    with stack(hero_agent(skip_execute=True)) as (url, _):
        report = run_script(url)
    assert "run created the transaction" in failed(report)


def test_checkpoint_2_catches_a_second_transaction(stack):
    with stack(hero_agent(extra_txn=True)) as (url, _):
        report = run_script(url)
    assert failed(report) == ["exactly one transaction in the table"]


def test_checkpoint_2_catches_a_double_stock_decrement(stack):
    with stack(hero_agent(extra_atp_drop=True)) as (url, _):
        report = run_script(url)
    assert len(failed(report)) == 1 and failed(report)[0].startswith("stock -1 at")


def test_checkpoint_2_stops_cleanly_when_a_run_never_completes(stack):
    def never_finishes(payload: dict, headers: dict) -> dict:
        runs.mark_agent_started(payload["run_id"])
        return {"run_id": payload["run_id"], "status": "completed", "metrics": {}}  # the worker then closes it as an error

    with stack(never_finishes) as (url, _):
        report = run_script(url, timeout_s=30)
    assert "run produced a quote" in failed(report) and not report.passed


def test_the_wrapper_exits_one_on_failure_and_two_when_it_cannot_start(stack, monkeypatch, capsys):
    monkeypatch.setenv("DEMO_ADMIN_KEY", ADMIN_KEY)
    with stack(hero_agent(skip_quote=True)) as (url, _):
        assert checkpoint2.main(["--base-url", url, "--timeout", "30"]) == 1
    assert "FAIL" in capsys.readouterr().out

    monkeypatch.delenv("DEMO_BASE_URL", raising=False)
    with mock.patch.object(checkpoint2, "resolve_base_url", side_effect=checkpoint2.CheckpointError("no base URL")):
        assert checkpoint2.main(["--skip-reset"]) == 2
    assert "cannot start" in capsys.readouterr().err


def test_resolve_base_url_prefers_the_argument_then_the_environment(monkeypatch):
    monkeypatch.setenv("DEMO_BASE_URL", "https://env.example.com/")
    assert checkpoint2.resolve_base_url("https://arg.example.com/") == "https://arg.example.com"
    assert checkpoint2.resolve_base_url(None) == "https://env.example.com"


# ---------------------------------------------------------------------------- local_api


def test_the_local_server_is_the_deployed_handler(ddb, personal_table, monkeypatch):
    monkeypatch.setenv("DEMO_ADMIN_KEY", ADMIN_KEY)
    with serving(entry.handler) as url:
        status, headers, body = fetch(f"{url}/api/demo/personas")
        assert status == 200 and len(body["personas"]) == 5
        assert headers["Cache-Control"] == "no-store" and headers["X-Request-Id"]
        status, _, session = fetch(f"{url}/api/demo/sessions", method="POST", body={"customer_id": AVA})
        assert status == 200
        bearer = {"Authorization": f"Bearer {session['token']}"}
        assert fetch(f"{url}/api/orders", headers=bearer)[0] == 200
        status, _, error = fetch(f"{url}/api/orders")
        assert status == 401 and error["error"]["code"] == "NOT_AUTHENTICATED"
        assert fetch(f"{url}/api/quotes/QUO-NOPE?x=1", headers=bearer)[0] == 404
        assert fetch(f"{url}/api/nope")[0] == 404


def test_the_local_event_matches_what_api_gateway_sends():
    event = local_api.v2_event("POST", "/api/conversations/CONV-1/messages?after=3&after=4", {"Host": "h:3001", "X-A": "b"}, b'{"text": "hi"}')
    assert event["rawPath"] == "/api/conversations/CONV-1/messages"
    assert event["queryStringParameters"] == {"after": "3,4"}
    assert event["headers"] == {"host": "h:3001", "x-a": "b"}
    assert event["body"] == '{"text": "hi"}' and event["isBase64Encoded"] is False
    assert event["requestContext"]["http"]["method"] == "POST" and event["requestContext"]["stage"] == "$default"
    binary = local_api.v2_event("POST", "/x", {}, b"\xff\xfe")
    assert binary["isBase64Encoded"] is True
    assert local_api.v2_event("GET", "/x", {}, b"")["body"] is None


def test_moto_mode_serves_a_complete_offline_demo():
    args = local_api.parse_args(["--moto", "--port", "0"])
    with mock.patch.dict(os.environ):
        server, aws = local_api.build(args)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            status, _, session = fetch(f"{url}/api/demo/sessions", method="POST", body={"customer_id": AVA})
            assert status == 200 and session["customer"]["home_region"] == "QC"
            bearer = {"Authorization": f"Bearer {session['token']}"}
            orders_ = fetch(f"{url}/api/orders", headers=bearer)[2]["orders"]
            assert {o["order_id"] for o in orders_} == {"DEMO-1001", "DEMO-1002", "DEMO-1008"}
            memory = fetch(f"{url}/api/personal-agent/memory", headers=bearer)[2]
            assert memory["typical_size"] == "7.5"
            status, _, card = fetch(f"{url}/.well-known/agent-card.json")
            assert status == 200 and card["name"] == "ALDO bot"
            reset = fetch(
                f"{url}/api/demo/reset", method="POST", body={"variant": "dc_stockout"},
                headers={"X-Demo-Admin": local_api.LOCAL_ADMIN_KEY},
            )
            assert reset[0] == 200
        finally:
            server.shutdown()
            server.server_close()
            aws.stop()
            store.reset_clients()
            from personal_agent import memory as personal_memory

            personal_memory.set_repo(None)


# ------------------------------------------------------------------------- wait_for_run


class ScriptedHttp:
    """Answers GET .../events with the next scripted page; records the requests."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.paths: list[str] = []

    def request(self, method, path, body=None, *, token=None, headers=None):
        self.paths.append(path)
        page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return 200, page


def page(status: str, *events: tuple[int, str], next_after: int | None = None) -> dict:
    listed = [{"seq": seq, "type": kind, "ts": "t", "actor": "aldo_agent", "payload": {"status": "completed"}} for seq, kind in events]
    return {"run_id": "RUN-1", "status": status, "events": listed, "next_after": next_after if next_after is not None else (listed[-1]["seq"] if listed else 0)}


def test_wait_for_run_waits_for_the_event_not_just_the_status():
    http = ScriptedHttp(
        [
            page("running", (1, "run_started")),
            page("completed", next_after=1),  # the status flipped, the last event is not visible yet
            page("completed", (2, "run_completed")),
            page("completed", (1, "run_started"), (2, "run_completed")),  # the re-read from 0
        ]
    )
    events = checkpoint2.wait_for_run(http, "token", "RUN-1", timeout_s=5, poll_s=0.0)
    assert [e["type"] for e in events] == ["run_started", "run_completed"]
    assert http.paths == [
        "/api/runs/RUN-1/events?after=0",
        "/api/runs/RUN-1/events?after=1",
        "/api/runs/RUN-1/events?after=1",
        "/api/runs/RUN-1/events?after=0",
    ]


def test_wait_for_run_gives_up_on_a_run_that_ended_without_its_last_event():
    http = ScriptedHttp([page("error")])
    with pytest.raises(checkpoint2.CheckpointError, match="never wrote run_completed"):
        checkpoint2.wait_for_run(http, "token", "RUN-1", timeout_s=30, poll_s=0.0)


def test_wait_for_run_times_out_on_a_run_that_never_ends():
    http = ScriptedHttp([page("running", (1, "run_started"))])
    with pytest.raises(checkpoint2.CheckpointError, match="did not complete within"):
        checkpoint2.wait_for_run(http, "token", "RUN-1", timeout_s=0.2, poll_s=0.05)
