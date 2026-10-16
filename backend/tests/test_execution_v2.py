"""execution.py and quotes.py, domain v2: the store notice, the colour exchange and the A2A mirrors.

* Approving an in-store exchange or return puts ONE ``STORENOTE#{txn}`` in the same
  TransactWriteItems, returns it as ``store_notice`` and mirrors ``store_notified`` (actor
  ``aldo_store``) to the A2A stream after the commit; replays write and emit nothing.
* ``create_quote`` mirrors ``quote_created`` to the A2A stream (once per quote).
* A colour exchange is a PREFERENCE write-back with ``fit_feedback = "size_ok"``.
* The member tier's authority limit applies to the policy check and to the re-check at execution.

``no_flush`` replaces the post-commit call into the gateway. John's world comes from test builders.
"""

from __future__ import annotations

import json

import pytest

from returns_domain import audit, execution, fixtures, memory_updates, options, orders, quotes, store, trace
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN, Flow, HeroQuote
from tests.domain_v2 import DC, JOHN, LAVAL, MEN_NAME, MTL, TOR, john_order, men_sku, v2_world  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_flush")

BROWN_95, BLACK_95 = men_sku("BROWN", "9.5"), men_sku("BLACK", "9.5")
ORDER, LINE = "DEMO-A2A-5A1B2C", "DEMO-A2A-5A1B2C-L1"
PICKUP_TEXT = "ALDO Sainte-Catherine will have your Brown pair ready, John is about 5 minutes away"


@pytest.fixture
def world(v2_world):
    john_order(ORDER)
    return Flow()


def colour_quote(world: Flow, *, color="BROWN", types=("exchange_store_pickup", "return_in_store"), customer=JOHN,
                 reason="CHANGE_OF_MIND") -> HeroQuote:
    conv_id, run_id = world.start_run(customer)
    check = orders.check_policy(customer, run_id, ORDER, LINE, 1, reason, "unworn", "unknown")
    cand = options.find_options(customer, check.policy_check_id, None, None, None, color, run_id=run_id)
    wanted = [o.option_id for kind in types for o in cand.options if o.type == kind]
    assert wanted, [o.type for o in cand.options]
    return HeroQuote(customer, conv_id, run_id, check, cand, quotes.create_quote(customer, run_id, conv_id, cand.candidate_set_id, wanted))


def a2a(customer=JOHN, *types):
    return [e for e in trace.list_a2a_events(customer).events if not types or e["type"] in types]


def keys(prefix):
    return sorted(store.scan_keys((prefix,)))


def atp(sku, location):
    row = store.get_item(store.inventory_key(sku, location))
    return row["atp"], row["version"]


# ------------------------------------------------------------------ quote_created mirror


def test_create_quote_mirrors_quote_created_to_the_a2a_stream_once(world):
    hq = colour_quote(world)
    (event,) = a2a(JOHN)
    assert (event["type"], event["actor"]) == ("quote_created", "aldo_agent")
    # one type per option, in the quote's order (John's two pickup stores are two options)
    assert event["payload"] == {"quote_id": hq.quote_id, "primary_option_type": "exchange_store_pickup",
                                "option_types": ["exchange_store_pickup", "exchange_store_pickup", "return_in_store"]}
    assert event["payload"]["option_types"] == [o.type for o in hq.quote.options]
    assert "DEMO-CUST" not in json.dumps(event)
    # a retry returns the same quote and mirrors nothing again
    again = quotes.create_quote(JOHN, hq.run_id, hq.conv_id, hq.candidates.candidate_set_id, [o.option_id for o in hq.quote.options])
    assert again.quote_id == hq.quote_id and len(a2a(JOHN, "quote_created")) == 1
    # the first option of the quote leads, whatever kind it is
    refund_first = colour_quote(world, types=("return_in_store", "exchange_store_pickup"))
    assert a2a(JOHN, "quote_created")[-1]["payload"]["primary_option_type"] == "return_in_store"
    assert a2a(JOHN, "quote_created")[-1]["payload"]["quote_id"] == refund_first.quote_id


def test_the_quote_mirror_is_the_owners_and_does_not_need_the_run_to_exist(v2_world):
    john_order(ORDER)
    flow = Flow()
    conv_id, run_id = flow.start_run(JOHN)
    check = orders.check_policy(JOHN, run_id, ORDER, LINE, 1, "EXPECTATION", "unworn", "unknown")
    cand = options.find_options(JOHN, check.policy_check_id, None, None, None, "TAN", run_id=run_id)
    quotes.create_quote(JOHN, "RUN-NOT-STORED", conv_id, cand.candidate_set_id, [cand.options[0].option_id, cand.options[-1].option_id])
    assert [e["type"] for e in a2a(JOHN)] == ["quote_created"] and a2a(AVA) == []


