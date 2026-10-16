"""Shared pytest fixtures for the persistence and services tests (moto DynamoDB, no real AWS).

* ``aws_env``: fake credentials, the table name, a signing key, the repo fixtures directory and
  a cleared set of the variables ``returns_domain.config`` reads. Never touches real AWS.
* ``ddb``: ``aws_env`` + ``mock_aws`` + an empty table (``store.create_table``).
* ``seeded``: ``ddb`` + ``fixtures.reset("default")``.

The pure-logic tests in ``tests/unit`` have their own hermetic conftest and do not use these.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from moto import mock_aws

from returns_domain import clock, config, fit, fixtures, store

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "fixtures"

# Variables returns_domain.config (and the AWS SDK) read; cleared so a developer shell
# cannot change a result or point a test at real AWS.
_CLEARED = (
    "AWS_PROFILE",
    "AWS_SESSION_TOKEN",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_DYNAMODB",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "DEMO_NOW",
    "SECRETS_ARN",
    "DEMO_ADMIN_KEY",
    "WRITEBACK_KEY",
    "BEDROCK_MODEL_ID",
    "BEDROCK_FALLBACK_MODEL_ID",
    "MAX_ROUNDS",
    "MAX_MESSAGE_CHARS",
    "TOKEN_TTL_SECONDS",
    "QUOTE_TTL_SECONDS",
    "EXECUTE_WINDOW_SECONDS",
    "RUN_LEASE_SECONDS",
    "CONVERSATION_MAX_BYTES",
    "PERSONAL_AGENT_TABLE_NAME",
    "GATEWAY_TRANSPORT",
    "GATEWAY_BASE_URL",
    "DELEGATION_MAX_CENTS",
)


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CLEARED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("TABLE_NAME", "aldo-returns-demo")
    monkeypatch.setenv("DEMO_TOKEN_KEY", "test-only-token-key-0123456789")
    monkeypatch.setenv("FIXTURES_DIR", str(FIXTURES_ROOT))
    config.clear_secret_cache()
    fit.clear_cache()
    fixtures.clear_cache()
    store.reset_clients()


@pytest.fixture
def ddb(aws_env: None) -> Iterator[None]:
    with mock_aws():
        store.reset_clients()
        store.create_table()
        yield
    store.reset_clients()


@pytest.fixture
def seeded(ddb: None) -> None:
    fixtures.reset("default")


@pytest.fixture
def no_flush(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the post-commit write-back flush of ``execution.execute`` with a recorder.

    The real flush calls ``gateway.flush_outbox`` (another stream's module and a network
    transport). The recorder lists the customer ids it was called for and leaves outbox items
    pending; ``execution._sleep`` is a no-op so no test waits.
    """
    from returns_domain import execution

    class FlushRecorder(list):
        original = None  # the real ``_flush_outbox``, for tests that need it back

    calls = FlushRecorder()
    calls.original = execution._flush_outbox
    monkeypatch.setattr(execution, "_flush_outbox", lambda customer_id: calls.append(customer_id) or 0)
    monkeypatch.setattr(execution, "_sleep", lambda seconds: None)
    return calls


AVA = "DEMO-CUST-AVA"
BEN = "DEMO-CUST-BEN"
CHLOE = "DEMO-CUST-CHLOE"


@pytest.fixture
def snapshot(ddb: None):
    """``snapshot()`` returns every item of the table as ``{(pk, sk): item}``.

    ``snapshot(exclude=("AUDIT#",))`` leaves out the given pk prefixes. Tests compare a snapshot
    before and after a call to prove that a denied or replayed call wrote nothing.
    """
    from returns_domain import store

    def take(exclude: tuple[str, ...] = ()) -> dict:
        items: dict = {}
        client = store.get_client()
        kwargs = {"TableName": store.table_name(), "ConsistentRead": True}
        while True:
            page = client.scan(**kwargs)
            for raw in page["Items"]:
                item = store._deserialize_item(raw)
                if not item["pk"].startswith(exclude):
                    items[(item["pk"], item["sk"])] = item
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    return take


