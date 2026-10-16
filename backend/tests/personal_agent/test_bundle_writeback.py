"""ALDO bot accepts the write-back of a BUNDLE return (``outcome`` ``return_created``, an ``items`` list).

The body comes from the domain's own ``memory_updates.build_bundle_update`` so the two sides cannot
drift apart. Part one is the receiver (validation, idempotency, the memory view); part two runs David's
whole bundle through the real domain, the outbox and the ``local`` transport on two moto tables and
looks at the A2A stream: one ``memory_updated`` on success, nothing at all on a rejected delivery.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Iterator

import pytest

from personal_agent import context, memory
from personal_agent.memory import MemoryRepo
from returns_domain import execution, gateway, memory_updates, store, trace
from returns_domain import personal_agent_client as pac
from returns_domain.errors import DomainError

DAVID, AVA = "DEMO-CUST-DAVID", "DEMO-CUST-AVA"
ORDER = "DEMO-3001"
TRANSACTION = "DEMO-RET-AB12CD"
ITEMS = [
    {"style_id": "DEMO-STYLE-NEW02", "product_name": "Brando", "color": "TAN", "size": "10.5", "reason": "OTHER"},
    {"style_id": "DEMO-STYLE-NEW05", "product_name": "Oscar", "color": "WHITE", "size": "10.0", "reason": "OTHER"},
    {"style_id": "DEMO-STYLE-NEW08", "product_name": "Patten", "color": "NAVY", "size": "10.0", "reason": "OTHER"},
]
TOP_LEVEL = {"update_id", "owner_ref", "order_id", "reason", "outcome", "provisional", "source", "ts", "items"}


def bundle(owner: str = DAVID, transaction: str = TRANSACTION, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The write-back body exactly as the domain builds it (its builder refuses an empty or oversized list)."""
    return memory_updates.build_bundle_update(
        customer_id=owner, order_id=ORDER, transaction_id=transaction, items=copy.deepcopy(ITEMS if items is None else items)
    )


def exchange(update_id: str = "MU-5C0A11", **changes: Any) -> dict[str, Any]:
    body = {
        "update_id": update_id, "owner_ref": DAVID, "order_id": ORDER, "style_id": "DEMO-STYLE-NEW02",
        "product_name": "Brando", "color": "TAN", "original_size": "10.5", "new_size": "11.0",
        "reason": "TOO_SMALL", "outcome": "exchange_created", "provisional": True, "source": "ALDO_CA",
        "ts": "2026-10-16T16:05:00+00:00",
    }
    body.update(changes)
    return body


def stored(repo: MemoryRepo) -> list[tuple[str, str]]:
    """Keys of every write-back item the owner has (the table must not change when a body is refused)."""
    return sorted((i["pk"], i["sk"]) for i in repo._query(memory.owner_pk(DAVID), begins_with="UPD#"))


# ======================================================================== the receiver


def test_the_body_the_domain_builds_has_exactly_the_fields_the_receiver_expects() -> None:
    body = bundle()
    assert set(body) == TOP_LEVEL and body["outcome"] == "return_created" and body["provisional"] is True
    assert [set(item) for item in body["items"]] == [{"style_id", "product_name", "color", "size", "reason"}] * 3
    assert memory.validate_update(body).update_id == body["update_id"]


def test_a_bundle_return_is_accepted_and_kept_once(repo: MemoryRepo) -> None:
    body = bundle()
    first = repo.apply_update(body)
    assert (first["applied"], first["duplicate"], first["provisional"], first["update_id"]) == (True, False, True, body["update_id"])
    assert [item["product_name"] for item in first["update"]["items"]] == ["Brando", "Oscar", "Patten"]
    again = repo.apply_update(body)  # the outbox may deliver twice
    assert (again["applied"], again["duplicate"]) == (False, True) and again["update"] == first["update"]
    assert len(repo.get_owner_memory(DAVID).returns) == 1


def test_a_return_is_listed_under_returns_and_not_as_a_size_swap(repo: MemoryRepo) -> None:
    repo.apply_update(bundle())
    view = repo.get_owner_memory(DAVID)
    assert view.updates == []  # the memory sheet lists exchanges there; a return has no sizes to swap
    (record,) = view.returns
    assert (record.order_id, record.outcome, record.reason, record.provisional, record.confidence) == (ORDER, "return_created", "OTHER", True, "provisional")
    assert [(i.color, i.size) for i in record.items] == [("TAN", "10.5"), ("WHITE", "10.0"), ("NAVY", "10.0")]


