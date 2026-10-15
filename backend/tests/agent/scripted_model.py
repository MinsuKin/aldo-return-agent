"""A scripted Strands model for the agent tests: no Bedrock, no network.

Each call to ``stream`` plays the next *turn* of the script. A turn is a list of content
blocks, either ``{"text": "..."}`` or ``{"toolUse": {"name": ..., "input": {...}}}``, or an
exception instance (raised when that call is made). When the script runs out the model keeps
repeating ``tail`` (default: a short final answer), or raises if ``tail`` is ``None``.
The model records what it was sent, so tests can assert on messages, tools and the prompt.
"""

from __future__ import annotations

import json
import threading
from typing import Any, AsyncIterator

from strands.models.model import Model

Turn = list[dict[str, Any]] | BaseException


class ScriptedModel(Model):
    def __init__(self, turns: list[Turn], *, tail: Turn | None = None, model_id: str = "scripted") -> None:
        self.turns = list(turns)
        self.tail = tail if tail is not None else [{"text": "Done."}]
        self.calls: list[dict[str, Any]] = []
        self.model_id = model_id
        self._config: dict[str, Any] = {"model_id": model_id}
        self._counter = 0

    # --- Model interface -------------------------------------------------------------

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> Any:
        return self._config

    def structured_output(self, output_model: Any, prompt: Any, system_prompt: Any = None, **kwargs: Any) -> Any:
        raise NotImplementedError("structured output is not used by the ALDO agent")

    async def stream(  # type: ignore[override]
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: Any = None,
        *,
        tool_choice: Any = None,
        system_prompt_content: Any = None,
        invocation_state: Any = None,
        cancel_signal: threading.Event | None = None,
        agent_metadata: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        index = len(self.calls)
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages, default=str)),
                "tool_names": [spec["name"] for spec in (tool_specs or [])],
                "system_prompt": system_prompt,
                "tool_choice": tool_choice,
            }
        )
        turn = self.turns[index] if index < len(self.turns) else self.tail
        if isinstance(turn, BaseException):
            raise turn
        yield {"messageStart": {"role": "assistant"}}
        has_tool = False
        for block in turn:
            if "text" in block:
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
                yield {"contentBlockStop": {}}
                continue
            tool_use = block["toolUse"]
            has_tool = True
            self._counter += 1
            tool_use_id = tool_use.get("toolUseId") or f"tooluse_{self._counter}"
            yield {
                "contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": tool_use["name"]}}}
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(tool_use.get("input", {}))}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use" if has_tool else "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "metrics": {"latencyMs": 5},
            }
        }


def tool_turn(name: str, **arguments: Any) -> list[dict[str, Any]]:
    """A model turn that calls one tool."""
    return [{"toolUse": {"name": name, "input": arguments}}]


def text_turn(text: str) -> list[dict[str, Any]]:
    return [{"text": text}]


def tool_results(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """tool name -> parsed result envelope, for the tool results found in ``messages``."""
    names: dict[str, str] = {}
    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        for block in message.get("content", []):
            if "toolUse" in block:
                names[block["toolUse"]["toolUseId"]] = block["toolUse"]["name"]
            if "toolResult" in block:
                text = block["toolResult"]["content"][0].get("text", "{}")
                try:
                    results[names.get(block["toolResult"]["toolUseId"], "?")] = json.loads(text)
                except ValueError:
                    pass
    return results


class AdaptiveModel(ScriptedModel):
    """A script whose turns may be callables: ``fn(results) -> turn``.

    ``results`` maps tool names to the parsed envelope of their latest result, so a turn can
    use ids that the real services generated (policy check id, option ids, transaction id).
    """

    def __init__(self, turns: list[Any], **kwargs: Any) -> None:
        super().__init__(turns, **kwargs)
        self.seen: dict[str, dict[str, Any]] = {}

    async def stream(self, messages: Any, tool_specs: Any = None, *args: Any, **kwargs: Any):  # type: ignore[override]
        self.seen = tool_results(messages)
        index = len(self.calls)
        if index < len(self.turns) and callable(self.turns[index]):
            self.turns[index] = self.turns[index](self.seen)
        async for event in super().stream(messages, tool_specs, *args, **kwargs):
            yield event
