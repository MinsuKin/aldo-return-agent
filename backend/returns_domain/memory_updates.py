"""Size write-back from ALDO to the owner's personal agent: payload and outbox (PIVOT_ADDENDUM 5.4).

This module is the single implementation of the update payload and of the outbox item; the
gateway's ``build_memory_update`` / ``enqueue_memory_update`` / ``flush_outbox`` are thin wrappers.

* An exchange creates a PROVISIONAL update (``outcome = exchange_created``, ``provisional``
  true): at that moment ALDO cannot know the owner keeps the pair. Only a later pickup
  (``exchange_completed_kept``) sends ``provisional`` false.
* A colour exchange (same size, a reason that is not a fit reason) is a ``PREFERENCE`` update with
  ``fit_feedback = "size_ok"``: the owner kept the size and only changed the colour (D11).
  Every other update has exactly the fields of addendum 5.4.
* A BUNDLE return sends ONE small update for the whole shipment (``build_bundle_update``):
  ``outcome = return_created``, ``provisional`` true, a common ``reason`` (or ``OTHER``) and an
  ``items`` list (style, product, colour, size, reason per line), no ``fit_feedback`` and no sizes.
* The payload carries no typical size, hint, other retailer or event (C3). ``owner_ref`` is the
  synthetic persona id; it goes only to the owner's own agent endpoint and into no trace event.
* ``update_id = derived_id("MU-", transaction_id, outcome, n=6)``, so a replayed transaction
  can never create a second update.
* The outbox item ``A2AOUT#{update_id}/META`` is written in the SAME ``TransactWriteItems`` as the
  exchange (see ``execution``), so the write-back exists if and only if the exchange does.
  Pending items sit in GSI1 under ``A2AOUT#pending``; delivered or failed items leave the index.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from . import policy, store, trace
from .errors import DomainError, ErrorCode
from .taxonomy import parse_reason_category

REASONS: tuple[str, ...] = ("TOO_SMALL", "TOO_BIG", "OTHER", "PREFERENCE")
OUTCOMES: tuple[str, ...] = ("exchange_created", "exchange_completed_kept")
#: A colour exchange keeps the size, which tells the owner's agent the size was right.
PREFERENCE_REASON = "PREFERENCE"
FIT_FEEDBACK_SIZE_OK = "size_ok"
SOURCE = "ALDO_CA"
#: A bundle return (several lines, one shipment) sends ONE update with an ``items`` list.
RETURN_OUTCOME = "return_created"
MAX_BUNDLE_ITEMS = 10
MAX_ATTEMPTS = 5
MAX_ERROR_CHARS = 200
PENDING_GSI_PK = store.gsi1_outbox_pending("x")[0]


def _is_non_fit_reason(reason_category: object) -> bool:
    """A real reason category that is not a fit problem (None or an unknown value is neither)."""
    try:
        return not policy.is_fit_reason(parse_reason_category(reason_category))
    except DomainError:
        return False


def reason_for(
    size_direction: str | None,
    original_size: str,
    new_size: str,
    reason_category: str | None = None,
) -> str:
    """``PREFERENCE`` for a same-size exchange whose return reason is not a fit reason (a colour
    exchange: the size was right), else ``TOO_SMALL`` / ``TOO_BIG`` from the policy check's size
    direction, else from the sizes (a larger replacement means the pair was too small), else
    ``OTHER``. ``reason_category`` is the policy check's reason (None when unknown)."""
    if _is_non_fit_reason(reason_category) and store.size_str(original_size) == store.size_str(new_size):
        return PREFERENCE_REASON
    if size_direction == "too_small":
        return "TOO_SMALL"
    if size_direction == "too_big":
        return "TOO_BIG"
    original, new = Decimal(store.size_str(original_size)), Decimal(store.size_str(new_size))
    if new > original:
        return "TOO_SMALL"
    if new < original:
        return "TOO_BIG"
    return "OTHER"


