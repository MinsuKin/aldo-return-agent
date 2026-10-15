"""Immutable quotes, payload hashes and the one-time approval (requirement 7, design 5.4).

A quote copies SERVER-computed options from a candidate set (the model only picks option ids and
their order), binds each option to a ``payload_hash`` and expires after 15 minutes of real time.
An approval is created only by the authenticated approve endpoint (``approve``); no agent tool
can create one. ``approve`` is idempotent per quote: the same option returns the same approval
and run id, another option is an ``IDEMPOTENCY_CONFLICT``.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, model_serializer

from . import audit, benefits, clock, config, orders, policy, store, trace
from .errors import DomainError, ErrorCode
from .options import Option


class QuoteOption(Option):
    payload_hash: str


class Approval(BaseModel):
    model_config = ConfigDict(extra="ignore")

    approval_id: str
    quote_id: str
    option_id: str
    payload_hash: str
    run_id: str
    approved_at: str
    consumed_txn_id: str | None = None


class Quote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    quote_id: str
    order_id: str
    line_id: str
    conv_id: str
    run_id: str
    policy_check_id: str
    candidate_set_id: str
    policy_version: str
    currency: str = "CAD"
    created_at: str
    expires_at: str
    status: Literal["open", "executed", "superseded", "expired"]
    options: list[QuoteOption]
    approval: Approval | None = None
    inventory_versions: dict[str, int]
    #: Bundle returns only (left out of the JSON otherwise): every policy check id and the lines
    #: (the same list each option carries). ``line_id`` / ``policy_check_id`` are the first of them.
    policy_check_ids: list[str] | None = None
    lines: list[dict[str, Any]] | None = None

    @model_serializer(mode="wrap")
    def _omit_bundle_fields_when_none(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        for name in ("policy_check_ids", "lines"):
            if data.get(name) is None:
                data.pop(name, None)
        return data


def compute_payload_hash(quote_id: str, option: Mapping[str, Any] | BaseModel, policy_version: str) -> str:
    """SHA-256 hex of the canonical JSON of the fields a customer approves.

    Computed from the stored (``from_ddb``) form, so recomputation at execute time is byte-stable.
    """
    body = store.json_safe(option)
    fields: dict[str, Any] = {
        "quote_id": quote_id,
        "option_id": body["option_id"],
        "type": body["type"],
        "sku": body["sku"],
        "location": body["location_id"],
        "qty": body["qty"],
        "refund_cents": body["refund_cents"],
        "fee_cents": body["fee_cents"],
        "currency": body["currency"],
        "policy_version": policy_version,
    }
    if body.get("lines"):  # a bundle return: the approved lines and the perk are part of what is signed
        fields["lines"] = [
            {key: entry[key] for key in ("line_id", "sku", "qty", "refund_cents")} for entry in body["lines"]
        ]
        fields["refund_total_cents"] = body.get("refund_total_cents")
        if body.get("benefit"):
            fields["benefit"] = {
                key: body["benefit"].get(key) for key in ("type", "saved_cents", "used", "allowed", "period")
            }
    return hashlib.sha256(store.canonical_json(fields).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------- create


def create_quote(
    customer_id: str,
    run_id: str,
    conv_id: str,
    candidate_set_id: str,
    option_ids: list[str],
) -> Quote:
    """Turn chosen options of a candidate set into an open quote (retry-safe, same id)."""
    chosen_ids = _clean_option_ids(option_ids)
    cand = store.get_item(store.cand_key(candidate_set_id))
    if cand is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Candidate set not found.")
    if cand.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "candidate_set", candidate_set_id, run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Candidate set not found for this customer.")

    by_id = {o["option_id"]: o for o in cand["options"]}
    missing = [oid for oid in chosen_ids if oid not in by_id]
    if missing:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"option_ids not in this candidate set: {', '.join(missing)}",
        )
    selected = [by_id[oid] for oid in chosen_ids]  # the agent's order is the ranking the UI shows
    held = [o for o in selected if o.get("decision") == "needs_review"]
    if held:  # a bundle above the member's authority limit: visible as an option, never grantable by the agent
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "This return needs a person's review (the total is above the authority limit); "
            "open a case instead of a quote.",
            details={"decision": "needs_review", "rule_ids": list(held[0].get("rule_ids") or [])},
        )
    returns_offered = any(o["type"] in policy.RETURN_OPTION_TYPES for o in cand["options"])
    if returns_offered and not any(o["type"] in policy.RETURN_OPTION_TYPES for o in selected):
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "include at least one return option so the customer can compare it with the exchange",
        )

    conv = store.get_item(store.conv_key(conv_id))
    if conv is not None and conv.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "conversation", conv_id, run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Conversation not found for this customer.")

    versions = _used_inventory_versions(cand, selected)
    _require_unchanged_stock(selected, versions)
    _require_benefit_still_available(customer_id, selected)

    check = orders.load_policy_check_item(customer_id, cand["policy_check_id"])
    policy_version = check["policy_version"]
    quote_id = store.derived_id("QUO-", run_id, candidate_set_id, ",".join(sorted(chosen_ids)))
    options = [{**o, "payload_hash": compute_payload_hash(quote_id, o, policy_version)} for o in selected]
    now = clock.now_utc()
    item = {
        "quote_id": quote_id,
        "customer_id": customer_id,
        "order_id": cand["order_id"],
        "line_id": cand["line_id"],
        "conv_id": conv_id,
        "run_id": run_id,
        "policy_check_id": cand["policy_check_id"],
        "candidate_set_id": candidate_set_id,
        "policy_version": policy_version,
        "currency": "CAD",
        "created_at": clock.iso(now),
        "expires_at": clock.iso(now + timedelta(seconds=config.get_settings().quote_ttl_seconds)),
        "status": "open",
        "options": options,
        "inventory_versions": versions,
    }
    if cand.get("policy_check_ids"):  # a bundle return: carry the lines (and, per option, the perk note)
        item["policy_check_ids"] = list(cand["policy_check_ids"])
        item["lines"] = next((list(o["lines"]) for o in options if o.get("lines")), [])
    pk, sk = store.quote_key(quote_id)
    created = True
    try:
        store.put_item({"pk": pk, "sk": sk, **item}, condition="attribute_not_exists(#pk)")
    except store.ConditionFailed:
        created = False
        existing = store.get_item(store.quote_key(quote_id))
        if existing is None or existing.get("customer_id") != customer_id:
            raise DomainError(ErrorCode.NOT_AUTHORIZED, "Quote not found for this customer.") from None
        item = existing

    _supersede_previous_quote(conv_id, quote_id)
    if created:  # a retry that found the existing quote writes and emits nothing
        trace.emit_safe(
            run_id,
            "quote_created",
            {"quote_id": quote_id, "option_ids": chosen_ids, "expires_at": item["expires_at"]},
        )
        trace.emit_a2a_safe(  # the middle timeline: what kind of resolution leads, no amounts or ids of people
            customer_id,
            "quote_created",
            {
                "quote_id": quote_id,
                "primary_option_type": options[0]["type"],
                "option_types": [option["type"] for option in options],
            },
            actor="aldo_agent",
        )
    return _quote_view(item, None)


def _clean_option_ids(option_ids: Any) -> list[str]:
    if not isinstance(option_ids, (list, tuple)) or not option_ids:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "option_ids must list at least one option")
    cleaned: list[str] = []
    for option_id in option_ids:
        if not isinstance(option_id, str) or not option_id.strip():
            raise DomainError(ErrorCode.VALIDATION_ERROR, "option_ids must be strings such as OPT-1")
        if option_id.strip() not in cleaned:
            cleaned.append(option_id.strip())
    return cleaned


def _used_inventory_versions(cand: Mapping[str, Any], selected: list[Mapping[str, Any]]) -> dict[str, int]:
    """Versions recorded for the locations the SELECTED options use (G-26)."""
    versions: dict[str, int] = {}
    for option in selected:
        if option["type"] in policy.EXCHANGE_OPTION_TYPES:
            key = f"{option['sku']}|{option['location_id']}"
            versions[key] = int(cand["inventory_versions"][key])
    return versions


def _require_unchanged_stock(selected: list[Mapping[str, Any]], versions: Mapping[str, int]) -> None:
    exchanges = [o for o in selected if o["type"] in policy.EXCHANGE_OPTION_TYPES]
    if not exchanges:
        return
    rows = {
        (row["sku"], row["location_id"]): row
        for row in store.batch_get([store.inventory_key(o["sku"], o["location_id"]) for o in exchanges])
    }
    for option in exchanges:
        row = rows.get((option["sku"], option["location_id"]))
        recorded = versions[f"{option['sku']}|{option['location_id']}"]
        if row is None or int(row.get("version", 0)) != recorded or int(row.get("atp", 0)) < int(option["qty"]):
            raise DomainError(
                ErrorCode.STOCK_CHANGED,
                "Stock changed since the options were found. Find the options again.",
            )


def _require_benefit_still_available(customer_id: str, selected: list[Mapping[str, Any]]) -> None:
    """A selected option that carries the member perk needs room left in that month's ledger."""
    for option in selected:
        perk = option.get("benefit")
        if not perk:
            continue
        usage = benefits.ledger_usage(customer_id)  # this month on the DEMO clock
        if usage["period"] != perk.get("period") or usage["used"] >= usage["allowed"]:
            raise DomainError(
                ErrorCode.STOCK_CHANGED,
                "The free return shipping of this month was used since the options were found. Find the options again.",
                details={"reason": "BENEFIT_USED"},
            )


