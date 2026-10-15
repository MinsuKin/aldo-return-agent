"""memory_updates.py: the write-back payload and its outbox (PIVOT_ADDENDUM 5.4, 9.2; contracts.md F.5)."""

from __future__ import annotations

import json
import re

import pytest

from returns_domain import memory_updates as mu
from returns_domain import store
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN

LINE = {"style": "DEMO-STYLE-HERO", "product_name": "Stessy2.0", "color": "BLACK"}
TXN = {"transaction_id": "DEMO-RMA-3F9A1C"}


def build(**overrides):
    args = dict(customer_id=AVA, order_id="DEMO-A2A-81B20E", line=LINE, txn=TXN, original_size="8.0",
                new_size="8.5", reason="TOO_SMALL")
    args.update(overrides)
    return mu.build_update(**args)


def outbox_row(update_id):
    return store.get_item(store.outbox_key(update_id))


# -------------------------------------------------------------------------- payload


def test_update_has_exactly_the_documented_fields_and_nothing_private(aws_env, fixed_now):
    update = build()
    assert set(update) == {"update_id", "owner_ref", "order_id", "style_id", "product_name", "color", "original_size",
                           "new_size", "reason", "outcome", "provisional", "source", "ts"}
    assert update == {
        "update_id": mu.build_update.__globals__["store"].derived_id("MU-", "DEMO-RMA-3F9A1C", "exchange_created", n=6),
        "owner_ref": AVA, "order_id": "DEMO-A2A-81B20E", "style_id": "DEMO-STYLE-HERO", "product_name": "Stessy2.0",
        "color": "BLACK", "original_size": "8.0", "new_size": "8.5", "reason": "TOO_SMALL", "outcome": "exchange_created",
        "provisional": True, "source": "ALDO_CA", "ts": "2026-10-16T16:00:00+00:00"}
    assert re.fullmatch(r"MU-[0-9A-F]{6}", update["update_id"])
    text = json.dumps(update).lower()
    for private in ("typical", "hint", "retailer", "memory"):
        assert private not in text


def test_update_id_is_derived_from_transaction_and_outcome(aws_env):
    created = build()["update_id"]
    assert created == build()["update_id"]  # a replayed transaction cannot make a second update
    assert build(txn={"transaction_id": "DEMO-RMA-OTHER1"})["update_id"] != created
    kept = build(outcome="exchange_completed_kept")["update_id"]
    assert kept != created and re.fullmatch(r"MU-[0-9A-F]{6}", kept)


def test_provisional_follows_the_outcome(aws_env):
    assert build()["provisional"] is True
    assert build(provisional=True)["provisional"] is True
    assert build(outcome="exchange_completed_kept")["provisional"] is False
    assert build(outcome="exchange_completed_kept", provisional=False)["provisional"] is False
    for bad in (dict(provisional=False), dict(outcome="exchange_completed_kept", provisional=True)):
        with pytest.raises(DomainError) as exc:
            build(**bad)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize(
    "bad",
    [dict(reason="WHATEVER"), dict(outcome="exchange_cancelled"), dict(new_size="abc"), dict(original_size=""),
     dict(txn={}), dict(txn={"transaction_id": ""}), dict(customer_id="")],
)
def test_invalid_input_is_rejected(aws_env, bad):
    with pytest.raises(DomainError) as exc:
        build(**bad)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_sizes_are_normalised_to_one_decimal(aws_env):
    update = build(original_size=8, new_size=8.5)
    assert (update["original_size"], update["new_size"]) == ("8.0", "8.5")


@pytest.mark.parametrize(
    ("direction", "original", "new", "expected"),
    [("too_small", "8.0", "8.5", "TOO_SMALL"), ("too_big", "8.0", "7.5", "TOO_BIG"),
     ("too_small", "8.0", "7.5", "TOO_SMALL"),  # the stored direction wins over the sizes
     ("unknown", "8.0", "8.5", "TOO_SMALL"), ("unknown", "8.0", "7.5", "TOO_BIG"), (None, "7.0", "7.5", "TOO_SMALL"),
     ("unknown", "8.0", "8.0", "OTHER")],
)
def test_reason_for(aws_env, direction, original, new, expected):
    assert mu.reason_for(direction, original, new) == expected


