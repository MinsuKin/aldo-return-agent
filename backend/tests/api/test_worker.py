"""Worker Lambda and the API-side hand-off (contracts.md C.5/C.6, design-gap G-05, addendum 10.3).

Real ``runs`` / ``conversations`` / ``trace`` on moto. AgentCore is a fake boto3 client, so the
tests read what the worker sent and decide what the runtime answers.
"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

import boto3
import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from api import providers, wiring
from api.routes_returns import run_payload
from returns_domain import conversations, runs, store, trace
from returns_domain.errors import DomainError
from worker import enqueue as enqueue_module
from worker import handler as worker

from .conftest import AVA
from .fakes import FakeGateway
from .helpers import FakeContext

RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/aldo_returns_agent-AbC123"


# ------------------------------------------------------------------------------ fakes


def answer(body: Any) -> dict:
    """What ``invoke_agent_runtime`` returns: a streaming body with the handler's JSON."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return {"response": io.BytesIO(raw), "statusCode": 200}


class FakeAgentCore:
    def __init__(self, behavior: Callable[[dict], dict]) -> None:
        self.behavior = behavior
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        return self.behavior(kwargs)


class FakeBoto3:
    """Stands in for ``boto3`` inside ``worker.handler``; everything but AgentCore is real (moto)."""

    def __init__(self, agentcore: FakeAgentCore) -> None:
        self.agentcore = agentcore
        self.created: list[tuple[str, dict]] = []

    def client(self, service: str, **kwargs: Any):
        self.created.append((service, kwargs))
        return self.agentcore if service == "bedrock-agentcore" else boto3.client(service, **kwargs)


def agent_finishes_the_run(kwargs: dict) -> dict:
    """The runner's contract: start once, say something, finish the run, answer."""
    payload = json.loads(kwargs["payload"])
    run_id = payload["run_id"]
    assert runs.mark_agent_started(run_id)
    trace.emit(run_id, "assistant_message", {"text": "Done."})
    runs.finish_run(run_id, "completed", {"rounds": 1, "tool_count": 0})
    return answer({"run_id": run_id, "status": "completed", "metrics": {"rounds": 1}})


@pytest.fixture
def agentcore(monkeypatch) -> FakeAgentCore:
    fake = FakeAgentCore(agent_finishes_the_run)
    monkeypatch.setattr(worker, "boto3", FakeBoto3(fake))
    return fake