def test_the_memory_body_has_a_returns_key_only_once_there_is_a_return(repo: MemoryRepo) -> None:
    assert "returns" not in repo.list_memory(DAVID)
    assert "returns" not in repo.list_memory(AVA)
    repo.apply_update(bundle())
    body = repo.list_memory(DAVID)
    assert [r["update_id"] for r in body["returns"]] == [bundle()["update_id"]] and body["updates"] == []
    assert "returns" not in repo.list_memory(AVA)  # another owner's view is untouched
    assert json.loads(json.dumps(body)) == body  # plain JSON


def test_a_return_and_an_exchange_of_the_same_order_do_not_supersede_each_other(repo: MemoryRepo) -> None:
    repo.apply_update(exchange())
    repo.apply_update(bundle())
    repo.apply_update(exchange("MU-FINAL1", outcome="exchange_completed_kept", provisional=False, ts="2026-10-17T15:00:00+00:00"))
    view = repo.get_owner_memory(DAVID)
    assert [(u.update_id, u.status) for u in view.updates] == [("MU-FINAL1", "active")]  # the exchange rule works as before
    assert len(view.returns) == 1  # and the return is still there, active
    assert repo.get_owner_memory(DAVID, include_superseded=True).returns[0].update_id == bundle()["update_id"]


def test_two_bundles_of_one_order_are_two_independent_returns(repo: MemoryRepo) -> None:
    repo.apply_update(bundle(transaction="DEMO-RET-AAAAAA", items=ITEMS[:2]))
    repo.apply_update(bundle(transaction="DEMO-RET-BBBBBB", items=ITEMS[2:]))
    returns = repo.get_owner_memory(DAVID).returns
    assert len(returns) == 2 and sorted(len(r.items) for r in returns) == [1, 2]


def test_the_stored_return_holds_only_plain_strings_ints_and_booleans(repo: MemoryRepo) -> None:
    repo.apply_update(bundle())
    (item,) = [i for i in repo._query(memory.owner_pk(DAVID), begins_with="UPD#")]
    assert isinstance(item["items_json"], str) and item["provisional"] is True and item["status"] == "active"
    assert not {"style_id", "product_name", "color", "original_size", "new_size"} & set(item)  # the items carry them
    assert all(isinstance(v, (str, int, bool)) for v in item.values())


def test_recall_and_the_memory_context_do_not_use_a_return(repo: MemoryRepo) -> None:
    """A returned pair is not a kept colour, and the bot's size rules only read exchange updates."""
    before = (repo.recall_size_memory(DAVID), context.build_memory_context(repo.get_owner_memory(DAVID), []))
    repo.apply_update(bundle())
    after = (repo.recall_size_memory(DAVID), context.build_memory_context(repo.get_owner_memory(DAVID), []))
    assert after == before and after[0]["aldo_updates"] == []


def test_the_optional_a2a_emission_of_a_return_names_no_item(repo: MemoryRepo) -> None:
    sent: list[tuple[Any, ...]] = []
    from personal_agent import a2a

    a2a.set_a2a_emitter(lambda *args, **kwargs: sent.append((args, kwargs)))
    repo.apply_update(bundle(), emit_a2a=True)
    repo.apply_update(bundle(), emit_a2a=True)  # a replay emits nothing
    (args, kwargs), = sent
    assert args[1] == "memory_updated" and set(args[2]) == {"update_id", "reason", "outcome", "provisional"}
    assert "Brando" not in json.dumps(sent, default=str)