def make_agent_order(
    order_id: str = "DEMO-A2A-81B20E",
    customer_id: str = AVA,
    *,
    sku: str = "DEMO-SKU-HERO-BLACK-080",
    size: str = "8.0",
    delivered_at: str | None = None,
    price_cents: int = 12999,
) -> str:
    """Write an order shaped like the gateway's purchase (channel agent, delivered_at null)."""
    from returns_domain import fixtures, store

    product = next(p for p in fixtures.load_bundle().products if p["sku"] == sku)
    order = {
        "order_id": order_id,
        "customer_id": customer_id,
        "banner": "ALDO_CA",
        "currency": "CAD",
        "channel": "agent",
        "ordered_at": "2026-10-16",
        "delivered_at": delivered_at,
        "size_request_id": "SZR-3F9A1C",
        "fulfillment": {"type": "store_pickup", "location_id": "DEMO-STORE-MTL-01",
                        "location_name": "ALDO Sainte-Catherine", "eta_date": "2026-10-17"},
        "source": "synthetic",
    }
    line = {
        "line_id": f"{order_id}-L1",
        "sku": sku,
        "product_name": product["product_name"],
        "style": product["style"],
        "size": size,
        "color": product["color"],
        "purchased_qty": 1,
        "returned_or_reserved_qty": 0,
        "unit_paid_cents": price_cents,
        "final_sale": False,
        "source": "synthetic",
    }
    for item in store.order_items(order, [line]):
        store.put_item(item)
    return order_id


class HeroQuote:
    """A quote built through the real services: run, policy check, options, quote."""

    def __init__(self, customer_id, conv_id, run_id, check, candidates, quote):
        self.customer_id, self.conv_id, self.run_id = customer_id, conv_id, run_id
        self.check, self.candidates, self.quote = check, candidates, quote

    @property
    def quote_id(self) -> str:
        return self.quote.quote_id

    def option(self, option_type: str):
        """The quote option of this type (raises AssertionError when the quote has none)."""
        for option in self.quote.options:
            if option.type == option_type:
                return option
        raise AssertionError(f"{option_type} is not in the quote: {[o.type for o in self.quote.options]}")


class Flow:
    """Drives the hero path against the moto table. Every call goes through the real modules."""

    def start_run(self, customer_id: str = AVA, conv_id: str | None = None, kind: str = "customer"):
        from returns_domain import conversations, runs, store

        conv_id = conv_id or conversations.create_conversation(customer_id)
        run_id = store.new_id("RUN-", 10)
        runs.create_run(customer_id, conv_id, run_id, kind)
        runs.claim_run(run_id)
        return conv_id, run_id

    def hero_quote(
        self,
        customer_id: str = AVA,
        *,
        order_id: str = "DEMO-1001",
        line_id: str = "DEMO-1001-L1",
        qty: int = 1,
        reason: str = "FIT_SIZE",
        condition: str = "unworn",
        direction: str = "too_small",
        replacement_size: str | None = None,
        deadline: str | None = "2026-10-17",
        types: tuple[str, ...] = ("exchange_store_pickup", "exchange_ship_from_dc", "return_in_store"),
        conv_id: str | None = None,
    ) -> HeroQuote:
        from returns_domain import options, orders, quotes

        conv_id, run_id = self.start_run(customer_id, conv_id)
        check = orders.check_policy(customer_id, run_id, order_id, line_id, qty, reason, condition, direction)
        candidates = options.find_options(
            customer_id, check.policy_check_id, replacement_size, deadline, None, run_id=run_id
        )
        wanted = []
        for option_type in types:
            wanted += [o.option_id for o in candidates.options if o.type == option_type]
        assert wanted, f"none of {types} offered: {[o.type for o in candidates.options]}"
        quote = quotes.create_quote(customer_id, run_id, conv_id, candidates.candidate_set_id, wanted)
        return HeroQuote(customer_id, conv_id, run_id, check, candidates, quote)

    def approve(self, hq: HeroQuote, option_type: str = "exchange_store_pickup"):
        from returns_domain import quotes, store

        option = hq.option(option_type)
        approval, created = quotes.approve(
            hq.customer_id, hq.quote_id, option.option_id, option.payload_hash, store.new_id("RUN-", 10)
        )
        assert created
        return approval


@pytest.fixture
def flow(seeded: None) -> Flow:
    return Flow()


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch):
    """Freeze ``clock.now_utc`` (the wall clock) and return a setter to move it.

    Usage: ``fixed_now.set("2026-10-16T16:00:00+00:00"); fixed_now.advance(seconds=900)``.
    The default instant is 2026-10-16T16:00:00+00:00.
    """
    from datetime import datetime, timedelta

    class _Now:
        def __init__(self) -> None:
            self.value = clock.parse_iso("2026-10-16T16:00:00+00:00")

        def set(self, text: str) -> None:
            self.value = clock.parse_iso(text)

        def advance(self, **kwargs: float) -> None:
            self.value = self.value + timedelta(**kwargs)

        def __call__(self) -> datetime:
            return self.value

    now = _Now()
    monkeypatch.setattr(clock, "now_utc", now)
    return now
