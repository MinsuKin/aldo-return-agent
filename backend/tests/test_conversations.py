"""conversations.py: ownership, session ids and message persistence (G-12)."""

from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from returns_domain import audit, conversations, store
from returns_domain.errors import DomainError, ErrorCode

AVA, BEN = "DEMO-CUST-AVA", "DEMO-CUST-BEN"


def user(text):
    return {"role": "user", "content": [{"text": text}]}


def assistant(text):
    return {"role": "assistant", "content": [{"text": text}]}


def tool_use(tool_use_id="tu1", **tool_input):
    return {"role": "assistant", "content": [{"toolUse": {"toolUseId": tool_use_id, "name": "get_order_context",
                                                          "input": tool_input or {"order_id": "DEMO-1001"}}}]}


def tool_result(tool_use_id="tu1", text="ok", json_body=None):
    content = [{"json": json_body}] if json_body is not None else [{"text": text}]
    return {"role": "user", "content": [{"toolResult": {"toolUseId": tool_use_id, "status": "success", "content": content}}]}


def stored_text(conv_id):
    return store.get_item(store.conv_key(conv_id))["messages"]


def pairing_ok(messages):
    """Every toolUse is answered by a toolResult in the next message, and vice versa."""
    uses, results = [], []
    for i, m in enumerate(messages):
        for block in m["content"]:
            if "toolUse" in block:
                uses.append((i, block["toolUse"]["toolUseId"]))
            if "toolResult" in block:
                results.append((i, block["toolResult"]["toolUseId"]))
    return all((i + 1, tid) in results for i, tid in uses) and all((i - 1, tid) in uses for i, tid in results)


# -------------------------------------------------------------------- create / get / set


def test_create_conversation_stores_a_fresh_session(ddb):
    one, two = conversations.create_conversation(AVA), conversations.create_conversation(AVA)
    assert one != two and one.startswith("CONV-") and len(one) == len("CONV-") + 10
    item = store.get_item(store.conv_key(one))
    assert item["customer_id"] == AVA and item["selected_order_id"] is None and item["latest_quote_id"] is None
    assert item["runtime_session_id"].startswith("conv-") and len(item["runtime_session_id"]) == 45  # >= 33 for AgentCore
    assert item["runtime_session_id"] != store.get_item(store.conv_key(two))["runtime_session_id"]
    assert item["messages"] == "[]"
    assert "active_run_id" not in item


def test_get_conversation_returns_the_four_documented_fields(ddb):
    conv = conversations.create_conversation(AVA)
    got = conversations.get_conversation(AVA, conv)
    assert set(got) == {"conv_id", "selected_order_id", "runtime_session_id", "latest_quote_id"}
    assert got["conv_id"] == conv and got["runtime_session_id"].startswith("conv-")


def test_get_conversation_ownership_and_missing(ddb):
    conv = conversations.create_conversation(AVA)
    with pytest.raises(DomainError) as exc:
        conversations.get_conversation(BEN, conv)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    denied = audit.list_audit(action="access_denied")
    assert [(d["actor"], d["refs"]) for d in denied] == [(BEN, {"resource_type": "conversation", "resource_id": conv})]
    with pytest.raises(DomainError) as exc:
        conversations.get_conversation(AVA, "CONV-MISSING")
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_set_selected_order(ddb):
    conv = conversations.create_conversation(AVA)
    conversations.set_selected_order(conv, "DEMO-1001")
    assert conversations.get_conversation(AVA, conv)["selected_order_id"] == "DEMO-1001"
    conversations.set_selected_order(conv, None)
    assert conversations.get_conversation(AVA, conv)["selected_order_id"] is None
    with pytest.raises(DomainError) as exc:
        conversations.set_selected_order("CONV-MISSING", "DEMO-1001")
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert store.get_item(store.conv_key("CONV-MISSING")) is None


# ------------------------------------------------------------------------ save / load


def test_messages_round_trip_as_a_json_string(ddb):
    conv = conversations.create_conversation(AVA)
    assert conversations.load_messages(conv) == []
    history = [user("hi"), tool_use(replacement_size=7.5), tool_result(json_body={"cents": 12999, "share": 0.799}),
               assistant("done")]
    conversations.save_messages(conv, history)
    assert isinstance(stored_text(conv), str)
    loaded = conversations.load_messages(conv)
    assert loaded == history  # floats survive because the list is stored as JSON text, not as DynamoDB numbers
    assert loaded[1]["content"][0]["toolUse"]["input"]["replacement_size"] == 7.5


