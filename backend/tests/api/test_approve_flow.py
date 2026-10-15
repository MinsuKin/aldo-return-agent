"""Approve flow (contracts.md C.4, design-gap G-06) and the requote route (G-13).

Real domain modules on moto: a quote is built exactly the way the agent's tools build it, then
the HTTP routes are exercised. Assertions are on table state, not on message text.
"""

from __future__ import annotations

import pytest

from returns_domain import audit, execution, runs, store, trace
from returns_domain.errors import DomainError

from .conftest import AVA, BEN, session_token
from .flows import make_quote

PICKUP = "exchange_store_pickup"
DC_SHIP = "exchange_ship_from_dc"
IN_STORE = "return_in_store"


def approve(client, world, option, *, token=None, payload_hash=None):
    return client.post(
        f"/api/quotes/{world.quote_id}/approve",
        {"option_id": option.option_id, "payload_hash": payload_hash or option.payload_hash},
        token=token or session_token(world.customer_id),
    )


def txn_keys() -> list:
    return list(store.scan_keys(("TXN#",)))


def run_partitions() -> set[str]:
    """Partition keys of every run in the table (events share their run's partition).

    The owner's A2A stream (``RUN#A2A-<customer>``) is not a run: the quote and the transaction
    mirror their events there, so it appears as soon as a quote exists.
    """
    return {pk for pk, _ in store.scan_keys(("RUN#",)) if not pk.startswith("RUN#A2A-")}


def stock(option) -> int:
    return store.get_item(store.inventory_key(option.sku, option.location_id))["atp"]


def line_reserved() -> int:
    return store.get_item(store.line_key("DEMO-1001", "DEMO-1001-L1"))["returned_or_reserved_qty"]


@pytest.fixture
def world(client, seeded):
    return make_quote(AVA)


# ----------------------------------------------------------------------- first approval


def test_approve_creates_one_approval_and_starts_one_continuation_run(client, world, enqueued):
    option = world.option(PICKUP)
    atp_before, reserved_before = stock(option), line_reserved()

    reply = approve(client, world, option)

    assert reply.status == 202
    body = reply.json
    assert body["replayed"] is False
    assert (body["quote_id"], body["option_id"]) == (world.quote_id, option.option_id)
    run_id = body["run_id"]
    assert enqueued == [
        {
            "run_id": run_id,
            "conv_id": world.conv_id,
            "customer_id": AVA,
            "order_id": "DEMO-1001",
            "kind": "approval",
            "message": "",
            "quote_id": world.quote_id,
            "option_id": option.option_id,
            "approval_id": body["approval_id"],
            "agent": "aldo",
        }
    ]
    run = runs.get_run(AVA, run_id)
    assert run["status"] == "queued" and run["kind"] == "approval"
    events = trace.list_events(run_id).events
    assert [(e["seq"], e["type"]) for e in events] == [(1, "approval_received")]
    assert events[0]["payload"] == {
        "approval_id": body["approval_id"],
        "quote_id": world.quote_id,
        "option_id": option.option_id,
    }
    assert runs.lease_active(world.conv_id)  # the continuation run holds the conversation
    stored = store.get_item(store.approval_key(world.quote_id))
    assert stored["run_id"] == run_id and stored["option_id"] == option.option_id
    # R7.6 / R8: approving executes nothing
    assert txn_keys() == [] and stock(option) == atp_before and line_reserved() == reserved_before
    assert audit.list_audit(action="approve", actor=AVA)


def test_the_quote_route_shows_the_approval_and_hashes_to_the_ui(client, world):
    option = world.option(PICKUP)
    before = client.get(f"/api/quotes/{world.quote_id}", token=session_token(AVA))
    assert before.status == 200 and before.json["approval"] is None
    assert before.json["status"] == "open"
    shown = {o["option_id"]: o["payload_hash"] for o in before.json["options"]}
    assert shown[option.option_id] == option.payload_hash

    approve(client, world, option)
    after = client.get(f"/api/quotes/{world.quote_id}", token=session_token(AVA))
    assert after.json["approval"]["option_id"] == option.option_id
    assert after.json["approval"]["consumed_txn_id"] is None


# ------------------------------------------------------------------------------ replay