def _supersede_previous_quote(conv_id: str, quote_id: str) -> None:
    """A newer quote retires the conversation's previous open one (best effort, idempotent).

    A conversation that does not exist (a direct domain call without one) has nothing to update.
    """
    conv = store.get_item(store.conv_key(conv_id))
    if conv is None:
        return
    previous = conv.get("latest_quote_id")
    if previous and previous != quote_id:
        try:
            store.update_item(
                store.quote_key(previous),
                set={"status": "superseded"},
                condition="#status = :open",
                values={"open": "open"},
                return_new=False,
            )
        except store.ConditionFailed:
            pass  # already executed, superseded or gone
    if previous != quote_id:
        store.update_item(store.conv_key(conv_id), set={"latest_quote_id": quote_id}, return_new=False)


# ------------------------------------------------------------------------------- read


def _quote_view(item: Mapping[str, Any], approval: Mapping[str, Any] | None) -> Quote:
    status = item["status"]
    if status == "open" and clock.parse_iso(item["expires_at"]) <= clock.now_utc():
        status = "expired"
    fields = {k: v for k, v in item.items() if k not in ("pk", "sk", "customer_id", "status")}
    return Quote(
        **fields,
        status=status,
        approval=Approval(**{k: v for k, v in approval.items() if k not in ("pk", "sk")}) if approval else None,
    )


