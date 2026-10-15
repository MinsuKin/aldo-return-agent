"""Fakes for the persistence layer so the agent is tested without DynamoDB or Bedrock.

The fakes follow contracts.md A.6-A.14 and PIVOT_ADDENDUM 9: they return pydantic models
where the real modules do (so ``store.json_safe`` is exercised for real), record every call,
and can be told to fail. The policy decision comes from the REAL ``returns_domain.policy``,
so the tool wrappers are checked against the shape the real service produces.
"""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from agent.services import Services
from returns_domain import policy
from returns_domain.errors import DomainError, ErrorCode

CUSTOMER = "DEMO-CUST-AVA"
OTHER_CUSTOMER = "DEMO-CUST-BEN"
SEEDED_ORDER = "DEMO-1001"
AGENT_ORDER = "DEMO-A2A-81B20E"  # placed by the customer's personal agent (channel "agent")
OTHER_ORDER = "DEMO-1003"
HERO_PRODUCT = "Stessy2.0"

#: Variables returns_domain.config and the runner read; cleared so the shell cannot change results.
ENV_VARS = (
    "TABLE_NAME", "AWS_REGION", "AWS_DEFAULT_REGION", "DEMO_NOW", "BEDROCK_MODEL_ID",
    "BEDROCK_FALLBACK_MODEL_ID", "FIXTURES_DIR", "MAX_ROUNDS", "MAX_MESSAGE_CHARS", "TOOL_RETRIES",
    "MAX_SUMMARY_CHARS", "MAX_DECISION_CHARS", "EVENT_STRING_CAP", "DEMO_TOKEN_KEY", "SECRETS_ARN",
)


# --------------------------------------------------------------------- pydantic shapes


class OrderLineM(BaseModel):
    line_id: str
    sku: str
    product_name: str
    style: str
    size: str
    color: str
    purchased_qty: int
    returned_or_reserved_qty: int
    unit_paid_cents: int
    final_sale: bool | None


class OrderM(BaseModel):
    order_id: str
    banner: str = "ALDO_CA"
    currency: str = "CAD"
    channel: str
    ordered_at: str
    delivered_at: str | None
    lines: list[OrderLineM]


class PolicyCheckM(BaseModel):
    policy_check_id: str
    order_id: str
    line_id: str
    qty: int
    reason_category: str
    condition: str
    size_direction: str
    refund_cents: int
    currency: str = "CAD"
    result: dict[str, Any]
    policy_version: str
    created_at: str = "2026-10-16T16:00:00+00:00"


class OptionM(BaseModel):
    option_id: str
    type: str
    sku: str
    replacement_size: str | None
    location_id: str
    location_name: str
    qty: int = 1
    refund_cents: int
    fee_cents: int
    currency: str = "CAD"
    eta_date: str
    eta_text: str
    meets_deadline: bool | None
    conditions: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    provenance: str = "synthetic"


class CandidateSetM(BaseModel):
    candidate_set_id: str
    policy_check_id: str
    options: list[OptionM]
    availability: dict[str, Any]
    other_sizes_in_stock: list[dict[str, Any]] = Field(default_factory=list)
    inventory_versions: dict[str, int] = Field(default_factory=dict)
    deadline_date: str | None
    created_at: str = "2026-10-16T16:00:00+00:00"


class QuoteM(BaseModel):
    quote_id: str
    order_id: str
    status: str = "open"
    expires_at: str = "2026-10-16T16:15:00+00:00"
    options: list[OptionM]


class ExecutionResultM(BaseModel):
    transaction_id: str
    state: str = "completed"
    option_type: str
    replayed: bool = False
    quote_id: str
    approval_id: str = "APR-000001"
    memory_update_id: str | None = None


class TransactionViewM(BaseModel):
    transaction_id: str
    state: str = "completed"
    option_type: str
    order_id: str
    line_id: str
    qty: int = 1
    sku: str
    location_id: str
    location_name: str
    replacement_size: str | None
    refund_cents: int = 0
    fee_cents: int = 0
    currency: str = "CAD"
    eta_date: str
    eta_text: str
    next_step: str
    simulated: bool = True
    quote_id: str