def test_same_option_twice_returns_the_same_approval_and_run(client, world, enqueued):
    option = world.option(PICKUP)
    first = approve(client, world, option)
    assert runs.claim_run(first.json["run_id"])  # the worker picked it up

    second = approve(client, world, option)

    assert second.status == 200
    assert second.json["replayed"] is True
    assert (second.json["approval_id"], second.json["run_id"]) == (
        first.json["approval_id"],
        first.json["run_id"],
    )
    assert len(enqueued) == 1  # a running run is never started again
    assert run_partitions() == {f"RUN#{world.run_id}", f"RUN#{first.json['run_id']}"}
    approvals = store.query(f"QUOTE#{world.quote_id}", sk_begins_with="APPROVAL")
    assert len(approvals) == 1


def test_a_replay_while_the_run_is_still_queued_reinvokes_the_worker(client, world, enqueued):
    option = world.option(PICKUP)
    first = approve(client, world, option)
    second = approve(client, world, option)  # the first invoke may have been lost

    assert second.status == 200 and second.json["run_id"] == first.json["run_id"]
    assert len(enqueued) == 2 and enqueued[0] == enqueued[1]  # same payload, same run_id
    assert [e["type"] for e in trace.list_events(first.json["run_id"]).events] == ["approval_received"]


def test_a_failed_invoke_keeps_the_approval_and_the_retry_finishes_the_job(app, client, world, enqueued):
    option = world.option(PICKUP)
    app.enqueue = lambda payload: (_ for _ in ()).throw(RuntimeError("lambda throttled"))

    failed = approve(client, world, option)

    assert failed.status == 504 and failed.code == "UPSTREAM_TIMEOUT" and failed.error["retryable"] is True
    approval = store.get_item(store.approval_key(world.quote_id))
    assert approval is not None  # the customer's click is not lost
    assert runs.get_run(AVA, approval["run_id"])["status"] == "queued"

    app.enqueue = enqueued.append  # the platform recovers; the UI retries the same click
    retry = approve(client, world, option)
    assert retry.status == 200 and retry.json["run_id"] == approval["run_id"]
    assert [p["run_id"] for p in enqueued] == [approval["run_id"]]


def test_a_replay_recreates_a_run_that_was_never_written(client, world, enqueued):
    option = world.option(PICKUP)
    run_id = approve(client, world, option).json["run_id"]
    store.delete_item(store.run_key(run_id))  # the first request died after the approval put
    enqueued.clear()

    replay = approve(client, world, option)

    assert replay.status == 200 and replay.json["run_id"] == run_id
    assert runs.get_run(AVA, run_id)["status"] == "queued"
    assert [e["type"] for e in trace.list_events(run_id).events] == ["approval_received"]
    assert [p["run_id"] for p in enqueued] == [run_id]


# ----------------------------------------------------------------------------- conflicts


def test_a_different_option_on_an_approved_quote_is_a_conflict(client, world, enqueued):
    first = approve(client, world, world.option(PICKUP))
    other = approve(client, world, world.option(DC_SHIP))

    assert other.status == 409 and other.code == "IDEMPOTENCY_CONFLICT"
    stored = store.get_item(store.approval_key(world.quote_id))
    assert stored["option_id"] == world.option(PICKUP).option_id
    assert stored["run_id"] == first.json["run_id"]
    assert len(enqueued) == 1


def test_a_wrong_hash_or_unknown_option_is_a_validation_error(client, world, enqueued):
    option = world.option(PICKUP)
    wrong_hash = approve(client, world, option, payload_hash="0" * 64)
    assert wrong_hash.status == 422 and wrong_hash.code == "VALIDATION_ERROR"
    swapped = approve(client, world, option, payload_hash=world.option(DC_SHIP).payload_hash)
    assert swapped.status == 422  # another option's hash does not approve this one
    missing = client.post(
        f"/api/quotes/{world.quote_id}/approve",
        {"option_id": "OPT-99", "payload_hash": option.payload_hash},
        token=session_token(AVA),
    )
    assert missing.status == 422
    assert store.get_item(store.approval_key(world.quote_id)) is None and enqueued == []


