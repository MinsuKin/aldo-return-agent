"""The purchase story through HTTP with the real gateway module on moto (PIVOT_ADDENDUM 10.1).

size-assist and size-decision are called with the ALDO bot's DELEGATED token; the owner reads the
proposal and confirms with a SESSION token; only that click creates the order. Assertions are on
table state: the SIZEREQ item, the order, ATP and the A2A stream.
"""

from __future__ import annotations

from typing import Any

import pytest

from returns_domain import audit, store

from .conftest import AVA, BEN, delegated_token, session_token

HERO_80 = "DEMO-SKU-HERO-BLACK-080"  # SKU of size 8.0 (SIZE10 = 080)
ASSIST = {
    "intent": "size_assist",
    "product_query": "Stessy",
    "color": "BLACK",
    "size_system": "US_W",
    "deadline_date": "2026-10-17",
    "channel_pref": None,
}


def assist(client, *, token=None, body=None):
    return client.post("/api/gateway/size-assist", body or ASSIST, token=token or delegated_token(AVA))


def offered(reply, size: str, option_type: str) -> dict[str, Any]:
    group = next(g for g in reply.json["sizes"] if g["size"] == size)
    return next(o for o in group["options"] if o["type"] == option_type)


def propose(client, assisted, *, size="8.0", option_type="store_pickup", token=None, **override):
    option = offered(assisted, size, option_type)
    body = {
        "size_request_id": assisted.json["size_request_id"],
        "option_id": option["option_id"],
        "chosen_size": size,
        "rationale_public": "runs small per ALDO signal; a half size up",
        "confidence": "high",
        **override,
    }
    return client.post("/api/gateway/size-decision", body, token=token or delegated_token(AVA)), option


def atp(sku: str, location_id: str) -> int:
    return store.get_item(store.inventory_key(sku, location_id))["atp"]


def a2a_types(client, customer: str = AVA) -> list[str]:
    page = client.get("/api/a2a/events", token=session_token(customer)).json
    return [e["type"] for e in page["events"]]


def order_keys() -> list:
    return list(store.scan_keys(("ORDER#DEMO-A2A-",)))


def sizereq(size_request_id: str) -> dict:
    return store.get_item(store.sizereq_key(size_request_id))


@pytest.fixture(autouse=True)
def fresh_world(client, seeded):
    """Default stock, empty runtime tables."""


# ------------------------------------------------------------------------- size assist


def test_size_assist_returns_the_fit_signal_and_the_options_for_the_owners_region(client):
    reply = assist(client)

    assert reply.status == 200
    body = reply.json
    assert body["size_request_id"].startswith("SZR-") and body["style_id"] == "DEMO-STYLE-HERO"
    assert body["fit_signal"]["signal"] == "runs_small" and body["fit_signal"]["scope"] == "ALDO_CA"
    assert body["fit_signal"]["n_fit"] == 582 and body["fit_signal"]["provenance"] == "derived"
    assert type(body["price_cents"]) is int and body["currency"] == "CAD"
    pickup = offered(reply, "8.0", "store_pickup")
    assert pickup["location_id"] == "DEMO-STORE-MTL-01" and pickup["eta_date"] == "2026-10-17"
    assert pickup["meets_deadline"] is True
    assert offered(reply, "8.0", "ship_from_dc")["meets_deadline"] is False  # Tue Oct 20 misses Saturday
    assert "runs small" in body["question"]  # server template, never model text
    stored = sizereq(body["size_request_id"])
    assert stored["status"] == "open" and stored["customer_id"] == AVA
    assert a2a_types(client) == ["size_assist_requested", "fit_signal_shared", "size_requested"]


def test_size_assist_responses_carry_no_owner_data_and_the_stream_shows_public_fields(client):
    assist(client)
    page = client.get("/api/a2a/events", token=session_token(AVA)).json
    text = str(page)
    assert AVA not in text
    assert [e["actor"] for e in page["events"]] == ["personal_agent", "aldo_gateway", "aldo_gateway"]