def test_a_failed_quote_mirrors_nothing(world):
    hq = colour_quote(world)
    store.update_item(store.inventory_key(BROWN_95, MTL), add={"atp": -1, "version": 1})
    conv_id, run_id = world.start_run(JOHN)
    before = a2a(JOHN)
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(JOHN, run_id, conv_id, hq.candidates.candidate_set_id, [hq.candidates.options[2].option_id, hq.candidates.options[-1].option_id])
    assert exc.value.code == ErrorCode.STOCK_CHANGED and a2a(JOHN) == before


# ------------------------------------------------------- colour exchange at the store


def test_a_colour_exchange_in_store_writes_the_notice_in_the_same_transaction(world, snapshot):
    hq = colour_quote(world)
    world.approve(hq, "exchange_store_pickup")
    assert keys("STORENOTE#") == []  # nothing before the customer's click
    result = execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)

    notice = {"store_id": MTL, "store_name": "ALDO Sainte-Catherine", "display_name": "John", "tier_label": "Silver member",
              "eta_minutes": 5, "summary": PICKUP_TEXT}
    assert result.option_type == "exchange_store_pickup" and result.replayed is False and result.store_notice == notice
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert txn["store_notice"] == notice
    assert (txn["sku"], txn["replacement_color"], txn["replacement_size"], txn["original_size"]) == (BROWN_95, "BROWN", "9.5", "9.5")
    assert txn["next_step"] == "ALDO Sainte-Catherine will have your Brown pair, size 9.5, ready from Sat Oct 17; bring the original pair, unworn."

    assert keys("STORENOTE#") == [(f"STORENOTE#{result.transaction_id}", "META")]
    note = store.get_item(store.storenote_key(result.transaction_id))
    assert note == {"pk": f"STORENOTE#{result.transaction_id}", "sk": "META", "transaction_id": result.transaction_id,
                    "option_type": "exchange_store_pickup", "order_id": ORDER, **notice, "status": "notified",
                    "created_at": txn["created_at"]}
    assert "customer_id" not in note and "DEMO-CUST" not in json.dumps(note)

    assert atp(BROWN_95, MTL) == (1, 2) and atp(BROWN_95, DC) == (4, 1)  # one pair reserved at the store he will visit
    assert store.get_item(store.line_key(ORDER, LINE))["returned_or_reserved_qty"] == 1
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "executed"

    view = execution.get_status(JOHN, result.transaction_id)
    assert view.store_notice == notice and view.replacement_color == "BROWN" and view.next_step == txn["next_step"]
    assert "customer_id" not in view.model_dump()

    before = snapshot(exclude=("AUDIT#",))
    again = execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)  # a replay: same notice, nothing written, nothing emitted
    assert again.replayed is True and again.store_notice == notice and again.transaction_id == result.transaction_id
    assert snapshot(exclude=("AUDIT#",)) == before
    assert len(keys("STORENOTE#")) == 1


def test_store_notified_comes_after_transaction_created_and_only_once(world):
    hq = colour_quote(world)
    world.approve(hq, "exchange_store_pickup")
    result = execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)
    execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)
    events = a2a(JOHN)
    assert [(e["type"], e["actor"]) for e in events] == [
        ("quote_created", "aldo_agent"), ("transaction_created", "aldo_agent"), ("store_notified", "aldo_store")]
    assert events[-1]["payload"] == {"store_name": "ALDO Sainte-Catherine", "eta_minutes": 5, "summary": PICKUP_TEXT}
    assert events[1]["payload"]["transaction_id"] == result.transaction_id
    assert "DEMO-CUST" not in json.dumps(events) and "John (" not in json.dumps(events)
    run_events = [e["type"] for e in trace.list_events(hq.run_id).events]
    assert "store_notified" not in run_events  # the store desk speaks on the A2A timeline only