BAD_BODIES = [
    pytest.param({"items": []}, "items", id="no items"),
    pytest.param({"items": ITEMS * 4}, "items", id="more than ten items"),
    pytest.param({"items": "Brando"}, "items", id="items is not a list"),
    pytest.param({"provisional": False}, "provisional", id="a return is always provisional"),
    pytest.param({"provisional": "yes"}, "provisional", id="provisional is a real boolean"),
    pytest.param({"fit_feedback": "size_ok"}, "fit_feedback", id="no fit_feedback on a return"),
    pytest.param({"style_id": "DEMO-STYLE-NEW02"}, "style_id", id="no style of its own"),
    pytest.param({"original_size": "10.5", "new_size": "11.0"}, "new_size", id="no sizes of its own"),
    pytest.param({"reason": "too big"}, "reason", id="reason shape"),
    pytest.param({"owner_ref": "../etc"}, "owner_ref", id="owner shape"),
    pytest.param({"ts": "yesterday"}, "ts", id="timestamp"),
]


@pytest.mark.parametrize("changes,field", BAD_BODIES)
def test_a_bad_bundle_is_rejected_naming_the_field_and_stores_nothing(repo: MemoryRepo, changes: dict[str, Any], field: str) -> None:
    before = stored(repo)
    body = bundle()
    body.update(changes)  # applied after the domain built it: the builder itself refuses some of these
    with pytest.raises(DomainError) as caught:
        repo.apply_update(body)
    assert caught.value.code == "VALIDATION_ERROR" and field in caught.value.message
    assert stored(repo) == before


@pytest.mark.parametrize(
    "item_change,field",
    [({"size": "10"}, "items.0.size"), ({"reason": "oops"}, "items.0.reason"), ({"extra": 1}, "items.0.extra"),
     ({"style_id": ""}, "items.0.style_id"), ({"product_name": " "}, "items.0.product_name"), ({"color": "x" * 41}, "items.0.color")],
)
def test_a_bad_item_is_named_by_its_position_never_by_its_value(repo: MemoryRepo, item_change: dict[str, Any], field: str) -> None:
    items = copy.deepcopy(ITEMS)
    items[0].update(item_change)
    body = bundle()
    body["items"] = items
    with pytest.raises(DomainError) as caught:
        repo.apply_update(body)
    assert field in caught.value.message and "Brando" not in caught.value.message and "x" * 41 not in caught.value.message
    assert repo.get_owner_memory(DAVID).returns == []


def test_a_return_for_an_unknown_owner_is_not_found(repo: MemoryRepo) -> None:
    with pytest.raises(DomainError) as caught:
        repo.apply_update(bundle(owner="DEMO-CUST-NOBODY"))
    assert caught.value.code == "NOT_FOUND"


def test_an_exchange_update_still_cannot_carry_items(repo: MemoryRepo) -> None:
    with pytest.raises(DomainError) as caught:
        repo.apply_update(exchange(items=ITEMS))
    assert "items" in caught.value.message
    assert repo.get_owner_memory(DAVID).updates == []


def test_an_unknown_outcome_is_still_rejected(repo: MemoryRepo) -> None:
    with pytest.raises(DomainError) as caught:
        repo.apply_update(exchange(outcome="return_completed"))
    assert "outcome" in caught.value.message


# ======================================================== the receiver over HTTP (HMAC)


def test_the_signed_route_applies_a_bundle_once_and_the_memory_route_shows_it(repo: MemoryRepo) -> None:
    from .test_routes import Api, session, signed

    api = Api(repo)  # the route tests own the HTTP plumbing; this asserts the bundle body through it
    raw, signature = signed(bundle())
    status, body, _ = api.call("POST", "/personal-agent/memory-updates", raw=raw, signature=signature)
    assert (status, body) == (200, {"ok": True, "update_id": bundle()["update_id"], "applied": True, "duplicate": False, "provisional": True})
    status, body, _ = api.call("POST", "/personal-agent/memory-updates", raw=raw, signature=signature)
    assert status == 200 and (body["applied"], body["duplicate"]) == (False, True)
    shown = api.call("GET", "/personal-agent/memory", token=session(DAVID))[1]
    assert shown["updates"] == [] and [r["order_id"] for r in shown["returns"]] == [ORDER]
    assert [i["product_name"] for i in shown["returns"][0]["items"]] == ["Brando", "Oscar", "Patten"]
    assert "returns" not in api.call("GET", "/personal-agent/memory", token=session(AVA))[1]


