"""runs.py: the run state machine, exactly-once completion and the conversation lease (G-05, G-06)."""

from __future__ import annotations

import pytest

from returns_domain import conversations, runs, store, trace
from returns_domain.errors import DomainError, ErrorCode

AVA, BEN = "DEMO-CUST-AVA", "DEMO-CUST-BEN"


def new_run(run_id="RUN-1", conv_id="CONV-1", customer=AVA, kind="customer"):
    assert runs.create_run(customer, conv_id, run_id, kind)
    return run_id


def run_item(run_id="RUN-1"):
    return store.get_item(store.run_key(run_id))


def new_conv(customer=AVA):
    return conversations.create_conversation(customer)


def test_create_run_stores_a_queued_run_and_is_idempotent(ddb):
    assert runs.create_run(AVA, "CONV-1", "RUN-1", "customer") is True
    item = run_item()
    assert (item["status"], item["seq"], item["kind"], item["conv_id"], item["customer_id"]) == ("queued", 0, "customer", "CONV-1", AVA)
    assert item["created_at"].endswith("+00:00")
    assert runs.create_run(BEN, "CONV-2", "RUN-1", "approval") is False  # never overwrites
    assert run_item()["customer_id"] == AVA and run_item()["kind"] == "customer"


@pytest.mark.parametrize("kind", ["", "Customer", "customer;x", None, 5])
def test_create_run_rejects_a_bad_kind(ddb, kind):
    with pytest.raises(DomainError) as exc:
        runs.create_run(AVA, "CONV-1", "RUN-1", kind)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_duplicate_claim_returns_false(ddb):
    new_run()
    assert runs.claim_run("RUN-1") is True
    assert run_item()["status"] == "running" and run_item()["claimed_at"]
    assert runs.claim_run("RUN-1") is False
    assert runs.claim_run("RUN-MISSING") is False
    assert store.get_item(store.run_key("RUN-MISSING")) is None  # no stray item


def test_mark_agent_started_once(ddb):
    new_run()
    assert runs.mark_agent_started("RUN-1") is True
    first = run_item()["agent_started_at"]
    assert runs.mark_agent_started("RUN-1") is False
    assert run_item()["agent_started_at"] == first


def test_finish_run_winner_emits_run_completed_with_the_metrics(ddb):
    new_run()
    runs.claim_run("RUN-1")
    metrics = {"rounds": 4, "tool_count": 6, "input_tokens": 1200, "output_tokens": 340, "total_latency_ms": 9100,
               "model_id_used": "us.anthropic.claude-sonnet-5-5", "fallback_used": False, "error_code": None}
    assert runs.finish_run("RUN-1", "completed", metrics) is True
    item = run_item()
    assert item["status"] == "completed" and item["metrics"] == metrics and item["finished_at"]
    events = trace.list_events("RUN-1").events
    assert [e["type"] for e in events] == ["run_completed"]
    assert events[0]["payload"] == {"status": "completed", **metrics}


def test_finish_run_loser_writes_nothing_and_run_completed_is_emitted_once(ddb):
    new_run()
    runs.claim_run("RUN-1")
    assert runs.finish_run("RUN-1", "completed", {"rounds": 1}) is True
    before = run_item()
    assert runs.finish_run("RUN-1", "error", {"error_code": "INTERNAL_ERROR"}) is False
    assert run_item() == before
    assert [e["type"] for e in trace.list_events("RUN-1").events] == ["run_completed"]


def test_finish_run_fills_missing_metrics_with_defaults(ddb):
    new_run()
    runs.claim_run("RUN-1")
    runs.finish_run("RUN-1", "error", {"error_code": "UPSTREAM_TIMEOUT"})
    payload = trace.list_events("RUN-1").events[0]["payload"]
    assert payload == {"status": "error", "rounds": 0, "tool_count": 0, "input_tokens": 0, "output_tokens": 0,
                       "total_latency_ms": 0, "model_id_used": "", "fallback_used": False, "error_code": "UPSTREAM_TIMEOUT"}


def test_a_queued_run_can_be_failed_when_the_async_invoke_fails(ddb):
    new_run()
    assert runs.finish_run("RUN-1", "error", {"error_code": "UPSTREAM_TIMEOUT"}) is True
    assert run_item()["status"] == "error"
    assert runs.claim_run("RUN-1") is False  # a late worker invocation cannot restart it


