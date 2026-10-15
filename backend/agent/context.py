"""Per-run state shared by the tools, the hooks and the runner (contracts.md B.1)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .services import Services

RunKind = Literal["customer", "approval", "requote"]
RUN_KINDS: tuple[str, ...] = ("customer", "approval", "requote")

#: Tools that still work after the round limit is reached (G-02): the one-line public
#: rationale always, and ``escalate_case`` once.
ALWAYS_EXEMPT_TOOLS: frozenset[str] = frozenset({"record_decision"})
ESCALATION_TOOL = "escalate_case"

#: The hard stop fires this many model rounds after ``max_rounds``.
HARD_STOP_EXTRA_ROUNDS = 3


class RoundLimitExceeded(Exception):
    """Raised from the BeforeModelCallEvent hook when the agent keeps going past the limit."""


@dataclass
class RunContext:
    """Trusted identifiers and counters of one agent run.

    ``customer_id`` comes from the worker payload (the runtime is IAM-invoked), never from a
    tool argument or a customer message. No tool takes or returns it.
    """

    run_id: str
    conv_id: str
    customer_id: str
    order_id: str | None = None
    kind: RunKind = "customer"
    max_rounds: int = 8
    rounds: int = 0  # MODEL rounds, incremented by the BeforeModelCallEvent hook
    tool_calls: int = 0  # every tool invocation, including cancelled ones
    escalated: bool = False
    quote_id: str | None = None
    option_id: str | None = None
    approval_id: str | None = None
    model_id: str = ""  # the model actually used (primary or fallback)
    fallback_used: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    # Not part of the contract: collaborators and bookkeeping private to the agent package.
    services: Services = field(default_factory=Services, repr=False)
    case_id: str | None = None  # set when escalate_case succeeds
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: Validated ``memory_context`` of the payload (``owner_context.sanitize``); None when absent.
    #: Trusted server data, never filled from model or owner text.
    memory_context: dict[str, Any] | None = field(default=None, repr=False)
    rank_orders: dict[str, list[str]] = field(default_factory=dict, repr=False)
    #: candidate_set_id -> ``{"options": [...], "reason_no_exchange", "preferred_channel"}`` of the
    #: finds of THIS run (JSON-safe). Later runs read the persisted candidate set instead.
    offered: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    pending_call: dict[str, Any] | None = field(default=None, repr=False)
    seen_message_count: int = field(default=-1, repr=False)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def is_over_limit(self, tool_name: str) -> bool:
        """True if ``tool_name`` must be cancelled with LIMIT_REACHED right now."""
        if self.rounds <= self.max_rounds:
            return False
        if tool_name in ALWAYS_EXEMPT_TOOLS:
            return False
        if tool_name == ESCALATION_TOOL and not self.escalated:
            return False
        return True

    def hard_stop_due(self) -> bool:
        return self.rounds > self.max_rounds + HARD_STOP_EXTRA_ROUNDS