def get_quote(customer_id: str, quote_id: str, *, run_id: str | None = None) -> Quote:
    """The customer's quote with its approval (if any). ``status`` is ``expired`` once past ``expires_at``."""
    item = store.get_item(store.quote_key(quote_id))
    if item is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Quote not found.")
    if item.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "quote", quote_id, run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Quote not found for this customer.")
    return _quote_view(item, store.get_item(store.approval_key(quote_id)))


# ------------------------------------------------------------------------------ approve


def approve(
    customer_id: str,
    quote_id: str,
    option_id: str,
    payload_hash: str,
    run_id: str,
    before_create: Callable[[], None] | None = None,
) -> tuple[Approval, bool]:
    """Record the customer's approval of one option. Returns ``(approval, created)``.

    Check order (G-06): ownership; the option exists and the hash matches it; an existing
    approval (same option: replay with the same run id, other option: IDEMPOTENCY_CONFLICT);
    the quote is open and unexpired; ``before_create`` (the API's active-run guard, no side
    effects when it raises); the conditional put.
    """
    quote = store.get_item(store.quote_key(quote_id))
    if quote is None:
        raise DomainError(ErrorCode.NOT_FOUND, "Quote not found.")
    if quote.get("customer_id") != customer_id:
        audit.access_denied(customer_id, "quote", quote_id, run_id)
        raise DomainError(ErrorCode.NOT_AUTHORIZED, "Quote not found for this customer.")

    option = next((o for o in quote["options"] if o["option_id"] == option_id), None)
    if option is None or not isinstance(payload_hash, str) or option["payload_hash"] != payload_hash:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "The option or its payload hash does not match the quote.")

    existing = store.get_item(store.approval_key(quote_id))
    if existing is not None:
        return _existing_approval(existing, option_id), False

    if quote["status"] != "open" or clock.parse_iso(quote["expires_at"]) <= clock.now_utc():
        raise DomainError(ErrorCode.QUOTE_EXPIRED, "This quote has expired or was replaced. Ask for a new one.")

    if before_create is not None:
        before_create()

    pk, sk = store.approval_key(quote_id)
    item = {
        "pk": pk,
        "sk": sk,
        "approval_id": store.new_id("APR-"),
        "quote_id": quote_id,
        "customer_id": customer_id,
        "option_id": option_id,
        "payload_hash": payload_hash,
        "run_id": run_id,
        "approved_at": store.now_iso(),
    }
    try:
        store.put_item(item, condition="attribute_not_exists(#pk)")
    except store.ConditionFailed:
        winner = store.get_item(store.approval_key(quote_id))
        if winner is None:  # cannot happen: the condition failed because it exists
            raise DomainError(ErrorCode.UPSTREAM_TIMEOUT, "Please try again.", retryable=True) from None
        return _existing_approval(winner, option_id), False
    audit.write_audit(customer_id, "approve", "allowed", {"quote_id": quote_id, "option_id": option_id})
    return _approval_view(item), True


def _existing_approval(item: Mapping[str, Any], option_id: str) -> Approval:
    if item["option_id"] != option_id:
        raise DomainError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "A different option was already approved on this quote.",
        )
    return _approval_view(item)


def _approval_view(item: Mapping[str, Any]) -> Approval:
    return Approval(**{k: v for k, v in item.items() if k not in ("pk", "sk", "customer_id")})