class CaseM(BaseModel):
    case_id: str
    reason_code: str
    summary: str
    status: str = "open"
    order_id: str | None = None
    line_id: str | None = None


# ----------------------------------------------------------------------------- fakes


class _Fake:
    """Records calls as (method, args, kwargs) and raises queued failures first."""

    def __init__(self, calls: list[tuple[str, str, tuple, dict]], failures: dict[str, list[BaseException]]):
        self._calls = calls
        self._failures = failures

    def _enter(self, method: str, *args: Any, **kwargs: Any) -> None:
        key = f"{type(self).__name__}.{method}"
        self._calls.append((type(self).__name__, method, args, kwargs))
        queue = self._failures.get(key)
        if queue:
            raise queue.pop(0)


def _line(order_id: str, size: str, *, final_sale: bool = False, unit: int = 12999) -> OrderLineM:
    return OrderLineM(
        line_id=f"{order_id}-L1",
        sku=f"DEMO-SKU-HERO-BLACK-{int(float(size) * 10):03d}",
        product_name=HERO_PRODUCT,
        style="DEMO-STYLE-HERO",
        size=size,
        color="BLACK",
        purchased_qty=1,
        returned_or_reserved_qty=0,
        unit_paid_cents=unit,
        final_sale=final_sale,
    )


ORDERS: dict[str, tuple[str, OrderM]] = {
    AGENT_ORDER: (
        CUSTOMER,
        OrderM(order_id=AGENT_ORDER, channel="agent", ordered_at="2026-10-16", delivered_at="2026-10-16",
               lines=[_line(AGENT_ORDER, "8.0")]),
    ),
    SEEDED_ORDER: (
        CUSTOMER,
        OrderM(order_id=SEEDED_ORDER, channel="web", ordered_at="2026-09-30", delivered_at="2026-10-04",
               lines=[_line(SEEDED_ORDER, "7.0")]),
    ),
    "DEMO-1002": (
        CUSTOMER,
        OrderM(order_id="DEMO-1002", channel="web", ordered_at="2026-10-02", delivered_at="2026-10-06",
               lines=[_line("DEMO-1002", "8.0", final_sale=True, unit=5999)]),
    ),
    OTHER_ORDER: (
        OTHER_CUSTOMER,
        OrderM(order_id=OTHER_ORDER, channel="web", ordered_at="2026-09-10", delivered_at="2026-09-16",
               lines=[_line(OTHER_ORDER, "10.0")]),
    ),
}


class FakeOrders(_Fake):
    def _owned(self, customer_id: str, order_id: str) -> OrderM:
        if order_id not in ORDERS:
            raise DomainError(ErrorCode.ORDER_NOT_FOUND, "Order not found.")
        owner, order = ORDERS[order_id]
        if owner != customer_id:
            raise DomainError(ErrorCode.NOT_AUTHORIZED, "Order not found for this customer.")
        return order

    def list_orders(self, customer_id: str) -> list[OrderM]:
        self._enter("list_orders", customer_id)
        mine = [order for owner, order in ORDERS.values() if owner == customer_id]
        return sorted(mine, key=lambda o: o.ordered_at, reverse=True)

    def order_context(self, customer_id: str, order_id: str, *, run_id: str | None = None) -> dict:
        self._enter("order_context", customer_id, order_id, run_id=run_id)
        order = self._owned(customer_id, order_id)
        return {
            "order_id": order.order_id,
            "delivered_at": order.delivered_at,
            "ordered_at": order.ordered_at,
            "currency": "CAD",
            "channel": order.channel,
            "lines": [
                {
                    "line_id": l.line_id, "sku": l.sku, "product_name": l.product_name, "size": l.size,
                    "color": l.color, "purchased_qty": l.purchased_qty,
                    "returned_or_reserved_qty": l.returned_or_reserved_qty,
                    "already_returned_qty": l.returned_or_reserved_qty,
                    "remaining_qty": Decimal(l.purchased_qty - l.returned_or_reserved_qty),  # as boto3 would
                    "unit_paid_cents": l.unit_paid_cents, "final_sale": l.final_sale,
                }
                for l in order.lines
            ],
            "source_refs": [f"ORDER#{order.order_id}/META"],
        }

    def check_policy(self, customer_id: str, run_id: str | None, order_id: str, line_id: str, qty: int,
                     reason_category: str, condition: str, size_direction: str = "unknown") -> PolicyCheckM:
        self._enter("check_policy", customer_id, run_id, order_id, line_id, qty, reason_category,
                    condition, size_direction)
        order = self._owned(customer_id, order_id)
        line = next(l for l in order.lines if l.line_id == line_id)
        result = policy.evaluate(order.model_dump(), line.model_dump(), qty, reason_category, condition)
        return PolicyCheckM(
            policy_check_id="PCHK-0000000A", order_id=order_id, line_id=line_id, qty=qty,
            reason_category=reason_category, condition=condition, size_direction=size_direction,
            refund_cents=line.unit_paid_cents * qty, result=result.to_dict(),
            policy_version=result.policy_version,
        )


