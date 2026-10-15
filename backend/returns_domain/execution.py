"""Exactly-once execution of an approved resolution (requirement 8, design 5.5, G-04, G-08).

``execute`` never trusts its caller: the option ALWAYS comes from the server-side approval row
that only the authenticated approve endpoint creates. A missing approval, or an ``option_id`` that
differs from the approved one, is ``APPROVAL_REQUIRED`` with an audit event and no table change.
The commit is ONE ``TransactWriteItems`` over distinct items (transaction, idempotency record,
approval, order line, stock for exchanges, quote status and, for exchanges, the write-back outbox
item), so either everything happens or nothing does, and a retry or a second caller gets the
same transaction (``replayed``) without any write.

After the commit (and never for a replay) the call mirrors ``transaction_created`` to the
owner's agent-to-agent stream and flushes the write-back outbox through ``gateway.flush_outbox``.
Both are best effort: a failure there is logged (and shown as a ``warning`` event on the run)
but the exchange stays successful.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date, timedelta
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict

from . import audit, clock, config, memory_updates, money, orders, policy, quotes, store, trace
from .errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

MAX_TRANSIENT_RETRIES = 4
JITTER_SECONDS = (0.05, 0.2)
FLUSH_RETRY_DELAY_SECONDS = 0.2
FLUSH_LIMIT = 3
EXCHANGE_PREFIX = "exchange_"


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_id: str
    state: str  # "completed" = a simulated exchange or return was created, NOT refunded or shipped
    option_type: str
    replayed: bool
    quote_id: str
    approval_id: str
    memory_update_id: str | None = None  # exchanges only: the pending write-back to the owner's agent


class TransactionView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_id: str
    state: str
    option_type: str
    order_id: str
    line_id: str
    qty: int
    sku: str
    location_id: str
    location_name: str
    replacement_size: str | None = None
    refund_cents: int
    fee_cents: int
    currency: str = "CAD"
    eta_date: str
    eta_text: str
    next_step: str
    simulated: bool = True
    created_at: str
    quote_id: str
    reservation: dict[str, Any]
    original_size: str | None = None
    memory_update_id: str | None = None


class _Transient(Exception):
    """The transaction was cancelled for a reason a retry can resolve."""


def _sleep(seconds: float) -> None:  # a seam: tests replace it
    time.sleep(seconds)


def _is_exchange(option_type: str) -> bool:
    return option_type.startswith(EXCHANGE_PREFIX)


# --------------------------------------------------------------------------- execute


def execute(
    customer_id: str, quote_id: str, option_id: str | None = None, *, run_id: str | None = None
) -> ExecutionResult:
    """Execute the approved option of ``quote_id`` exactly once (see the module note)."""
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            return _execute_once(customer_id, quote_id, option_id, run_id)
        except _Transient:
            if attempt == MAX_TRANSIENT_RETRIES:
                break
            _sleep(random.uniform(*JITTER_SECONDS))
    raise DomainError(
        ErrorCode.TRANSACTION_CONFLICT,
        "The request collided with another change. Please try again.",
    )


def _deny(customer_id: str, quote_id: str, error: DomainError, run_id: str | None) -> DomainError:
    """Audit a denied execution and hand back the error to raise. Nothing else is written."""
    refs: dict[str, Any] = {"quote_id": quote_id, "code": error.code}
    if run_id:
        refs["run_id"] = run_id
    audit.write_audit(customer_id, "execute_denied", "denied", refs)
    return error


def _execute_once(
    customer_id: str, quote_id: str, option_id: str | None, run_id: str | None
) -> ExecutionResult:
    quote = store.get_item(store.quote_key(quote_id))
    if quote is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Quote not found.")
    if quote.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "quote", quote_id, run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Quote not found for this customer.")

    approval = store.get_item(store.approval_key(quote_id))
    if approval is None:
        raise _deny(customer_id, quote_id, DomainError(
            ErrorCode.APPROVAL_REQUIRED, "The customer has not approved this quote."), run_id)
    if option_id is not None and option_id != approval["option_id"]:
        raise _deny(customer_id, quote_id, DomainError(
            ErrorCode.APPROVAL_REQUIRED, "That option is not the one the customer approved."), run_id)
    option = next((o for o in quote["options"] if o["option_id"] == approval["option_id"]), None)
    expected_hash = (
        quotes.compute_payload_hash(quote_id, option, quote["policy_version"]) if option else None
    )
    if option is None or expected_hash != approval["payload_hash"] or expected_hash != option["payload_hash"]:
        raise _deny(customer_id, quote_id, DomainError(
            ErrorCode.VALIDATION_ERROR, "The approved option no longer matches the quote."), run_id)
    approval_id = approval["approval_id"]

    existing = store.get_item(store.idem_key(approval_id))
    if existing is not None:  # replays succeed even after the quote or the window expired
        return _replay(existing, quote_id, approval_id)

    try:
        check, order, line = _validate_for_commit(customer_id, quote, option, approval, run_id)
    except DomainError as denial:
        # A concurrent caller may have committed while we validated; its transaction wins (the
        # line it used up would otherwise make this look like an ineligible second request).
        winner = store.get_item(store.idem_key(approval_id))
        if winner is not None:
            return _replay(winner, quote_id, approval_id)
        if denial.code == ErrorCode.NOT_AUTHORIZED:  # already audited as access_denied
            raise
        raise _deny(customer_id, quote_id, denial, run_id) from None
    qty, kind = int(option["qty"]), option["type"]

    transaction_id = store.new_id("DEMO-RMA-" if _is_exchange(kind) else "DEMO-RET-")
    created_at = store.now_iso()
    txn = _transaction_item(
        transaction_id, customer_id, quote_id, approval_id, run_id, check, order, line, option, created_at
    )
    ops: list[store.TxPut | store.TxUpdate] = [
        store.TxPut(txn, condition="attribute_not_exists(#pk)", label="txn"),
        store.TxPut(
            {
                "pk": store.idem_key(approval_id)[0],
                "sk": store.idem_key(approval_id)[1],
                "approval_id": approval_id,
                "transaction_id": transaction_id,
                "quote_id": quote_id,
                "created_at": created_at,
            },
            condition="attribute_not_exists(#pk)",
            label="idem",
        ),
        store.TxUpdate(
            store.approval_key(quote_id),
            set={"consumed_txn_id": transaction_id},
            condition="attribute_not_exists(#consumed_txn_id)",
            label="approval",
        ),
        store.TxUpdate(
            store.line_key(check["order_id"], check["line_id"]),
            add={"returned_or_reserved_qty": qty},
            condition="#returned_or_reserved_qty <= :max",
            values={"max": int(line["purchased_qty"]) - qty},
            label="line",
        ),
    ]
    if _is_exchange(kind):
        ops.append(
            store.TxUpdate(
                store.inventory_key(option["sku"], option["location_id"]),
                add={"atp": -qty, "version": 1},
                condition="#atp >= :qty",
                values={"qty": qty},
                label="inventory",
            )
        )
    ops.append(store.TxUpdate(store.quote_key(quote_id), set={"status": "executed"}, label="quote"))
    memory_update_id: str | None = txn.get("memory_update_id")
    if memory_update_id:  # the write-back exists if and only if the exchange does
        update = memory_updates.update_from_txn_item(txn)
        ops.append(
            store.TxPut(
                memory_updates.outbox_item(update, customer_id),
                condition="attribute_not_exists(#pk)",
                label="outbox",
            )
        )

    try:
        store.transact_write(ops)
    except store.TransactionCanceled as exc:
        failed = set(exc.failed())
        if failed & {"idem", "approval"}:  # another caller won: return its transaction
            winner = store.get_item(store.idem_key(approval_id))
            if winner is not None:
                return _replay(winner, quote_id, approval_id)
        if "inventory" in failed:
            raise DomainError(ErrorCode.STOCK_CHANGED, "The replacement size is no longer in stock.") from None
        if "line" in failed:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR, "The quantity is no longer available to return or exchange."
            ) from None
        if exc.transient():
            raise _Transient() from exc
        raise DomainError(ErrorCode.INTERNAL_ERROR, "The resolution could not be recorded.") from exc

    result = ExecutionResult(
        transaction_id=transaction_id,
        state="completed",
        option_type=kind,
        replayed=False,
        quote_id=quote_id,
        approval_id=approval_id,
        memory_update_id=memory_update_id,
    )
    _after_commit(customer_id, run_id, result, txn, memory_update_id)
    return result


def _replay(idem: Mapping[str, Any], quote_id: str, approval_id: str) -> ExecutionResult:
    txn = store.get_item(store.txn_key(idem["transaction_id"]))
    if txn is None:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "The original transaction could not be read.")
    return ExecutionResult(
        transaction_id=txn["transaction_id"],
        state=txn["state"],
        option_type=txn["option_type"],
        replayed=True,
        quote_id=quote_id,
        approval_id=approval_id,
        memory_update_id=txn.get("memory_update_id"),
    )


def _validate_for_commit(
    customer_id: str,
    quote: Mapping[str, Any],
    option: Mapping[str, Any],
    approval: Mapping[str, Any],
    run_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Everything that must hold before the commit: window, policy against CURRENT data, stock.

    Raises a plain DomainError (the caller audits it); writes nothing. Returns the stored policy
    check, order and line that the transaction is built from.
    """
    window = timedelta(seconds=config.get_settings().execute_window_seconds)
    if clock.now_utc() > clock.parse_iso(approval["approved_at"]) + window:
        raise DomainError(ErrorCode.QUOTE_EXPIRED, "The approval is too old to execute. Ask for a new quote.")

    check = store.get_item(store.pchk_key(quote["policy_check_id"]))
    if check is None:
        raise DomainError(ErrorCode.MISSING_POLICY, "The policy check behind this quote is missing.")
    order, lines = orders.load_order_items(customer_id, check["order_id"], run_id=run_id)
    line = lines.get(check["line_id"])
    if line is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Order line not found on this order.")
    result = policy.evaluate(
        order, line, int(check["qty"]), check["reason_category"], check["condition"], now=clock.demo_now()
    )
    if not result.is_eligible or result.policy_version != quote["policy_version"]:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"policy check is not eligible (decision={result.decision.value})"
        )
    if option["type"] not in result.allowed_option_types:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "the policy does not allow this option type")

    if _is_exchange(option["type"]):
        stock = store.get_item(store.inventory_key(option["sku"], option["location_id"]))
        if stock is None or int(stock.get("atp", 0)) < int(option["qty"]):
            raise DomainError(ErrorCode.STOCK_CHANGED, "The replacement size is no longer in stock.")
    return check, order, line