@pytest.mark.parametrize("body", [None, {}, {"option_id": "OPT-1"}, {"payload_hash": "abc"}, {"option_id": 1, "payload_hash": "x"}])
def test_approve_needs_an_option_and_a_hash(client, world, body):
    reply = client.post(f"/api/quotes/{world.quote_id}/approve", body, token=session_token(AVA))
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"


def test_another_customers_quote_is_refused_with_an_audit_event(client, world, enqueued):
    option = world.option(PICKUP)
    reply = approve(client, world, option, token=session_token(BEN))
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED"
    assert store.get_item(store.approval_key(world.quote_id)) is None and enqueued == []
    assert audit.list_audit(action="access_denied", actor=BEN)
    read = client.get(f"/api/quotes/{world.quote_id}", token=session_token(BEN))
    assert read.status == 403


def test_an_unknown_quote_is_a_404(client, seeded):
    reply = client.post(
        "/api/quotes/QUO-NOPE/approve", {"option_id": "OPT-1", "payload_hash": "x"}, token=session_token(AVA)
    )
    assert reply.status == 404 and reply.code == "NOT_FOUND"


# ---------------------------------------------------------------------- active-run guard


def test_run_in_progress_blocks_a_new_approval_without_side_effects(client, world, enqueued):
    option = world.option(PICKUP)
    assert runs.try_acquire_lease(world.conv_id, "RUN-SOMEONE-ELSE")

    blocked = approve(client, world, option)

    assert blocked.status == 409 and blocked.code == "RUN_IN_PROGRESS"
    assert blocked.error["retryable"] is True
    assert store.get_item(store.approval_key(world.quote_id)) is None
    assert enqueued == [] and run_partitions() == {f"RUN#{world.run_id}"}  # no run was created

    runs.release_lease(world.conv_id, "RUN-SOMEONE-ELSE")
    assert approve(client, world, option).status == 202


def test_the_guard_does_not_block_a_replay(client, world):
    option = world.option(PICKUP)
    first = approve(client, world, option)  # the continuation run holds the lease now
    assert runs.lease_active(world.conv_id)
    again = approve(client, world, option)
    assert again.status == 200 and again.json["run_id"] == first.json["run_id"]


# ------------------------------------------------------------------------------ expiry


def test_an_expired_quote_cannot_be_approved(client, seeded, fixed_now, enqueued):
    world = make_quote(AVA)
    option = world.option(PICKUP)
    fixed_now.advance(seconds=16 * 60)

    reply = approve(client, world, option)

    assert reply.status == 409 and reply.code == "QUOTE_EXPIRED"
    assert store.get_item(store.approval_key(world.quote_id)) is None and enqueued == []
    shown = client.get(f"/api/quotes/{world.quote_id}", token=session_token(AVA))
    assert shown.json["status"] == "expired"


def test_a_superseded_quote_cannot_be_approved(client, seeded, enqueued):
    old = make_quote(AVA)
    # a second quote in the same conversation supersedes the first (G-17)
    from returns_domain import options, orders, quotes

    run_id = store.new_id("RUN-", 10)
    runs.try_acquire_lease(old.conv_id, run_id)
    runs.create_run(AVA, old.conv_id, run_id, "customer")
    check = orders.check_policy(AVA, run_id, "DEMO-1001", "DEMO-1001-L1", 1, "FIT_SIZE", "unworn", "too_small")
    candidates = options.find_options(AVA, check.policy_check_id, run_id=run_id)
    quotes.create_quote(AVA, run_id, old.conv_id, candidates.candidate_set_id, [o.option_id for o in candidates.options])
    runs.claim_run(run_id)
    runs.finish_run(run_id, "completed", {})

    reply = approve(client, old, old.option(PICKUP))

    assert reply.status == 409 and reply.code == "QUOTE_EXPIRED"
    assert enqueued == []


# ------------------------------------------------------------------------------ requote


def requote(client, world, *, conv_id=None, token=None, quote_id=None):
    return client.post(
        f"/api/conversations/{conv_id or world.conv_id}/requote",
        {"quote_id": quote_id or world.quote_id},
        token=token or session_token(world.customer_id),
    )


