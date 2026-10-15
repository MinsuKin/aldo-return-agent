"""trace.py: atomic seq, redaction, contiguous-prefix reads (G-11), actors and the pivot event types."""

from __future__ import annotations

import json
import threading
from decimal import Decimal

import pytest

from returns_domain import runs, store, trace
from returns_domain.errors import DomainError, ErrorCode

CUSTOMER = "DEMO-CUST-AVA"


def make_run(run_id: str = "RUN-T1", customer: str = CUSTOMER) -> str:
    assert runs.create_run(customer, "CONV-T1", run_id, "customer")
    return run_id


def stored_event(run_id: str, seq: int) -> dict:
    return store.get_item(store.event_key(run_id, seq))


def walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)
    else:
        yield obj


# --------------------------------------------------------------------------------- emit


def test_emit_returns_increasing_seq_and_stores_the_event_row(ddb):
    run_id = make_run()
    assert [trace.emit(run_id, "run_started", {"kind": "customer"}) for _ in range(3)] == [1, 2, 3]
    row = stored_event(run_id, 2)
    assert (row["pk"], row["sk"]) == ("RUN#RUN-T1", "EVT#000002")
    assert row["seq"] == 2 and row["type"] == "run_started" and row["actor"] == "aldo_agent"
    assert row["payload"] == {"kind": "customer"} and row["ts"].endswith("+00:00")
    assert store.get_item(store.run_key(run_id))["seq"] == 3


def test_every_documented_event_type_is_accepted_and_the_pivot_types_are_present(ddb):
    expected = {"run_started", "tool_call", "tool_result", "decision", "quote_created", "approval_received",
                "transaction_created", "case_created", "assistant_message", "run_completed", "warning",
                "size_assist_requested", "fit_signal_shared", "size_requested", "size_recommended",
                "size_confirmed", "memory_updated"}
    assert set(trace.EVENT_TYPES) == expected
    run_id = make_run()
    for event_type in trace.EVENT_TYPES:
        trace.emit(run_id, event_type, {"x": 1})
    page = trace.list_events(run_id)
    assert [e["type"] for e in page.events] == list(trace.EVENT_TYPES)


def test_unknown_event_type_or_actor_is_a_validation_error_and_writes_nothing(ddb):
    run_id = make_run()
    with pytest.raises(DomainError) as exc:
        trace.emit(run_id, "made_up", {})
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(DomainError) as exc:
        trace.emit(run_id, "decision", {}, actor="mallory")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert store.get_item(store.run_key(run_id))["seq"] == 0


def test_actor_keyword_is_stored_and_returned(ddb):
    run_id = make_run()
    for actor in trace.ACTORS:
        trace.emit(run_id, "decision", {"summary": actor}, actor=actor)
    page = trace.list_events(run_id)
    assert [e["actor"] for e in page.events] == list(trace.ACTORS)
    assert trace.ACTORS == ("personal_agent", "aldo_gateway", "aldo_agent", "system")
    trace.emit(run_id, "decision", {"summary": "default"})
    assert trace.list_events(run_id, after=4).events[0]["actor"] == "aldo_agent"


def test_emit_on_a_missing_run_is_not_found_and_creates_nothing(ddb):
    with pytest.raises(DomainError) as exc:
        trace.emit("RUN-GHOST", "decision", {"summary": "x"})
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert store.get_item(store.run_key("RUN-GHOST")) is None
    assert trace.emit_safe("RUN-GHOST", "decision", {"summary": "x"}) is None  # logged, not raised


def test_floats_in_payloads_are_stored_and_read_back(ddb):
    """G-03: a model passes replacement_size=7.5 and a fit share 0.799; emit must not crash."""
    run_id = make_run()
    trace.emit(run_id, "tool_call", {"tool": "find_resolution_options", "args": {"replacement_size": 7.5}, "round": 2})
    trace.emit(run_id, "tool_result", {"tool": "get_fit_insight", "ok": True, "evidence": {"too_small_share": 0.799}})
    events = trace.list_events(run_id).events
    assert events[0]["payload"]["args"]["replacement_size"] == 7.5
    assert events[1]["payload"]["evidence"]["too_small_share"] == 0.799
    assert not any(isinstance(v, Decimal) for e in events for v in walk(e["payload"]))
    json.dumps(events)