def _transaction_item(
    transaction_id: str,
    customer_id: str,
    quote_id: str,
    approval_id: str,
    run_id: str | None,
    check: Mapping[str, Any],
    order: Mapping[str, Any],
    line: Mapping[str, Any],
    option: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    kind = option["type"]
    eta_text = clock.format_day(date.fromisoformat(option["eta_date"]))
    pk, sk = store.txn_key(transaction_id)
    item: dict[str, Any] = {
        "pk": pk,
        "sk": sk,
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        "quote_id": quote_id,
        "approval_id": approval_id,
        "run_id": run_id,
        "order_id": check["order_id"],
        "line_id": check["line_id"],
        "qty": int(option["qty"]),
        "sku": option["sku"],
        "option_id": option["option_id"],
        "option_type": kind,
        "location_id": option["location_id"],
        "location_name": option["location_name"],
        "replacement_size": option.get("replacement_size"),
        "refund_cents": int(option["refund_cents"]),
        "fee_cents": int(option["fee_cents"]),
        "currency": option.get("currency", money.CURRENCY),
        "eta_date": option["eta_date"],
        "eta_text": eta_text,
        "next_step": _next_step(option, eta_text),
        "state": "completed",
        "simulated": True,
        "created_at": created_at,
        "option": dict(option),
        # snapshot for the write-back and for a later simulated pickup
        "original_size": line["size"],
        "product_name": line["product_name"],
        "style": line["style"],
        "color": line["color"],
    }
    if _is_exchange(kind):
        item["memory_reason"] = memory_updates.reason_for(
            check.get("size_direction"), line["size"], option["replacement_size"]
        )
        item["memory_update_id"] = store.derived_id("MU-", transaction_id, "exchange_created", n=6)
    return item


def _next_step(option: Mapping[str, Any], eta_text: str) -> str:
    """Server-authored guidance. The model repeats it; it never writes the dates or amounts."""
    kind, where = option["type"], option["location_name"]
    size = option.get("replacement_size")
    if kind == "exchange_store_pickup":
        return f"Pick up size {size} at {where} from {eta_text}; bring the original pair, unworn."
    if _is_exchange(kind):
        return (
            f"Size {size} ships from {where} and should arrive by {eta_text}; "
            "send back the original pair, unworn."
        )
    refund = money.format_cad(int(option["refund_cents"]))
    if kind == "return_in_store":
        return f"Bring the original pair, unworn, to {where}. Your refund of {refund} is processed at drop-off."
    fee = money.format_cad(int(option["fee_cents"]))
    return (
        f"Mail the original pair, unworn, with the return label ({fee} is deducted). "
        f"Your refund of {refund} is expected by {eta_text} after the parcel is received."
    )


# --------------------------------------------------------------------- after the commit


def _flush_outbox(customer_id: str) -> Any:
    from . import gateway  # lazy: gateway.py is written by another stream

    return gateway.flush_outbox(customer_id, limit=FLUSH_LIMIT)


def _after_commit(
    customer_id: str,
    run_id: str | None,
    result: ExecutionResult,
    txn: Mapping[str, Any],
    memory_update_id: str | None,
) -> None:
    """Audit, trace, mirror to the A2A stream and flush the outbox. None of it can fail the call."""
    audit.write_audit(
        customer_id,
        "execute",
        "allowed",
        {"quote_id": result.quote_id, "transaction_id": result.transaction_id, "option_type": result.option_type},
    )
    if run_id:
        trace.emit_safe(
            run_id,
            "transaction_created",
            {
                "transaction_id": result.transaction_id,
                "state": result.state,
                "option_type": result.option_type,
                "quote_id": result.quote_id,
                "replayed": False,
            },
        )
    trace.emit_a2a_safe(  # public fields only: no customer id, no policy inputs, no free text
        customer_id,
        "transaction_created",
        {
            "transaction_id": result.transaction_id,
            "state": result.state,
            "option_type": result.option_type,
            "order_id": txn["order_id"],
            "replayed": False,
            "original_size": txn.get("original_size"),
            "new_size": txn.get("replacement_size"),  # None for returns
            "location_id": txn["location_id"],
            "fee_cents": txn["fee_cents"],
        },
        actor="aldo_agent",
    )
    if memory_update_id:
        _flush_after_commit(customer_id, run_id, memory_update_id)


def _flush_after_commit(customer_id: str, run_id: str | None, update_id: str) -> None:
    try:
        _flush_outbox(customer_id)
        item = memory_updates.get(update_id)
        # No delivery attempt was recorded and it is still pending: the index had not caught up
        # with the new item yet. One more flush after a short pause; real failures are retried later.
        if item and item.get("status") == "pending" and int(item.get("attempts", 0)) == 0:
            _sleep(FLUSH_RETRY_DELAY_SECONDS)
            _flush_outbox(customer_id)
    except Exception:
        logger.warning("write-back flush failed", extra={"update_id": update_id}, exc_info=True)
        if run_id:
            trace.emit_safe(
                run_id,
                "warning",
                {"code": "WRITEBACK_PENDING", "message": "The size update for the owner's agent will be retried."},
            )


# ------------------------------------------------------------------------------ status


def get_status(customer_id: str, transaction_id: str, *, run_id: str | None = None) -> TransactionView:
    """The stored transaction plus the CURRENT reservation snapshot (consistent reads)."""
    txn = store.get_item(store.txn_key(transaction_id))
    if txn is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Transaction not found.")
    if txn.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "transaction", transaction_id, run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Transaction not found for this customer.")
    line = store.get_item(store.line_key(txn["order_id"], txn["line_id"])) or {}
    stock = (
        store.get_item(store.inventory_key(txn["sku"], txn["location_id"]))
        if _is_exchange(txn["option_type"])
        else None
    )
    reservation = {
        "line_returned_or_reserved_qty": line.get("returned_or_reserved_qty"),
        "line_purchased_qty": line.get("purchased_qty"),
        "location_atp": stock.get("atp") if stock else None,
    }
    fields = {
        k: v
        for k, v in txn.items()
        if k not in ("pk", "sk", "customer_id", "option", "approval_id", "run_id")
    }
    return TransactionView(**fields, reservation=reservation)