@pytest.fixture(autouse=True)
def worker_env(ddb, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", RUNTIME_ARN)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "http")
    monkeypatch.delenv("AGENT_MODE", raising=False)
    monkeypatch.delenv("AGENT_LOCAL_URL", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_ARN_PARAM", raising=False)
    monkeypatch.delenv("WORKER_FUNCTION_NAME", raising=False)
    worker.reset_runtime_arn()
    enqueue_module.reset_client()
    yield
    worker.reset_runtime_arn()
    enqueue_module.reset_client()


def queued_run(kind: str = "customer", customer_id: str = AVA, **extra: Any) -> dict:
    """What the API leaves behind before it enqueues: conversation, lease, queued RUN, payload."""
    conv_id = conversations.create_conversation(customer_id)
    run_id = store.new_id("RUN-", 10)
    assert runs.try_acquire_lease(conv_id, run_id)
    assert runs.create_run(customer_id, conv_id, run_id, kind)
    return run_payload(
        run_id=run_id,
        conv_id=conv_id,
        customer_id=customer_id,
        kind=kind,
        order_id=None,
        message="hello" if kind == "customer" else "",
        **extra,
    )


def event_types(run_id: str) -> list[str]:
    return [e["type"] for e in trace.list_events(run_id).events]


# ------------------------------------------------------------------------ the happy path


def test_the_runtime_is_invoked_with_the_conversations_session_and_the_payload(agentcore):
    payload = queued_run()
    result = worker.process(payload)

    assert result == {"run_id": payload["run_id"], "status": "completed"}
    (call,) = agentcore.calls
    session = conversations.get_conversation(AVA, payload["conv_id"])["runtime_session_id"]
    assert call["runtimeSessionId"] == session and len(session) >= 33
    assert call["agentRuntimeArn"] == RUNTIME_ARN
    assert call["contentType"] == "application/json" and call["accept"] == "application/json"
    assert json.loads(call["payload"]) == payload  # exactly what the API built, "agent" included


def test_a_run_the_agent_finished_is_left_alone(agentcore):
    payload = queued_run()
    worker.process(payload)
    assert event_types(payload["run_id"]) == ["assistant_message", "run_completed"]
    assert runs.get_run(AVA, payload["run_id"])["status"] == "completed"
    assert not runs.lease_active(payload["conv_id"])


def test_client_config(agentcore, monkeypatch):
    """G-05: no automatic retries, a read timeout that fits the Lambda budget, 5 s to connect."""
    config = worker.agentcore_config(300_000)
    assert (config.read_timeout, config.connect_timeout) == (285, 5)
    assert config.retries == {"mode": "standard", "max_attempts": 1}
    assert worker.agentcore_config(None).read_timeout == 285
    assert worker.agentcore_config(100_000).read_timeout == 85
    assert worker.agentcore_config(20_000).read_timeout == 30  # never below 30 s

    payload = queued_run()
    fake = worker.boto3
    worker.process(payload, remaining_ms=120_000)
    service, kwargs = next(c for c in fake.created if c[0] == "bedrock-agentcore")
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["config"].read_timeout == 105
    assert kwargs["config"].retries["max_attempts"] == 1


def test_duplicate_claim_returns_early(agentcore):
    """G-05: a second invocation of the same run does nothing."""
    payload = queued_run()
    assert worker.process(payload)["status"] == "completed"
    again = worker.process(payload)
    assert again == {"run_id": payload["run_id"], "status": "duplicate"}
    assert len(agentcore.calls) == 1
    assert event_types(payload["run_id"]) == ["assistant_message", "run_completed"]


def test_a_run_another_worker_holds_is_not_touched(agentcore):
    payload = queued_run()
    assert runs.claim_run(payload["run_id"])  # a concurrent worker got there first
    assert worker.process(payload) == {"run_id": payload["run_id"], "status": "duplicate"}
    assert agentcore.calls == []
    assert runs.get_run(AVA, payload["run_id"])["status"] == "running"


def test_the_lambda_entry_point_passes_the_remaining_time(agentcore):
    payload = queued_run()
    result = worker.handler(payload, FakeContext(remaining_ms=100_000))
    assert result["status"] == "completed"
    assert worker.boto3.created[-1][1]["config"].read_timeout == 85


# --------------------------------------------------------------------- runtime failures

TIMEOUT = ReadTimeoutError(endpoint_url="https://bedrock-agentcore.us-east-1.amazonaws.com")


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "secret detail"}}, "InvokeAgentRuntime")


@pytest.mark.parametrize(
    "failure,code",
    [
        (TIMEOUT, "UPSTREAM_TIMEOUT"),
        (ConnectTimeoutError(endpoint_url="x"), "UPSTREAM_TIMEOUT"),
        (client_error("ThrottlingException"), "UPSTREAM_TIMEOUT"),
        (client_error("RetryableConflictException"), "UPSTREAM_TIMEOUT"),
        (client_error("ServiceQuotaExceededException"), "UPSTREAM_TIMEOUT"),
        (client_error("AccessDeniedException"), "INTERNAL_ERROR"),
        (client_error("RuntimeClientError"), "INTERNAL_ERROR"),
        (ValueError("password=hunter2"), "INTERNAL_ERROR"),
        (DomainError("UPSTREAM_TIMEOUT", "slow"), "UPSTREAM_TIMEOUT"),
    ],
)
def test_a_runtime_failure_closes_the_run_with_a_customer_safe_message(agentcore, failure, code):
    def explode(kwargs: dict) -> dict:
        raise failure

    agentcore.behavior = explode
    payload = queued_run()

    result = worker.process(payload)

    assert result == {"run_id": payload["run_id"], "status": "error", "error_code": code}
    run_id = payload["run_id"]
    assert runs.get_run(AVA, run_id)["status"] == "error"
    events = trace.list_events(run_id).events
    assert [e["type"] for e in events] == ["assistant_message", "run_completed"]  # last event
    assert events[0]["payload"]["text"] == worker.GENERIC_FAILURE_TEXT
    assert events[1]["payload"]["status"] == "error" and events[1]["payload"]["error_code"] == code
    assert "hunter2" not in json.dumps(events) and "secret detail" not in json.dumps(events)
    assert not runs.lease_active(payload["conv_id"])  # the conversation is free again