def test_requote_only_when_expired(client, seeded, fixed_now, enqueued):
    world = make_quote(AVA)

    fresh = requote(client, world)
    assert fresh.status == 422 and fresh.code == "VALIDATION_ERROR"
    assert enqueued == [] and not runs.lease_active(world.conv_id)

    fixed_now.advance(seconds=16 * 60)
    expired = requote(client, world)

    assert expired.status == 202
    run_id = expired.json["run_id"]
    assert expired.json["conv_id"] == world.conv_id
    assert enqueued == [
        {
            "run_id": run_id,
            "conv_id": world.conv_id,
            "customer_id": AVA,
            "order_id": "DEMO-1001",
            "kind": "requote",
            "message": "",
            "quote_id": world.quote_id,
            "option_id": None,
            "approval_id": None,
            "agent": "aldo",
        }
    ]
    assert runs.get_run(AVA, run_id)["kind"] == "requote"
    assert runs.lease_active(world.conv_id)
    # a second requote while that run is active is refused
    assert requote(client, world).code == "RUN_IN_PROGRESS"


def test_requote_is_scoped_to_the_conversation_and_the_owner(client, seeded, fixed_now, enqueued):
    mine = make_quote(AVA)
    fixed_now.advance(seconds=16 * 60)
    other_conv = client.post("/api/conversations", {}, token=session_token(AVA)).json["conv_id"]

    wrong_conversation = requote(client, mine, conv_id=other_conv)
    assert wrong_conversation.status == 404 and wrong_conversation.code == "NOT_FOUND"

    not_theirs = requote(client, mine, token=session_token(BEN))  # Ben's token, Ava's conversation
    assert not_theirs.status == 403 and not_theirs.code == "NOT_AUTHORIZED"

    missing = requote(client, mine, quote_id="QUO-NOPE")
    assert missing.status == 404
    assert enqueued == []


def test_requote_after_approval_is_not_possible(client, seeded, enqueued):
    world = make_quote(AVA)
    assert approve(client, world, world.option(PICKUP)).status == 202
    enqueued.clear()
    assert requote(client, world).status == 422  # still open, not expired
    assert enqueued == []


# ------------------------------------------------------- the whole flow, read routes


def test_after_execution_the_read_routes_show_the_transaction(client, world, enqueued):
    option = world.option(PICKUP)
    atp_before = stock(option)
    approved = approve(client, world, option)
    run_id = approved.json["run_id"]

    # what the worker and the agent do next
    assert runs.claim_run(run_id)
    assert runs.mark_agent_started(run_id)
    result = execution.execute(AVA, world.quote_id, run_id=run_id)
    assert runs.finish_run(run_id, "completed", {"rounds": 2, "tool_count": 1})

    assert len(txn_keys()) == 1 and stock(option) == atp_before - 1 and line_reserved() == 1
    quote = client.get(f"/api/quotes/{world.quote_id}", token=session_token(AVA)).json
    assert quote["status"] == "executed"
    assert quote["approval"]["consumed_txn_id"] == result.transaction_id

    view = client.get(f"/api/transactions/{result.transaction_id}", token=session_token(AVA))
    assert view.status == 200
    assert view.json["transaction_id"] == result.transaction_id and view.json["simulated"] is True
    assert view.json["option_type"] == PICKUP and view.json["refund_cents"] == 0
    assert isinstance(view.json["fee_cents"], int)
    assert client.get(f"/api/transactions/{result.transaction_id}", token=session_token(BEN)).status == 403

    page = client.get(f"/api/runs/{run_id}/events", query={"after": "0"}, token=session_token(AVA)).json
    types = [e["type"] for e in page["events"]]
    assert types[0] == "approval_received" and types[-1] == "run_completed"
    assert "transaction_created" in types
    assert page["status"] == "completed" and page["next_after"] == len(types)
    assert not runs.lease_active(world.conv_id)  # finishing the run released the conversation

    replay = execution.execute(AVA, world.quote_id, run_id=run_id)
    assert replay.replayed is True and replay.transaction_id == result.transaction_id
    assert len(txn_keys()) == 1


def test_executing_without_the_click_is_refused_by_the_domain(client, world):
    with pytest.raises(DomainError) as excinfo:
        execution.execute(AVA, world.quote_id)
    assert excinfo.value.code == "APPROVAL_REQUIRED"
    assert txn_keys() == []
