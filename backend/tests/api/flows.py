"""Builders that put real domain data in the moto table, the way the agent's tools would."""

from __future__ import annotations

from dataclasses import dataclass

from returns_domain import conversations, options, orders, quotes, runs, store

HERO_ORDER = "DEMO-1001"
HERO_LINE = "DEMO-1001-L1"
HERO_DEADLINE = "2026-10-17"


@dataclass
class QuoteWorld:
    customer_id: str
    conv_id: str
    run_id: str
    quote: quotes.Quote

    @property
    def quote_id(self) -> str:
        return self.quote.quote_id

    def option(self, option_type: str) -> quotes.QuoteOption:
        return next(o for o in self.quote.options if o.type == option_type)


def make_quote(
    customer_id: str,
    *,
    order_id: str = HERO_ORDER,
    line_id: str = HERO_LINE,
    finish_run: bool = True,
) -> QuoteWorld:
    """A customer run that reaches ``quote_created`` for the hero size exchange (default variant).

    Options: DC exchange, store-ship exchange, store-pickup exchange, in-store return, mail return.
    The run that produced the quote is finished by default (the UI waits for ``run_completed``).
    """
    conv_id = conversations.create_conversation(customer_id, order_id)
    run_id = store.new_id("RUN-", 10)
    assert runs.try_acquire_lease(conv_id, run_id)
    assert runs.create_run(customer_id, conv_id, run_id, "customer")
    check = orders.check_policy(
        customer_id, run_id, order_id, line_id, 1, "FIT_SIZE", "unworn", "too_small"
    )
    candidates = options.find_options(
        customer_id, check.policy_check_id, deadline_date=HERO_DEADLINE, run_id=run_id
    )
    quote = quotes.create_quote(
        customer_id, run_id, conv_id, candidates.candidate_set_id, [o.option_id for o in candidates.options]
    )
    if finish_run:
        assert runs.claim_run(run_id)
        assert runs.finish_run(run_id, "completed", {})
    return QuoteWorld(customer_id, conv_id, run_id, quote)