def test_a_runtime_that_returns_with_the_run_still_running_is_closed(agentcore):
    agentcore.behavior = lambda kwargs: answer({"run_id": "x", "status": "completed", "metrics": {}})
    payload = queued_run()

    result = worker.process(payload)

    assert result["status"] == "completed"  # what the runtime claimed
    run_id = payload["run_id"]
    assert runs.get_run(AVA, run_id)["status"] == "error"
    events = trace.list_events(run_id).events
    assert [e["type"] for e in events] == ["assistant_message", "run_completed"]
    assert events[-1]["payload"]["error_code"] == "INTERNAL_ERROR"
    assert not runs.lease_active(payload["conv_id"])


def test_a_runtime_that_reports_a_duplicate_start_leaves_the_run_alone(agentcore):
    agentcore.behavior = lambda kwargs: answer({"run_id": "x", "status": "duplicate", "metrics": {}})
    payload = queued_run()

    assert worker.process(payload)["status"] == "duplicate"

    assert runs.get_run(AVA, payload["run_id"])["status"] == "running"  # its owner will end it
    assert event_types(payload["run_id"]) == []


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"text"', b""])
def test_an_unusable_runtime_answer_closes_the_run(agentcore, raw):
    agentcore.behavior = lambda kwargs: answer(raw)
    payload = queued_run()
    result = worker.process(payload)
    assert result["status"] == "error" and result["error_code"] == "INTERNAL_ERROR"
    assert runs.get_run(AVA, payload["run_id"])["status"] == "error"


def test_a_run_the_agent_already_ended_is_not_closed_twice(agentcore):
    def agent_ends_then_runtime_dies(kwargs: dict) -> dict:
        agent_finishes_the_run(kwargs)
        raise TIMEOUT  # the connection broke after the agent was done

    agentcore.behavior = agent_ends_then_runtime_dies
    payload = queued_run()
    result = worker.process(payload)
    assert result["status"] == "error"
    assert event_types(payload["run_id"]) == ["assistant_message", "run_completed"]  # no second ending
    assert runs.get_run(AVA, payload["run_id"])["status"] == "completed"


# -------------------------------------------------------------------- runtime ARN lookup


def test_the_runtime_arn_comes_from_the_environment_when_it_is_real():
    assert worker.runtime_arn() == RUNTIME_ARN


