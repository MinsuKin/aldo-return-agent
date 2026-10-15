"""The per-owner agent-to-agent stream (PIVOT_ADDENDUM 5.6, 9.1; contracts.md F.6)."""

from __future__ import annotations

import json

import pytest

from returns_domain import fixtures, store, trace
from returns_domain.errors import DomainError, ErrorCode

AVA, BEN = "DEMO-CUST-AVA", "DEMO-CUST-BEN"


def a2a_event(customer: str, seq: int) -> dict:
    return store.get_item(store.event_key(trace.a2a_run_id(customer), seq))


def test_a2a_run_id_and_item_keys(ddb):
    assert trace.a2a_run_id(AVA) == "A2A-DEMO-CUST-AVA"
    trace.emit_a2a(AVA, "size_assist_requested", {"size_request_id": "SZR-1"}, actor="personal_agent")
    assert a2a_event(AVA, 1)["pk"] == "RUN#A2A-DEMO-CUST-AVA"
    assert a2a_event(AVA, 1)["sk"] == "EVT#000001"


def test_first_emit_creates_the_stream_meta_once(ddb):
    assert store.get_item(store.run_key(trace.a2a_run_id(AVA))) is None
    assert trace.emit_a2a(AVA, "size_assist_requested", {"size_request_id": "SZR-1"}, actor="personal_agent") == 1
    meta = store.get_item(store.run_key(trace.a2a_run_id(AVA)))
    assert (meta["status"], meta["kind"], meta["customer_id"]) == ("open", "a2a", AVA)
    assert meta["seq"] == 1
    assert trace.emit_a2a(AVA, "fit_signal_shared", {"size_request_id": "SZR-1"}, actor="aldo_gateway") == 2
    assert store.get_item(store.run_key(trace.a2a_run_id(AVA)))["seq"] == 2  # not reset by the second emit


def test_the_six_pivot_types_and_the_transaction_mirror_are_the_only_a2a_types(ddb):
    assert set(trace.A2A_PUBLIC_FIELDS) == {
        "size_assist_requested", "fit_signal_shared", "size_requested", "size_recommended", "size_confirmed",
        "memory_updated", "transaction_created"}
    for bad in ("tool_call", "run_completed", "decision", "assistant_message", "warning", "made_up"):
        with pytest.raises(DomainError) as exc:
            trace.emit_a2a(AVA, bad, {"summary": "x"}, actor="aldo_agent")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert store.get_item(store.run_key(trace.a2a_run_id(AVA))) is None  # nothing was created


def test_allowlist_matches_contract_f6_exactly():
    assert trace.A2A_PUBLIC_FIELDS == {
        "size_assist_requested": frozenset({"size_request_id", "product_query", "color", "deadline_date", "channel_pref"}),
        "fit_signal_shared": frozenset({"size_request_id", "style_id", "product_name", "signal", "too_small_share", "n_fit", "scope", "provenance"}),
        "size_requested": frozenset({"size_request_id", "sizes", "option_count", "question", "expires_at"}),
        "size_recommended": frozenset({"size_request_id", "option_id", "chosen_size", "option_type", "location_name", "eta_date", "rationale_public", "confidence"}),
        "size_confirmed": frozenset({"size_request_id", "order_id", "status", "fulfillment", "next_step"}),
        "memory_updated": frozenset({"update_id", "style_id", "product_name", "original_size", "new_size", "reason", "outcome", "provisional"}),
        "transaction_created": frozenset({"transaction_id", "state", "option_type", "order_id", "replayed"}),
    }