# ------------------------------------------------- domain v2: a colour exchange keeps the size


@pytest.mark.parametrize("reason_category", ["EXPECTATION", "CHANGE_OF_MIND", "COMFORT", "OTHER", "UNKNOWN", "quality_damage"])
def test_a_same_size_exchange_for_a_non_fit_reason_is_a_preference(aws_env, reason_category):
    assert mu.reason_for("unknown", "9.5", "9.5", reason_category) == "PREFERENCE"
    assert mu.reason_for("too_small", "9.5", "9.5", reason_category) == "PREFERENCE"  # a stored direction cannot make it a fit problem
    assert mu.reason_for(None, 9.5, "9.50", reason_category) == "PREFERENCE"


@pytest.mark.parametrize(
    ("direction", "original", "new", "reason_category", "expected"),
    [
        ("unknown", "9.5", "9.5", "FIT_SIZE", "OTHER"),  # a fit reason with the same size is not a preference
        ("unknown", "9.5", "9.5", None, "OTHER"),  # reason unknown: the old behaviour
        ("too_small", "9.5", "9.5", "FIT_SIZE", "TOO_SMALL"),
        ("too_small", "8.0", "8.5", "FIT_SIZE", "TOO_SMALL"),
        ("unknown", "8.0", "8.5", "CHANGE_OF_MIND", "TOO_SMALL"),  # the size changed: it is about fit after all
        ("too_big", "8.0", "7.5", "EXPECTATION", "TOO_BIG"),
        ("unknown", "8.0", "7.5", None, "TOO_BIG"),
    ],
)
def test_only_a_same_size_non_fit_exchange_becomes_a_preference(aws_env, direction, original, new, reason_category, expected):
    assert mu.reason_for(direction, original, new, reason_category) == expected


def test_a_preference_update_adds_fit_feedback_size_ok_and_nothing_else(aws_env, fixed_now):
    update = build(original_size="9.5", new_size="9.5", reason="PREFERENCE")
    assert update["reason"] == "PREFERENCE" and update["fit_feedback"] == "size_ok"
    assert (update["original_size"], update["new_size"], update["outcome"], update["provisional"]) == ("9.5", "9.5", "exchange_created", True)
    assert set(update) == {"update_id", "owner_ref", "order_id", "style_id", "product_name", "color", "original_size", "new_size",
                           "reason", "outcome", "provisional", "source", "ts", "fit_feedback"}
    assert mu.PREFERENCE_REASON == "PREFERENCE" and mu.FIT_FEEDBACK_SIZE_OK == "size_ok" and "PREFERENCE" in mu.REASONS
    text = json.dumps(update).lower()
    for private in ("typical", "hint", "retailer", "memory"):
        assert private not in text


def test_a_preference_update_has_the_same_id_and_provisional_rules_as_any_exchange(aws_env):
    created = build(original_size="9.5", new_size="9.5", reason="PREFERENCE")
    kept = build(original_size="9.5", new_size="9.5", reason="PREFERENCE", outcome="exchange_completed_kept")
    assert created["update_id"] == build(original_size="9.5", new_size="9.5", reason="PREFERENCE")["update_id"]
    assert created["update_id"] != kept["update_id"] and created["provisional"] is True and kept["provisional"] is False
    assert kept["fit_feedback"] == "size_ok"
    with pytest.raises(DomainError) as exc:
        build(original_size="9.5", new_size="9.5", reason="PREFERENCE", provisional=False)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_a_preference_update_must_keep_the_size(aws_env):
    with pytest.raises(DomainError) as exc:
        build(original_size="9.5", new_size="10.0", reason="PREFERENCE")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "keeps the size" in exc.value.message