def hero_options(*, dc_stock: bool = True, deadline: str | None = "2026-10-17") -> list[OptionM]:
    def meets(eta: str) -> bool | None:
        return None if deadline is None else eta <= deadline

    opts: list[OptionM] = []
    if dc_stock:
        opts.append(OptionM(
            option_id="OPT-1", type="exchange_ship_from_dc", sku="DEMO-SKU-HERO-BLACK-085",
            replacement_size="8.5", location_id="DEMO-DC-QC", location_name="ALDO Demo Distribution Centre",
            refund_cents=0, fee_cents=0, eta_date="2026-10-20", eta_text="Tue Oct 20",
            meets_deadline=meets("2026-10-20"), conditions=["Bring the original pair, unworn"],
            evidence={"atp": Decimal("5"), "inventory_version": 1},
            source_refs=["INV#DEMO-SKU-HERO-BLACK-085/LOC#DEMO-DC-QC"],
        ))
    opts += [
        OptionM(
            option_id="OPT-2", type="exchange_ship_from_store", sku="DEMO-SKU-HERO-BLACK-085",
            replacement_size="8.5", location_id="DEMO-STORE-MTL-01", location_name="ALDO Demo Store Montreal",
            refund_cents=0, fee_cents=0, eta_date="2026-10-19", eta_text="Mon Oct 19",
            meets_deadline=meets("2026-10-19"), conditions=["Bring the original pair, unworn"],
            evidence={"atp": 1, "inventory_version": 1},
            source_refs=["INV#DEMO-SKU-HERO-BLACK-085/LOC#DEMO-STORE-MTL-01"],
        ),
        OptionM(
            option_id="OPT-3", type="exchange_store_pickup", sku="DEMO-SKU-HERO-BLACK-085",
            replacement_size="8.5", location_id="DEMO-STORE-MTL-01", location_name="ALDO Demo Store Montreal",
            refund_cents=0, fee_cents=0, eta_date="2026-10-17", eta_text="Sat Oct 17",
            meets_deadline=meets("2026-10-17"), conditions=["Bring the original pair, unworn"],
            evidence={"atp": 1, "inventory_version": 1},
            source_refs=["INV#DEMO-SKU-HERO-BLACK-085/LOC#DEMO-STORE-MTL-01"],
        ),
        OptionM(
            option_id="OPT-4", type="return_in_store", sku="DEMO-SKU-HERO-BLACK-080", replacement_size=None,
            location_id="DEMO-STORE-MTL-01", location_name="ALDO Demo Store Montreal",
            refund_cents=12999, fee_cents=0, eta_date="2026-10-16", eta_text="Fri Oct 16", meets_deadline=None,
            conditions=["Bring the original pair, unworn"], source_refs=["DEMO-POLICY-CA@2026.10-demo"],
        ),
        OptionM(
            option_id="OPT-5", type="return_mail", sku="DEMO-SKU-HERO-BLACK-080", replacement_size=None,
            location_id="DEMO-DC-QC", location_name="ALDO Demo Distribution Centre",
            refund_cents=12404, fee_cents=595, eta_date="2026-10-26", eta_text="Mon Oct 26", meets_deadline=None,
            conditions=["Ship within 14 days"], source_refs=["DEMO-POLICY-CA@2026.10-demo"],
        ),
    ]
    return opts