def test_a_signed_bad_bundle_is_a_422_that_names_fields_and_writes_nothing(repo: MemoryRepo) -> None:
    from .test_routes import Api, assert_error, signed

    api = Api(repo)
    raw, signature = signed({**bundle(), "items": []})
    error = assert_error(api.call("POST", "/personal-agent/memory-updates", raw=raw, signature=signature), 422, "VALIDATION_ERROR")
    assert "items" in json.dumps(error)
    assert repo.get_owner_memory(DAVID).returns == []


# ============================================ David's whole bundle through the real domain


@pytest.fixture
def both(flow, monkeypatch: pytest.MonkeyPatch) -> Iterator[MemoryRepo]:
    """The seeded ALDO table (tests/conftest.py ``flow``) plus a seeded personal table, one moto session,
    and the ``local`` transport wired from the outbox to ALDO bot's receiver."""
    table = MemoryRepo()
    table.create_table()
    table.seed()
    memory.set_repo(table)
    monkeypatch.setenv("GATEWAY_TRANSPORT", "local")
    pac.register_local_handler(memory.apply_update)
    yield table
    pac.register_local_handler(None)
    memory.set_repo(None)


def memory_updated(owner: str = DAVID) -> list[dict[str, Any]]:
    return [e for e in trace.list_a2a_events(owner).events if e["type"] == "memory_updated"]


def approved_bundle(flow):
    from tests.bundle_world import Bundles

    return Bundles(flow).approved()


def test_a_real_bundle_return_is_written_back_once(both: MemoryRepo, flow) -> None:
    hq = approved_bundle(flow)
    result = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    assert result.option_type == "return_mail" and result.memory_update_id.startswith("MU-")

    outbox = memory_updates.get(result.memory_update_id)
    assert outbox["status"] == "delivered" and outbox["attempts"] == 0  # delivered at once, no failed try
    (record,) = both.get_owner_memory(DAVID).returns
    assert record.update_id == result.memory_update_id and record.order_id == ORDER
    assert [(i.product_name, i.color, i.size) for i in record.items] == [("Brando", "TAN", "10.5"), ("Oscar", "WHITE", "10.0"), ("Patten", "NAVY", "10.0")]

    (event,) = memory_updated()
    assert event["actor"] == "personal_agent"
    assert event["payload"]["outcome"] == "return_created" and event["payload"]["update_id"] == result.memory_update_id
    assert set(event["payload"]) <= {"update_id", "reason", "outcome", "provisional"}  # the public allowlist; no item, no size

    # a replayed click and another flush change nothing: still one record, one event
    assert execution.execute(DAVID, hq.quote_id, run_id=hq.run_id).replayed is True
    assert gateway.flush_outbox(DAVID, limit=10) == 0
    assert len(both.get_owner_memory(DAVID).returns) == 1 and len(memory_updated()) == 1


def test_a_write_back_the_receiver_rejects_is_retried_five_times_and_never_reaches_the_a2a_stream(both: MemoryRepo, flow) -> None:
    """The failure S17B saw before the receiver learned the bundle shape, now with a body that is really wrong."""
    bad = bundle()
    bad["items"][0]["size"] = "sz-99"  # a shape the domain's builder never produces; only the receiver can refuse it
    assert memory_updates.enqueue(bad) is True
    update_id = bad["update_id"]
    events_before = [e["type"] for e in trace.list_a2a_events(DAVID).events]

    delivered = [gateway.flush_outbox(DAVID, limit=10) for _ in range(7)]

    assert delivered == [0] * 7
    item = memory_updates.get(update_id)
    assert (item["status"], int(item["attempts"])) == ("failed", memory_updates.MAX_ATTEMPTS)  # stops retrying
    assert "items.0.size" in item["last_error"] and "sz-99" not in item["last_error"]  # the field, never the value
    assert [e["type"] for e in trace.list_a2a_events(DAVID).events] == events_before  # nothing was shown
    assert both.get_owner_memory(DAVID).returns == []
    assert memory_updates.list_pending(DAVID) == []


def test_the_outbox_item_is_idempotent_for_a_bundle(both: MemoryRepo, flow) -> None:
    hq = approved_bundle(flow)
    result = execution.execute(DAVID, hq.quote_id, run_id=hq.run_id)
    payload = memory_updates.get(result.memory_update_id)["payload"]
    assert memory_updates.enqueue(payload) is False  # the same update id is never queued twice
    assert len([k for k in store.scan_keys(("A2AOUT#",))]) == 1