def test_a_placeholder_arn_is_ignored_and_ssm_is_read_once(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/aldo_placeholder_not_deployed")
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/aldo-demo/runtime-arn", Value=RUNTIME_ARN, Type="String")
    assert worker.runtime_arn() == RUNTIME_ARN
    ssm.put_parameter(Name="/aldo-demo/runtime-arn", Value="arn:aws:x:::changed", Type="String", Overwrite=True)
    assert worker.runtime_arn() == RUNTIME_ARN  # cached for the life of the process


def test_the_ssm_parameter_name_is_configurable(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_ARN")
    monkeypatch.setenv("AGENT_RUNTIME_ARN_PARAM", "/custom/runtime")
    boto3.client("ssm", region_name="us-east-1").put_parameter(Name="/custom/runtime", Value=RUNTIME_ARN, Type="String")
    assert worker.runtime_arn() == RUNTIME_ARN


def test_a_runtime_that_was_never_deployed_fails_the_run_cleanly(agentcore, monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_ARN")  # and no SSM parameter exists
    payload = queued_run()
    result = worker.process(payload)
    assert result["status"] == "error" and result["error_code"] == "INTERNAL_ERROR"
    assert agentcore.calls == []
    assert runs.get_run(AVA, payload["run_id"])["status"] == "error"


def test_an_ssm_value_that_is_not_an_arn_is_refused(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_ARN")
    boto3.client("ssm", region_name="us-east-1").put_parameter(Name="/aldo-demo/runtime-arn", Value="not-an-arn", Type="String")
    with pytest.raises(DomainError):
        worker.runtime_arn()


# ---------------------------------------------------------------------------- the ALDO bot


class FakeRunner:
    """Stands in for ``personal_agent.runner``."""

    def __init__(self, result: Any = None, failure: Exception | None = None) -> None:
        self.payloads: list[dict] = []
        self.result = result if result is not None else {"run_id": "PRUN-1", "status": "completed", "metrics": {}}
        self.failure = failure

    def run(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if self.failure:
            raise self.failure
        return self.result


class FakePersonalMemory:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def emit_run_event(self, owner, run_id, type, payload, *, actor="personal_agent"):
        self.calls.append(("emit", owner, run_id, type, payload["text"], actor))

    def finish_run(self, owner, run_id, status, metrics):
        self.calls.append(("finish", owner, run_id, status, dict(metrics)))
        return True

    def release_lease(self, owner, conv_id, run_id):
        self.calls.append(("release", owner, conv_id, run_id))


PERSONAL = {
    "agent": "personal",
    "run_id": "PRUN-0123456789",
    "conv_id": "PCONV-0123456789",
    "customer_id": AVA,
    "message": "Order me the Stessy heels for Saturday",
    "kind": "customer",
}


def test_personal_runs_execute_in_process_without_touching_the_returns_run_table(agentcore):
    runner = FakeRunner()
    providers.override("personal_agent.runner", runner)

    result = worker.process(dict(PERSONAL))

    assert result == runner.result
    assert runner.payloads == [PERSONAL]
    assert agentcore.calls == []  # never goes to the ALDO returns agent
    assert run_ids_in_returns_table() == set()  # no claim in the ALDO table


def run_ids_in_returns_table() -> set[str]:
    return {pk for pk, _ in store.scan_keys(("RUN#",))}


def test_a_crashed_personal_runner_closes_the_run_so_the_owner_is_not_left_waiting(agentcore):
    memory = FakePersonalMemory()
    providers.override("personal_agent.runner", FakeRunner(failure=ValueError("secret=abc")))
    providers.override("personal_agent.memory", memory)

    result = worker.process(dict(PERSONAL))

    assert result == {"run_id": "PRUN-0123456789", "status": "error", "error_code": "INTERNAL_ERROR"}
    assert memory.calls == [
        ("emit", AVA, "PRUN-0123456789", "assistant_message", worker.PERSONAL_FAILURE_TEXT, "system"),
        ("finish", AVA, "PRUN-0123456789", "error", {"error_code": "INTERNAL_ERROR"}),
        ("release", AVA, "PCONV-0123456789", "PRUN-0123456789"),
    ]
    assert "ALDO bot" in worker.PERSONAL_FAILURE_TEXT


def test_a_crashed_personal_runner_against_the_real_personal_table(agentcore, personal_table):
    repo = personal_table.get_repo()
    repo.seed()
    conv_id = repo.create_conversation(AVA)
    run_id = personal_table.new_run_id()
    assert repo.try_acquire_lease(AVA, conv_id, run_id)
    repo.create_run(AVA, conv_id, run_id)
    providers.override("personal_agent.runner", FakeRunner(failure=TIMEOUT))
    payload = {**PERSONAL, "run_id": run_id, "conv_id": conv_id}

    result = worker.process(payload)

    assert result["status"] == "error" and result["error_code"] == "UPSTREAM_TIMEOUT"
    page = repo.list_events(AVA, run_id)
    assert page["status"] == "error"
    assert [e["type"] for e in page["events"]] == ["assistant_message", "run_completed"]
    assert page["events"][-1]["payload"]["error_code"] == "UPSTREAM_TIMEOUT"
    assert not repo.lease_active(AVA, conv_id)


def test_the_personal_agent_package_being_absent_does_not_crash_the_worker(agentcore):
    providers.override("personal_agent.runner", None)
    providers.override("personal_agent.memory", None)
    result = worker.process(dict(PERSONAL))
    assert result == {"run_id": "PRUN-0123456789", "status": "error", "error_code": "INTERNAL_ERROR"}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"run_id": "RUN-1", "conv_id": "CONV-1"},  # no customer
        {"run_id": "", "conv_id": "CONV-1", "customer_id": AVA},
        {"run_id": "RUN-1", "conv_id": "CONV-1", "customer_id": AVA, "agent": "root"},
    ],
)
def test_a_malformed_payload_is_rejected_without_side_effects(agentcore, payload):
    gateway = FakeGateway()
    providers.override("gateway", gateway)
    assert worker.process(payload) == {"status": "error", "error_code": "VALIDATION_ERROR"}
    assert agentcore.calls == [] and gateway.calls == []


# ---------------------------------------------------------------------- the write-back outbox


def test_the_outbox_is_flushed_before_every_run_starts(agentcore):
    order: list[str] = []

    class OrderedGateway(FakeGateway):
        def flush_outbox(self, customer_id=None, limit=10, client=None):
            order.append(f"flush(customer={customer_id}, limit={limit})")
            return 2

    providers.override("gateway", OrderedGateway())
    original = agentcore.behavior
    agentcore.behavior = lambda kwargs: (order.append("invoke"), original(kwargs))[1]
    providers.override("personal_agent.runner", FakeRunner())

    worker.process(queued_run())
    worker.process(dict(PERSONAL))

    assert order == [
        "flush(customer=None, limit=10)",
        "invoke",
        "flush(customer=None, limit=10)",
    ]


def test_a_failing_flush_never_stops_the_run(agentcore):
    gateway = FakeGateway()
    gateway.failure = RuntimeError("receiver down")
    providers.override("gateway", gateway)
    payload = queued_run()
    assert worker.process(payload)["status"] == "completed"
    assert gateway.calls[0][0] == "flush_outbox"


def test_flush_outbox_quietly_returns_the_delivered_count_and_swallows_everything():
    gateway = FakeGateway()
    providers.override("gateway", gateway)
    assert wiring.flush_outbox_quietly(limit=3) == 0
    assert gateway.calls == [("flush_outbox", (None,), {"limit": 3})]
    gateway.failure = RuntimeError("boom")
    assert wiring.flush_outbox_quietly("DEMO-CUST-AVA") == 0
    providers.override("gateway", None)  # module missing
    assert wiring.flush_outbox_quietly() == 0


class FakeClientModule:
    def __init__(self) -> None:
        self.handler = None

    def register_local_handler(self, handler) -> None:
        self.handler = handler


def test_the_local_write_back_handler_is_registered_only_for_the_local_transport(monkeypatch):
    client = FakeClientModule()

    class Memory:
        @staticmethod
        def apply_memory_update(update):
            return {"applied": True}

    providers.override("personal_agent_client", client)
    providers.override("personal_agent.memory", Memory)

    monkeypatch.setenv("GATEWAY_TRANSPORT", "http")
    assert wiring.register_local_write_back() is False and client.handler is None
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    assert wiring.register_local_write_back() is True
    assert client.handler is Memory.apply_memory_update


def test_the_older_write_back_name_still_works(monkeypatch):
    client = FakeClientModule()

    class OldMemory:
        @staticmethod
        def apply_update(update):
            return {"applied": True}

    providers.override("personal_agent_client", client)
    providers.override("personal_agent.memory", OldMemory)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    assert wiring.register_local_write_back() is True
    assert client.handler is OldMemory.apply_update


def test_local_write_back_without_the_personal_agent_is_reported_not_raised(monkeypatch):
    providers.override("personal_agent_client", FakeClientModule())
    providers.override("personal_agent.memory", None)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    assert wiring.register_local_write_back() is False


# ------------------------------------------------------------------------- AGENT_MODE=local


class LocalAgent:
    """A tiny HTTP server that plays the locally running agent (``python agent_app.py``)."""

    def __init__(self, on_request: Callable[[dict, dict], dict], status: int = 200) -> None:
        outer = self
        self.requests: list[tuple[dict, dict, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                headers = {k.lower(): v for k, v in self.headers.items()}
                outer.requests.append((body, headers, self.path))
                reply = json.dumps(on_request(body, headers)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):  # silence the test output
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/invocations"

    def __enter__(self) -> "LocalAgent":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_local_mode_posts_the_payload_to_the_local_agent(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")

    def agent(body: dict, headers: dict) -> dict:
        return json.loads(agent_finishes_the_run({"payload": json.dumps(body)})["response"].read())

    payload = queued_run()
    with LocalAgent(agent) as local:
        monkeypatch.setenv("AGENT_LOCAL_URL", local.url)
        result = worker.process(payload)

    assert result == {"run_id": payload["run_id"], "status": "completed"}
    (body, headers, path), = local.requests
    assert path == "/invocations" and body == payload
    session = conversations.get_conversation(AVA, payload["conv_id"])["runtime_session_id"]
    assert headers["x-amzn-bedrock-agentcore-runtime-session-id"] == session
    assert runs.get_run(AVA, payload["run_id"])["status"] == "completed"


def test_local_mode_does_not_need_the_runtime_arn(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    monkeypatch.delenv("AGENT_RUNTIME_ARN")
    payload = queued_run()
    with LocalAgent(lambda body, headers: {"run_id": body["run_id"], "status": "duplicate"}) as local:
        monkeypatch.setenv("AGENT_LOCAL_URL", local.url)
        assert worker.process(payload)["status"] == "duplicate"


def test_local_mode_with_nothing_listening_is_an_upstream_timeout(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    monkeypatch.setenv("AGENT_LOCAL_URL", "http://127.0.0.1:9/invocations")  # discard port: refused
    payload = queued_run()
    result = worker.process(payload)
    assert result == {"run_id": payload["run_id"], "status": "error", "error_code": "UPSTREAM_TIMEOUT"}
    assert runs.get_run(AVA, payload["run_id"])["status"] == "error"


def test_local_mode_only_speaks_http(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    monkeypatch.setenv("AGENT_LOCAL_URL", "file:///etc/passwd")
    payload = queued_run()
    result = worker.process(payload)
    assert result["status"] == "error" and result["error_code"] == "INTERNAL_ERROR"


def test_a_local_agent_error_status_closes_the_run(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    payload = queued_run()
    with LocalAgent(lambda body, headers: {"detail": "boom"}, status=500) as local:
        monkeypatch.setenv("AGENT_LOCAL_URL", local.url)
        result = worker.process(payload)
    assert result["status"] == "error"
    assert runs.get_run(AVA, payload["run_id"])["status"] == "error"


# ---------------------------------------------------------------------- API-side hand-off


class FakeLambda:
    def __init__(self, status: int = 202, failure: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.status = status
        self.failure = failure

    def invoke(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return {"StatusCode": self.status}


def test_enqueue_invokes_the_worker_asynchronously(monkeypatch):
    fake = FakeLambda()
    monkeypatch.setattr(enqueue_module, "_client", lambda: fake)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "aldo-returns-demo-worker")
    payload = {"run_id": "RUN-1", "conv_id": "CONV-1", "customer_id": AVA, "agent": "aldo"}

    enqueue_module.enqueue_async(payload)

    (call,) = fake.calls
    assert call["FunctionName"] == "aldo-returns-demo-worker"
    assert call["InvocationType"] == "Event"  # never waits for the run
    assert json.loads(call["Payload"]) == payload


def test_enqueue_is_reachable_as_worker_enqueue_async():
    import worker as package

    assert package.enqueue_async is enqueue_module.enqueue_async


def test_enqueue_client_does_not_retry(monkeypatch):
    seen: dict = {}

    class FakeBoto:
        @staticmethod
        def client(service, **kwargs):
            seen.update(kwargs, service=service)
            return FakeLambda()

    monkeypatch.setattr(enqueue_module, "boto3", FakeBoto)
    enqueue_module._client()
    assert seen["service"] == "lambda"
    assert (seen["config"].connect_timeout, seen["config"].read_timeout) == (3, 10)
    assert seen["config"].retries["max_attempts"] == 1


@pytest.mark.parametrize(
    "fake",
    [FakeLambda(failure=RuntimeError("endpoint secret")), FakeLambda(status=200), FakeLambda(failure=TIMEOUT)],
)
def test_enqueue_failures_are_retryable_upstream_timeouts(monkeypatch, fake):
    monkeypatch.setattr(enqueue_module, "_client", lambda: fake)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "aldo-returns-demo-worker")
    with pytest.raises(DomainError) as excinfo:
        enqueue_module.enqueue_async({"run_id": "RUN-1"})
    assert excinfo.value.code == "UPSTREAM_TIMEOUT" and excinfo.value.retryable is True
    assert "secret" not in excinfo.value.message


def test_enqueue_without_a_worker_function_is_a_configuration_error():
    with pytest.raises(DomainError) as excinfo:
        enqueue_module.enqueue_async({"run_id": "RUN-1"})
    assert excinfo.value.code == "INTERNAL_ERROR"


def test_local_mode_runs_the_worker_inline_without_blocking_the_caller(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    done = threading.Event()
    seen: list[dict] = []
    release = threading.Event()

    def slow_process(payload: dict, **_: Any) -> dict:
        seen.append(payload)
        release.wait(5)  # still running while enqueue_async has already returned
        done.set()
        return {}

    monkeypatch.setattr(worker, "process", slow_process)
    payload = {"run_id": "RUN-1", "conv_id": "CONV-1", "customer_id": AVA}
    enqueue_module.enqueue_async(payload)  # must return immediately
    assert not done.is_set()
    release.set()
    assert done.wait(5) and seen == [payload]


def test_an_exception_in_the_inline_worker_never_escapes(monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "local")
    finished = threading.Event()

    def broken(payload: dict, **_: Any) -> dict:
        finished.set()
        raise RuntimeError("worker bug")

    monkeypatch.setattr(worker, "process", broken)
    enqueue_module.enqueue_async({"run_id": "RUN-1"})
    assert finished.wait(5)