def test_a2a_public_allowlist(ddb):
    """C3: private fields are dropped, whatever the caller passes."""
    trace.emit_a2a(
        AVA, "size_recommended",
        {"size_request_id": "SZR-1", "option_id": "SO-2", "chosen_size": "8.0", "option_type": "store_pickup",
         "location_name": "ALDO Demo Store Montreal", "eta_date": "2026-10-17", "rationale_public": "runs small per ALDO signal",
         "confidence": "high",
         "private_rationale": "Retailer C (synthetic) 8.0 was too big", "typical_size": "7.5", "owner_ref": AVA,
         "customer_id": AVA, "token": "secret-token-value", "retailer": "Retailer A (synthetic)", "owner_fit_hint": "x"},
        actor="personal_agent",
    )
    payload = a2a_event(AVA, 1)["payload"]
    assert set(payload) == {"size_request_id", "option_id", "chosen_size", "option_type", "location_name",
                            "eta_date", "rationale_public", "confidence"}
    event = a2a_event(AVA, 1)
    text = json.dumps({k: v for k, v in event.items() if k not in ("pk", "sk")}, default=str)  # pk holds the stream id
    for leaked in ("Retailer", "typical_size", "owner_ref", "DEMO-CUST", "secret-token", "owner_fit_hint"):
        assert leaked not in text


def test_every_pivot_type_round_trips_with_its_actor(ddb):
    samples = [
        ("size_assist_requested", "personal_agent", {"size_request_id": "SZR-1", "product_query": "Stessy", "color": "BLACK", "deadline_date": "2026-10-17", "channel_pref": None}),
        ("fit_signal_shared", "aldo_gateway", {"size_request_id": "SZR-1", "style_id": "DEMO-STYLE-HERO", "product_name": "P", "signal": "runs_small", "too_small_share": 0.799, "n_fit": 582, "scope": "ALDO_CA", "provenance": "derived"}),
        ("size_requested", "aldo_gateway", {"size_request_id": "SZR-1", "sizes": ["7.5", "8.0"], "option_count": 4, "question": "Which size?", "expires_at": "2026-10-16T16:15:00+00:00"}),
        ("size_recommended", "personal_agent", {"size_request_id": "SZR-1", "option_id": "SO-2", "chosen_size": "8.0", "confidence": "high"}),
        ("size_confirmed", "aldo_gateway", {"size_request_id": "SZR-1", "order_id": "DEMO-A2A-81B20E", "status": "placed_simulated", "fulfillment": {"type": "store_pickup", "location_name": "L", "eta_date": "2026-10-17"}, "next_step": "Pick up"}),
        ("memory_updated", "personal_agent", {"update_id": "MU-5C0A11", "style_id": "DEMO-STYLE-HERO", "product_name": "P", "original_size": "8.0", "new_size": "8.5", "reason": "TOO_SMALL", "outcome": "exchange_created", "provisional": True}),
        ("transaction_created", "aldo_agent", {"transaction_id": "DEMO-RMA-3F9A1C", "state": "completed", "option_type": "exchange_store_pickup", "order_id": "DEMO-A2A-81B20E", "replayed": False}),
    ]
    for event_type, actor, payload in samples:
        trace.emit_a2a(AVA, event_type, payload, actor=actor)
    page = trace.list_a2a_events(AVA)
    assert [(e["type"], e["actor"]) for e in page.events] == [(t, a) for t, a, _ in samples]
    assert page.events[1]["payload"]["too_small_share"] == 0.799 and page.events[5]["payload"]["provisional"] is True
    assert page.events[4]["payload"]["fulfillment"]["eta_date"] == "2026-10-17"
    for (_, _, payload), event in zip(samples, page.events):
        assert event["payload"] == payload  # nothing public was lost


def test_emit_a2a_requires_a_known_actor_and_keyword_only(ddb):
    with pytest.raises(DomainError):
        trace.emit_a2a(AVA, "size_confirmed", {"size_request_id": "SZR-1"}, actor="nobody")
    with pytest.raises(TypeError):
        trace.emit_a2a(AVA, "size_confirmed", {"size_request_id": "SZR-1"})  # actor is required


