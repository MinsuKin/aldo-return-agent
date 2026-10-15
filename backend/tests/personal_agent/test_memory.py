"""The personal-agent store on moto: seed/reset, ALDO write-backs, conversations, leases, runs, events."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any

import pytest

from personal_agent import a2a, memory
from personal_agent.memory import MemoryRepo
from returns_domain import clock
from returns_domain.errors import DomainError

AVA = "DEMO-CUST-AVA"
BEN = "DEMO-CUST-BEN"


def update(update_id: str = "MU-5C0A11", **overrides: Any) -> dict[str, Any]:
    body = {
        "update_id": update_id,
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


def final_update(update_id: str = "MU-FINAL1", **overrides: Any) -> dict[str, Any]:
    return update(
        update_id,
        outcome="exchange_completed_kept",
        provisional=False,
        ts="2026-10-17T15:00:00+00:00",
        **overrides,
    )


def all_items(repo: MemoryRepo) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in repo.table.meta.client.get_paginator("scan").paginate(TableName=repo.table_name):
        items.extend(page["Items"])
    return items


# --------------------------------------------------------------------------- seed and reset


def test_seed_loads_the_fixture(repo: MemoryRepo) -> None:
    ava = repo.get_owner_memory(AVA)
    assert (ava.display_name, ava.size_system, ava.typical_size, ava.source) == ("Ava", "US_W", "7.5", "synthetic")
    assert [(m.memory_id, m.size, m.outcome) for m in ava.memory] == [
        ("PM-1", "7.5", "kept"),
        ("PM-2", "7.5", "kept"),
        ("PM-3", "8.0", "returned_too_big"),
    ]
    assert len(repo.get_owner_memory(BEN).memory) == 1
    assert len(repo.get_owner_memory("DEMO-CUST-CHLOE").memory) == 1
    assert ava.updates == [] and ava.label == "Synthetic demo memory"


def test_seed_is_repeatable_and_returns_counts(repo: MemoryRepo) -> None:
    assert repo.seed() == {"owners": 3, "entries": 5}
    assert len(all_items(repo)) == 8


def test_reset_restores_the_fixture_and_deletes_everything_else(repo: MemoryRepo) -> None:
    repo.apply_update(update())
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-AAA")
    repo.emit(AVA, "PRUN-AAA", "run_started", {})
    repo._update(f"OWNER#{AVA}", "MEM#PM-1", "SET #s = :v", names={"#s": "size"}, values={":v": "9.0"})
    summary = repo.reset()
    assert summary["owners"] == 3 and summary["entries"] == 5 and summary["deleted"] >= 8
    assert len(all_items(repo)) == 8
    ava = repo.get_owner_memory(AVA)
    assert ava.updates == [] and ava.memory[0].size == "7.5"
    with pytest.raises(DomainError):
        repo.get_conversation(AVA, conv)


def test_reset_with_an_unusable_fixture_deletes_nothing(repo: MemoryRepo, tmp_path: Any) -> None:
    repo.apply_update(update())
    before = all_items(repo)
    broken = tmp_path / "memory.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        repo.reset(broken)
    assert caught.value.code == "INTERNAL_ERROR"
    assert all_items(repo) == before


def test_module_level_names_of_the_contract_exist(repo: MemoryRepo) -> None:
    assert memory.apply_memory_update is memory.apply_update
    assert memory.reset() == memory.reset_memory()
    assert memory.seed_memory() == 3
    assert memory.get_owner_memory(AVA).typical_size == "7.5"
    assert memory.list_memory(AVA)["owner_id"] == AVA


def test_unknown_owner_is_not_found(repo: MemoryRepo) -> None:
    with pytest.raises(DomainError) as caught:
        repo.get_owner_memory("DEMO-CUST-NOBODY")
    assert caught.value.code == "NOT_FOUND"


def test_recall_view_filters_by_retailer_and_never_includes_ids(repo: MemoryRepo) -> None:
    full = repo.recall_size_memory(AVA)
    assert full["typical_size"] == "7.5" and full["size_system"] == "US_W"
    assert len(full["entries"]) == 3 and full["aldo_updates"] == [] and full["note"] == "synthetic demo memory"
    only_c = repo.recall_size_memory(AVA, "retailer c")
    assert [e["memory_id"] for e in only_c["entries"]] == ["PM-3"]
    assert AVA not in json.dumps(full)


# ------------------------------------------------------------------------------ write-backs


def test_apply_update_idempotent(repo: MemoryRepo) -> None:
    first = repo.apply_update(update())
    assert first["applied"] is True and first["duplicate"] is False and first["provisional"] is True
    items_after_first = all_items(repo)
    second = repo.apply_update(update())
    assert second["applied"] is False and second["duplicate"] is True and second["update_id"] == "MU-5C0A11"
    assert all_items(repo) == items_after_first  # the conditional put wrote nothing
    updates = repo.get_owner_memory(AVA).updates
    assert [u.update_id for u in updates] == ["MU-5C0A11"]


def test_a_replay_with_other_content_does_not_overwrite_the_first(repo: MemoryRepo) -> None:
    repo.apply_update(update())
    replay = repo.apply_update(update(new_size="9.0"))
    assert replay["duplicate"] is True
    assert repo.get_owner_memory(AVA).updates[0].new_size == "8.5"


def test_provisional_updates_are_stored_with_lower_confidence(repo: MemoryRepo) -> None:
    applied = repo.apply_update(update())["update"]
    assert (applied["provisional"], applied["confidence"], applied["status"]) == (True, "provisional", "active")
    assert (applied["original_size"], applied["new_size"], applied["reason"]) == ("8.0", "8.5", "TOO_SMALL")
    final = repo.apply_update(final_update(order_id="DEMO-A2A-OTHER"))["update"]
    assert (final["provisional"], final["confidence"]) == (False, "confirmed")


def test_provisional_update_is_superseded_by_a_later_final_one_for_the_same_order(repo: MemoryRepo) -> None:
    repo.apply_update(update())
    result = repo.apply_update(final_update())
    assert result["applied"] is True and result["provisional"] is False
    active = repo.get_owner_memory(AVA).updates
    assert [(u.update_id, u.confidence) for u in active] == [("MU-FINAL1", "confirmed")]
    everything = {u.update_id: u for u in repo.get_owner_memory(AVA, include_superseded=True).updates}
    assert everything["MU-5C0A11"].status == "superseded"
    assert everything["MU-5C0A11"].superseded_by == "MU-FINAL1"
    assert everything["MU-FINAL1"].status == "active" and everything["MU-FINAL1"].superseded_by is None


def test_a_late_provisional_update_never_overrides_a_final_one(repo: MemoryRepo) -> None:
    repo.apply_update(final_update())
    late = repo.apply_update(update())
    assert late["applied"] is True
    assert [u.update_id for u in repo.get_owner_memory(AVA).updates] == ["MU-FINAL1"]
    stored = {u.update_id: u for u in repo.get_owner_memory(AVA, include_superseded=True).updates}
    assert stored["MU-5C0A11"].status == "superseded"


def test_updates_of_different_orders_do_not_supersede_each_other(repo: MemoryRepo) -> None:
    repo.apply_update(update())
    repo.apply_update(final_update("MU-OTHER1", order_id="DEMO-A2A-OTHER"))
    assert {u.update_id for u in repo.get_owner_memory(AVA).updates} == {"MU-5C0A11", "MU-OTHER1"}


def test_the_newest_provisional_update_of_an_order_wins(repo: MemoryRepo) -> None:
    repo.apply_update(update("MU-OLD001", ts="2026-10-16T16:05:00+00:00"))
    repo.apply_update(update("MU-NEW001", new_size="9.0", ts="2026-10-16T17:00:00+00:00"))
    assert [(u.update_id, u.new_size) for u in repo.get_owner_memory(AVA).updates] == [("MU-NEW001", "9.0")]


@pytest.mark.parametrize(
    "bad",
    [
        {"original_size": "8"},
        {"new_size": "eight"},
        {"reason": "too small"},
        {"outcome": "exchange_refunded"},
        {"provisional": False},  # exchange_created is always provisional
        {"provisional": "true"},
        {"update_id": "bad id with spaces"},
        {"ts": "yesterday"},
        {"product_name": ""},
        {"owner_hint": "7.5"},  # unknown field
    ],
)
def test_invalid_updates_are_rejected_and_write_nothing(repo: MemoryRepo, bad: dict[str, Any]) -> None:
    before = all_items(repo)
    with pytest.raises(DomainError) as caught:
        repo.apply_update(update(**bad))
    assert caught.value.code == "VALIDATION_ERROR"
    assert all_items(repo) == before


def test_final_update_with_provisional_true_is_rejected(repo: MemoryRepo) -> None:
    with pytest.raises(DomainError) as caught:
        repo.apply_update(update(outcome="exchange_completed_kept", provisional=True))
    assert caught.value.code == "VALIDATION_ERROR"


def test_missing_field_and_non_object_bodies_are_rejected(repo: MemoryRepo) -> None:
    body = update()
    del body["new_size"]
    for raw in (body, [1, 2], "text", None):
        with pytest.raises(DomainError) as caught:
            repo.apply_update(raw)
        assert caught.value.code == "VALIDATION_ERROR"


def test_update_for_an_unknown_owner_is_not_found_and_creates_no_partition(repo: MemoryRepo) -> None:
    before = all_items(repo)
    with pytest.raises(DomainError) as caught:
        repo.apply_update(update(owner_ref="DEMO-CUST-NOBODY"))
    assert caught.value.code == "NOT_FOUND"
    assert all_items(repo) == before


def test_a2a_event_is_off_by_default_and_public_only_when_enabled(
    repo: MemoryRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[Any, ...]] = []
    a2a.set_a2a_emitter(lambda owner, kind, payload, *, actor: sent.append((owner, kind, payload, actor)))
    repo.apply_update(update("MU-OFF001"))
    assert sent == []  # ALDO's outbox emits memory_updated exactly once (contracts F.5)
    repo.apply_update(update("MU-ON0001", order_id="DEMO-A2A-TWO"), emit_a2a=True)
    assert len(sent) == 1
    owner, kind, payload, actor = sent[0]
    assert (owner, kind, actor) == (AVA, "memory_updated", "personal_agent")
    assert set(payload) == {
        "update_id", "style_id", "product_name", "color", "original_size", "new_size",
        "reason", "outcome", "provisional",
    }
    repo.apply_update(update("MU-ON0001", order_id="DEMO-A2A-TWO"), emit_a2a=True)
    assert len(sent) == 1  # a replay never re-emits
    monkeypatch.setenv("PERSONAL_AGENT_EMIT_A2A", "1")
    repo.apply_update(update("MU-ENV0001", order_id="DEMO-A2A-THREE"))
    assert len(sent) == 2


def test_a_failing_a2a_emitter_never_fails_the_write_back(repo: MemoryRepo) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("stream down")

    a2a.set_a2a_emitter(broken)
    assert repo.apply_update(update(), emit_a2a=True)["applied"] is True


def test_only_strings_ints_and_booleans_are_stored(repo: MemoryRepo) -> None:
    repo.apply_update(update())
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-TYPES")
    repo.emit(AVA, "PRUN-TYPES", "tool_result", {"evidence": {"too_small_share": 0.799, "n_fit": 582}})

    def check(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                check(item)
        else:
            assert isinstance(value, (str, bool, int, Decimal)), type(value)
            if isinstance(value, Decimal):
                assert value == value.to_integral_value()  # no fractional numbers reach DynamoDB

    for item in all_items(repo):
        check(item)


# ------------------------------------------------------------------------------ conversations


def tool_turn(call_id: str = "t1", text: str = "ok") -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": [{"text": "Order me the Stessy"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": call_id, "name": "recall_size_memory", "input": {}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": call_id, "status": "success", "content": [{"text": "{}"}]}}]},
        {"role": "assistant", "content": [{"text": text}]},
    ]


def test_messages_round_trip_and_start_empty(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    assert repo.load_messages(AVA, conv) == []
    turn = tool_turn()
    assert repo.save_messages(AVA, conv, turn) > 0
    assert repo.load_messages(AVA, conv) == turn


def test_saving_strips_reasoning_and_makes_bytes_json_safe(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    turn = tool_turn()
    turn[3]["content"].insert(0, {"reasoningContent": {"reasoningText": {"text": "hidden"}}})
    turn[1]["content"][0]["toolUse"]["input"] = {"size": 7.5, "blob": b"\x00\x01"}
    repo.save_messages(AVA, conv, turn)
    stored = repo.load_messages(AVA, conv)
    assert "reasoningContent" not in json.dumps(stored)
    assert stored[1]["content"][0]["toolUse"]["input"] == {"size": 7.5, "blob": base64.b64encode(b"\x00\x01").decode()}


def test_an_unanswered_tool_use_is_dropped_on_save_and_on_load(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    complete = tool_turn("t1", "first answer")
    dangling = [
        {"role": "user", "content": [{"text": "second question"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t2", "name": "ask_aldo_size_assist", "input": {}}}]},
    ]
    repo.save_messages(AVA, conv, complete + dangling)
    assert repo.load_messages(AVA, conv) == complete
    assert repo.save_messages(AVA, conv, dangling) == 0  # no complete turn: nothing written
    assert repo.load_messages(AVA, conv) == complete


def test_a_stored_dangling_history_is_repaired_on_load(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    broken = tool_turn()[:3]  # ends with a toolResult user message
    repo._update(
        f"OWNER#{AVA}", f"PCONV#{conv}", "SET #m = :m", names={"#m": "messages"}, values={":m": json.dumps(broken)}
    )
    assert repo.load_messages(AVA, conv) == []


def test_a_450_kb_conversation_is_trimmed_under_the_cap_without_splitting_tool_pairs(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    history: list[dict[str, Any]] = []
    for index in range(30):
        turn = tool_turn(f"call{index}", f"answer {index}")
        turn[2]["content"][0]["toolResult"]["content"][0]["text"] = "x" * 15_000  # 15 KB per result
        history.extend(turn)
    assert len(json.dumps(history)) > 450_000
    size = repo.save_messages(AVA, conv, history)
    assert 0 < size <= 300_000
    stored = repo.load_messages(AVA, conv)
    assert stored and stored[0]["role"] == "user" and stored[-1]["role"] == "assistant"
    assert stored[-1]["content"][0]["text"] == "answer 29"  # the newest turn survives
    uses = {b["toolUse"]["toolUseId"] for m in stored for b in m["content"] if "toolUse" in b}
    results = {b["toolResult"]["toolUseId"] for m in stored for b in m["content"] if "toolResult" in b}
    assert uses == results
    assert all(
        len(b["toolResult"]["content"][0]["text"]) <= 8_100
        for m in stored
        for b in m["content"]
        if "toolResult" in b
    )


def test_conversations_are_private_to_their_owner(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    with pytest.raises(DomainError) as caught:
        repo.get_conversation(BEN, conv)
    assert caught.value.code == "NOT_FOUND"
    with pytest.raises(DomainError):
        repo.load_messages(BEN, conv)


# ------------------------------------------------------------------------------------ leases


def test_one_active_run_per_conversation(repo: MemoryRepo, fixed_now: Any) -> None:
    conv = repo.create_conversation(AVA)
    other = repo.create_conversation(AVA)
    assert repo.try_acquire_lease(AVA, conv, "PRUN-1") is True
    assert repo.try_acquire_lease(AVA, conv, "PRUN-2") is False
    assert repo.try_acquire_lease(AVA, conv, "PRUN-1") is True  # the holder may renew
    assert repo.try_acquire_lease(AVA, other, "PRUN-2") is True  # other conversations are independent
    assert repo.lease_active(AVA, conv) is True


def test_a_lease_expires_and_can_be_released(repo: MemoryRepo, fixed_now: Any) -> None:
    conv = repo.create_conversation(AVA)
    assert repo.try_acquire_lease(AVA, conv, "PRUN-1", ttl_seconds=330)
    fixed_now.advance(seconds=331)
    assert repo.lease_active(AVA, conv) is False
    assert repo.try_acquire_lease(AVA, conv, "PRUN-2") is True
    repo.release_lease(AVA, conv, "PRUN-1")  # not the holder: no effect
    assert repo.lease_active(AVA, conv) is True
    repo.release_lease(AVA, conv, "PRUN-2")
    assert repo.lease_active(AVA, conv) is False


def test_lease_on_a_missing_conversation_is_not_found(repo: MemoryRepo) -> None:
    with pytest.raises(DomainError) as caught:
        repo.try_acquire_lease(AVA, "PCONV-MISSING", "PRUN-1")
    assert caught.value.code == "NOT_FOUND"


# ------------------------------------------------------------------------------ runs and events


def test_run_state_machine_is_conditional(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    assert repo.create_run(AVA, conv, "PRUN-1") is True
    assert repo.create_run(AVA, conv, "PRUN-1") is False
    assert repo.get_run(AVA, "PRUN-1")["status"] == "queued"
    assert repo.mark_started(AVA, "PRUN-1") is True
    assert repo.mark_started(AVA, "PRUN-1") is False  # the once-only guard
    assert repo.claim_run(AVA, "PRUN-1") is False  # already running
    assert repo.get_run(AVA, "PRUN-1")["status"] == "running"
    assert repo.finish_run(AVA, "PRUN-1", "completed", {"rounds": 3}) is True
    assert repo.finish_run(AVA, "PRUN-1", "error", {}) is False  # only the first finisher wins
    assert repo.get_run(AVA, "PRUN-1")["status"] == "completed"
    assert repo.mark_started(AVA, "PRUN-1") is False  # finished runs never restart


def test_mark_started_also_works_after_a_worker_claim(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-2")
    assert repo.claim_run(AVA, "PRUN-2") is True
    assert repo.mark_started(AVA, "PRUN-2") is True
    assert repo.mark_started(AVA, "PRUN-2") is False


def test_events_are_ordered_and_run_completed_is_last_and_unique(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-3")
    repo.mark_started(AVA, "PRUN-3")
    repo.emit(AVA, "PRUN-3", "run_started", {"conv_id": conv})
    repo.emit(AVA, "PRUN-3", "tool_call", {"tool": "recall_size_memory", "args": {}, "round": 1})
    repo.emit(AVA, "PRUN-3", "assistant_message", {"text": "done"})
    assert repo.finish_run(AVA, "PRUN-3", "completed", {"tool_count": 1, "input_tokens": 120, "output_tokens": 30})
    assert repo.finish_run(AVA, "PRUN-3", "completed", {}) is False
    page = repo.list_events(AVA, "PRUN-3")
    assert [e["seq"] for e in page["events"]] == [1, 2, 3, 4]
    assert [e["type"] for e in page["events"]] == ["run_started", "tool_call", "assistant_message", "run_completed"]
    assert page["status"] == "completed" and page["next_after"] == 4
    assert [e["actor"] for e in page["events"]] == ["personal_agent"] * 3 + ["system"]
    completed = page["events"][-1]["payload"]
    assert completed["status"] == "completed" and completed["input_tokens"] == 120  # counts survive redaction


def test_after_cursor_returns_only_newer_events(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-4")
    for index in range(5):
        repo.emit(AVA, "PRUN-4", "warning", {"code": f"W{index}", "message": "m"})
    page = repo.list_events(AVA, "PRUN-4", after=3)
    assert [e["seq"] for e in page["events"]] == [4, 5] and page["next_after"] == 5
    empty = repo.list_events(AVA, "PRUN-4", after=5)
    assert empty["events"] == [] and empty["next_after"] == 5


def test_a_gap_in_the_sequence_stops_the_page_so_a_cursor_never_skips(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-5")
    repo.emit(AVA, "PRUN-5", "run_started", {})
    repo._update(f"OWNER#{AVA}", "PRUN#PRUN-5#META", "ADD #seq :one", names={"#seq": "seq"}, values={":one": 1})  # seq 2 reserved, not written
    repo.emit(AVA, "PRUN-5", "tool_call", {"tool": "x"})  # seq 3
    page = repo.list_events(AVA, "PRUN-5")
    assert [e["seq"] for e in page["events"]] == [1] and page["next_after"] == 1


def test_event_payloads_are_redacted_and_capped(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-6")
    repo.emit(
        AVA,
        "PRUN-6",
        "tool_call",
        {"tool": "x", "args": {"token": "SECRET-1", "Authorization": "Bearer SECRET-2", "customer_id": AVA, "owner_id": AVA, "note": "n" * 900}},
    )
    repo.emit(AVA, "PRUN-6", "assistant_message", {"text": "w" * 1400})
    events = repo.list_events(AVA, "PRUN-6")["events"]
    dumped = json.dumps(events)
    assert "SECRET" not in dumped and AVA not in dumped
    assert len(events[0]["payload"]["args"]["note"]) == 500
    assert len(events[1]["payload"]["text"]) == 1400  # assistant messages keep up to 1500


def test_emit_validates_type_actor_and_run(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-7")
    with pytest.raises(DomainError):
        repo.emit(AVA, "PRUN-7", "size_confirmed", {})  # a gateway event type, not ours
    with pytest.raises(DomainError):
        repo.emit(AVA, "PRUN-7", "warning", {}, actor="aldo_gateway")
    with pytest.raises(DomainError) as caught:
        repo.emit(AVA, "PRUN-MISSING", "warning", {})
    assert caught.value.code == "NOT_FOUND"


def test_runs_and_events_are_private_to_their_owner(repo: MemoryRepo) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-8")
    repo.emit(AVA, "PRUN-8", "run_started", {})
    with pytest.raises(DomainError) as caught:
        repo.list_events(BEN, "PRUN-8")
    assert caught.value.code == "NOT_FOUND"
    assert memory.list_run_events is not None and memory.is_personal_run_id("PRUN-8")
    assert not memory.is_personal_run_id("RUN-8")


def test_event_timestamps_use_the_wall_clock_helper(repo: MemoryRepo, fixed_now: Any) -> None:
    conv = repo.create_conversation(AVA)
    repo.create_run(AVA, conv, "PRUN-9")
    repo.emit(AVA, "PRUN-9", "run_started", {})
    assert repo.list_events(AVA, "PRUN-9")["events"][0]["ts"] == clock.iso(fixed_now())
