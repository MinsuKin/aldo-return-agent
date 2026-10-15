"""A scripted Strands model: lets tests run the REAL Agent loop (tools, hooks, messages) with no Bedrock.

``ScriptedModel(policy)``: each model call asks ``policy(messages, call_number)`` for the next
action: ``("text", "reply")`` or ``("tools", [(name, input_dict), ...])``. The policy sees the full
message list, so it can read earlier tool results (for example the ``size_request_id``).
"""

from __future__ import annotations

import json
from typing import Any, AsyncGenerator, Callable

from strands.models import Model

Action = tuple[str, Any]
Policy = Callable[[list[dict[str, Any]], int], Action]


def last_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The JSON envelope of the most recent toolResult (what the tool returned to the model)."""
    for message in reversed(messages):
        for block in message.get("content", []):
            result = block.get("toolResult")
            if result:
                return json.loads(result["content"][0]["text"])
    return {}


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for message in messages:
        for block in message.get("content", []):
            result = block.get("toolResult")
            if result:
                found.append(json.loads(result["content"][0]["text"]))
    return found


class ScriptedModel(Model):
    def __init__(self, policy: Policy, *, fail_with: BaseException | None = None) -> None:
        self.policy = policy
        self.fail_with = fail_with
        self.calls = 0
        self.seen_system_prompts: list[str | None] = []
        self.seen_tool_names: list[list[str]] = []
        self.config: dict[str, Any] = {"model_id": "scripted-model"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> Any:
        return self.config

    async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        raise NotImplementedError("structured output is never used")
        yield {}  # pragma: no cover

    async def stream(  # type: ignore[override]
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.calls += 1
        self.seen_system_prompts.append(system_prompt)
        self.seen_tool_names.append([spec["name"] for spec in (tool_specs or [])])
        if self.fail_with is not None:
            raise self.fail_with
        kind, value = self.policy(list(messages), self.calls)
        yield {"messageStart": {"role": "assistant"}}
        if kind == "text":
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": value}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            for index, (name, arguments) in enumerate(value):
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tool-{self.calls}-{index}", "name": name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 1000, "outputTokens": 100, "totalTokens": 1100}, "metrics": {"latencyMs": 5}}}


def good_policy(
    chosen_size: str = "8.0",
    option_id: str = "SO-10",
    reply: str = "Your proposal is ready: size 8.0 Stessy2.0 in black, pickup at ALDO Demo Store Montreal on Sat Oct 17. "
    "ALDO says 80% of 582 fit returns were too small, so I went half a size up from your usual. "
    "Please confirm it in the app. Prototype, simulated data, no payment is taken.",
) -> Policy:
    """A well-behaved model: recall, ask, record, propose, then a compliant reply."""

    def policy(messages: list[dict[str, Any]], call: int) -> Action:
        found = tool_results(messages)
        if call == 1:
            return "tools", [("recall_size_memory", {})]
        if call == 2:
            return "tools", [("ask_aldo_size_assist", {"product_query": "Stessy", "deadline_date": "2026-10-17"})]
        if call == 3:
            return "tools", [("record_decision", {"summary": "Size 8.0: usual 7.5 plus half a size, the style runs small"})]
        if call == 4:
            request_id = found[1]["data"]["size_request_id"]
            return "tools", [
                (
                    "send_size_decision",
                    {
                        "size_request_id": request_id,
                        "option_id": option_id,
                        "chosen_size": chosen_size,
                        "rationale_public": "Style runs small per ALDO fit signal; pickup meets the deadline",
                        "confidence": "high",
                    },
                )
            ]
        return "text", reply

    return policy