class FakeOptions(_Fake):
    dc_stock = True

    def find_options(self, customer_id: str, policy_check_id: str, replacement_size: str | None = None,
                     deadline_date: str | None = None, preferred_channel: str | None = None, *,
                     run_id: str | None = None) -> CandidateSetM:
        self._enter("find_options", customer_id, policy_check_id, replacement_size, deadline_date,
                    preferred_channel, run_id=run_id)
        return CandidateSetM(
            candidate_set_id="CAND-0000000B", policy_check_id=policy_check_id,
            options=hero_options(dc_stock=self.dc_stock, deadline=deadline_date),
            availability={
                "replacement_size": replacement_size or "8.5", "replacement_sku": "DEMO-SKU-HERO-BLACK-085",
                "atp_by_location": {"DEMO-DC-QC": Decimal("5"), "DEMO-STORE-MTL-01": 1}, "dc_atp": Decimal("5"),
                "reason_no_exchange": None,
            },
            other_sizes_in_stock=[{"size": "9.0", "sku": "DEMO-SKU-HERO-BLACK-090", "total_atp": 4}],
            inventory_versions={"DEMO-SKU-HERO-BLACK-085|DEMO-STORE-MTL-01": 1}, deadline_date=deadline_date,
        )


class FakeQuotes(_Fake):
    def __init__(self, calls: list, failures: dict, options: FakeOptions) -> None:
        super().__init__(calls, failures)
        self._options = options

    def create_quote(self, customer_id: str, run_id: str, conv_id: str, candidate_set_id: str,
                     option_ids: list[str]) -> QuoteM:
        self._enter("create_quote", customer_id, run_id, conv_id, candidate_set_id, list(option_ids))
        known = {o.option_id: o for o in hero_options(dc_stock=self._options.dc_stock)}
        chosen = [known[i] for i in option_ids]  # KeyError would be a test bug
        digest = hashlib.sha1("|".join([run_id, candidate_set_id, *sorted(option_ids)]).encode()).hexdigest()
        return QuoteM(quote_id="QUO-" + digest[:10].upper(), order_id=AGENT_ORDER, options=chosen)


class FakeExecution(_Fake):
    def execute(self, customer_id: str, quote_id: str, option_id: str | None = None, *,
                run_id: str | None = None) -> ExecutionResultM:
        self._enter("execute", customer_id, quote_id, option_id, run_id=run_id)
        return ExecutionResultM(transaction_id="DEMO-RMA-ABC123", option_type="exchange_store_pickup",
                                quote_id=quote_id, memory_update_id="MU-5C0A11")

    def get_status(self, customer_id: str, transaction_id: str, *, run_id: str | None = None) -> TransactionViewM:
        self._enter("get_status", customer_id, transaction_id, run_id=run_id)
        return TransactionViewM(
            transaction_id=transaction_id, option_type="exchange_store_pickup", order_id=AGENT_ORDER,
            line_id=f"{AGENT_ORDER}-L1", sku="DEMO-SKU-HERO-BLACK-085", location_id="DEMO-STORE-MTL-01",
            location_name="ALDO Demo Store Montreal", replacement_size="8.5", eta_date="2026-10-17",
            eta_text="Sat Oct 17", quote_id="QUO-X",
            next_step="Pick up size 8.5 at ALDO Demo Store Montreal from Sat Oct 17; bring the original pair, unworn.",
        )


class FakeCases(_Fake):
    def escalate(self, customer_id: str, run_id: str | None, reason_code: str, summary: str,
                 order_id: str | None = None, line_id: str | None = None,
                 policy_check_id: str | None = None) -> CaseM:
        self._enter("escalate", customer_id, run_id, reason_code, summary, order_id, line_id)
        if len(summary) > 800:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "summary is too long")
        digest = hashlib.sha1(f"{run_id}|{reason_code}".encode()).hexdigest()[:6].upper()
        return CaseM(case_id=f"DEMO-CASE-{digest}", reason_code=reason_code, summary=summary,
                     order_id=order_id, line_id=line_id)


