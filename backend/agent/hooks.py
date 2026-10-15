"""Strands hooks that enforce the round limit (G-02) and keep the trace complete (R11.1).

* ``BeforeModelCallEvent``: count MODEL rounds (not tool calls). A throttle retry resends the
  same messages, so a call is a new round only when the conversation grew. Past
  ``max_rounds + 3`` rounds raise ``RoundLimitExceeded``; the runner catches it, escalates
  server-side and finishes the run.
* ``BeforeToolCallEvent``: above ``max_rounds`` cancel every tool except ``record_decision``
  and one ``escalate_case``, returning the LIMIT_REACHED envelope to the model.
* ``AfterToolCallEvent``: when Strands rejected a call before the tool body ran (invalid
  arguments, unknown tool), write the missing ``tool_call`` / ``tool_result`` events.

All three run on the event-loop thread and the tools run one at a time
(``SequentialToolExecutor``), so ``RunContext`` needs no locking.
"""

from __future__ import annotations

import json
from typing import Any

from strands.hooks import (
    AfterToolCallEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from returns_domain.errors import ErrorCode

from .context import RoundLimitExceeded, RunContext
from .envelope import emit_tool_call, emit_tool_result, fail, limit_envelope
from .log import logger


class RunHooks(HookProvider):
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self.before_model_call)
        registry.add_callback(BeforeToolCallEvent, self.before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    # ------------------------------------------------------------------ model rounds

    def before_model_call(self, event: BeforeModelCallEvent) -> None:
        ctx = self.ctx
        message_count = len(getattr(event.agent, "messages", None) or [])
        if message_count != ctx.seen_message_count:
            ctx.seen_message_count = message_count
            ctx.rounds += 1
        if ctx.hard_stop_due():
            logger.warning(
                "round_limit_hard_stop", run_id=ctx.run_id, rounds=ctx.rounds, max_rounds=ctx.max_rounds
            )
            raise RoundLimitExceeded(
                f"model round {ctx.rounds} is beyond the hard stop (max_rounds={ctx.max_rounds})"
            )

    # ------------------------------------------------------------------ tool calls

    def before_tool_call(self, event: BeforeToolCallEvent) -> None:
        ctx = self.ctx
        tool_use = event.tool_use
        name = str(tool_use.get("name"))
        ctx.pending_call = {"id": str(tool_use.get("toolUseId")), "name": name, "handled": False}
        if not ctx.is_over_limit(name):
            return
        ctx.pending_call["handled"] = True
        ctx.tool_calls += 1
        envelope = limit_envelope()
        emit_tool_call(ctx, name, _input_of(tool_use))
        emit_tool_result(ctx, name, envelope, 0)
        event.cancel_tool = json.dumps(envelope)

    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        ctx = self.ctx
        pending, ctx.pending_call = ctx.pending_call, None
        if not pending or pending.get("handled"):
            return
        # Strands answered with an error without running the tool body.
        name = str(event.tool_use.get("name") or pending.get("name"))
        ctx.tool_calls += 1
        emit_tool_call(ctx, name, _input_of(event.tool_use))
        envelope = fail(
            ErrorCode.VALIDATION_ERROR,
            "The tool call was rejected before it ran: check the tool name and argument values.",
        )
        emit_tool_result(ctx, name, envelope, 0)


def _input_of(tool_use: Any) -> dict[str, Any]:
    raw = tool_use.get("input") if hasattr(tool_use, "get") else None
    return dict(raw) if isinstance(raw, dict) else {}
