"""David's bundle through the API: the quote and transaction views of the domain reach the client unchanged.

The routes return the domain's views as they are (domain v2 phase 2, S17B): ``lines``, the refund total
and the net refund, the fee, the Platinum perk and the per-line reservation. A bundle quote also says
which conversation it belongs to, because the requote route is addressed with that conversation id.
"""

from __future__ import annotations

import json

import pytest

from returns_domain import execution, quotes, store
from tests.bundle_world import DAVID, ORDER, PERK_OCTOBER, Bundles, line_id
from tests.conftest import Flow

from .conftest import session_token


@pytest.fixture
def bundles(flow: Flow) -> Bundles:
    return Bundles(flow)


def test_a_bundle_quote_reaches_the_client_with_its_lines_the_perk_and_its_conversation(client, bundles: Bundles):
    hq = bundles.quote()
    reply = client.get(f"/api/quotes/{hq.quote_id}", token=session_token(DAVID))
    assert reply.status == 200
    quote = reply.json
    assert quote["conv_id"] == hq.conv_id and quote["order_id"] == ORDER  # what the requote route is addressed with
    assert [line["line_id"] for line in quote["lines"]] == [line_id(2), line_id(5), line_id(8)]
    option = next(o for o in quote["options"] if o["type"] == "return_mail")
    assert (option["refund_total_cents"], option["net_refund_cents"], option["fee_cents"], option["qty"]) == (41997, 41997, 0, 3)
    assert option["benefit"] == PERK_OCTOBER
    assert [(line["product_name"], line["category"]) for line in option["lines"]] == [("Brando", "boot"), ("Oscar", "sneaker"), ("Patten", "loafer")]
    assert option["payload_hash"] and option["decision"] == "eligible"
    # the route adds nothing and drops nothing: it is the domain's own view
    assert quote == json.loads(json.dumps(store.json_safe(quotes.get_quote(DAVID, hq.quote_id))))


def test_a_bundle_transaction_reaches_the_client_with_its_lines_the_fee_the_perk_and_the_reservation(client, bundles: Bundles, no_flush):
    hq = bundles.approved()
    result = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    reply = client.get(f"/api/transactions/{result.transaction_id}", token=session_token(DAVID))
    assert reply.status == 200
    txn = reply.json
    assert (txn["option_type"], txn["fee_cents"], txn["refund_total_cents"], txn["net_refund_cents"]) == ("return_mail", 0, 41997, 41997)
    assert txn["benefit"] == PERK_OCTOBER and "Mail the 3 items" in txn["next_step"]
    assert [line["line_id"] for line in txn["lines"]] == [line_id(2), line_id(5), line_id(8)]
    assert all("policy_check_id" not in line for line in txn["lines"])
    assert [(l["line_id"], l["returned_or_reserved_qty"], l["purchased_qty"]) for l in txn["reservation"]["lines"]] == [
        (line_id(2), 1, 1), (line_id(5), 1, 1), (line_id(8), 1, 1)]


def test_the_perk_is_on_the_a2a_stream_without_a_customer_id(client, bundles: Bundles, no_flush):
    hq = bundles.approved()
    result = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    events = client.get("/api/a2a/events", token=session_token(DAVID)).json["events"]
    (perk,) = [e for e in events if e["type"] == "benefit_applied"]
    assert perk["actor"] == "aldo_agent"
    assert perk["payload"]["transaction_id"] == result.transaction_id and perk["payload"]["order_id"] == ORDER
    assert {"benefit", "tier_label", "saved_cents", "fee_cents", "used", "per_month", "period"} <= set(perk["payload"])
    assert DAVID not in json.dumps(perk)


def test_another_owner_cannot_read_the_bundle(client, bundles: Bundles, no_flush):
    hq = bundles.approved()
    result = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    other = session_token("DEMO-CUST-AVA")
    assert client.get(f"/api/quotes/{hq.quote_id}", token=other).status in (403, 404)
    assert client.get(f"/api/transactions/{result.transaction_id}", token=other).status in (403, 404)