def test_load_messages_for_a_missing_or_empty_conversation_is_empty(ddb):
    assert conversations.load_messages("CONV-MISSING") == []
    conv = conversations.create_conversation(AVA)
    store.update_item(store.conv_key(conv), set={"messages": ""})
    assert conversations.load_messages(conv) == []
    store.update_item(store.conv_key(conv), set={"messages": "{not json"})
    assert conversations.load_messages(conv) == []
    store.update_item(store.conv_key(conv), set={"messages": '{"a": 1}'})
    assert conversations.load_messages(conv) == []


def test_save_to_a_missing_conversation_is_not_found_and_creates_nothing(ddb):
    with pytest.raises(DomainError) as exc:
        conversations.save_messages("CONV-GHOST", [user("hi"), assistant("yo")])
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert store.get_item(store.conv_key("CONV-GHOST")) is None


def test_save_keeps_the_conversation_fields_and_the_lease(ddb):
    from returns_domain import runs

    conv = conversations.create_conversation(AVA)
    runs.try_acquire_lease(conv, "RUN-1")
    conversations.set_selected_order(conv, "DEMO-1001")
    conversations.save_messages(conv, [user("hi"), assistant("yo")])
    item = store.get_item(store.conv_key(conv))
    assert item["active_run_id"] == "RUN-1" and item["selected_order_id"] == "DEMO-1001" and item["customer_id"] == AVA


# --------------------------------------------------------------- strip_reasoning (G-12)


def test_strips_reasoning(ddb):
    thinking = {"role": "assistant", "content": [
        {"reasoningContent": {"reasoningText": {"text": "hmm", "signature": "sig"}}},
        {"reasoningContent": {"redactedContent": b"\x00\x01\x02"}},
        {"text": "answer"},
    ]}
    original = copy.deepcopy([user("q"), thinking])
    cleaned = conversations.strip_reasoning([user("q"), thinking])
    assert cleaned[1]["content"] == [{"text": "answer"}]
    json.dumps(cleaned)  # serializable
    assert [user("q"), thinking] == original  # the input is not mutated


def test_strip_reasoning_converts_bytes_decimals_and_keeps_other_blocks(ddb):
    message = {"role": "user", "content": [{"image": {"format": "png", "source": {"bytes": b"hi"}}},
                                           {"toolResult": {"toolUseId": "t", "status": "success",
                                                           "content": [{"json": {"n": Decimal("7.5"), "k": Decimal("3")}}]}}]}
    cleaned = conversations.strip_reasoning([message])[0]
    assert cleaned["content"][0]["image"]["source"]["bytes"] == "aGk="
    assert cleaned["content"][1]["toolResult"]["content"][0]["json"] == {"n": 7.5, "k": 3}
    json.dumps(cleaned)


def test_a_message_that_was_only_reasoning_keeps_a_placeholder(ddb):
    only = {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "x"}}}]}
    cleaned = conversations.strip_reasoning([only])
    assert cleaned[0]["content"] == [{"text": "(no content)"}] and cleaned[0]["role"] == "assistant"


def test_save_strips_reasoning_before_storing(ddb):
    conv = conversations.create_conversation(AVA)
    thinking = {"role": "assistant", "content": [{"reasoningContent": {"reasoningText": {"text": "secret thoughts"}}}, {"text": "hello"}]}
    conversations.save_messages(conv, [user("hi"), thinking])
    assert "secret thoughts" not in stored_text(conv) and "reasoningContent" not in stored_text(conv)
    assert conversations.load_messages(conv)[1]["content"] == [{"text": "hello"}]


# ------------------------------------------------------------- truncation and trimming


def test_oversized_tool_results_are_truncated_to_8kb(ddb):
    conv = conversations.create_conversation(AVA)
    big_text, big_json = "A" * 50_000, {"rows": ["B" * 100] * 400}
    conversations.save_messages(conv, [user("q"), tool_use("a"), tool_result("a", text=big_text),
                                       tool_use("b"), tool_result("b", json_body=big_json), assistant("done")])
    loaded = conversations.load_messages(conv)
    first = loaded[2]["content"][0]["toolResult"]["content"][0]["text"]
    second = loaded[4]["content"][0]["toolResult"]["content"][0]["text"]
    assert len(first) <= 8192 + len("…[truncated]") and first.endswith("…[truncated]")
    assert len(second) <= 8192 + len("…[truncated]") and second.startswith('{"rows"')
    small = tool_result("c", text="short")
    conversations.save_messages(conv, [user("q"), tool_use("c"), small, assistant("done")])
    assert conversations.load_messages(conv)[2] == small