def build_update(
    *,
    customer_id: str,
    order_id: str,
    line: Mapping[str, Any],
    txn: Mapping[str, Any],
    original_size: str,
    new_size: str,
    reason: str,
    outcome: str = "exchange_created",
    provisional: bool | None = None,
) -> dict[str, Any]:
    """The write-back body. ``line`` supplies style, product name and colour; ``txn`` the id."""
    if outcome not in OUTCOMES:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"outcome must be one of: {', '.join(OUTCOMES)}")
    if reason not in REASONS:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"reason must be one of: {', '.join(REASONS)}")
    expected = outcome == "exchange_created"  # only a completed pickup is final
    if provisional is None:
        provisional = expected
    if provisional is not expected:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "exchange_created is always provisional; exchange_completed_kept never is",
        )
    transaction_id = txn.get("transaction_id")
    if not isinstance(transaction_id, str) or not transaction_id:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "txn needs a transaction_id")
    if not isinstance(customer_id, str) or not customer_id:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "customer_id is required")
    sizes = (store.size_str(original_size), store.size_str(new_size))
    if reason == PREFERENCE_REASON and sizes[0] != sizes[1]:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "a PREFERENCE update keeps the size (original_size and new_size must be equal)",
        )
    update = {
        "update_id": store.derived_id("MU-", transaction_id, outcome, n=6),
        "owner_ref": customer_id,
        "order_id": order_id,
        "style_id": line["style"],
        "product_name": line["product_name"],
        "color": line["color"],
        "original_size": sizes[0],
        "new_size": sizes[1],
        "reason": reason,
        "outcome": outcome,
        "provisional": provisional,
        "source": SOURCE,
        "ts": store.now_iso(),
    }
    if reason == PREFERENCE_REASON:
        update["fit_feedback"] = FIT_FEEDBACK_SIZE_OK  # the size was right: only the colour changed
    return update


def build_bundle_update(
    *, customer_id: str, order_id: str, transaction_id: str, items: list[Mapping[str, Any]]
) -> dict[str, Any]:
    """The write-back of a BUNDLE return (domain v2 phase 2): ONE small update for all its lines.

    ``items`` are ``{"style_id", "product_name", "color", "size", "reason"}`` (one per line, at
    most ten); the update's own ``reason`` is the items' common reason or ``OTHER`` when they
    differ. ``outcome`` is ``return_created`` and ``provisional`` is true (the parcel has not
    arrived). There is no ``fit_feedback``, no original/new size and no typical size or other
    retailer (C3). ``update_id`` is derived from the transaction, so a replay is the same update.
    """
    if not isinstance(transaction_id, str) or not transaction_id:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "a bundle update needs a transaction_id")
    if not isinstance(customer_id, str) or not customer_id:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "customer_id is required")
    if not items or len(items) > MAX_BUNDLE_ITEMS:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"a bundle update has 1 to {MAX_BUNDLE_ITEMS} items")
    body: list[dict[str, Any]] = []
    for item in items:
        reason = item["reason"]
        if reason not in REASONS:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"reason must be one of: {', '.join(REASONS)}")
        body.append(
            {
                "style_id": item["style_id"],
                "product_name": item["product_name"],
                "color": item["color"],
                "size": store.size_str(item["size"]),
                "reason": reason,
            }
        )
    reasons = {entry["reason"] for entry in body}
    return {
        "update_id": store.derived_id("MU-", transaction_id, RETURN_OUTCOME, n=6),
        "owner_ref": customer_id,
        "order_id": order_id,
        "reason": reasons.pop() if len(reasons) == 1 else "OTHER",
        "outcome": RETURN_OUTCOME,
        "provisional": True,
        "source": SOURCE,
        "ts": store.now_iso(),
        "items": body,
    }


def update_from_txn_item(
    txn_item: Mapping[str, Any], *, outcome: str = "exchange_created", provisional: bool | None = None
) -> dict[str, Any] | None:
    """Update for a stored ``TXN#`` item, or None when the transaction is not an exchange
    (or a bundle return, which carries ``memory_items``; see ``build_bundle_update``).

    Uses the snapshot ``execution.execute`` stores on the item (customer, order, sizes, product,
    style, colour, reason), so a later pickup builds the same body without more lookups.
    """
    if txn_item.get("memory_items") and str(txn_item.get("option_type", "")).startswith("return_"):
        if outcome != "exchange_created":  # a return has no pickup completion
            return None
        return build_bundle_update(
            customer_id=txn_item["customer_id"],
            order_id=txn_item["order_id"],
            transaction_id=txn_item["transaction_id"],
            items=txn_item["memory_items"],
        )
    if not str(txn_item.get("option_type", "")).startswith("exchange_"):
        return None
    return build_update(
        customer_id=txn_item["customer_id"],
        order_id=txn_item["order_id"],
        line={
            "style": txn_item["style"],
            "product_name": txn_item["product_name"],
            "color": txn_item["color"],
        },
        txn={"transaction_id": txn_item["transaction_id"]},
        original_size=txn_item["original_size"],
        new_size=txn_item["replacement_size"],
        reason=txn_item.get("memory_reason") or "OTHER",
        outcome=outcome,
        provisional=provisional,
    )