def test_concurrent_emits_get_unique_sequence_numbers(ddb):
    run_id = make_run()
    seqs: list[int] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        seq = trace.emit(run_id, "decision", {"summary": f"d{n}"})
        with lock:
            seqs.append(seq)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seqs) == list(range(1, 13))
    assert [e["seq"] for e in trace.list_events(run_id).events] == list(range(1, 13))


# ----------------------------------------------------------------------------- redaction


def test_redact_drops_credential_and_identity_keys_at_any_depth(ddb):
    payload = {
        "tool": "x", "token": "t", "Secret": "s", "authorization": "Bearer abc", "password": "p", "api_key": "k",
        "apiKey": "k2", "customer_id": "DEMO-CUST-AVA", "customerId": "c", "email": "a@b.c", "owner_ref": "DEMO-CUST-AVA",
        "owner_id": "o", "access_token": "a", "idToken": "i", "signature": "sig", "admin_key": "ak",
        "args": {"nested": {"customer_id": "x", "keep": 1}, "list": [{"token": "t", "ok": 2}]},
    }
    cleaned = trace.redact(payload)
    assert cleaned == {"tool": "x", "args": {"nested": {"keep": 1}, "list": [{"ok": 2}]}}


def test_redact_keeps_token_usage_metrics(ddb):
    metrics = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150, "tool_count": 3, "token": "x"}
    assert trace.redact(metrics) == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150, "tool_count": 3}


def test_redact_caps_strings_lists_and_depth(ddb):
    cleaned = trace.redact({"s": "x" * 900, "short": "ok", "many": list(range(80)),
                            "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": "end"}}}}}}}}}})
    assert len(cleaned["s"]) == 500 and cleaned["s"].endswith("…")
    assert cleaned["short"] == "ok"
    assert len(cleaned["many"]) == 50
    flat = json.dumps(cleaned["deep"])
    assert "[truncated]" in flat and "end" not in flat


def test_assistant_message_text_may_be_1500_characters_other_events_500(ddb):
    run_id = make_run()
    trace.emit(run_id, "assistant_message", {"text": "a" * 2000, "other": "b" * 2000})
    trace.emit(run_id, "decision", {"summary": "c" * 2000})
    first, second = (e["payload"] for e in trace.list_events(run_id).events)
    assert len(first["text"]) == 1500 and len(first["other"]) == 500
    assert len(second["summary"]) == 500


def test_redact_scrubs_token_shaped_text_and_customer_ids_inside_strings(ddb):
    jwt_like = "a" * 30 + "." + "b" * 30
    cleaned = trace.redact({"text": f"your key {jwt_like} and DEMO-CUST-AVA are private", "ok": "DEMO-1001 DEMO-A2A-81B20E"})
    assert jwt_like not in cleaned["text"] and "DEMO-CUST-AVA" not in cleaned["text"]
    assert "[redacted]" in cleaned["text"] and "[customer]" in cleaned["text"]
    assert cleaned["ok"] == "DEMO-1001 DEMO-A2A-81B20E"  # order ids are public


def test_redact_never_raises_and_handles_odd_values(ddb):
    assert trace.redact({"x": object()}) == {}  # not JSON-serializable: falls back to an empty payload
    assert trace.redact(None) is None
    assert trace.redact({"nan": float("nan"), "bytes": b"hi", "set": {2, 1}}) == {"nan": None, "bytes": "aGk=", "set": [1, 2]}


def test_events_in_the_table_never_contain_identity_or_credentials(ddb):
    run_id = make_run()
    trace.emit(run_id, "tool_call", {"tool": "t", "args": {"customer_id": CUSTOMER, "token": "abc", "note": "hi"}})
    text = json.dumps(stored_event(run_id, 1), default=str)
    assert "DEMO-CUST" not in text and "abc" not in text and "customer_id" not in text


# ------------------------------------------------------------------------------ reading


def test_list_events_returns_events_after_the_cursor(ddb):
    run_id = make_run()
    for n in range(5):
        trace.emit(run_id, "decision", {"summary": f"d{n}"})
    page = trace.list_events(run_id, after=2)
    assert [e["seq"] for e in page.events] == [3, 4, 5] and page.next_after == 5
    assert page.run_id == run_id and page.status == "queued"
    assert set(page.events[0]) == {"seq", "type", "ts", "actor", "payload"}
    empty = trace.list_events(run_id, after=5)
    assert empty.events == [] and empty.next_after == 5
    beyond = trace.list_events(run_id, after=99)
    assert beyond.events == [] and beyond.next_after == 99
    assert [e["seq"] for e in trace.list_events(run_id, after=0, limit=2).events] == [1, 2]