def test_the_write_back_of_a_colour_exchange_confirms_the_size(world):
    hq = colour_quote(world)
    world.approve(hq, "exchange_store_pickup")
    result = execution.execute(JOHN, hq.quote_id)
    payload = store.get_item(store.outbox_key(result.memory_update_id))["payload"]
    assert payload["reason"] == "PREFERENCE" and payload["fit_feedback"] == "size_ok"
    assert (payload["original_size"], payload["new_size"]) == ("9.5", "9.5")
    assert (payload["outcome"], payload["provisional"], payload["color"], payload["owner_ref"]) == ("exchange_created", True, "BLACK", JOHN)
    assert payload["product_name"] == MEN_NAME and payload["style_id"] == "DEMO-STYLE-MEN001"
    assert store.get_item(store.txn_key(result.transaction_id))["memory_reason"] == "PREFERENCE"
    kept = memory_updates.update_from_txn_item(store.get_item(store.txn_key(result.transaction_id)), outcome="exchange_completed_kept")
    assert (kept["reason"], kept["fit_feedback"], kept["provisional"]) == ("PREFERENCE", "size_ok", False)


def test_a_colour_exchange_shipped_from_the_dc_has_no_store_notice(world):
    hq = colour_quote(world, types=("exchange_ship_from_dc", "return_mail"))
    world.approve(hq, "exchange_ship_from_dc")
    result = execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)
    assert result.store_notice is None and keys("STORENOTE#") == []
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert "store_notice" not in txn and txn["replacement_color"] == "BROWN"
    assert txn["next_step"] == ("Your Brown pair, size 9.5, ships from ALDO Demo Distribution Centre and should arrive by "
                                "Tue Oct 20; send back the original pair, unworn.")
    assert atp(BROWN_95, DC) == (3, 2)
    assert [e["type"] for e in a2a(JOHN)] == ["quote_created", "transaction_created"]
    assert execution.get_status(JOHN, result.transaction_id).store_notice is None


def test_a_return_in_store_notifies_the_nearest_store(world):
    hq = colour_quote(world, color=None, types=("return_in_store", "return_mail"))
    world.approve(hq, "return_in_store")
    result = execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)
    assert result.memory_update_id is None and keys("A2AOUT#") == []  # a return has no size write-back
    assert result.store_notice == {
        "store_id": MTL, "store_name": "ALDO Sainte-Catherine", "display_name": "John", "tier_label": "Silver member",
        "eta_minutes": 5,
        "summary": f"ALDO Sainte-Catherine is ready for your return of {MEN_NAME} in size 9.5, John is about 5 minutes away"}
    assert keys("STORENOTE#") == [(f"STORENOTE#{result.transaction_id}", "META")]
    assert store.get_item(store.storenote_key(result.transaction_id))["option_type"] == "return_in_store"
    assert atp(BROWN_95, MTL) == (2, 1) and atp(BLACK_95, MTL) == (0, 1)  # no stock moves for a return
    assert [e["type"] for e in a2a(JOHN)] == ["quote_created", "transaction_created", "store_notified"]
    assert a2a(JOHN, "store_notified")[0]["payload"]["eta_minutes"] == 5


def test_a_mail_return_tells_no_store(world):
    hq = colour_quote(world, color=None, types=("return_mail", "return_in_store"))
    world.approve(hq, "return_mail")
    result = execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)
    assert result.store_notice is None and keys("STORENOTE#") == []
    assert [e["type"] for e in a2a(JOHN)] == ["quote_created", "transaction_created"]


def test_a_commit_that_fails_leaves_no_store_notice_and_no_event(world, snapshot, monkeypatch):
    hq = colour_quote(world)
    world.approve(hq, "exchange_store_pickup")
    real = store.transact_write

    def steal_then_commit(ops, **kwargs):
        store.update_item(store.inventory_key(BROWN_95, MTL), set={"atp": 0})  # someone else took the last pairs
        return real(ops, **kwargs)

    monkeypatch.setattr(store, "transact_write", steal_then_commit)
    events_before = a2a(JOHN)
    with pytest.raises(DomainError) as exc:
        execution.execute(JOHN, hq.quote_id, run_id=hq.run_id)
    assert exc.value.code == ErrorCode.STOCK_CHANGED
    assert keys("STORENOTE#") == [] and keys("TXN#") == [] and a2a(JOHN) == events_before


def test_the_notice_does_not_block_a_customer_whose_distance_is_not_on_file(flow):
    """Ava has no travel times: the store still hears about her, without a time of arrival."""
    hq = flow.hero_quote()
    flow.approve(hq, "exchange_store_pickup")
    result = execution.execute(AVA, hq.quote_id, run_id=hq.run_id)
    assert result.store_notice == {
        "store_id": MTL, "store_name": "ALDO Sainte-Catherine", "display_name": "Ava", "tier_label": "Member",
        "eta_minutes": None, "summary": "ALDO Sainte-Catherine will have your size 7.5 ready for Ava"}
    assert a2a(AVA, "store_notified")[0]["payload"] == {
        "store_name": "ALDO Sainte-Catherine", "eta_minutes": None,
        "summary": "ALDO Sainte-Catherine will have your size 7.5 ready for Ava"}
    # a fit-size pickup keeps its original next_step text
    assert execution.get_status(AVA, result.transaction_id).next_step == (
        "Pick up size 7.5 at ALDO Sainte-Catherine from Sat Oct 17; bring the original pair, unworn.")
    assert "replacement_color" not in store.get_item(store.txn_key(result.transaction_id))