def test_unknown_style_names_the_candidates(client):
    none = assist(client, body={**ASSIST, "product_query": "Zzzzzz"})
    assert none.status == 422 and none.code == "UNKNOWN_STYLE"
    assert none.error["candidates"] == [] and none.error["retryable"] is False

    several = assist(client, body={**ASSIST, "product_query": "ri"})  # Krios, Harriot, Verity ...
    assert several.status == 422 and several.code == "UNKNOWN_STYLE"
    candidates = several.error["candidates"]
    assert len(candidates) >= 2
    assert all(set(c) == {"style_id", "product_name", "colors"} for c in candidates)
    assert list(store.scan_keys(("SIZEREQ#",))) == []  # nothing was recorded


@pytest.mark.parametrize(
    "extra",
    [
        {"customer_id": "DEMO-CUST-BEN"},
        {"owner_ref": "DEMO-CUST-AVA"},
        {"owner_fit_hint": "usually a 9"},
        {"typical_size": "7.5"},
        {"retailer": "Retailer A (synthetic)"},
    ],
)
def test_the_owner_profile_cannot_be_sent_to_aldo(client, extra):
    """C3: ALDO never receives the owner profile; unknown fields are refused and nothing is stored."""
    reply = assist(client, body={**ASSIST, **extra})
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"
    assert list(store.scan_keys(("SIZEREQ#",))) == [] and a2a_types(client) == []


@pytest.mark.parametrize(
    "change",
    [
        {"intent": "buy_it_now"},
        {"product_query": "x"},
        {"product_query": "x" * 81},
        {"size_system": "EU"},
        {"deadline_date": "2026-13-45"},
        {"deadline_date": "Saturday"},
        {"channel_pref": "teleport"},
        {"color": "B" * 21},
    ],
)
def test_size_assist_validates_every_field(client, change):
    reply = assist(client, body={**ASSIST, **change})
    assert reply.status == 422 and reply.code == "VALIDATION_ERROR"
    assert list(store.scan_keys(("SIZEREQ#",))) == []


def test_the_customer_is_the_token_not_the_body(client):
    reply = assist(client, token=delegated_token(BEN))
    assert reply.status == 200
    assert sizereq(reply.json["size_request_id"])["customer_id"] == BEN
    assert a2a_types(client, BEN) and a2a_types(client, AVA) == []


# ---------------------------------------------------------------- proposal, not an order


def test_size_decision_only_records_a_proposal(client):
    assisted = assist(client)
    option_stock = atp(HERO_80, "DEMO-STORE-MTL-01")

    proposed, option = propose(client, assisted)

    assert proposed.status == 200
    body = proposed.json
    assert body["size_request_id"] == assisted.json["size_request_id"]
    assert body["status"] == "proposed" and body["awaiting"] == "owner_confirmation"
    assert body["order_id"] is None  # a proposal is not an order
    stored = sizereq(body["size_request_id"])
    assert stored["status"] == "proposed" and stored["proposed_option_id"] == option["option_id"]
    assert stored["proposed_size"] == "8.0"
    assert order_keys() == [] and atp(HERO_80, "DEMO-STORE-MTL-01") == option_stock  # no order, no stock
    assert a2a_types(client)[-1] == "size_recommended"


def test_a_newer_proposal_replaces_an_older_one(client):
    assisted = assist(client)
    propose(client, assisted, size="8.0")
    again, option = propose(client, assisted, size="7.5", option_type="ship_from_dc")
    assert again.status == 200
    stored = sizereq(assisted.json["size_request_id"])
    assert stored["proposed_size"] == "7.5" and stored["proposed_option_id"] == option["option_id"]
    assert order_keys() == []