def outbox_item(update: Mapping[str, Any], customer_id: str) -> dict[str, Any]:
    """The pending ``A2AOUT#{update_id}/META`` item for ``update``."""
    update_id = update.get("update_id")
    if not isinstance(update_id, str) or not update_id:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "update needs an update_id")
    created_at = store.now_iso()
    pk, sk = store.outbox_key(update_id)
    g_pk, g_sk = store.gsi1_outbox_pending(created_at)
    return {
        "pk": pk,
        "sk": sk,
        "update_id": update_id,
        "payload": dict(update),
        "customer_id": customer_id,
        "status": "pending",
        "attempts": 0,
        "created_at": created_at,
        "gsi1pk": g_pk,
        "gsi1sk": g_sk,
    }


def enqueue(update: Mapping[str, Any], customer_id: str | None = None) -> bool:
    """Put the outbox item unless it exists. False when it already did (idempotent)."""
    owner = customer_id or update.get("owner_ref")
    if not isinstance(owner, str) or not owner:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "the update has no owner")
    try:
        store.put_item(outbox_item(update, owner), condition="attribute_not_exists(#pk)")
    except store.ConditionFailed:
        return False
    return True


def get(update_id: str) -> dict[str, Any] | None:
    """The outbox item of one update (consistent read), or None."""
    return store.get_item(store.outbox_key(update_id))


def list_pending(customer_id: str | None = None, *, limit: int = 10) -> list[dict[str, Any]]:
    """Pending outbox items, oldest first (GSI1, eventually consistent), optionally for one owner.

    Each item carries ``update_id``, ``payload``, ``customer_id``, ``status``, ``attempts``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "limit must be a positive integer")
    rows = store.query(PENDING_GSI_PK, index="GSI1")
    pending = [
        row
        for row in rows
        if row.get("status") == "pending" and (customer_id is None or row.get("customer_id") == customer_id)
    ]
    return pending[:limit]


def mark_delivered(update_id: str) -> bool:
    """``pending -> delivered`` and drop the item from the pending index.

    Returns True only for the caller that made the transition; the personal agent's
    ``applied`` and ``duplicate`` answers both lead here, and only the winner emits the
    ``memory_updated`` trace event, so it appears exactly once.
    """
    try:
        store.update_item(
            store.outbox_key(update_id),
            set={"status": "delivered", "delivered_at": store.now_iso()},
            remove=["gsi1pk", "gsi1sk"],
            condition="#status = :pending",
            values={"pending": "pending"},
            return_new=False,
        )
    except store.ConditionFailed:
        return False
    return True


def mark_failed(update_id: str, error: str) -> None:
    """Record a failed delivery attempt. The item stays pending until ``MAX_ATTEMPTS``, then ``failed``."""
    redacted = trace.redact({"error": str(error)})
    message = str(redacted.get("error", "")) if isinstance(redacted, dict) else ""
    message = message[:MAX_ERROR_CHARS]
    try:
        item = store.update_item(
            store.outbox_key(update_id),
            add={"attempts": 1},
            set={"last_error": message, "last_attempt_at": store.now_iso()},
            condition="#status = :pending",
            values={"pending": "pending"},
        )
    except store.ConditionFailed:
        return  # already delivered or failed, or gone after a reset
    if item and int(item.get("attempts", 0)) >= MAX_ATTEMPTS:
        try:
            store.update_item(
                store.outbox_key(update_id),
                set={"status": "failed"},
                remove=["gsi1pk", "gsi1sk"],
                condition="#status = :pending",
                values={"pending": "pending"},
                return_new=False,
            )
        except store.ConditionFailed:
            pass
