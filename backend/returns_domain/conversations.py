"""Conversations: the persisted Strands message list and the AgentCore session id (G-12).

``CONV#{conv}/META`` holds ``customer_id``, ``selected_order_id``, ``runtime_session_id``,
``latest_quote_id`` and ``messages``, a JSON STRING of the Strands message list. Saving is
defensive because the item limit is 400 KB and models return blocks that do not serialize:

* ``strip_reasoning``: drop ``reasoningContent`` blocks, turn bytes into base64 text, make every
  value JSON-native (Decimal, tuples, unknown objects).
* any ``toolResult`` text over 8 KB is truncated;
* the oldest completed turns (a user message plus everything up to the next user message,
  keeping each ``toolUse`` with its ``toolResult``) are dropped while the JSON is over 300 KB.

Loading returns the history up to the last assistant message that ends a turn with text, so a
run that died between a ``toolUse`` and its ``toolResult`` can never poison the next one.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
from decimal import Decimal
from typing import Any, Mapping

from . import audit, config, store
from .errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

TOOL_RESULT_TEXT_CAP = 8 * 1024
TIGHT_TOOL_RESULT_TEXT_CAP = 2 * 1024
ITEM_HARD_LIMIT_BYTES = 380_000
_TRUNCATION_MARK = "…[truncated]"
_EMPTY_ASSISTANT_TEXT = "(no content)"


def create_conversation(customer_id: str, order_id: str | None = None) -> str:
    """Create a conversation and return its id (``CONV-`` + 10 hex).

    ``order_id`` (optional) must belong to ``customer_id``. The AgentCore session id is
    ``conv-`` + 40 hex characters (45 characters; the service needs at least 33).
    """
    if order_id is not None:
        from . import orders  # local import: orders needs audit and policy, not this module

        orders.get_order(customer_id, order_id)  # ORDER_NOT_FOUND / NOT_AUTHORIZED
    conv_id = store.new_id("CONV-", 10)
    pk, sk = store.conv_key(conv_id)
    store.put_item(
        {
            "pk": pk,
            "sk": sk,
            "conv_id": conv_id,
            "customer_id": customer_id,
            "selected_order_id": order_id,
            "runtime_session_id": "conv-" + secrets.token_hex(20),
            "latest_quote_id": None,
            "messages": "[]",
            "created_at": store.now_iso(),
        },
        condition="attribute_not_exists(#pk)",
    )
    return conv_id


def get_conversation(customer_id: str, conv_id: str) -> dict[str, Any]:
    """``{conv_id, selected_order_id, runtime_session_id, latest_quote_id}`` for the owner."""
    item = store.get_item(store.conv_key(conv_id))
    if item is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Conversation not found.")
    if item.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "conversation", conv_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Conversation not found for this customer.")
    return {
        "conv_id": conv_id,
        "selected_order_id": item.get("selected_order_id"),
        "runtime_session_id": item.get("runtime_session_id"),
        "latest_quote_id": item.get("latest_quote_id"),
    }


def set_selected_order(conv_id: str, order_id: str | None) -> None:
    """Remember the order the customer picked in the UI (the caller already checked ownership)."""
    try:
        store.update_item(
            store.conv_key(conv_id), set={"selected_order_id": order_id}, return_new=False
        )
    except store.ConditionFailed:
        raise DomainError(ErrorCode.NOT_FOUND, "Conversation not found.") from None


# ----------------------------------------------------------------------- load and save


def load_messages(conv_id: str) -> list[dict[str, Any]]:
    """The stored history, cut back to its last clean boundary; ``[]`` when there is none."""
    item = store.get_item(store.conv_key(conv_id))
    raw = (item or {}).get("messages")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        messages = json.loads(raw)
    except ValueError:
        logger.warning("stored conversation is not valid JSON", extra={"conv_id": conv_id})
        return []
    if not isinstance(messages, list):
        return []
    return _clean_boundary([m for m in messages if isinstance(m, dict)])


def save_messages(conv_id: str, messages: list[dict[str, Any]]) -> None:
    """Store ``messages`` after stripping reasoning, truncating tool results and trimming old turns."""
    text = _prepare(messages)
    try:
        store.update_item(
            store.conv_key(conv_id),
            set={"messages": text, "messages_saved_at": store.now_iso()},
            return_new=False,
        )
    except store.ConditionFailed:
        raise DomainError(ErrorCode.NOT_FOUND, "Conversation not found.") from None


def strip_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of ``messages`` without ``reasoningContent`` blocks; JSON-serializable throughout."""
    cleaned: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        copy = {k: _plain(v) for k, v in message.items() if k != "content"}
        if isinstance(content, (list, tuple)):
            blocks = [_plain(b) for b in content if not _is_reasoning(b)]
            if not blocks:  # a message with no content is rejected by the model API
                blocks = [{"text": _EMPTY_ASSISTANT_TEXT}]
            copy["content"] = blocks
        else:
            copy["content"] = _plain(content)
        cleaned.append(copy)
    return cleaned


# --------------------------------------------------------------------------- internals


def _prepare(messages: list[dict[str, Any]]) -> str:
    limit = min(config.get_settings().conversation_max_bytes, ITEM_HARD_LIMIT_BYTES)
    prepared = _truncate_tool_results(strip_reasoning(messages), TOOL_RESULT_TEXT_CAP)
    turns = _turns(prepared)
    text = _dump([m for turn in turns for m in turn])
    while len(text.encode("utf-8")) > limit and len(turns) > 1:
        turns.pop(0)  # oldest completed turn first; the latest turn is always kept
        text = _dump([m for turn in turns for m in turn])
    if len(text.encode("utf-8")) > limit:  # one huge turn: squeeze its tool results harder
        tight = _truncate_tool_results([m for turn in turns for m in turn], TIGHT_TOOL_RESULT_TEXT_CAP)
        text = _dump(tight)
    if len(text.encode("utf-8")) > ITEM_HARD_LIMIT_BYTES:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "The conversation is too large to store.")
    return text


def _dump(messages: list[dict[str, Any]]) -> str:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))


def _is_reasoning(block: Any) -> bool:
    return isinstance(block, Mapping) and "reasoningContent" in block


def _plain(value: Any) -> Any:
    """Recursive JSON-native copy: bytes -> base64 text, Decimal -> number, unknown -> str."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Decimal):
        return store.from_ddb(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    return str(value)


def _truncate_text(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + _TRUNCATION_MARK


def _truncate_tool_results(messages: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            result = block.get("toolResult") if isinstance(block, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("content"), list):
                continue
            shrunk = []
            for part in result["content"]:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part = {**part, "text": _truncate_text(part["text"], cap)}
                elif isinstance(part, dict) and "json" in part:
                    rendered = json.dumps(part["json"], ensure_ascii=False, separators=(",", ":"))
                    if len(rendered) > cap:
                        part = {"text": _truncate_text(rendered, cap)}
                shrunk.append(part)
            result["content"] = shrunk
    return messages


def _has_block(message: Mapping[str, Any], name: str) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and name in b for b in content)


def _starts_turn(message: Mapping[str, Any]) -> bool:
    """A user message that is not purely a tool result starts a new turn."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return True
    return any(not (isinstance(b, dict) and "toolResult" in b) for b in content) or not content


def _turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    for message in messages:
        if _starts_turn(message) or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


def _clean_boundary(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cut the history after its last assistant message that has no ``toolUse`` block.

    That drops a trailing ``toolUse`` without its ``toolResult`` and a trailing user message
    that never got an answer, so the next run starts from a state the model API accepts.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant" and not _has_block(message, "toolUse"):
            return messages[: index + 1]
    return []
