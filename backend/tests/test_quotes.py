"""quotes.py: payload hashes, supersede, stock versions and the one-time approval (G-06, G-17, G-26)."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest

from returns_domain import audit, conversations, options, orders, quotes, runs, store, trace
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN

MTL, LAVAL, TOR, DC = "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01", "DEMO-DC-QC"
OPTION = {"option_id": "OPT-3", "type": "exchange_store_pickup", "sku": "DEMO-SKU-HERO-BLACK-075", "location_id": MTL,
          "qty": 1, "refund_cents": 0, "fee_cents": 0, "currency": "CAD", "eta_date": "2026-10-17"}


def expected_hash(quote_id, option, policy_version="2026.10-demo"):
    body = {"quote_id": quote_id, "option_id": option["option_id"], "type": option["type"], "sku": option["sku"],
            "location": option["location_id"], "qty": option["qty"], "refund_cents": option["refund_cents"],
            "fee_cents": option["fee_cents"], "currency": option["currency"], "policy_version": policy_version}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# --------------------------------------------------------------------------- hashing


def test_payload_hash_stable():
    digest = quotes.compute_payload_hash("QUO-1", OPTION, "2026.10-demo")
    assert digest == expected_hash("QUO-1", OPTION) and len(digest) == 64
    assert digest == quotes.compute_payload_hash("QUO-1", dict(reversed(list(OPTION.items()))), "2026.10-demo")
    other_fields = {**OPTION, "eta_date": "2099-01-01", "conditions": ["x"], "evidence": {"atp": 99}}
    assert quotes.compute_payload_hash("QUO-1", other_fields, "2026.10-demo") == digest  # only approved fields count


@pytest.mark.parametrize(
    "change",
    [{"option_id": "OPT-4"}, {"type": "exchange_ship_from_dc"}, {"sku": "DEMO-SKU-HERO-BLACK-080"}, {"location_id": LAVAL},
     {"qty": 2}, {"refund_cents": 1}, {"fee_cents": 595}, {"currency": "USD"}],
)
def test_payload_hash_changes_with_every_approved_field(change):
    assert quotes.compute_payload_hash("QUO-1", {**OPTION, **change}, "2026.10-demo") != quotes.compute_payload_hash("QUO-1", OPTION, "2026.10-demo")


def test_payload_hash_binds_quote_and_policy_version():
    base = quotes.compute_payload_hash("QUO-1", OPTION, "2026.10-demo")
    assert quotes.compute_payload_hash("QUO-2", OPTION, "2026.10-demo") != base
    assert quotes.compute_payload_hash("QUO-1", OPTION, "2026.11-demo") != base


def test_payload_hash_is_identical_after_a_table_round_trip(flow):
    hq = flow.hero_quote()
    stored = store.get_item(store.quote_key(hq.quote_id))
    for option in stored["options"]:
        assert option["payload_hash"] == quotes.compute_payload_hash(hq.quote_id, option, stored["policy_version"])
    assert [o.payload_hash for o in hq.quote.options] == [o["payload_hash"] for o in stored["options"]]


# ------------------------------------------------------------------------------ create


def test_create_quote_copies_server_amounts_and_keeps_the_agents_order(flow, fixed_now):
    hq = flow.hero_quote(types=("exchange_store_pickup", "exchange_ship_from_dc", "return_in_store"))
    quote = hq.quote
    assert quote.status == "open" and quote.currency == "CAD" and quote.policy_version == "2026.10-demo"
    assert [o.type for o in quote.options] == ["exchange_store_pickup", "exchange_ship_from_dc", "return_in_store"]
    assert [o.option_id for o in quote.options] == ["OPT-3", "OPT-1", "OPT-4"]  # candidate ids are kept
    candidates = {o.option_id: o for o in hq.candidates.options}
    for option in quote.options:
        original = candidates[option.option_id]
        assert (option.refund_cents, option.fee_cents, option.eta_date, option.sku, option.location_id) == (
            original.refund_cents, original.fee_cents, original.eta_date, original.sku, original.location_id)
    assert quote.created_at == "2026-10-16T16:00:00+00:00" and quote.expires_at == "2026-10-16T16:15:00+00:00"  # +900 s wall clock
    assert quote.order_id == "DEMO-1001" and quote.line_id == "DEMO-1001-L1" and quote.conv_id == hq.conv_id
    assert quote.run_id == hq.run_id and quote.candidate_set_id == hq.candidates.candidate_set_id
    assert quote.inventory_versions == {f"DEMO-SKU-HERO-BLACK-075|{MTL}": 1, f"DEMO-SKU-HERO-BLACK-075|{DC}": 1}
    assert store.get_item(store.quote_key(quote.quote_id))["customer_id"] == AVA
    assert quote.quote_id.startswith("QUO-") and len(quote.quote_id) == len("QUO-") + 10


def test_the_agents_order_can_be_reversed(flow):
    hq = flow.hero_quote(types=("return_in_store", "exchange_ship_from_dc", "exchange_store_pickup"))
    assert [o.type for o in hq.quote.options] == ["return_in_store", "exchange_ship_from_dc", "exchange_store_pickup"]


def test_quote_created_event_is_emitted_once(flow):
    hq = flow.hero_quote()
    events = [e for e in trace.list_events(hq.run_id).events if e["type"] == "quote_created"]
    assert len(events) == 1
    assert events[0]["payload"] == {"quote_id": hq.quote_id, "option_ids": [o.option_id for o in hq.quote.options],
                                    "expires_at": hq.quote.expires_at}


def test_create_quote_idempotent_and_supersedes(flow):
    hq = flow.hero_quote()
    ids = [o.option_id for o in hq.quote.options]
    again = quotes.create_quote(AVA, hq.run_id, hq.conv_id, hq.candidates.candidate_set_id, ids)
    assert again.quote_id == hq.quote_id and again.created_at == hq.quote.created_at  # a retry returns the same quote
    assert len(list(store.scan_keys(("QUOTE#",)))) == 1
    assert len([e for e in trace.list_events(hq.run_id).events if e["type"] == "quote_created"]) == 1
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "open"

    # a second quote in the same conversation supersedes the first
    conv_id, run2 = flow.start_run(AVA, conv_id=hq.conv_id)
    cand2 = options.find_options(AVA, hq.check.policy_check_id, None, "2026-10-17", run_id=run2)
    second = quotes.create_quote(AVA, run2, hq.conv_id, cand2.candidate_set_id, [cand2.options[0].option_id, cand2.options[-1].option_id])
    assert second.quote_id != hq.quote_id
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "superseded"
    assert store.get_item(store.quote_key(second.quote_id))["status"] == "open"
    assert conversations.get_conversation(AVA, hq.conv_id)["latest_quote_id"] == second.quote_id
    with pytest.raises(DomainError) as exc:  # approving the superseded quote is refused
        quotes.approve(AVA, hq.quote_id, hq.quote.options[0].option_id, hq.quote.options[0].payload_hash, "RUN-9")
    assert exc.value.code == ErrorCode.QUOTE_EXPIRED


def test_superseding_never_touches_an_executed_quote(flow, no_flush):
    from returns_domain import execution

    hq = flow.hero_quote()
    flow.approve(hq)
    execution.execute(AVA, hq.quote_id)
    conv_id, run2 = flow.start_run(AVA, conv_id=hq.conv_id)
    cand2 = options.find_options(AVA, hq.check.policy_check_id, "8.0", "2026-10-17", run_id=run2)
    quotes.create_quote(AVA, run2, hq.conv_id, cand2.candidate_set_id, [cand2.options[0].option_id, cand2.options[-1].option_id])
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "executed"


@pytest.mark.parametrize("bad", [[], None, "OPT-1", [1], [""], ["OPT-99"], ["OPT-1", "OPT-99"], 5])
def test_option_ids_must_be_a_non_empty_list_of_offered_ids(flow, bad):
    hq = flow.hero_quote()
    conv_id, run2 = flow.start_run(AVA)
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(AVA, run2, conv_id, hq.candidates.candidate_set_id, bad)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_a_quote_must_keep_a_return_option_visible(flow):
    hq = flow.hero_quote()
    conv_id, run2 = flow.start_run(AVA)
    exchanges = [o.option_id for o in hq.candidates.options if o.type.startswith("exchange_")]
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(AVA, run2, conv_id, hq.candidates.candidate_set_id, exchanges)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "return" in exc.value.message
    assert len(list(store.scan_keys(("QUOTE#",)))) == 1  # only the fixture quote of hero_quote


def test_duplicate_option_ids_are_collapsed(flow):
    hq = flow.hero_quote()
    conv_id, run2 = flow.start_run(AVA)
    ids = [hq.candidates.options[2].option_id, hq.candidates.options[2].option_id, hq.candidates.options[3].option_id]
    quote = quotes.create_quote(AVA, run2, conv_id, hq.candidates.candidate_set_id, ids)
    assert [o.option_id for o in quote.options] == ["OPT-3", "OPT-4"]


def test_unrelated_stock_move_ok(flow):
    """G-26: only the locations the SELECTED options use are compared."""
    hq = flow.hero_quote(types=("exchange_store_pickup", "return_in_store"))
    store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", DC), add={"atp": -1, "version": 1})  # DC not used
    store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", TOR), add={"atp": -1, "version": 1})  # TOR not used
    conv_id, run2 = flow.start_run(AVA)
    quote = quotes.create_quote(AVA, run2, conv_id, hq.candidates.candidate_set_id,
                                [hq.candidates.options[2].option_id, hq.candidates.options[3].option_id])
    assert quote.inventory_versions == {f"DEMO-SKU-HERO-BLACK-075|{MTL}": 1}


def test_stock_changed_when_a_used_location_moved(flow, snapshot):
    hq = flow.hero_quote(types=("exchange_store_pickup", "return_in_store"))
    store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", MTL), add={"atp": -1, "version": 1})
    conv_id, run2 = flow.start_run(AVA)
    before = snapshot()
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(AVA, run2, conv_id, hq.candidates.candidate_set_id,
                            [hq.candidates.options[2].option_id, hq.candidates.options[3].option_id])
    assert exc.value.code == ErrorCode.STOCK_CHANGED
    assert snapshot() == before  # nothing written: no quote, no event, no supersede


def test_returns_only_quotes_ignore_stock_entirely(flow):
    hq = flow.hero_quote()
    for location in (DC, MTL, TOR):
        store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", location), set={"atp": 0, "version": 9})
    conv_id, run2 = flow.start_run(AVA)
    returns = [o.option_id for o in hq.candidates.options if o.type.startswith("return_")]
    assert quotes.create_quote(AVA, run2, conv_id, hq.candidates.candidate_set_id, returns).inventory_versions == {}


def test_create_quote_ownership_and_missing_objects(flow):
    hq = flow.hero_quote()
    ids = [o.option_id for o in hq.quote.options]
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(BEN, "RUN-X", "CONV-X", hq.candidates.candidate_set_id, ids)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert audit.list_audit(action="access_denied")[-1]["refs"]["resource_type"] == "candidate_set"
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(AVA, "RUN-X", "CONV-X", "CAND-NOPE", ids)
    assert exc.value.code == ErrorCode.NOT_FOUND
    other_conv = conversations.create_conversation(BEN)  # a conversation of someone else
    with pytest.raises(DomainError) as exc:
        quotes.create_quote(AVA, "RUN-X", other_conv, hq.candidates.candidate_set_id, ids)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert len(list(store.scan_keys(("QUOTE#",)))) == 1


def test_create_quote_without_a_conversation_item_still_works(flow):
    hq = flow.hero_quote()
    quote = quotes.create_quote(AVA, "RUN-D", "CONV-NOT-STORED", hq.candidates.candidate_set_id,
                                [o.option_id for o in hq.candidates.options[2:4]])
    assert quote.conv_id == "CONV-NOT-STORED" and store.get_item(store.conv_key("CONV-NOT-STORED")) is None


# --------------------------------------------------------------------------------- read


def test_get_quote_shows_expiry_without_changing_the_stored_status(flow, fixed_now):
    hq = flow.hero_quote()
    assert quotes.get_quote(AVA, hq.quote_id).status == "open"
    fixed_now.advance(seconds=899)
    assert quotes.get_quote(AVA, hq.quote_id).status == "open"
    fixed_now.advance(seconds=1)
    assert quotes.get_quote(AVA, hq.quote_id).status == "expired"
    assert store.get_item(store.quote_key(hq.quote_id))["status"] == "open"


def test_get_quote_includes_the_approval_and_checks_ownership(flow):
    hq = flow.hero_quote()
    assert quotes.get_quote(AVA, hq.quote_id).approval is None
    approval = flow.approve(hq)
    assert quotes.get_quote(AVA, hq.quote_id).approval == approval
    with pytest.raises(DomainError) as exc:
        quotes.get_quote(BEN, hq.quote_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    with pytest.raises(DomainError) as exc:
        quotes.get_quote(AVA, "QUO-MISSING")
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert "customer_id" not in quotes.get_quote(AVA, hq.quote_id).model_dump()


# ------------------------------------------------------------------------------ approve


def approve(hq, option, run_id="RUN-NEW", **kwargs):
    return quotes.approve(hq.customer_id, hq.quote_id, option.option_id, option.payload_hash, run_id, **kwargs)


def test_approve_creates_one_approval_bound_to_the_run(flow, fixed_now):
    hq = flow.hero_quote()
    option = hq.option("exchange_store_pickup")
    approval, created = approve(hq, option, "RUN-APPROVAL-1")
    assert created is True
    assert approval.option_id == option.option_id and approval.payload_hash == option.payload_hash
    assert approval.run_id == "RUN-APPROVAL-1" and approval.approved_at == "2026-10-16T16:00:00+00:00"
    assert approval.approval_id.startswith("APR-") and approval.consumed_txn_id is None
    stored = store.get_item(store.approval_key(hq.quote_id))
    assert "consumed_txn_id" not in stored  # absent, so execute can claim it with attribute_not_exists
    assert audit.list_audit(action="approve")[0]["refs"] == {"quote_id": hq.quote_id, "option_id": option.option_id}


def test_double_approve_same_option_returns_the_same_approval_and_run(flow):
    hq = flow.hero_quote()
    option = hq.option("exchange_store_pickup")
    first, created_first = approve(hq, option, "RUN-A")
    second, created_second = approve(hq, option, "RUN-B")
    assert (created_first, created_second) == (True, False)
    assert second == first and second.run_id == "RUN-A"
    assert len(list(store.scan_keys(("QUOTE#",)))) == 2  # the quote META and the one APPROVAL


def test_approving_another_option_conflicts(flow):
    hq = flow.hero_quote()
    approve(hq, hq.option("exchange_store_pickup"))
    with pytest.raises(DomainError) as exc:
        approve(hq, hq.option("return_in_store"))
    assert exc.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert store.get_item(store.approval_key(hq.quote_id))["option_id"] == hq.option("exchange_store_pickup").option_id


@pytest.mark.parametrize("which", ["wrong_hash", "other_options_hash", "unknown_option", "no_hash", "numeric_hash"])
def test_approve_rejects_a_hash_or_option_that_does_not_match(flow, which):
    hq = flow.hero_quote()
    pickup, back = hq.option("exchange_store_pickup"), hq.option("return_in_store")
    args = {
        "wrong_hash": (pickup.option_id, "0" * 64),
        "other_options_hash": (pickup.option_id, back.payload_hash),
        "unknown_option": ("OPT-99", pickup.payload_hash),
        "no_hash": (pickup.option_id, None),
        "numeric_hash": (pickup.option_id, 12345),
    }[which]
    with pytest.raises(DomainError) as exc:
        quotes.approve(AVA, hq.quote_id, args[0], args[1], "RUN-X")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert store.get_item(store.approval_key(hq.quote_id)) is None


def test_approve_expired_quote_writes_nothing(flow, fixed_now):
    hq = flow.hero_quote()
    fixed_now.advance(seconds=900)
    with pytest.raises(DomainError) as exc:
        approve(hq, hq.option("exchange_store_pickup"))
    assert exc.value.code == ErrorCode.QUOTE_EXPIRED
    assert store.get_item(store.approval_key(hq.quote_id)) is None


def test_a_replay_still_succeeds_after_the_quote_expired(flow, fixed_now):
    hq = flow.hero_quote()
    option = hq.option("exchange_store_pickup")
    first, _ = approve(hq, option, "RUN-A")
    fixed_now.advance(hours=3)
    again, created = approve(hq, option, "RUN-B")
    assert created is False and again == first


def test_before_create_runs_only_for_new_approvals_and_can_veto(flow):
    hq = flow.hero_quote()
    option = hq.option("exchange_store_pickup")
    calls = []

    def guard():
        calls.append(1)

    approve(hq, option, "RUN-A", before_create=guard)
    approve(hq, option, "RUN-B", before_create=guard)  # a replay: the guard is not consulted
    assert calls == [1]

    hq2 = flow.hero_quote()

    def veto():
        raise DomainError(ErrorCode.RUN_IN_PROGRESS, "busy")

    with pytest.raises(DomainError) as exc:
        approve(hq2, hq2.option("exchange_store_pickup"), before_create=veto)
    assert exc.value.code == ErrorCode.RUN_IN_PROGRESS and exc.value.retryable
    assert store.get_item(store.approval_key(hq2.quote_id)) is None  # no side effects


def test_approve_check_order_ownership_first(flow):
    hq = flow.hero_quote()
    with pytest.raises(DomainError) as exc:
        quotes.approve(BEN, hq.quote_id, "OPT-99", "bad", "RUN-X")  # wrong customer AND wrong option
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert audit.list_audit(action="access_denied")[0]["actor"] == BEN
    with pytest.raises(DomainError) as exc:
        quotes.approve(AVA, "QUO-NOPE", "OPT-1", "x", "RUN-X")
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_racing_approvals_of_the_same_option_create_exactly_one(flow):
    hq = flow.hero_quote()
    option = hq.option("exchange_store_pickup")
    outcomes = []

    def go(n):
        outcomes.append(approve(hq, option, f"RUN-{n}"))

    threads = [threading.Thread(target=go, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for _, created in outcomes if created) == 1
    assert len({approval.approval_id for approval, _ in outcomes}) == 1
    assert len({approval.run_id for approval, _ in outcomes}) == 1