@pytest.mark.parametrize(
    ("minutes", "phrase"),
    [(0, "less than a minute away"), (1, "about 1 minute away"), (2, "about 2 minutes away"), (120, "about 120 minutes away"),
     (None, ""), (-1, ""), (True, ""), ("5", "")],
)
def test_the_distance_phrase(minutes, phrase):
    assert execution._distance_text(minutes) == phrase


def test_the_notice_and_next_step_speak_as_the_store_and_never_name_another_agent(world):
    """Addendum 12.1: ALDO bot is the only brand; the store and ALDO answer, no 'Returns Agent' exists for the user."""
    texts = []
    for kind, color in (("exchange_store_pickup", "BROWN"), ("return_in_store", None)):
        hq = colour_quote(world, color=color, types=(kind, "return_mail"))
        world.approve(hq, kind)
        result = execution.execute(JOHN, hq.quote_id)
        texts += [result.store_notice["summary"], store.get_item(store.txn_key(result.transaction_id))["next_step"]]
        store.update_item(store.line_key(ORDER, LINE), set={"returned_or_reserved_qty": 0})  # the next quote may use the pair again
    assert len(texts) == 4
    for text in texts:
        lowered = text.lower()
        assert "agent" not in lowered and "simulated" not in lowered and "demo assumption" not in lowered
        assert "ALDO Sainte-Catherine" in text


def test_reset_clears_the_store_notices(world):
    hq = colour_quote(world)
    world.approve(hq, "exchange_store_pickup")
    execution.execute(JOHN, hq.quote_id)
    assert keys("STORENOTE#")
    fixtures.reset("default")
    assert keys("STORENOTE#") == [] and keys("TXN#") == []


# ------------------------------------------------------------------ the member tier limit


def test_a_platinum_member_can_return_a_boot_above_250_and_the_limit_is_rechecked_at_execution(flow):
    ben_boot = dict(customer_id=BEN, order_id="DEMO-1005", line_id="DEMO-1005-L1", reason="CHANGE_OF_MIND",
                    direction="unknown", deadline=None, types=("return_mail",))
    with pytest.raises(DomainError) as refused:  # no tier: CAD 289.00 is above the CAD 250.00 limit, so no options at all
        flow.hero_quote(**ben_boot)
    assert refused.value.code == ErrorCode.VALIDATION_ERROR and "decision=needs_review" in refused.value.message
    silver = orders.check_policy(BEN, "RUN-S", "DEMO-1005", "DEMO-1005-L1", 1, "CHANGE_OF_MIND", "unworn", "unknown")
    assert silver.result["decision"] == "needs_review" and silver.result["facts"]["authority_limit_cents"] == 25000

    store.update_item(store.customer_key(BEN), set={"tier": "platinum"})
    hq = flow.hero_quote(**ben_boot)
    assert hq.check.result["decision"] == "eligible" and hq.check.result["facts"]["authority_limit_cents"] == 75000
    flow.approve(hq, "return_mail")

    store.update_item(store.customer_key(BEN), set={"tier": "silver"})  # the tier changed before the commit
    with pytest.raises(DomainError) as exc:
        execution.execute(BEN, hq.quote_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "decision=needs_review" in exc.value.message
    assert keys("TXN#") == []
    assert audit.list_audit(action="execute_denied")[-1]["refs"]["code"] == "VALIDATION_ERROR"

    store.update_item(store.customer_key(BEN), set={"tier": "platinum"})
    result = execution.execute(BEN, hq.quote_id)
    assert result.option_type == "return_mail" and len(keys("TXN#")) == 1


def test_the_default_limit_stays_250_for_customers_without_a_tier(seeded):
    check = orders.check_policy(BEN, "RUN-1", "DEMO-1005", "DEMO-1005-L1", 1, "FIT_SIZE", "unworn", "too_small")
    assert check.result["decision"] == "needs_review" and check.result["rule_ids"] == ["authority.refund_limit"]
    assert check.result["facts"]["authority_limit_cents"] == 25000 and check.refund_cents == 28900