def test_450kb_conversation(ddb):
    """A 450 KB history is trimmed to 300 KB by whole turns; the newest turn survives intact."""
    conv = conversations.create_conversation(AVA)
    turns = []
    for n in range(45):
        turns += [user(f"turn {n} " + "u" * 6_000), tool_use(f"tu{n}", order_id="DEMO-1001"),
                  tool_result(f"tu{n}", text="r" * 3_000), assistant(f"answer {n} " + "a" * 1_000)]
    raw = len(json.dumps(turns).encode())
    assert raw > 450_000
    conversations.save_messages(conv, turns)
    text = stored_text(conv)
    assert len(text.encode()) <= 300_000
    loaded = conversations.load_messages(conv)
    assert 20 < len(loaded) < len(turns)
    assert loaded[0]["role"] == "user" and "toolResult" not in loaded[0]["content"][0]  # starts at a turn boundary
    assert loaded[-1] == turns[-1] and loaded[-4:] == turns[-4:]  # the newest turn is intact
    assert pairing_ok(loaded)  # toolUse / toolResult pairing preserved
    assert loaded[0]["content"][0]["text"].startswith("turn ") and f"turn {45 - len(loaded) // 4}" in loaded[0]["content"][0]["text"]


def test_the_newest_turn_is_kept_even_when_it_alone_is_over_the_limit(ddb):
    conv = conversations.create_conversation(AVA)
    history = [user("old"), assistant("old answer"), user("new")]
    for n in range(60):  # one turn with 60 tool round trips of 8 KB each
        history += [tool_use(f"t{n}"), tool_result(f"t{n}", text="z" * 20_000)]
    history.append(assistant("final"))
    conversations.save_messages(conv, history)
    assert len(stored_text(conv).encode()) <= 300_000
    loaded = conversations.load_messages(conv)
    assert loaded[0] == user("new") and loaded[-1] == assistant("final") and pairing_ok(loaded[1:-1])


def test_a_conversation_that_cannot_fit_raises_instead_of_storing_garbage(ddb):
    conv = conversations.create_conversation(AVA)
    history = [user("u" * 500_000), assistant("a")]
    with pytest.raises(DomainError) as exc:
        conversations.save_messages(conv, history)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert stored_text(conv) == "[]"


# ---------------------------------------------------------------------- clean boundary


def test_orphan_tool_use(ddb):
    """A run that died after a toolUse leaves nothing the next run cannot continue from."""
    conv = conversations.create_conversation(AVA)
    conversations.save_messages(conv, [user("hi"), assistant("hello"), user("order?"), tool_use("t1")])
    assert conversations.load_messages(conv) == [user("hi"), assistant("hello")]


def test_trailing_tool_result_without_an_answer_is_dropped_with_its_turn(ddb):
    conv = conversations.create_conversation(AVA)
    conversations.save_messages(conv, [user("hi"), assistant("hello"), user("order?"), tool_use("t1"), tool_result("t1")])
    assert conversations.load_messages(conv) == [user("hi"), assistant("hello")]


def test_trailing_unanswered_user_message_is_dropped(ddb):
    conv = conversations.create_conversation(AVA)
    conversations.save_messages(conv, [user("hi"), assistant("hello"), user("again")])
    assert conversations.load_messages(conv) == [user("hi"), assistant("hello")]


def test_history_with_no_clean_boundary_loads_empty(ddb):
    conv = conversations.create_conversation(AVA)
    conversations.save_messages(conv, [user("hi"), tool_use("t1")])
    assert conversations.load_messages(conv) == []


def test_complete_tool_turns_load_unchanged(ddb):
    conv = conversations.create_conversation(AVA)
    history = [user("hi"), tool_use("t1"), tool_result("t1"), tool_use("t2"), tool_result("t2"), assistant("done")]
    conversations.save_messages(conv, history)
    assert conversations.load_messages(conv) == history


def test_assistant_message_with_text_and_tool_use_is_not_a_boundary(ddb):
    conv = conversations.create_conversation(AVA)
    mixed = {"role": "assistant", "content": [{"text": "let me check"}, {"toolUse": {"toolUseId": "t1", "name": "x", "input": {}}}]}
    conversations.save_messages(conv, [user("hi"), assistant("hello"), user("q"), mixed])
    assert conversations.load_messages(conv) == [user("hi"), assistant("hello")]