def test_gap_not_skipped(ddb):
    """G-11: event N+1 visible before N must not move the cursor past N."""
    run_id = make_run()
    trace.emit(run_id, "decision", {"summary": "one"})
    store.update_item(store.run_key(run_id), add={"seq": 1})  # seq 2 reserved, its event not yet written
    assert trace.emit(run_id, "decision", {"summary": "three"}) == 3
    page = trace.list_events(run_id, after=1)
    assert page.events == [] and page.next_after == 1
    assert [e["seq"] for e in trace.list_events(run_id, after=0).events] == [1]
    pk, sk = store.event_key(run_id, 2)  # the slow writer finishes
    store.put_item({"pk": pk, "sk": sk, "seq": 2, "type": "decision", "actor": "aldo_agent", "ts": store.now_iso(),
                    "payload": {"summary": "two"}})
    page = trace.list_events(run_id, after=1)
    assert [e["seq"] for e in page.events] == [2, 3] and page.next_after == 3


def test_list_events_status_follows_the_run(ddb):
    run_id = make_run()
    assert trace.list_events(run_id).status == "queued"
    runs.claim_run(run_id)
    assert trace.list_events(run_id).status == "running"
    runs.finish_run(run_id, "completed", {})
    page = trace.list_events(run_id)
    assert page.status == "completed" and page.events[-1]["type"] == "run_completed"


def test_list_events_validates_arguments_and_missing_runs(ddb):
    run_id = make_run()
    for kwargs in ({"after": -1}, {"after": "1"}, {"after": True}, {"limit": 0}, {"limit": 501}):
        with pytest.raises(DomainError) as exc:
            trace.list_events(run_id, **kwargs)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
    with pytest.raises(DomainError) as exc:
        trace.list_events("RUN-GHOST")
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_meta_and_event_items_do_not_collide_in_reads(ddb):
    """sk 'META' sorts after 'EVT#...', so a naive sk > cursor query would return it."""
    run_id = make_run()
    for _ in range(11):
        trace.emit(run_id, "decision", {"summary": "x"})
    page = trace.list_events(run_id, after=10)
    assert [e["seq"] for e in page.events] == [11]


# -------------------------------------------------------------------------------- audit


def test_audit_events_are_stored_per_utc_day_and_redacted(ddb, fixed_now):
    from returns_domain import audit

    fixed_now.set("2026-10-16T23:59:59+00:00")
    audit.write_audit(CUSTOMER, "access_denied", "denied", {"resource_type": "order", "resource_id": "DEMO-1006", "token": "t"})
    fixed_now.set("2026-10-17T00:00:01+00:00")
    audit.write_audit("DEMO-CUST-BEN", "execute", "allowed", {"quote_id": "QUO-1"})
    day1, day2 = audit.list_audit("2026-10-16"), audit.list_audit("2026-10-17")
    assert len(day1) == 1 and len(day2) == 1
    assert day1[0]["pk"] == "AUDIT#2026-10-16" and day1[0]["sk"].startswith("2026-10-16T23:59:59+00:00#")
    assert (day1[0]["actor"], day1[0]["action"], day1[0]["outcome"]) == (CUSTOMER, "access_denied", "denied")
    assert day1[0]["refs"] == {"resource_type": "order", "resource_id": "DEMO-1006"}
    assert audit.list_audit("2026-10-17", action="execute")[0]["actor"] == "DEMO-CUST-BEN"
    assert audit.list_audit("2026-10-17", action="nope") == []
    assert audit.list_audit() == day2  # default day is the clock's UTC date


def test_audit_write_never_raises(ddb, monkeypatch):
    from returns_domain import audit

    def boom(*args, **kwargs):
        raise RuntimeError("table on fire")

    monkeypatch.setattr(store, "put_item", boom)
    audit.write_audit(CUSTOMER, "access_denied", "denied", {})  # swallowed and logged
    assert audit.write is audit.write_audit