def test_streams_are_per_owner(ddb):
    trace.emit_a2a(AVA, "size_assist_requested", {"size_request_id": "SZR-AVA"}, actor="personal_agent")
    trace.emit_a2a(BEN, "size_assist_requested", {"size_request_id": "SZR-BEN"}, actor="personal_agent")
    ava, ben = trace.list_a2a_events(AVA), trace.list_a2a_events(BEN)
    assert [e["payload"]["size_request_id"] for e in ava.events] == ["SZR-AVA"]
    assert [e["payload"]["size_request_id"] for e in ben.events] == ["SZR-BEN"]


def test_list_a2a_events_cursor_empty_stream_and_alias(ddb):
    empty = trace.list_a2a_events(AVA, after=0)
    assert (empty.events, empty.next_after, empty.status) == ([], 0, "open")
    for n in range(4):
        trace.emit_a2a(AVA, "size_requested", {"size_request_id": f"SZR-{n}"}, actor="aldo_gateway")
    page = trace.list_a2a_events(AVA, after=1)
    assert [e["seq"] for e in page.events] == [2, 3, 4] and page.next_after == 4
    assert trace.list_a2a_events(AVA, after=4).events == [] and trace.list_a2a_events(AVA, after=4).next_after == 4
    assert trace.read_a2a is trace.list_a2a_events
    with pytest.raises(DomainError):
        trace.list_a2a_events(AVA, after=-1)


def test_a2a_reads_use_the_same_contiguous_prefix_rule(ddb):
    trace.emit_a2a(AVA, "size_requested", {"size_request_id": "SZR-1"}, actor="aldo_gateway")
    rid = trace.a2a_run_id(AVA)
    store.update_item(store.run_key(rid), add={"seq": 1})  # seq 2 reserved but not written
    trace.emit(rid, "size_confirmed", {"size_request_id": "SZR-1"}, actor="aldo_gateway")  # seq 3 visible
    page = trace.list_a2a_events(AVA, after=1)
    assert page.events == [] and page.next_after == 1


def test_the_a2a_page_carries_no_customer_identifier(ddb):
    trace.emit_a2a(AVA, "size_confirmed", {"size_request_id": "SZR-1", "order_id": "DEMO-A2A-81B20E",
                                           "next_step": "for DEMO-CUST-AVA pick up"}, actor="aldo_gateway")
    page = trace.list_a2a_events(AVA)
    assert page.run_id == "A2A"
    assert "DEMO-CUST" not in page.model_dump_json()


def test_emit_a2a_safe_logs_instead_of_raising(ddb, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("table on fire")

    monkeypatch.setattr(trace, "emit_a2a", boom)
    assert trace.emit_a2a_safe(AVA, "size_confirmed", {}, actor="aldo_gateway") is None


def test_reset_clears_the_a2a_streams(ddb):
    fixtures.seed("default")
    trace.emit_a2a(AVA, "size_assist_requested", {"size_request_id": "SZR-1"}, actor="personal_agent")
    trace.emit_a2a(BEN, "size_assist_requested", {"size_request_id": "SZR-2"}, actor="personal_agent")
    fixtures.reset("default")
    assert store.query(f"RUN#{trace.a2a_run_id(AVA)}") == []
    assert trace.list_a2a_events(AVA).events == [] and trace.list_a2a_events(BEN).events == []
    trace.emit_a2a(AVA, "size_assist_requested", {"size_request_id": "SZR-3"}, actor="personal_agent")
    assert trace.list_a2a_events(AVA).events[0]["seq"] == 1  # a fresh stream starts at 1 again


def test_run_streams_accept_pivot_types_with_actors_too(ddb):
    """The ALDO run and the personal-agent run share emit(); the six types are valid there as well."""
    from returns_domain import runs

    runs.create_run(AVA, "CONV-1", "RUN-1", "customer")
    for event_type in ("size_assist_requested", "fit_signal_shared", "size_requested", "size_recommended",
                       "size_confirmed", "memory_updated"):
        trace.emit("RUN-1", event_type, {"size_request_id": "SZR-1"}, actor="aldo_gateway")
    assert [e["type"] for e in trace.list_events("RUN-1").events][-1] == "memory_updated"
