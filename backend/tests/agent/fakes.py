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

from pydantic import BaseModel, Field, model_serializer

from agent.services import Services
from returns_domain import policy, store as real_store
from returns_domain.errors import DomainError, ErrorCode

CUSTOMER = "DEMO-CUST-AVA"
DAVID = "DEMO-CUST-DAVID"  # Platinum, rural New Brunswick: owns the ten-line order DEMO-3001
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
    category: str | None = None  # David's order names what each shoe is (boot, sneaker, ...)


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
    # Domain v2: left out of the JSON when unknown, like the real Option (never null).
    replacement_sku: str | None = None
    replacement_color: str | None = None
    minutes_away: int | None = None
    benefit: dict[str, Any] | None = None  # phase 2 (a tier perk); the agent passes it through untouched
    # Bundle returns (domain v2 phase 2): left out of the JSON for single-line options, like the real Option.
    lines: list[dict[str, Any]] | None = None
    refund_total_cents: int | None = None
    net_refund_cents: int | None = None
    decision: str | None = None
    rule_ids: list[str] | None = None
    authority_limit_cents: int | None = None

    @model_serializer(mode="wrap")
    def _omit_unknown_optionals(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in (
            "replacement_sku", "replacement_color", "minutes_away", "benefit", "lines", "refund_total_cents",
            "net_refund_cents", "decision", "rule_ids", "authority_limit_cents",
        ):
            if data.get(name) is None:
                data.pop(name, None)
        return data


class CandidateSetM(BaseModel):
    candidate_set_id: str
    policy_check_id: str
    options: list[OptionM]
    availability: dict[str, Any]
    other_sizes_in_stock: list[dict[str, Any]] = Field(default_factory=list)
    inventory_versions: dict[str, int] = Field(default_factory=dict)
    deadline_date: str | None
    created_at: str = "2026-10-16T16:00:00+00:00"
    preferred_channel: str | None = None
    needs_choice: dict[str, Any] | None = None
    policy_check_ids: list[str] | None = None
    bundle: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_needs_choice_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in ("needs_choice", "policy_check_ids", "bundle"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


class QuoteM(BaseModel):
    quote_id: str
    order_id: str
    status: str = "open"
    expires_at: str = "2026-10-16T16:15:00+00:00"
    options: list[OptionM]
    policy_check_ids: list[str] | None = None
    lines: list[dict[str, Any]] | None = None

    @model_serializer(mode="wrap")
    def _omit_bundle_fields_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in ("policy_check_ids", "lines"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


#: What the real domain puts in a store notice (display_name and tier_label are NOT for the model).
STORE_NOTICE = {
    "store_id": "DEMO-STORE-MTL-01",
    "store_name": "ALDO Demo Store Montreal",
    "display_name": "John",
    "tier_label": "Silver member",
    "eta_minutes": 5,
    "summary": "ALDO Demo Store Montreal will have your Brown pair ready, John is about 5 minutes away",
}


class ExecutionResultM(BaseModel):
    transaction_id: str
    state: str = "completed"
    option_type: str
    replayed: bool = False
    quote_id: str
    approval_id: str = "APR-000001"
    memory_update_id: str | None = None
    store_notice: dict[str, Any] | None = None
    benefit: dict[str, Any] | None = None  # a bundle that used the member perk

    @model_serializer(mode="wrap")
    def _omit_benefit_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("benefit") is None:
            data.pop("benefit", None)
        return data


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
    original_size: str | None = None
    memory_update_id: str | None = None
    replacement_color: str | None = None
    store_notice: dict[str, Any] | None = None
    lines: list[dict[str, Any]] | None = None
    refund_total_cents: int | None = None
    net_refund_cents: int | None = None
    benefit: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_bundle_fields_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in ("lines", "refund_total_cents", "net_refund_cents", "benefit"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


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


#: David's ten-line order (the shipped fixture DEMO-3001), here owned by the fake customer so the fakes can serve it.
BUNDLE_ORDER = "DEMO-3001"
#: number -> (product, colour, category, size, price in cents)
BUNDLE_LINES: dict[int, tuple[str, str, str, str, int]] = {
    1: ("Lededanten", "BLACK", "derby", "10.0", 15999),
    2: ("Aahliyah", "TAN", "boot", "10.5", 17999),
    3: ("Joselie", "BROWN", "oxford", "10.0", 16999),
    4: ("Tanerdee", "BLACK", "chelsea boot", "11.0", 19999),
    5: ("Alanie", "WHITE", "sneaker", "10.0", 10999),
    6: ("Mazey", "BROWN", "boat shoe", "9.5", 13999),
    7: ("Caraelia", "BLACK", "monk strap", "10.5", 18999),
    8: ("Valegyn", "NAVY", "loafer", "10.0", 12999),
    9: ("Krishelle", "BROWN", "brogue", "11.0", 14999),
    10: ("Oneameth", "GREY", "sneaker", "10.5", 9999),
}
BUNDLE_SCRIPT = (2, 5, 8)  # the tan boot, the white sneaker, the navy loafer: 17999 + 10999 + 12999 = 41997


def bundle_check_id(number: int) -> str:
    return f"PCHK-D{number:07d}"


def _bundle_order() -> OrderM:
    return OrderM(
        order_id=BUNDLE_ORDER, channel="web", ordered_at="2026-10-09", delivered_at="2026-10-13",
        lines=[
            OrderLineM(
                line_id=f"{BUNDLE_ORDER}-L{n}", sku=f"DEMO-SKU-NEW{n:02d}-{color}-100", product_name=name,
                style=f"DEMO-STYLE-NEW{n:02d}", size=size, color=color, purchased_qty=1,
                returned_or_reserved_qty=0, unit_paid_cents=price, final_sale=False, category=category,
            )
            for n, (name, color, category, size, price) in BUNDLE_LINES.items()
        ],
    )


ORDERS: dict[str, tuple[str, OrderM]] = {
    BUNDLE_ORDER: (DAVID, _bundle_order()),
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
                    **({"category": l.category} if l.category else {}),
                }
                for l in sorted(order.lines, key=lambda line: line.line_id)  # the domain sorts the ids as text
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
        check_id = "PCHK-0000000A" if order_id != BUNDLE_ORDER else bundle_check_id(int(line_id.rsplit("-L", 1)[1]))
        return PolicyCheckM(
            policy_check_id=check_id, order_id=order_id, line_id=line_id, qty=qty,
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


#: Colours in stock in the owner's size (the domain sorts them by colour name).
COLOR_CHOICES = [
    {"color": "BROWN", "total_atp": 7, "nearest_store_minutes": 5},
    {"color": "TAN", "total_atp": 3, "nearest_store_minutes": None},
    {"color": "WHITE", "total_atp": 2, "nearest_store_minutes": 24},
]


def _opt(option_id: str, kind: str, name: str, location_id: str, eta: str, eta_text: str, *, color: str | None = None,
         minutes: int | None = None, refund: int = 0, fee: int = 0, size: str | None = "9.5") -> OptionM:
    returns = kind.startswith("return_")
    return OptionM(
        option_id=option_id, type=kind, sku="DEMO-SKU-MEN001-BLACK-095" if returns else "DEMO-SKU-MEN001-BROWN-095",
        replacement_size=None if returns else size, location_id=location_id, location_name=name,
        refund_cents=refund, fee_cents=fee, eta_date=eta, eta_text=eta_text, meets_deadline=None,
        conditions=["Bring the original pair, unworn"], evidence={"atp": 2, "inventory_version": 1},
        source_refs=[f"INV#{option_id}"], replacement_sku=None if returns or not color else "DEMO-SKU-MEN001-BROWN-095",
        replacement_color=None if returns else color, minutes_away=minutes,
    )


def colour_options() -> list[OptionM]:
    """The options of a colour exchange to BROWN for John: DC, two pickups (nearest first), two returns."""
    mtl, laval = "ALDO Demo Store Montreal", "ALDO Demo Store Laval"
    return [
        _opt("OPT-1", "exchange_ship_from_dc", "ALDO Demo Distribution Centre", "DEMO-DC-QC", "2026-10-20", "Tue Oct 20",
             color="BROWN"),
        _opt("OPT-2", "exchange_store_pickup", mtl, "DEMO-STORE-MTL-01", "2026-10-17", "Sat Oct 17", color="BROWN", minutes=5),
        _opt("OPT-3", "exchange_store_pickup", laval, "DEMO-STORE-LAVAL-01", "2026-10-17", "Sat Oct 17", color="BROWN",
             minutes=24),
        _opt("OPT-4", "return_in_store", mtl, "DEMO-STORE-MTL-01", "2026-10-16", "Fri Oct 16", minutes=5, refund=14999),
        _opt("OPT-5", "return_mail", "ALDO Demo Distribution Centre", "DEMO-DC-QC", "2026-10-26", "Mon Oct 26",
             refund=14404, fee=595),
    ]


class FakeStore:
    """The ``store`` service: real ``json_safe``, plus the stored candidate sets the fake options persist."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def json_safe(self, value: Any) -> Any:
        return real_store.json_safe(value)

    def cand_key(self, candidate_set_id: str) -> tuple[str, str]:
        return (f"CAND#{candidate_set_id}", "META")

    def get_item(self, key: tuple[str, str]) -> dict | None:
        return self.items.get(key)


class FakeOptions(_Fake):
    dc_stock = True
    #: False: the fit-size hero options. True: a preference (colour) return, see ``find_options``.
    preference = False
    #: Extra options appended to the next result (for the cap test).
    extra_options: list[OptionM] = []
    store: FakeStore | None = None

    #: Bundle returns: the member perk is applied to the mail option (False: the second bundle of the month).
    bundle_perk = True
    #: True: the bundle total is above the tier limit, so every option says needs_review.
    bundle_review = False
    #: True: a store is within reach, so the bundle also gets a return_in_store option.
    bundle_store = False

    def find_options(self, customer_id: str, policy_check_id: str | None = None, replacement_size: str | None = None,
                     deadline_date: str | None = None, preferred_channel: str | None = None,
                     replacement_color: str | None = None, *, run_id: str | None = None,
                     policy_check_ids: list[str] | None = None) -> CandidateSetM:
        if policy_check_ids is not None:
            self._enter("find_options", customer_id, policy_check_id, replacement_size, deadline_date,
                        preferred_channel, replacement_color, run_id=run_id, policy_check_ids=policy_check_ids)
            return self._find_bundle(customer_id, list(policy_check_ids), deadline_date, preferred_channel)
        self._enter("find_options", customer_id, policy_check_id, replacement_size, deadline_date,
                    preferred_channel, replacement_color, run_id=run_id)
        needs_choice = None
        if not self.preference:
            options = hero_options(dc_stock=self.dc_stock, deadline=deadline_date)
            availability = {
                "replacement_size": replacement_size or "8.5", "replacement_sku": "DEMO-SKU-HERO-BLACK-085",
                "atp_by_location": {"DEMO-DC-QC": Decimal("5"), "DEMO-STORE-MTL-01": 1}, "dc_atp": Decimal("5"),
                "reason_no_exchange": None,
            }
            others = [{"size": "9.0", "sku": "DEMO-SKU-HERO-BLACK-090", "total_atp": 4}]
        elif replacement_color is None:
            options = [o for o in colour_options() if o.type.startswith("return_")]
            options = [o.model_copy(update={"option_id": f"OPT-{i}"}) for i, o in enumerate(options, start=1)]
            needs_choice = {"replacement_color": [dict(c) for c in COLOR_CHOICES]}
            availability = {"replacement_size": "9.5", "replacement_sku": None, "atp_by_location": {},
                            "dc_atp": 0, "reason_no_exchange": "NEEDS_COLOR_CHOICE", "replacement_color": None}
            others = []
        else:
            options = colour_options()
            availability = {"replacement_size": "9.5", "replacement_sku": "DEMO-SKU-MEN001-BROWN-095",
                            "atp_by_location": {"DEMO-DC-QC": 4}, "dc_atp": 4, "reason_no_exchange": None,
                            "replacement_color": replacement_color.upper()}
            others = []
        options = [*options, *self.extra_options]
        result = CandidateSetM(
            candidate_set_id="CAND-0000000B", policy_check_id=policy_check_id, options=options,
            availability=availability, other_sizes_in_stock=others,
            inventory_versions={"DEMO-SKU-HERO-BLACK-085|DEMO-STORE-MTL-01": 1}, deadline_date=deadline_date,
            preferred_channel=preferred_channel, needs_choice=needs_choice,
        )
        self.candidates[result.candidate_set_id] = list(options)
        if self.store is not None:  # what the domain persists as CAND#{id}: a later run reads it back
            self.store.items[self.store.cand_key(result.candidate_set_id)] = {
                "customer_id": customer_id, "options": [o.model_dump() for o in options],
                "availability": availability, "preferred_channel": preferred_channel,
            }
        return result

    def _find_bundle(self, customer_id: str, check_ids: list[str], deadline_date: str | None,
                     preferred_channel: str | None) -> CandidateSetM:
        """Bundle options shaped like ``options._find_bundle_options`` (S17B report, section 5)."""
        numbers = [int(i[-7:]) for i in check_ids]
        entries = []
        for n in numbers:
            name, color, category, size, price = BUNDLE_LINES[n]
            entries.append({
                "line_id": f"{BUNDLE_ORDER}-L{n}", "policy_check_id": bundle_check_id(n),
                "sku": f"DEMO-SKU-NEW{n:02d}-{color}-100", "product_name": name, "color": color, "size": size,
                "qty": 1, "unit_paid_cents": price, "refund_cents": price, "category": category,
            })
        total = sum(e["refund_cents"] for e in entries)
        limit = 25000 if self.bundle_review else 75000
        review = total > limit
        decision = "needs_review" if review else "eligible"
        perk = None
        if self.bundle_perk and not review:
            perk = {"type": "free_return_shipping", "label": "Platinum free return shipping", "saved_cents": 595,
                    "used": 1, "allowed": 1, "period": "2026-10"}
        fee = 0 if perk else 595

        def option(option_id: str, kind: str, name: str, location_id: str, minutes: int | None, fee_cents: int,
                   eta: str, eta_text: str, benefit: dict | None = None) -> OptionM:
            return OptionM(
                option_id=option_id, type=kind, sku=entries[0]["sku"], replacement_size=None, location_id=location_id,
                location_name=name, qty=len(entries), refund_cents=max(total - fee_cents, 0), fee_cents=fee_cents,
                eta_date=eta, eta_text=eta_text, meets_deadline=None,
                conditions=["One return label fee of CAD 5.95 for the whole shipment is deducted from the refund"
                            if fee_cents else "Platinum free return shipping: no return label fee"],
                evidence={"n_lines": len(entries), "tier": "platinum"}, source_refs=[f"ORDER#{BUNDLE_ORDER}/LINE#x"],
                minutes_away=minutes, benefit=benefit, lines=[dict(e) for e in entries], refund_total_cents=total,
                net_refund_cents=max(total - fee_cents, 0), decision=decision,
                rule_ids=["authority.refund_limit"] if review else [], authority_limit_cents=limit,
            )

        options: list[OptionM] = []
        if self.bundle_store:
            options.append(option("OPT-1", "return_in_store", "ALDO Demo Store Montreal", "DEMO-STORE-MTL-01", 5, 0,
                                  "2026-10-16", "Fri Oct 16"))
        options.append(option(f"OPT-{len(options) + 1}", "return_mail", "ALDO Demo Distribution Centre",
                              "DEMO-DC-QC", None, fee, "2026-10-26", "Mon Oct 26", perk))
        bundle = {"order_id": BUNDLE_ORDER, "n_lines": len(entries), "qty": len(entries), "refund_total_cents": total,
                  "authority_limit_cents": limit, "tier": "platinum" if not review else "silver", "decision": decision}
        result = CandidateSetM(
            candidate_set_id="CAND-0000000B", policy_check_id=check_ids[0], options=options,
            availability={"replacement_size": None, "replacement_sku": None, "atp_by_location": {}, "dc_atp": 0,
                          "reason_no_exchange": "BUNDLE_RETURN_ONLY"},
            other_sizes_in_stock=[], inventory_versions={}, deadline_date=deadline_date,
            preferred_channel=preferred_channel, policy_check_ids=check_ids, bundle=bundle,
        )
        self.candidates[result.candidate_set_id] = list(options)
        if self.store is not None:
            self.store.items[self.store.cand_key(result.candidate_set_id)] = {
                "customer_id": customer_id, "options": [o.model_dump() for o in options],
                "availability": result.availability, "preferred_channel": preferred_channel,
            }
        return result

    candidates: dict[str, list[OptionM]]

    def __init__(self, calls: list, failures: dict) -> None:
        super().__init__(calls, failures)
        self.candidates = {}
        self.extra_options = []


class FakeQuotes(_Fake):
    def __init__(self, calls: list, failures: dict, options: FakeOptions) -> None:
        super().__init__(calls, failures)
        self._options = options

    def create_quote(self, customer_id: str, run_id: str, conv_id: str, candidate_set_id: str,
                     option_ids: list[str]) -> QuoteM:
        self._enter("create_quote", customer_id, run_id, conv_id, candidate_set_id, list(option_ids))
        offered = self._options.candidates.get(candidate_set_id) or hero_options(dc_stock=self._options.dc_stock)
        known = {o.option_id: o for o in offered}
        chosen = [known[i] for i in option_ids]  # KeyError would be a test bug
        digest = hashlib.sha1("|".join([run_id, candidate_set_id, *sorted(option_ids)]).encode()).hexdigest()
        bundle_lines = next((o.lines for o in chosen if o.lines), None)
        if bundle_lines and any(o.decision == "needs_review" for o in chosen):  # what quotes.create_quote does
            raise DomainError(ErrorCode.VALIDATION_ERROR, "A person must review this return; it cannot be quoted.",
                              details={"decision": "needs_review", "rule_ids": ["authority.refund_limit"]})
        if bundle_lines:
            return QuoteM(quote_id="QUO-" + digest[:10].upper(), order_id=BUNDLE_ORDER, options=chosen,
                          lines=[dict(l) for l in bundle_lines], policy_check_ids=[l["policy_check_id"] for l in bundle_lines])
        return QuoteM(quote_id="QUO-" + digest[:10].upper(), order_id=AGENT_ORDER, options=chosen)


class FakeExecution(_Fake):
    #: True: the in-store colour exchange of John, with a store notice (the default is the fit pickup of Ava).
    colour_exchange = False
    #: True: a mail bundle of David's script (3 lines, fee 0, perk used). ``bundle_fee`` 595 plays the no-perk variant.
    bundle = False
    bundle_fee = 0

    def _bundle_lines(self) -> list[dict[str, Any]]:
        out = []
        for n in BUNDLE_SCRIPT:
            name, color, category, size, price = BUNDLE_LINES[n]
            out.append({"line_id": f"{BUNDLE_ORDER}-L{n}", "sku": f"DEMO-SKU-NEW{n:02d}-{color}-100",
                        "product_name": name, "color": color, "size": size, "qty": 1, "unit_paid_cents": price,
                        "refund_cents": price, "category": category})
        return out

    def execute(self, customer_id: str, quote_id: str, option_id: str | None = None, *,
                run_id: str | None = None) -> ExecutionResultM:
        self._enter("execute", customer_id, quote_id, option_id, run_id=run_id)
        if self.bundle:
            perk = ({"type": "free_return_shipping", "label": "Platinum free return shipping", "saved_cents": 595,
                     "used": 1, "allowed": 1, "period": "2026-10"} if self.bundle_fee == 0 else None)
            return ExecutionResultM(transaction_id="DEMO-RET-ABC123", option_type="return_mail", quote_id=quote_id,
                                    memory_update_id="MU-5C0A12", benefit=perk)
        return ExecutionResultM(transaction_id="DEMO-RMA-ABC123", option_type="exchange_store_pickup",
                                quote_id=quote_id, memory_update_id="MU-5C0A11",
                                store_notice=dict(STORE_NOTICE) if self.colour_exchange else None)

    def get_status(self, customer_id: str, transaction_id: str, *, run_id: str | None = None) -> TransactionViewM:
        self._enter("get_status", customer_id, transaction_id, run_id=run_id)
        if self.bundle:
            lines = self._bundle_lines()
            total = sum(l["refund_cents"] for l in lines)
            perk = ({"type": "free_return_shipping", "label": "Platinum free return shipping", "saved_cents": 595,
                     "used": 1, "allowed": 1, "period": "2026-10"} if self.bundle_fee == 0 else None)
            return TransactionViewM(
                transaction_id=transaction_id, option_type="return_mail", order_id=BUNDLE_ORDER,
                line_id=lines[0]["line_id"], qty=len(lines), sku=lines[0]["sku"], location_id="DEMO-DC-QC",
                location_name="ALDO Demo Distribution Centre", replacement_size=None, refund_cents=total - self.bundle_fee,
                fee_cents=self.bundle_fee, eta_date="2026-10-26", eta_text="Mon Oct 26", quote_id="QUO-X",
                memory_update_id="MU-5C0A12", lines=lines, refund_total_cents=total,
                net_refund_cents=total - self.bundle_fee, benefit=perk,
                next_step="Mail the 3 items, unworn, in one parcel with the return label. Your refund is expected "
                          "by Mon Oct 26 after the parcel is received.",
            )
        if self.colour_exchange:
            return TransactionViewM(
                transaction_id=transaction_id, option_type="exchange_store_pickup", order_id=AGENT_ORDER,
                line_id=f"{AGENT_ORDER}-L1", sku="DEMO-SKU-MEN001-BROWN-095", location_id="DEMO-STORE-MTL-01",
                location_name="ALDO Demo Store Montreal", replacement_size="9.5", original_size="9.5",
                replacement_color="BROWN", eta_date="2026-10-17", eta_text="Sat Oct 17", quote_id="QUO-X",
                memory_update_id="MU-5C0A11", store_notice=dict(STORE_NOTICE),
                next_step=("ALDO Demo Store Montreal will have your Brown pair, size 9.5, ready from Sat Oct 17; "
                           "bring the original pair, unworn."),
            )
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


class FakeBenefits(_Fake):
    """The ``benefits`` service: how much of the monthly perk is used. Default: a tier without the perk."""

    def __init__(self, calls: list, failures: dict) -> None:
        super().__init__(calls, failures)
        self.usage: dict[str, Any] = {"benefit": "free_return_shipping", "tier": "none", "period": "2026-10",
                                      "used": 0, "allowed": 0}

    def ledger_usage(self, customer_id: str, now: Any = None) -> dict[str, Any]:
        self._enter("ledger_usage", customer_id)
        return dict(self.usage)


class FakeDomain:
    """All fakes plus the shared call log and failure queues."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple, dict]] = []
        self.failures: dict[str, list[BaseException]] = {}
        self.trace = FakeTrace(self.calls, self.failures)
        self.orders = FakeOrders(self.calls, self.failures)
        self.store = FakeStore()
        self.options = FakeOptions(self.calls, self.failures)
        self.options.store = self.store
        self.quotes = FakeQuotes(self.calls, self.failures, self.options)
        self.execution = FakeExecution(self.calls, self.failures)
        self.cases = FakeCases(self.calls, self.failures)
        self.runs = FakeRuns(self.calls, self.failures, self.trace)
        self.conversations = FakeConversations(self.calls, self.failures)
        self.gateway = FakeGateway(self.calls, self.failures)
        self.benefits = FakeBenefits(self.calls, self.failures)
        self.services = self.services_without()

    def services_without(self, *skip: str) -> Services:
        """A ``Services`` with all fakes except ``skip`` (those fall through to the real import)."""
        parts = {
            "orders": self.orders, "options": self.options, "quotes": self.quotes,
            "execution": self.execution, "cases": self.cases, "trace": self.trace, "runs": self.runs,
            "conversations": self.conversations, "gateway": self.gateway, "store": self.store,
            "benefits": self.benefits,
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

