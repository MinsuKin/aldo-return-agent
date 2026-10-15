"""A test world for bundle returns (domain v2 phase 2): David's ten-line order and a silver twin of it.

Everything runs against the SHIPPED fixtures (``fixtures/demo/*_v2.json``): David is
``DEMO-CUST-DAVID`` (platinum, every store 430+ minutes away), his order is ``DEMO-3001`` with ten
one-unit lines ``DEMO-3001-L1`` .. ``L10``; the script returns L2 (tan boot, 17999), L5 (white
sneaker, 10999) and L8 (navy loafer, 12999) = 41997, and optionally L10 (grey sneaker, 9999).
John is silver (limit 25000) and 5 minutes from the Montreal store; ``silver_twin_order`` copies four
of David's lines to an order of his so the same totals can be tried on another tier.
"""

from __future__ import annotations

from typing import Any

from returns_domain import options, orders, quotes, store
from tests.conftest import Flow, HeroQuote

DAVID = "DEMO-CUST-DAVID"
JOHN = "DEMO-CUST-JOHN"
ORDER = "DEMO-3001"
MTL = "DEMO-STORE-MTL-01"
DC = "DEMO-DC-QC"
STANDARD_FEE = 595
#: unit prices of the lines the script touches
PRICES = {2: 17999, 5: 10999, 8: 12999, 10: 9999}
SCRIPT = (2, 5, 8)
PERK_OCTOBER = {
    "type": "free_return_shipping",
    "label": "Platinum free return shipping",
    "saved_cents": 595,
    "used": 1,
    "allowed": 1,
    "period": "2026-10",
}
NEXT_MONTH_NOW = "2026-11-02T12:00:00-05:00"  # 20 days after David's delivery, still inside the window


def line_id(n: int, order: str = ORDER) -> str:
    return f"{order}-L{n}"


def silver_twin_order(order_id: str = "DEMO-A2A-5B1B2C", customer_id: str = JOHN) -> str:
    """Copy David's L2, L5, L8 and L10 to an order of ``customer_id`` (delivered 3 days before DEMO_NOW)."""
    source_order, source_lines = orders.load_order_items(DAVID, ORDER)
    order = {
        "order_id": order_id, "customer_id": customer_id, "banner": "ALDO_CA", "currency": "CAD", "channel": "web",
        "ordered_at": "2026-10-09", "delivered_at": "2026-10-13", "source": "synthetic",
    }
    lines = []
    for n in PRICES:
        copy = {k: v for k, v in source_lines[line_id(n)].items() if k not in ("pk", "sk", "order_id")}
        copy.update({"line_id": line_id(n, order_id), "returned_or_reserved_qty": 0})
        lines.append(copy)
    for item in store.order_items(order, lines):
        store.put_item(item)
    return order_id


class Bundles:
    """Drive the bundle path through the real modules (run, policy checks, options, quote, approval)."""

    def __init__(self, flow: Flow) -> None:
        self.flow = flow

    def checks(
        self,
        customer_id: str,
        order_id: str,
        line_ids: list[str],
        run_id: str,
        *,
        reason: str = "CHANGE_OF_MIND",
        condition: str = "unworn",
        qty: int = 1,
    ) -> list[str]:
        return [
            orders.check_policy(customer_id, run_id, order_id, lid, qty, reason, condition, "unknown").policy_check_id
            for lid in line_ids
        ]

    def find(
        self,
        customer_id: str = DAVID,
        numbers: tuple[int, ...] = SCRIPT,
        *,
        order_id: str = ORDER,
        reason: str = "CHANGE_OF_MIND",
    ):
        """``(conv_id, run_id, candidate_set)`` for the numbered lines of ``order_id``."""
        conv_id, run_id = self.flow.start_run(customer_id)
        ids = self.checks(customer_id, order_id, [line_id(n, order_id) for n in numbers], run_id, reason=reason)
        return conv_id, run_id, options.find_options(customer_id, policy_check_ids=ids, run_id=run_id)

    def quote(
        self,
        customer_id: str = DAVID,
        numbers: tuple[int, ...] = SCRIPT,
        *,
        order_id: str = ORDER,
        types: tuple[str, ...] = ("return_mail",),
        reason: str = "CHANGE_OF_MIND",
    ) -> HeroQuote:
        conv_id, run_id, cand = self.find(customer_id, numbers, order_id=order_id, reason=reason)
        wanted = [o.option_id for kind in types for o in cand.options if o.type == kind]
        assert wanted, [o.type for o in cand.options]
        quote = quotes.create_quote(customer_id, run_id, conv_id, cand.candidate_set_id, wanted)
        first_check = orders.get_policy_check(customer_id, cand.policy_check_id)
        return HeroQuote(customer_id, conv_id, run_id, first_check, cand, quote)

    def approved(self, customer_id: str = DAVID, numbers: tuple[int, ...] = SCRIPT, *, kind: str = "return_mail", **kw) -> HeroQuote:
        hq = self.quote(customer_id, numbers, types=(kind,), **kw)
        self.flow.approve(hq, kind)
        return hq


def keys(prefix: str) -> list[tuple[str, str]]:
    return sorted(store.scan_keys((prefix,)))


def ledger(customer_id: str = DAVID, period: str = "2026-10") -> dict[str, Any] | None:
    return store.get_item(store.benefit_key(customer_id, period))


def reserved(order_id: str, numbers: tuple[int, ...]) -> dict[int, int]:
    return {n: store.get_item(store.line_key(order_id, line_id(n, order_id)))["returned_or_reserved_qty"] for n in numbers}


def a2a(customer_id: str, *types: str) -> list[dict[str, Any]]:
    from returns_domain import trace

    return [e for e in trace.list_a2a_events(customer_id).events if not types or e["type"] in types]