def test_finish_run_rejects_non_terminal_status_and_missing_runs(ddb):
    new_run()
    with pytest.raises(DomainError):
        runs.finish_run("RUN-1", "running", {})
    assert runs.finish_run("RUN-MISSING", "completed", {}) is False


def test_finish_run_releases_the_conversation_lease(ddb):
    conv = new_conv()
    new_run("RUN-1", conv)
    assert runs.try_acquire_lease(conv, "RUN-1") is True
    runs.claim_run("RUN-1")
    assert runs.lease_active(conv)
    runs.finish_run("RUN-1", "completed", {})
    assert not runs.lease_active(conv)
    assert "active_run_id" not in store.get_item(store.conv_key(conv))


def test_get_run_is_owner_checked_and_audited(ddb):
    new_run()
    assert runs.get_run(AVA, "RUN-1")["status"] == "queued"
    assert "pk" not in runs.get_run(AVA, "RUN-1")
    with pytest.raises(DomainError) as exc:
        runs.get_run(BEN, "RUN-1")
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    with pytest.raises(DomainError) as exc:
        runs.get_run(AVA, "RUN-MISSING")
    assert exc.value.code == ErrorCode.NOT_FOUND
    from returns_domain import audit

    denied = audit.list_audit(action="access_denied")
    assert len(denied) == 1 and denied[0]["actor"] == BEN
    assert denied[0]["refs"] == {"resource_type": "run", "resource_id": "RUN-1"}


# ---------------------------------------------------------------------------------- lease


def test_lease_blocks_a_second_run_until_released_or_expired(ddb, fixed_now):
    conv = new_conv()
    assert runs.lease_active(conv) is False
    assert runs.try_acquire_lease(conv, "RUN-1") is True
    assert runs.lease_active(conv) is True
    assert runs.try_acquire_lease(conv, "RUN-2") is False
    assert runs.try_acquire_lease(conv, "RUN-1") is True  # the holder may re-take it
    item = store.get_item(store.conv_key(conv))
    assert item["active_run_id"] == "RUN-1" and item["active_run_expires_at"] == "2026-10-16T16:05:30+00:00"  # +330 s
    fixed_now.advance(seconds=329)
    assert runs.lease_active(conv) is True and runs.try_acquire_lease(conv, "RUN-2") is False
    fixed_now.advance(seconds=2)  # a crashed Lambda cannot hold the conversation longer than 330 s
    assert runs.lease_active(conv) is False
    assert runs.try_acquire_lease(conv, "RUN-2") is True
    assert store.get_item(store.conv_key(conv))["active_run_id"] == "RUN-2"


def test_release_lease_only_by_the_holder(ddb):
    conv = new_conv()
    runs.try_acquire_lease(conv, "RUN-1")
    runs.release_lease(conv, "RUN-2")  # not the holder: ignored
    assert runs.lease_active(conv) is True
    runs.release_lease(conv, "RUN-1")
    assert runs.lease_active(conv) is False
    runs.release_lease(conv, "RUN-1")  # releasing twice is fine
    runs.release_lease("CONV-MISSING", "RUN-1")
    assert runs.try_acquire_lease(conv, "RUN-2") is True


def test_lease_custom_ttl_and_validation(ddb, fixed_now):
    conv = new_conv()
    assert runs.try_acquire_lease(conv, "RUN-1", ttl_seconds=60)
    fixed_now.advance(seconds=61)
    assert runs.lease_active(conv) is False
    for bad in (0, -1, 1.5, True):
        with pytest.raises(DomainError):
            runs.try_acquire_lease(conv, "RUN-1", ttl_seconds=bad)
    with pytest.raises(DomainError):
        runs.try_acquire_lease(conv, "")


def test_lease_on_a_missing_conversation_is_never_created(ddb):
    assert runs.try_acquire_lease("CONV-GHOST", "RUN-1") is False
    assert store.get_item(store.conv_key("CONV-GHOST")) is None
    assert runs.lease_active("CONV-GHOST") is False


def test_only_one_of_two_racing_runs_gets_the_lease(ddb):
    import threading

    conv = new_conv()
    results = {}

    def grab(name):
        results[name] = runs.try_acquire_lease(conv, name)

    threads = [threading.Thread(target=grab, args=(f"RUN-{n}",)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results.values()) == 1
    assert store.get_item(store.conv_key(conv))["active_run_id"] in results
