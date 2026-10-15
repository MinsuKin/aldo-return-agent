"""Exactly-once execution of an approved resolution (requirement 8, design 5.5, G-04, G-08).

``execute`` never trusts its caller: the option ALWAYS comes from the server-side approval row
that only the authenticated approve endpoint creates. A missing approval, or an ``option_id`` that
differs from the approved one, is ``APPROVAL_REQUIRED`` with an audit event and no table change.
The commit is ONE ``TransactWriteItems`` over distinct items (transaction, idempotency record,
approval, order line, stock for exchanges, quote status, for exchanges the write-back outbox
item and, for in-store options, the store-desk notice ``STORENOTE#{txn}``), so either everything
happens or nothing does, and a retry or a second caller gets the same transaction (``replayed``)
without any write.

A BUNDLE option (it carries ``lines``) commits the same way with one guarded update per line, the
member's monthly perk ledger (condition ``used < per_month``, only when the option carries
``benefit``), one small write-back item and, in store, the notice: at most 16 operations for ten
lines. If the perk was used meanwhile, nothing is written and the error is STOCK_CHANGED with
``details.reason`` ``BENEFIT_USED`` so the agent can quote again without the waiver.

After the commit (and never for a replay) the call mirrors ``transaction_created`` (and, for an
in-store option, ``store_notified`` from the actor ``aldo_store``; for a bundle that used the perk,
``benefit_applied``) to the owner's agent-to-agent
stream and flushes the write-back outbox through ``gateway.flush_outbox``. All of it is best
effort: a failure there is logged (and shown as a ``warning`` event on the run) but the exchange
stays successful.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import date, timedelta
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, model_serializer

from . import audit, benefits, clock, config, memory_updates, money, orders, policy, quotes, store, trace
from .errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)

MAX_TRANSIENT_RETRIES = 4
JITTER_SECONDS = (0.05, 0.2)
FLUSH_RETRY_DELAY_SECONDS = 0.2
FLUSH_LIMIT = 3
EXCHANGE_PREFIX = "exchange_"
#: Options where the customer goes to a store; approving one notifies that store (simulated).
STORE_NOTICE_TYPES: tuple[str, ...] = ("exchange_store_pickup", "return_in_store")
MAX_NOTICE_CHARS = 240
#: A bundle return takes up to this many lines (same constant as ``options.MAX_BUNDLE_LINES``).
MAX_BUNDLE_LINES = 10


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_id: str
    state: str  # "completed" = a simulated exchange or return was created, NOT refunded or shipped
    option_type: str
    replayed: bool
    quote_id: str
    approval_id: str
    memory_update_id: str | None = None  # exchanges and bundle returns: the pending write-back to the owner's agent
    #: In-store options only: what the (simulated) store desk was told, written in the same
    #: transaction as the exchange or return. No customer id.
    store_notice: dict[str, Any] | None = None
    #: Bundle returns only (left out of the JSON otherwise): the member perk this shipment used.
    benefit: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_benefit_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("benefit") is None:
            data.pop("benefit", None)
        return data


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
    replacement_color: str | None = None  # a colour exchange: the colour the customer takes home
    store_notice: dict[str, Any] | None = None  # in-store options only
    # Bundle returns only (left out of the JSON otherwise). ``sku`` / ``line_id`` are the first
    # line's, ``qty`` the total quantity and ``refund_cents`` the net refund (``net_refund_cents``);
    # ``lines`` is the authoritative list. ``fee_cents`` is the ONE shipping fee of the shipment.
    lines: list[dict[str, Any]] | None = None
    refund_total_cents: int | None = None
    net_refund_cents: int | None = None
    benefit: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_bundle_fields_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in ("lines", "refund_total_cents", "net_refund_cents", "benefit"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


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

    is_bundle = bool(option.get("lines"))  # several lines of one order in one shipment
    try:
        if is_bundle:
            context = _validate_bundle_for_commit(customer_id, quote, option, approval, run_id)
        else:
            check, order, line, profile = _validate_for_commit(customer_id, quote, option, approval, run_id)
    except DomainError as denial:
        # A concurrent caller may have committed while we validated; its transaction wins (the
        # line it used up would otherwise make this look like an ineligible second request).
        winner = store.get_item(store.idem_key(approval_id))
        if winner is not None:
            return _replay(winner, quote_id, approval_id)
        if denial.code == ErrorCode.NOT_AUTHORIZED:  # already audited as access_denied
            raise
        raise _deny(customer_id, quote_id, denial, run_id) from None
    if is_bundle:
        return _commit_bundle(customer_id, quote_id, approval_id, run_id, option, context)
    qty, kind = int(option["qty"]), option["type"]

    transaction_id = store.new_id("DEMO-RMA-" if _is_exchange(kind) else "DEMO-RET-")
    created_at = store.now_iso()
    txn = _transaction_item(
        transaction_id, customer_id, quote_id, approval_id, run_id, check, order, line, option, created_at
    )
    store_notice = _store_notice(profile, option, line) if kind in STORE_NOTICE_TYPES else None
    if store_notice is not None:
        txn["store_notice"] = store_notice  # the same public copy the result and the status view return
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
    if store_notice is not None:  # the store desk's record exists if and only if the exchange or return does
        note_pk, note_sk = store.storenote_key(transaction_id)
        ops.append(
            store.TxPut(
                {
                    "pk": note_pk,
                    "sk": note_sk,
                    "transaction_id": transaction_id,
                    "option_type": kind,
                    "order_id": check["order_id"],
                    **store_notice,
                    "status": "notified",
                    "created_at": created_at,
                },
                condition="attribute_not_exists(#pk)",
                label="store_notice",
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
        store_notice=store_notice,
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
        store_notice=txn.get("store_notice"),
        benefit=txn.get("benefit"),
    )


def _validate_for_commit(
    customer_id: str,
    quote: Mapping[str, Any],
    option: Mapping[str, Any],
    approval: Mapping[str, Any],
    run_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Everything that must hold before the commit: window, policy against CURRENT data, stock.

    Raises a plain DomainError (the caller audits it); writes nothing. Returns the stored policy
    check, order and line that the transaction is built from, and the customer's profile (the
    member tier's authority limit applies to the policy re-check; the store notice names the
    customer).
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
    profile = orders.customer_profile(customer_id)
    result = policy.evaluate(
        order,
        line,
        int(check["qty"]),
        check["reason_category"],
        check["condition"],
        now=clock.demo_now(),
        authority_limit_cents=profile["authority_limit_cents"],
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
    return check, order, line, profile


# -------------------------------------------------------------------- bundle returns


def _benefit_gone(message: str, reason: str) -> DomainError:
    """STOCK_CHANGED-style: the quote was priced with a perk that cannot be used any more.
    The agent re-quotes without the waiver (``find_resolution_options`` again)."""
    return DomainError(ErrorCode.STOCK_CHANGED, message, details={"reason": reason})


def _validate_bundle_for_commit(
    customer_id: str,
    quote: Mapping[str, Any],
    option: Mapping[str, Any],
    approval: Mapping[str, Any],
    run_id: str | None,
) -> dict[str, Any]:
    """Everything that must hold before a bundle commits: window, EVERY line's policy against
    CURRENT data, the recomputed amounts, the tier authority limit on the total and the perk.

    Raises a plain DomainError (the caller audits it); writes nothing. Returns the context the
    transaction is built from.
    """
    window = timedelta(seconds=config.get_settings().execute_window_seconds)
    if clock.now_utc() > clock.parse_iso(approval["approved_at"]) + window:
        raise DomainError(ErrorCode.QUOTE_EXPIRED, "The approval is too old to execute. Ask for a new quote.")
    if option["type"] not in policy.RETURN_OPTION_TYPES:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "a bundle can only be returned, not exchanged")
    entries = option["lines"]
    changed = DomainError(ErrorCode.VALIDATION_ERROR, "The approved option no longer matches the quote.")
    if not 1 <= len(entries) <= MAX_BUNDLE_LINES or len({e["line_id"] for e in entries}) != len(entries):
        raise changed

    order, lines = orders.load_order_items(customer_id, quote["order_id"], run_id=run_id)
    profile = orders.customer_profile(customer_id)
    limit = int(profile["authority_limit_cents"])
    members: list[dict[str, Any]] = []
    total = 0
    for entry in entries:
        check = store.get_item(store.pchk_key(entry["policy_check_id"]))
        if check is None:
            raise DomainError(ErrorCode.MISSING_POLICY, "A policy check behind this quote is missing.")
        if (
            check.get("customer_id") != customer_id
            or check.get("order_id") != quote["order_id"]
            or check.get("line_id") != entry["line_id"]
        ):
            raise changed
        line = lines.get(entry["line_id"])
        if line is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Order line not found on this order.")
        qty = int(check["qty"])
        result = policy.evaluate(
            order, line, qty, check["reason_category"], check["condition"],
            now=clock.demo_now(), authority_limit_cents=limit,
        )
        if not result.is_eligible or result.policy_version != quote["policy_version"]:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                f"policy check is not eligible (decision={result.decision.value}) for line {entry['line_id']}",
            )
        if option["type"] not in result.allowed_option_types:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "the policy does not allow this option type")
        refund = policy.gross_refund_cents(line, qty)
        if qty != int(entry["qty"]) or refund != int(entry["refund_cents"]):
            raise changed  # the price or the quantity moved since the quote
        total += refund
        members.append({"entry": entry, "check": check, "line": line, "qty": qty})
    if total != int(option["refund_total_cents"]):
        raise changed
    if total > limit or option.get("decision") == "needs_review":  # the tier limit on the TOTAL, at execution too
        raise DomainError(ErrorCode.VALIDATION_ERROR, "policy check is not eligible (decision=needs_review)")

    perk = option.get("benefit")
    per_month: int | None = None
    if perk:
        if option["type"] != benefits.MAIL_RETURN or int(option["fee_cents"]) != 0:
            raise changed
        usage = benefits.ledger_usage(customer_id)
        if usage["allowed"] < 1 or usage["period"] != perk.get("period"):
            raise _benefit_gone(
                "The member's free return shipping no longer applies. Find the options again.", "BENEFIT_UNAVAILABLE"
            )
        if usage["used"] >= usage["allowed"]:
            raise _benefit_gone(
                "The free return shipping of this month was used since the quote. Find the options again.",
                "BENEFIT_USED",
            )
        per_month = usage["allowed"]
    return {"order": order, "profile": profile, "members": members, "total": total, "per_month": per_month}


def _commit_bundle(
    customer_id: str,
    quote_id: str,
    approval_id: str,
    run_id: str | None,
    option: Mapping[str, Any],
    context: Mapping[str, Any],
) -> ExecutionResult:
    """ONE TransactWriteItems for the whole bundle: the transaction (with its ``lines``), the
    idempotency record, the approval, one guarded update per line, the perk ledger (only when the
    option carries ``benefit``), the quote, the write-back outbox item and, in store, the notice.
    At most 3 + 10 lines + 3 = 16 operations (txn, idem, approval, the lines, then the ledger OR
    the store notice, quote, outbox), measured on moto; DynamoDB allows 100."""
    members, profile = context["members"], context["profile"]
    order_id = members[0]["check"]["order_id"]
    kind = option["type"]
    transaction_id = store.new_id("DEMO-RET-")
    created_at = store.now_iso()
    perk = dict(option["benefit"]) if option.get("benefit") else None
    txn = _bundle_transaction_item(
        transaction_id, customer_id, quote_id, approval_id, run_id, order_id, option, members, profile, perk, created_at
    )
    store_notice = _bundle_store_notice(profile, option) if kind in STORE_NOTICE_TYPES else None
    if store_notice is not None:
        txn["store_notice"] = store_notice

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
    ]
    for member in members:  # the same returned-quantity guard as the single-line path, per line
        ops.append(
            store.TxUpdate(
                store.line_key(order_id, member["entry"]["line_id"]),
                add={"returned_or_reserved_qty": member["qty"]},
                condition="#returned_or_reserved_qty <= :max",
                values={"max": int(member["line"]["purchased_qty"]) - member["qty"]},
                label=f"line:{member['entry']['line_id']}",
            )
        )
    if perk is not None:
        ops.append(
            benefits.ledger_update(
                customer_id, perk["period"], int(context["per_month"]), transaction_id=transaction_id, label="benefit"
            )
        )
    ops.append(store.TxUpdate(store.quote_key(quote_id), set={"status": "executed"}, label="quote"))
    memory_update_id: str | None = txn.get("memory_update_id")
    if memory_update_id:  # the write-back exists if and only if the return does
        update = memory_updates.update_from_txn_item(txn)
        ops.append(
            store.TxPut(
                memory_updates.outbox_item(update, customer_id), condition="attribute_not_exists(#pk)", label="outbox"
            )
        )
    if store_notice is not None:
        note_pk, note_sk = store.storenote_key(transaction_id)
        ops.append(
            store.TxPut(
                {
                    "pk": note_pk,
                    "sk": note_sk,
                    "transaction_id": transaction_id,
                    "option_type": kind,
                    "order_id": order_id,
                    **store_notice,
                    "status": "notified",
                    "created_at": created_at,
                },
                condition="attribute_not_exists(#pk)",
                label="store_notice",
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
        if "benefit" in failed:  # the perk was used by another shipment in the meantime: nothing was written
            raise _benefit_gone(
                "The free return shipping of this month was used since the quote. Find the options again.",
                "BENEFIT_USED",
            ) from None
        if any(label.startswith("line:") for label in failed):
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
        store_notice=store_notice,
        benefit=perk,
    )
    _after_commit(customer_id, run_id, result, txn, memory_update_id)
    return result


def _bundle_transaction_item(
    transaction_id: str,
    customer_id: str,
    quote_id: str,
    approval_id: str,
    run_id: str | None,
    order_id: str,
    option: Mapping[str, Any],
    members: list[Mapping[str, Any]],
    profile: Mapping[str, Any],
    perk: Mapping[str, Any] | None,
    created_at: str,
) -> dict[str, Any]:
    kind = option["type"]
    eta_text = clock.format_day(date.fromisoformat(option["eta_date"]))
    pk, sk = store.txn_key(transaction_id)
    lines = [{k: v for k, v in member["entry"].items() if k != "policy_check_id"} for member in members]
    item: dict[str, Any] = {
        "pk": pk,
        "sk": sk,
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        "quote_id": quote_id,
        "approval_id": approval_id,
        "run_id": run_id,
        "order_id": order_id,
        "line_id": lines[0]["line_id"],  # the first line; ``lines`` is the authoritative list
        "qty": int(option["qty"]),
        "sku": option["sku"],
        "option_id": option["option_id"],
        "option_type": kind,
        "location_id": option["location_id"],
        "location_name": option["location_name"],
        "replacement_size": None,
        "refund_cents": int(option["refund_cents"]),  # the net refund, as for a single line
        "refund_total_cents": int(option["refund_total_cents"]),
        "net_refund_cents": int(option["net_refund_cents"]),
        "fee_cents": int(option["fee_cents"]),
        "currency": option.get("currency", money.CURRENCY),
        "eta_date": option["eta_date"],
        "eta_text": eta_text,
        "next_step": _bundle_next_step(option, eta_text, perk),
        "state": "completed",
        "simulated": True,
        "created_at": created_at,
        "option": dict(option),
        "lines": lines,
        "tier_label": profile["tier_label"],
        # snapshot of the small write-back (one update for the whole shipment)
        "memory_items": [
            {
                "style_id": member["line"]["style"],
                "product_name": member["line"]["product_name"],
                "color": member["line"]["color"],
                "size": member["line"]["size"],
                "reason": memory_updates.reason_for(
                    member["check"].get("size_direction"),
                    member["line"]["size"],
                    member["line"]["size"],
                    member["check"].get("reason_category"),
                ),
            }
            for member in members
        ],
        "memory_update_id": store.derived_id("MU-", transaction_id, memory_updates.RETURN_OUTCOME, n=6),
    }
    if perk is not None:
        item["benefit"] = dict(perk)
    return item


def _bundle_next_step(option: Mapping[str, Any], eta_text: str, perk: Mapping[str, Any] | None) -> str:
    """Server-authored guidance for a bundle (the model repeats it; it never writes amounts)."""
    count = len(option["lines"])
    items = f"{count} item{'' if count == 1 else 's'}"
    refund = money.format_cad(int(option["net_refund_cents"]))
    where = option["location_name"]
    if option["type"] == "return_in_store":
        return f"Bring the {items}, unworn, to {where}. Your refund of {refund} is processed at drop-off."
    label = (
        f"no label fee: {perk['label']}"
        if perk
        else f"{money.format_cad(int(option['fee_cents']))} is deducted"
    )
    return (
        f"Mail the {items}, unworn, in one parcel with the return label ({label}). "
        f"Your refund of {refund} is expected by {eta_text} after the parcel is received."
    )


def _bundle_store_notice(profile: Mapping[str, Any], option: Mapping[str, Any]) -> dict[str, Any]:
    """What the (simulated) store desk is told about an in-store bundle return (public fields only)."""
    who = profile.get("display_name") or "the customer"
    minutes = option.get("minutes_away")
    distance = _distance_text(minutes)
    count = len(option["lines"])
    store_name = option["location_name"]
    summary = f"{store_name} is ready for your return of {count} item{'' if count == 1 else 's'}" + (
        f", {who} is {distance}" if distance else f" for {who}"
    )
    return {
        "store_id": option["location_id"],
        "store_name": store_name,
        "display_name": profile.get("display_name"),
        "tier_label": profile["tier_label"],
        "eta_minutes": minutes,
        "summary": summary if len(summary) <= MAX_NOTICE_CHARS else summary[: MAX_NOTICE_CHARS - 1] + "…",
    }


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
            check.get("size_direction"), line["size"], option["replacement_size"], check.get("reason_category")
        )
        item["memory_update_id"] = store.derived_id("MU-", transaction_id, "exchange_created", n=6)
        if option.get("replacement_color"):  # a colour exchange (the size stays)
            item["replacement_color"] = option["replacement_color"]
    return item


def _next_step(option: Mapping[str, Any], eta_text: str) -> str:
    """Server-authored guidance. The model repeats it; it never writes the dates or amounts."""
    kind, where = option["type"], option["location_name"]
    size = option.get("replacement_size")
    color = _color_text(option.get("replacement_color"))
    if kind == "exchange_store_pickup":
        if color:  # a colour exchange: the store speaks, and the pair is named by its colour
            return f"{where} will have your {color} pair, size {size}, ready from {eta_text}; bring the original pair, unworn."
        return f"Pick up size {size} at {where} from {eta_text}; bring the original pair, unworn."
    if _is_exchange(kind):
        if color:
            return (
                f"Your {color} pair, size {size}, ships from {where} and should arrive by {eta_text}; "
                "send back the original pair, unworn."
            )
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


def _color_text(color: object) -> str:
    """``BROWN`` -> ``Brown`` (empty for no colour)."""
    return color.strip().title() if isinstance(color, str) else ""


def _distance_text(minutes: object) -> str:
    """``5`` -> ``about 5 minutes away``; empty when the distance is not on file."""
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        return ""
    if minutes == 0:
        return "less than a minute away"
    return f"about {minutes} minute{'' if minutes == 1 else 's'} away"


def _store_notice(profile: Mapping[str, Any], option: Mapping[str, Any], line: Mapping[str, Any]) -> dict[str, Any]:
    """What the (simulated) store desk is told about an in-store exchange or return.

    The summary is written in the store's own voice ("ALDO Demo Store Montreal will have your
    Brown pair ready, John is about 5 minutes away"): the customer talks to ALDO and sees the
    store answer, so no other agent is ever named. Public fields only: the store, the
    customer's first name and member tier label, the minutes until they arrive (None when the
    distance is not on file) and the summary. There is no customer id in it, so it can be
    returned to the agent and mirrored to the trace.
    """
    who = profile.get("display_name") or "the customer"
    minutes = option.get("minutes_away")
    distance = _distance_text(minutes)
    store_name, product, old_size = option["location_name"], line["product_name"], line["size"]
    if option["type"] == "return_in_store":
        if distance:
            summary = f"{store_name} is ready for your return of {product} in size {old_size}, {who} is {distance}"
        else:
            summary = f"{store_name} is ready for {who}'s return of {product} in size {old_size}"
    else:
        new_color = _color_text(option.get("replacement_color"))
        what = f"{new_color} pair" if new_color else f"size {option.get('replacement_size')}"
        summary = f"{store_name} will have your {what} ready" + (f", {who} is {distance}" if distance else f" for {who}")
    return {
        "store_id": option["location_id"],
        "store_name": store_name,
        "display_name": profile.get("display_name"),
        "tier_label": profile["tier_label"],
        "eta_minutes": minutes,
        "summary": summary if len(summary) <= MAX_NOTICE_CHARS else summary[: MAX_NOTICE_CHARS - 1] + "…",
    }


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
    perk = txn.get("benefit")
    if perk:  # a bundle used the member's monthly perk in the same transaction; public fields only
        trace.emit_a2a_safe(
            customer_id,
            "benefit_applied",
            {
                "type": perk["type"],
                "benefit": perk["type"],
                "label": perk["label"],
                "tier_label": txn.get("tier_label"),
                "saved_cents": perk["saved_cents"],
                "fee_cents": txn["fee_cents"],
                "used": perk["used"],
                "per_month": perk["allowed"],
                "period": perk["period"],
                "order_id": txn["order_id"],
                "transaction_id": result.transaction_id,
            },
            actor="aldo_agent",
        )
    notice = txn.get("store_notice")
    if notice:  # the simulated store desk was told in the same transaction; show it on the timeline
        trace.emit_a2a_safe(
            customer_id,
            "store_notified",
            {
                "store_name": notice["store_name"],
                "eta_minutes": notice["eta_minutes"],
                "summary": notice["summary"],
            },
            actor="aldo_store",
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
    if txn.get("lines"):  # a bundle: the current reservation of EVERY line (the first is repeated above)
        current = {
            row["sk"][len("LINE#") :]: row
            for row in store.batch_get([store.line_key(txn["order_id"], e["line_id"]) for e in txn["lines"]])
        }
        reservation["lines"] = [
            {
                "line_id": e["line_id"],
                "returned_or_reserved_qty": current.get(e["line_id"], {}).get("returned_or_reserved_qty"),
                "purchased_qty": current.get(e["line_id"], {}).get("purchased_qty"),
            }
            for e in txn["lines"]
        ]
    fields = {
        k: v
        for k, v in txn.items()
        if k not in ("pk", "sk", "customer_id", "option", "approval_id", "run_id")
    }
    return TransactionView(**fields, reservation=reservation)