@pytest.mark.parametrize("reason", ["TOO_SMALL", "TOO_BIG", "OTHER"])
def test_the_other_reasons_have_no_fit_feedback_field(aws_env, reason):
    assert "fit_feedback" not in build(reason=reason)
    assert "fit_feedback" not in build(original_size="9.5", new_size="9.5", reason=reason)


def test_the_outbox_keeps_the_preference_payload_whole(ddb, fixed_now):
    update = build(original_size="9.5", new_size="9.5", reason="PREFERENCE")
    assert mu.enqueue(update) is True
    row = outbox_row(update["update_id"])
    assert row["payload"] == update and row["payload"]["fit_feedback"] == "size_ok"
    assert (row["status"], row["attempts"]) == ("pending", 0) and [i["update_id"] for i in mu.list_pending(AVA)] == [update["update_id"]]


def test_update_from_a_colour_exchange_transaction_item(aws_env):
    item = txn_item(replacement_size="9.5", original_size="9.5", memory_reason="PREFERENCE", replacement_color="BROWN")
    update = mu.update_from_txn_item(item)
    assert (update["reason"], update["fit_feedback"], update["original_size"], update["new_size"], update["provisional"]) == (
        "PREFERENCE", "size_ok", "9.5", "9.5", True)
    assert update["color"] == "BLACK"  # the purchased colour: the feedback is about the size of the style
    kept = mu.update_from_txn_item(item, outcome="exchange_completed_kept")
    assert kept["fit_feedback"] == "size_ok" and kept["provisional"] is False and kept["update_id"] != update["update_id"]


# --------------------------------------------------------------------------- outbox


def test_outbox_item_shape(ddb, fixed_now):
    update = build()
    item = mu.outbox_item(update, AVA)
    assert (item["pk"], item["sk"]) == (f"A2AOUT#{update['update_id']}", "META")
    assert item["payload"] == update and item["customer_id"] == AVA
    assert (item["status"], item["attempts"]) == ("pending", 0)
    assert item["created_at"] == "2026-10-16T16:00:00+00:00"
    assert (item["gsi1pk"], item["gsi1sk"]) == ("A2AOUT#pending", item["created_at"])
    assert item["update_id"] == update["update_id"]
    with pytest.raises(DomainError):
        mu.outbox_item({"owner_ref": AVA}, AVA)


def test_enqueue_is_idempotent_and_never_overwrites(ddb, fixed_now):
    update = build()
    assert mu.enqueue(update) is True  # the owner defaults to owner_ref
    fixed_now.advance(seconds=60)
    changed = {**update, "new_size": "9.0"}
    assert mu.enqueue(changed) is False
    row = outbox_row(update["update_id"])
    assert row["payload"]["new_size"] == "8.5" and row["created_at"] == "2026-10-16T16:00:00+00:00"
    assert row["customer_id"] == AVA
    with pytest.raises(DomainError):
        mu.enqueue({**update, "update_id": "MU-NOOWNR", "owner_ref": ""})


def test_list_pending_is_oldest_first_filtered_and_limited(ddb, fixed_now):
    updates = []
    for n, customer in enumerate((AVA, BEN, AVA, AVA)):
        update = build(customer_id=customer, txn={"transaction_id": f"DEMO-RMA-00000{n}"})
        mu.enqueue(update)
        updates.append(update)
        fixed_now.advance(seconds=10)
    ids = [u["update_id"] for u in updates]
    assert [i["update_id"] for i in mu.list_pending()] == ids
    assert [i["update_id"] for i in mu.list_pending(AVA)] == [ids[0], ids[2], ids[3]]
    assert [i["update_id"] for i in mu.list_pending(BEN)] == [ids[1]]
    assert [i["update_id"] for i in mu.list_pending(AVA, limit=2)] == [ids[0], ids[2]]
    assert mu.list_pending("DEMO-CUST-NOBODY") == []
    first = mu.list_pending(AVA)[0]
    assert first["payload"]["owner_ref"] == AVA and first["attempts"] == 0 and first["status"] == "pending"
    for bad in (0, -1, True, 1.5):
        with pytest.raises(DomainError):
            mu.list_pending(limit=bad)