class FakeTrace(_Fake):
    def __init__(self, calls: list, failures: dict) -> None:
        super().__init__(calls, failures)
        self.events: list[tuple[str, str, dict]] = []

    def emit(self, run_id: str, type: str, payload: dict, *, actor: str = "aldo_agent") -> int:  # noqa: A002
        self._enter("emit", run_id, type)
        self.events.append((run_id, type, payload))
        return len(self.events)

    def of_type(self, event_type: str) -> list[dict]:
        return [payload for _, kind, payload in self.events if kind == event_type]

    @property
    def types(self) -> list[str]:
        return [kind for _, kind, _ in self.events]


class FakeRuns(_Fake):
    def __init__(self, calls: list, failures: dict, trace: FakeTrace) -> None:
        super().__init__(calls, failures)
        self.started: set[str] = set()
        self.finished: list[tuple[str, str, dict]] = []
        self._trace = trace

    def mark_agent_started(self, run_id: str) -> bool:
        self._enter("mark_agent_started", run_id)
        if run_id in self.started:
            return False
        self.started.add(run_id)
        return True

    def finish_run(self, run_id: str, status: str, metrics: dict) -> bool:
        self._enter("finish_run", run_id, status)
        self.finished.append((run_id, status, metrics))
        self._trace.events.append((run_id, "run_completed", {"status": status, **metrics}))
        return True


class FakeConversations(_Fake):
    def __init__(self, calls: list, failures: dict) -> None:
        super().__init__(calls, failures)
        self.saved: dict[str, list[dict]] = {}
        self.stored: dict[str, list[dict]] = {}

    def load_messages(self, conv_id: str) -> list[dict]:
        self._enter("load_messages", conv_id)
        return [dict(m) for m in self.stored.get(conv_id, [])]

    def save_messages(self, conv_id: str, messages: list[dict]) -> None:
        self._enter("save_messages", conv_id)
        self.saved[conv_id] = list(messages)


class FakeGateway(_Fake):
    def __init__(self, calls: list, failures: dict) -> None:
        super().__init__(calls, failures)
        self.delay = 0.0
        self.delivered_for: list[str] = []

    def deliver_pending(self, customer_id: str | None = None, *, limit: int = 10) -> dict:
        self._enter("deliver_pending", customer_id, limit=limit)
        if self.delay:
            time.sleep(self.delay)
        self.delivered_for.append(str(customer_id))
        return {"delivered": 1, "failed": 0}


class FakeDomain:
    """All fakes plus the shared call log and failure queues."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple, dict]] = []
        self.failures: dict[str, list[BaseException]] = {}
        self.trace = FakeTrace(self.calls, self.failures)
        self.orders = FakeOrders(self.calls, self.failures)
        self.options = FakeOptions(self.calls, self.failures)
        self.quotes = FakeQuotes(self.calls, self.failures, self.options)
        self.execution = FakeExecution(self.calls, self.failures)
        self.cases = FakeCases(self.calls, self.failures)
        self.runs = FakeRuns(self.calls, self.failures, self.trace)
        self.conversations = FakeConversations(self.calls, self.failures)
        self.gateway = FakeGateway(self.calls, self.failures)
        self.services = self.services_without()

    def services_without(self, *skip: str) -> Services:
        """A ``Services`` with all fakes except ``skip`` (those fall through to the real import)."""
        parts = {
            "orders": self.orders, "options": self.options, "quotes": self.quotes,
            "execution": self.execution, "cases": self.cases, "trace": self.trace, "runs": self.runs,
            "conversations": self.conversations, "gateway": self.gateway,
        }
        for name in skip:
            parts.pop(name)
        return Services(**parts)

    def fail(self, key: str, *errors: BaseException) -> None:
        """Queue failures for ``Class.method`` (for example ``FakeOrders.order_context``)."""
        self.failures.setdefault(key, []).extend(errors)

    def calls_to(self, cls: str, method: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for c, m, a, k in self.calls if c == cls and m == method]


def payload(**overrides: Any) -> dict[str, Any]:
    """A worker payload (contracts C.6) for the hero return message."""
    base: dict[str, Any] = {
        "run_id": "RUN-TEST000001", "conv_id": "CONV-TEST000001", "customer_id": CUSTOMER,
        "order_id": AGENT_ORDER, "kind": "customer",
        "message": "They're still slightly tight. Can I swap for a half size up before the wedding Saturday?",
        "quote_id": None, "option_id": None, "approval_id": None,
    }
    base.update(overrides)
    return base

