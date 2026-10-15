"""Who may call what: the authorization matrix of the single chat's new and changed routes.

Real gateway, real routes, both tables on moto. One table, four callers per route: nobody, the
owner's session, another owner's session, and the ALDO bot's delegated token. The same callers are
swept over every other route in ``test_gateway_routes.py`` (delegated token rejected everywhere).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import ADMIN_KEY, AVA, BEN, delegated_token, session_token
from .helpers import ApiClient
from .test_single_chat import CONTEXT, ORDER, PREFIX, hand_off, repo, say, start_chat

ADMIN = {"X-Demo-Admin": ADMIN_KEY}
JOHN = "DEMO-CUST-JOHN"
REVIEWS = {"style_id": "DEMO-STYLE-MEN001", "limit": 20}

#: (route, method, caller, status, error code) for the owner JOHN and another owner BEN.
MATRIX = [
    ("reviews", "POST", "nobody", 401, "NOT_AUTHENTICATED"),
    ("reviews", "POST", "junk", 401, "NOT_AUTHENTICATED"),
    ("reviews", "POST", "owner session", 403, "NOT_AUTHORIZED"),
    ("reviews", "POST", "other session", 403, "NOT_AUTHORIZED"),
    ("reviews", "POST", "delegated", 200, None),
    ("mode", "GET", "nobody", 401, "NOT_AUTHENTICATED"),
    ("mode", "GET", "owner session", 200, None),
    ("mode", "GET", "other session", 404, "NOT_FOUND"),
    ("mode", "GET", "delegated", 403, "NOT_AUTHORIZED"),
    ("message", "POST", "nobody", 401, "NOT_AUTHENTICATED"),
    ("message", "POST", "owner session", 202, None),
    ("message", "POST", "other session", 404, "NOT_FOUND"),
    ("message", "POST", "delegated", 403, "NOT_AUTHORIZED"),
]


@pytest.fixture
def world(client, seeded):
    assert client.post("/api/demo/reset", {"variant": "default"}, headers=ADMIN).status == 200
    return client


def caller_token(caller: str) -> tuple[str | None, dict[str, str] | None]:
    return {
        "nobody": (None, None),
        "junk": (None, {"Authorization": "Bearer junk"}),
        "owner session": (session_token(JOHN), None),
        "other session": (session_token(BEN), None),
        "delegated": (delegated_token(JOHN), None),
    }[caller]


@pytest.mark.parametrize("route,method,caller,status,code", MATRIX, ids=[f"{r}-{c}" for r, _, c, _, _ in MATRIX])
def test_the_authorization_matrix(world: ApiClient, enqueued: list[dict], route, method, caller, status, code):
    conv = start_chat(world, JOHN)
    path, body = {
        "reviews": ("/api/gateway/reviews", REVIEWS),
        "mode": (f"{PREFIX}/{conv}", None),
        "message": (f"{PREFIX}/{conv}/messages", {"text": "Hello"}),
    }[route]
    token, headers = caller_token(caller)

    reply = world.call(method, path, body=body, token=token, headers=headers)

    assert reply.status == status, reply.text
    assert (reply.code if status >= 400 else None) == code
    if route == "message":
        # only the owner's own session ever starts a run, and it is the owner's bot run
        assert [p["customer_id"] for p in enqueued] == ([JOHN] if status == 202 else [])
    if route == "reviews" and status == 200:
        assert reply.json["provenance"] == "synthetic" and reply.json["n_reviews"] >= 1
        assert "DEMO-CUST" not in reply.text  # a review is public data: nothing of any customer in it


def test_a_review_request_carries_no_customer_and_cannot_name_one(world: ApiClient):
    token = delegated_token(JOHN)
    for extra in ({"customer_id": AVA}, {"owner_ref": AVA}, {"order_id": ORDER}):
        reply = world.post("/api/gateway/reviews", {**REVIEWS, **extra}, token=token)
        assert reply.status == 422 and reply.code == "VALIDATION_ERROR", extra
    nothing = world.post("/api/gateway/reviews", {}, token=token)
    assert nothing.status == 422 and nothing.code == "VALIDATION_ERROR"  # a style or a product query is needed


def test_a_return_cannot_be_started_for_somebody_elses_order(world: ApiClient, enqueued: list[dict]):
    """The order id only ever comes from the bot's hand-off; the API checks it belongs to the owner."""
    conv = start_chat(world, BEN)
    hand_off(BEN, conv, order_id=ORDER)  # DEMO-1001 is Ava's
    reply = say(world, conv, "Too tight.", owner=BEN)
    assert reply.status == 403 and reply.code == "NOT_AUTHORIZED"
    assert [p for p in enqueued if p.get("agent") == "aldo"] == []
    assert repo().get_conversation(BEN, conv)["mode"] == "bot"  # and the chat is the bot's again


def test_nothing_in_the_message_body_can_pick_the_order_or_the_owner(world: ApiClient, enqueued: list[dict]):
    conv = start_chat(world, AVA)
    hand_off(AVA, conv)
    forged: dict[str, Any] = {"order_id": "DEMO-1007", "customer_id": BEN, "selected_order_id": "DEMO-1007",
                              "memory_context": {"typical_size": "13.0"}, "agent": "personal"}
    reply = say(world, conv, "Too tight.", **forged)
    assert reply.status == 202 and reply.json["agent"] == "returns"
    (payload,) = [p for p in enqueued if p.get("agent") == "aldo"]
    assert payload["customer_id"] == AVA and payload["order_id"] == ORDER and payload["selected_order_id"] == ORDER
    assert payload["memory_context"] == CONTEXT  # what the bot stored at the hand-off, not what the request says
    assert "DEMO-1007" not in json.dumps(payload)