def test_mark_delivered_has_exactly_one_winner_and_leaves_the_pending_index(ddb):
    update = build()
    mu.enqueue(update)
    assert mu.mark_delivered(update["update_id"]) is True  # the caller that emits memory_updated
    assert mu.mark_delivered(update["update_id"]) is False
    row = outbox_row(update["update_id"])
    assert row["status"] == "delivered" and row["delivered_at"] and "gsi1pk" not in row and "gsi1sk" not in row
    assert mu.list_pending() == [] and mu.get(update["update_id"])["status"] == "delivered"


def test_mark_delivered_on_an_unknown_update_creates_nothing(ddb):
    assert mu.mark_delivered("MU-GHOST1") is False
    assert store.get_item(store.outbox_key("MU-GHOST1")) is None


def test_mark_failed_counts_attempts_and_gives_up_after_five(ddb):
    update = build()
    mu.enqueue(update)
    for attempt in range(1, 5):
        mu.mark_failed(update["update_id"], f"timeout {attempt}")
        row = outbox_row(update["update_id"])
        assert row["status"] == "pending" and row["attempts"] == attempt and row["last_error"] == f"timeout {attempt}"
        assert [i["update_id"] for i in mu.list_pending()] == [update["update_id"]]
    mu.mark_failed(update["update_id"], "timeout 5")
    row = outbox_row(update["update_id"])
    assert row["status"] == "failed" and row["attempts"] == 5 and "gsi1pk" not in row
    assert mu.list_pending() == []
    mu.mark_failed(update["update_id"], "after the end")  # no-op
    assert outbox_row(update["update_id"])["attempts"] == 5


def test_mark_failed_keeps_secrets_and_long_errors_out_of_the_table(ddb):
    update = build()
    mu.enqueue(update)
    token_like = "a" * 30 + "." + "b" * 30
    mu.mark_failed(update["update_id"], f"POST failed with {token_like} " + "x" * 500)
    error = outbox_row(update["update_id"])["last_error"]
    assert token_like not in error and len(error) <= 200


def test_mark_failed_ignores_delivered_and_unknown_updates(ddb):
    update = build()
    mu.enqueue(update)
    mu.mark_delivered(update["update_id"])
    mu.mark_failed(update["update_id"], "late failure")
    assert outbox_row(update["update_id"])["attempts"] == 0
    mu.mark_failed("MU-GHOST1", "x")
    assert store.get_item(store.outbox_key("MU-GHOST1")) is None


def test_the_pending_partition_name_matches_the_key_builder(aws_env):
    assert mu.PENDING_GSI_PK == store.gsi1_outbox_pending("x")[0] == "A2AOUT#pending"


# ------------------------------------------------------------------ from a stored TXN


def txn_item(**overrides):
    item = {"transaction_id": "DEMO-RMA-3F9A1C", "customer_id": AVA, "order_id": "DEMO-1001", "option_type": "exchange_store_pickup",
            "replacement_size": "7.5", "original_size": "7.0", "product_name": "Stessy2.0", "style": "DEMO-STYLE-HERO",
            "color": "BLACK", "memory_reason": "TOO_SMALL"}
    item.update(overrides)
    return item


def test_update_from_a_transaction_item(aws_env):
    update = mu.update_from_txn_item(txn_item())
    assert (update["original_size"], update["new_size"], update["reason"], update["provisional"]) == ("7.0", "7.5", "TOO_SMALL", True)
    kept = mu.update_from_txn_item(txn_item(), outcome="exchange_completed_kept")
    assert kept["provisional"] is False and kept["update_id"] != update["update_id"]
    assert kept["order_id"] == update["order_id"] and kept["owner_ref"] == AVA


def test_returns_have_no_update(aws_env):
    for option_type in ("return_in_store", "return_mail"):
        assert mu.update_from_txn_item(txn_item(option_type=option_type, replacement_size=None)) is None
    assert mu.update_from_txn_item(txn_item(memory_reason=None))["reason"] == "OTHER"