def test_proposals_are_untrusted_input(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    pickup = offered(assisted, "8.0", "store_pickup")

    cases = {
        "an option that was never offered": {"option_id": "SO-99"},
        "a size that does not match the option": {"chosen_size": "7.5"},
        "a malformed id": {"size_request_id": "nope"},
        "a request id that does not exist": {"size_request_id": "SZR-FFFFFF"},
        "an unknown confidence": {"confidence": "certain"},
        "a rationale over 120 characters": {"rationale_public": "x" * 121},
        "an extra field": {"customer_id": AVA},
    }
    for name, change in cases.items():
        body = {
            "size_request_id": rid,
            "option_id": pickup["option_id"],
            "chosen_size": "8.0",
            "rationale_public": "ok",
            "confidence": "high",
            **change,
        }
        reply = client.post("/api/gateway/size-decision", body, token=delegated_token(AVA))
        assert reply.status in (404, 422), (name, reply.status, reply.text)
    stored = sizereq(rid)
    assert stored["status"] == "open" and "proposed_option_id" not in stored  # nothing was recorded
    assert order_keys() == []


def test_a_delegation_over_the_cap_is_refused_before_anything_is_recorded(client):
    assisted = assist(client, token=delegated_token(AVA, max_amount_cents=5_000))  # hero costs CAD 129.99
    reply, _ = propose(client, assisted, token=delegated_token(AVA, max_amount_cents=5_000))
    assert reply.status == 403 and reply.code == "DELEGATION_LIMIT_EXCEEDED"
    assert sizereq(assisted.json["size_request_id"])["status"] == "open"
    assert a2a_types(client)[-1] != "size_recommended"


def test_another_customers_request_is_refused_with_an_audit_event(client):
    assisted = assist(client, token=delegated_token(AVA))
    reply, _ = propose(client, assisted, token=delegated_token(BEN))
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED"
    assert sizereq(assisted.json["size_request_id"])["status"] == "open"
    denied = audit.list_audit(action="access_denied")
    assert [d["refs"]["resource_id"] for d in denied] == [assisted.json["size_request_id"]]


def test_an_expired_request_cannot_be_proposed_or_confirmed(client, fixed_now):
    assisted = assist(client)
    fixed_now.advance(seconds=16 * 60)
    late, _ = propose(client, assisted)
    assert late.status == 409 and late.code == "REQUEST_EXPIRED"
    assert order_keys() == []


# ------------------------------------------------------------------ the owner's click


def test_only_the_owners_confirm_creates_the_order(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    proposed, option = propose(client, assisted)
    before = atp(HERO_80, option["location_id"])
    token = session_token(AVA)

    review = client.get(f"/api/purchases/{rid}", token=token)
    assert review.status == 200
    view = review.json
    assert view["status"] == "proposed" and view["size_request_id"] == rid
    assert type(view["price_cents"]) is int and view["currency"] == "CAD"
    assert view["rationale_public"] and view["order_id"] is None

    confirmed = client.post(f"/api/purchases/{rid}/confirm", {"option_id": option["option_id"]}, token=token)

    assert confirmed.status == 200
    order = confirmed.json
    assert order["order_id"].startswith("DEMO-A2A-") and order["status"] == "placed_simulated"
    assert order["replayed"] is False and order["fulfillment"]["type"] == "store_pickup"
    assert "Simulated order" in order["next_step"]
    assert len(order_keys()) == 2  # the order and its single line
    assert atp(HERO_80, option["location_id"]) == before - 1
    assert sizereq(rid)["status"] == "decided"
    assert a2a_types(client) == [
        "size_assist_requested",
        "fit_signal_shared",
        "size_requested",
        "size_recommended",
        "size_confirmed",
    ]
    decided = client.get(f"/api/purchases/{rid}", token=token).json
    assert decided["status"] == "decided" and decided["order_id"] == order["order_id"]
    listed = client.get("/api/orders", token=token).json["orders"]
    placed = next(o for o in listed if o["order_id"] == order["order_id"])
    assert placed["channel"] == "agent" and placed["delivered_at"] is None
    assert placed["lines"][0]["size"] == "8.0" and placed["lines"][0]["unit_paid_cents"] == view["price_cents"]


def test_reviewing_before_any_proposal_shows_an_open_request(client):
    assisted = assist(client)
    reply = client.get(f"/api/purchases/{assisted.json['size_request_id']}", token=session_token(AVA))
    assert reply.status == 200
    view = reply.json
    assert view["status"] == "open" and view["product_name"] == "Stessy2.0" and view["color"] == "BLACK"
    assert type(view["price_cents"]) is int and view["fit_signal"]["signal"] == "runs_small"
    for empty in ("option_id", "option_type", "size", "location_name", "eta_date", "order_id", "next_step", "confidence"):
        assert view[empty] is None, empty  # nothing has been proposed, so there is nothing to confirm


def test_an_unconfirmed_request_past_its_expiry_reads_as_expired(client, fixed_now):
    open_request = assist(client).json["size_request_id"]
    proposed_request = assist(client)
    propose(client, proposed_request)
    confirmed_request = assist(client)
    _, option = propose(client, confirmed_request)
    client.post(
        f"/api/purchases/{confirmed_request.json['size_request_id']}/confirm",
        {"option_id": option["option_id"]},
        token=session_token(AVA),
    )
    fixed_now.advance(seconds=16 * 60)
    token = session_token(AVA)

    expired_open = client.get(f"/api/purchases/{open_request}", token=token).json
    expired_proposal = client.get(f"/api/purchases/{proposed_request.json['size_request_id']}", token=token).json
    decided = client.get(f"/api/purchases/{confirmed_request.json['size_request_id']}", token=token).json

    assert expired_open["status"] == "expired"
    assert expired_proposal["status"] == "expired" and expired_proposal["size"] == "8.0"  # still shown
    assert decided["status"] == "decided" and decided["order_id"]  # a placed order does not expire


# The fields the three-column UI renders (report-frontend.md, "assumed by the UI").
PURCHASE_VIEW_KEYS = {
    "size_request_id", "status", "product_name", "color", "size", "option_id", "option_type", "location_name",
    "eta_date", "eta_text", "meets_deadline", "price_cents", "currency", "fit_signal", "rationale_public",
    "confidence", "expires_at", "order_id", "next_step",
}


def test_the_purchase_card_has_the_fields_the_ui_renders(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    token = session_token(AVA)

    card = client.get(f"/api/purchases/{rid}", token=token).json
    assert PURCHASE_VIEW_KEYS <= set(card)
    assert card["status"] == "proposed" and card["option_id"] == option["option_id"]
    assert card["option_type"] == "store_pickup" and card["size"] == "8.0" and card["meets_deadline"] is True
    assert card["eta_text"] == "Sat Oct 17" and card["location_name"] == "ALDO Demo Store Montreal"
    assert set(card["fit_signal"]) >= {"signal", "too_small_share", "n_fit", "provenance"}
    assert card["order_id"] is None and card["next_step"] is None

    confirmed = client.post(f"/api/purchases/{rid}/confirm", {"option_id": option["option_id"]}, token=token)
    assert {"order_id", "status", "line", "fulfillment", "next_step", "replayed"} <= set(confirmed.json)
    assert set(confirmed.json["line"]) >= {"sku", "size", "unit_paid_cents"}
    assert set(confirmed.json["fulfillment"]) >= {"type", "location_id", "location_name", "eta_date"}
    done = client.get(f"/api/purchases/{rid}", token=token).json
    assert done["status"] == "decided" and done["order_id"] == confirmed.json["order_id"] and done["next_step"]


def test_the_proposal_response_and_the_a2a_rows_carry_what_the_ui_correlates_on(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    proposed, option = propose(client, assisted)
    assert {"size_request_id", "status", "awaiting", "expires_at", "summary"} <= set(proposed.json)
    assert proposed.json["summary"]["option_id"] == option["option_id"]
    client.post(f"/api/purchases/{rid}/confirm", {}, token=session_token(AVA))

    events = client.get("/api/a2a/events", token=session_token(AVA)).json["events"]
    assert all({"seq", "type", "ts", "actor", "payload"} <= set(e) for e in events)
    by_type = {e["type"]: e for e in events}
    assert by_type["size_recommended"]["payload"]["size_request_id"] == rid
    assert by_type["size_confirmed"]["payload"]["order_id"].startswith("DEMO-A2A-")
    assert by_type["size_confirmed"]["actor"] == "aldo_gateway"


def test_confirming_twice_is_one_order_and_one_stock_decrement(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    before = atp(HERO_80, option["location_id"])
    token = session_token(AVA)

    first = client.post(f"/api/purchases/{rid}/confirm", {"option_id": option["option_id"]}, token=token)
    second = client.post(f"/api/purchases/{rid}/confirm", {}, token=token)  # a double click

    assert second.status == 200 and second.json["replayed"] is True
    assert second.json["order_id"] == first.json["order_id"]
    assert atp(HERO_80, option["location_id"]) == before - 1
    assert len(order_keys()) == 2
    assert a2a_types(client).count("size_confirmed") == 1


def test_confirming_a_different_option_than_the_one_reviewed_is_a_conflict(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    other = offered(assisted, "8.0", "ship_from_dc")["option_id"]
    assert other != option["option_id"]

    reply = client.post(f"/api/purchases/{rid}/confirm", {"option_id": other}, token=session_token(AVA))

    assert reply.status == 409 and reply.code == "IDEMPOTENCY_CONFLICT"
    assert order_keys() == [] and sizereq(rid)["status"] == "proposed"


def test_confirm_before_any_proposal_needs_approval(client):
    assisted = assist(client)
    reply = client.post(
        f"/api/purchases/{assisted.json['size_request_id']}/confirm", {}, token=session_token(AVA)
    )
    assert reply.status == 409 and reply.code == "APPROVAL_REQUIRED"
    assert order_keys() == []


def test_an_agent_cannot_confirm_and_another_customer_cannot_either(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    body = {"option_id": option["option_id"]}

    by_agent = client.post(f"/api/purchases/{rid}/confirm", body, token=delegated_token(AVA))
    assert by_agent.status == 403 and by_agent.code == "NOT_AUTHORIZED"
    by_ben = client.post(f"/api/purchases/{rid}/confirm", body, token=session_token(BEN))
    assert by_ben.status == 403 and by_ben.code == "NOT_AUTHORIZED"
    assert audit.list_audit(action="access_denied", actor=BEN)
    seen_by_ben = client.get(f"/api/purchases/{rid}", token=session_token(BEN))
    assert seen_by_ben.status == 403
    assert order_keys() == [] and sizereq(rid)["status"] == "proposed"


def test_a_proposal_that_went_out_of_stock_writes_nothing(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    store.update_item(store.inventory_key(HERO_80, option["location_id"]), set={"atp": 0})

    reply = client.post(f"/api/purchases/{rid}/confirm", {"option_id": option["option_id"]}, token=session_token(AVA))

    assert reply.status == 409 and reply.code == "STOCK_CHANGED"
    assert order_keys() == [] and sizereq(rid)["status"] == "proposed"


def test_an_expired_proposal_cannot_be_confirmed(client, fixed_now):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    fixed_now.advance(seconds=16 * 60)
    reply = client.post(f"/api/purchases/{rid}/confirm", {"option_id": option["option_id"]}, token=session_token(AVA))
    assert reply.status == 409 and reply.code == "REQUEST_EXPIRED"
    assert order_keys() == []


# --------------------------------------------------------------------------- demo controls


def test_simulate_delivery_only_for_the_owner_and_only_for_agent_orders(client):
    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    order_id = client.post(f"/api/purchases/{rid}/confirm", {}, token=session_token(AVA)).json["order_id"]

    foreign = client.post("/api/demo/simulate-delivery", {"order_id": order_id}, token=session_token(BEN))
    assert foreign.status == 403 and foreign.code == "NOT_AUTHORIZED"
    fixture_order = client.post("/api/demo/simulate-delivery", {"order_id": "DEMO-1007"}, token=session_token("DEMO-CUST-CHLOE"))
    assert fixture_order.status == 422  # fixture orders keep their dates
    delegated = client.post("/api/demo/simulate-delivery", {"order_id": order_id}, token=delegated_token(AVA))
    assert delegated.status == 403

    done = client.post("/api/demo/simulate-delivery", {"order_id": order_id}, token=session_token(AVA))
    assert done.status == 200 and done.json["delivered_at"] == "2026-10-16"
    listed = client.get("/api/orders", token=session_token(AVA)).json["orders"]
    assert next(o for o in listed if o["order_id"] == order_id)["delivered_at"] == "2026-10-16"


def test_reset_removes_the_proposal_the_order_and_the_trace_and_restores_stock(client):
    from .conftest import ADMIN_KEY

    assisted = assist(client)
    rid = assisted.json["size_request_id"]
    _, option = propose(client, assisted)
    client.post(f"/api/purchases/{rid}/confirm", {}, token=session_token(AVA))
    assert order_keys() and a2a_types(client)

    reset = client.post("/api/demo/reset", {"variant": "default"}, headers={"X-Demo-Admin": ADMIN_KEY})

    assert reset.status == 200
    assert order_keys() == [] and sizereq(rid) is None and a2a_types(client) == []
    assert atp(HERO_80, option["location_id"]) == 1  # the fixture value
