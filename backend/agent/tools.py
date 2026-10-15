"""The ten tools of the ALDO returns agent (design 6.3, contracts B.3, PIVOT_ADDENDUM C6).

Rules that hold for every tool:

* The agent has EXACTLY these ten tools (``TOOL_NAMES``): no SQL, shell, file, browser or
  HTTP tool, no tool that creates an approval, and no pre-purchase tool (PIVOT_ADDENDUM C6).
* ``customer_id`` comes from ``RunContext`` (the trusted worker payload). No tool takes or
  returns it, and tool arguments can never override it.
* Prices, dates, stock and policy come from ``returns_domain`` (the database), never from
  tool arguments. Orders placed by the customer's own assistant (``DEMO-A2A-XXXXXX``,
  channel ``agent``) are ordinary orders: nothing here looks at the id format.
* Every tool returns the same JSON-safe envelope (``agent.envelope``) and never raises to
  the model. Domain errors become ``{"ok": false, "error": {...}}``.
* READ tools and ``execute_approved_resolution`` retry retryable failures twice (100 ms then
  300 ms). ``create_quote`` and ``escalate_case`` are never retried by the wrapper (G-17);
  their ids are deterministic, so a retry by the model is also safe.
* After a successful ``execute_approved_resolution`` the wrapper asks the gateway to deliver
  pending write-backs to the customer's personal agent. That call is best effort: it runs on
  its own thread, waits at most ``DELIVER_BUDGET_SECONDS`` and swallows every failure
  (PIVOT_ADDENDUM 2 C6 delta 4).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

from returns_domain import config, fit, taxonomy
from returns_domain.errors import DomainError, ErrorCode

from . import ranking
from .context import RunContext
from .envelope import emit_tool_call, emit_tool_result, fail, limit_envelope, ok
from .log import logger

TOOL_NAMES: tuple[str, ...] = (
    "list_my_orders",
    "get_order_context",
    "evaluate_return_policy",
    "get_fit_insight",
    "find_resolution_options",
    "create_quote",
    "execute_approved_resolution",
    "get_resolution_status",
    "escalate_case",
    "record_decision",
)

#: Same values as ``cases.CASE_REASONS`` (contracts A.0); checked against it in the tests.
CASE_REASONS: tuple[str, ...] = (
    "POLICY_REVIEW",
    "TOOL_FAILURE",
    "LIMIT_REACHED",
    "VERIFICATION_MISMATCH",
    "CUSTOMER_REQUEST",
)
PREFERRED_CHANNELS: tuple[str, ...] = ("ship", "store_pickup", "return")

# Literal types for the tool signatures. They must be module-level names: Strands resolves
# the (string) annotations with ``get_type_hints`` against this module's globals.
ReasonCategoryArg = Literal[taxonomy.REASON_CATEGORIES]  # type: ignore[valid-type]
ConditionArg = Literal[taxonomy.CONDITIONS]  # type: ignore[valid-type]
SizeDirectionArg = Literal[taxonomy.SIZE_DIRECTIONS]  # type: ignore[valid-type]
PreferredChannelArg = Literal[PREFERRED_CHANNELS]  # type: ignore[valid-type]
CaseReasonArg = Literal[CASE_REASONS]  # type: ignore[valid-type]

FIT_LABEL = "derived from provided data"
INTERNAL_MESSAGE = "Something went wrong while running this step. Please try again."
UPSTREAM_MESSAGE = "A backing service did not answer in time. Please try again."

#: Upper bound for the best-effort write-back delivery after an execution.
DELIVER_BUDGET_SECONDS = 3.0

_TRANSIENT_AWS_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "ThrottledException",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
        "InternalServerError",
        "ServiceUnavailable",
        "TransactionInProgressException",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)

_sleep = time.sleep  # tests replace this to avoid real back-off delays


@dataclass
class Out:
    """What a tool body returns to ``_run_tool``."""

    data: Any
    refs: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    provenance: str = "synthetic"


# ------------------------------------------------------------------------------ wrapper


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code")) in _TRANSIENT_AWS_CODES
    return isinstance(exc, BotoCoreError) and type(exc).__name__ in {
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "EndpointConnectionError",
        "ConnectionClosedError",
    }


def _retry_policy(retry: bool) -> tuple[int, tuple[int, ...]]:
    if not retry:
        return 0, ()
    settings = config.get_settings()
    return settings.tool_retries, settings.tool_retry_delays_ms


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _run_tool(
    ctx: RunContext,
    name: str,
    args: Mapping[str, Any],
    fn: Callable[[], Out],
    *,
    retry: bool,
    after_ok: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Common wrapper: trace events, round limit, retries, error envelope, JSON safety."""
    if ctx.pending_call is not None and ctx.pending_call.get("name") == name:
        ctx.pending_call["handled"] = True  # the hooks must not trace this call a second time
    ctx.tool_calls += 1
    emit_tool_call(ctx, name, args)
    started = time.monotonic()

    if ctx.is_over_limit(name):
        envelope = limit_envelope()
        emit_tool_result(ctx, name, envelope, _ms(started))
        return envelope

    try:
        max_retries, delays = _retry_policy(retry)
    except Exception:  # noqa: BLE001 - a broken setting must not hide the real tool error
        max_retries, delays = 0, ()

    attempt = 0
    out: Out | None = None
    while True:
        try:
            out = fn()
            envelope = ctx.services.store.json_safe(
                ok(out.data, source_refs=out.refs, provenance=out.provenance)
            )
            break
        except DomainError as exc:
            if exc.retryable and attempt < max_retries:
                _sleep(delays[min(attempt, len(delays) - 1)] / 1000)
                attempt += 1
                continue
            envelope = fail(exc.code, exc.message, retryable=exc.retryable)
            if exc.details:
                envelope["error"]["details"] = _safe_details(ctx, exc.details)
            break
        except Exception as exc:  # noqa: BLE001 - tools never raise to the model
            if _is_transient(exc):
                if attempt < max_retries:
                    _sleep(delays[min(attempt, len(delays) - 1)] / 1000)
                    attempt += 1
                    continue
                envelope = fail(ErrorCode.UPSTREAM_TIMEOUT, UPSTREAM_MESSAGE, retryable=True)
                break
            logger.exception("tool_failed", run_id=ctx.run_id, tool=name)
            envelope = fail(ErrorCode.INTERNAL_ERROR, INTERNAL_MESSAGE)
            break

    succeeded = bool(envelope.get("ok")) and out is not None
    emit_tool_result(
        ctx,
        name,
        envelope,
        _ms(started),
        out.evidence if succeeded and out is not None else {},
        out.provenance if succeeded and out is not None else "synthetic",
    )
    if succeeded and after_ok is not None:
        try:
            after_ok()
        except Exception:  # noqa: BLE001 - best effort by definition
            logger.warning("after_ok_failed", run_id=ctx.run_id, tool=name)
    return envelope


def _safe_details(ctx: RunContext, details: Mapping[str, Any]) -> Any:
    try:
        return ctx.services.store.json_safe(dict(details))
    except Exception:  # noqa: BLE001
        return {}


def deliver_pending_best_effort(ctx: RunContext, budget_seconds: float | None = None) -> bool:
    """Ask the gateway to push pending write-backs for this customer. Never raises, never blocks long.

    ``returns_domain.gateway`` is written by another stream and may not exist yet, so it is
    imported lazily inside the worker thread (``ImportError`` is swallowed like any failure).
    Returns True if the call finished within the budget (used by the tests).
    """
    budget = DELIVER_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    finished = threading.Event()

    def work() -> None:
        try:
            gateway = ctx.services.gateway
            # PIVOT_ADDENDUM 9.3 names it deliver_pending; the module on disk may call it
            # flush_outbox (same arguments, same job). The code on disk wins.
            deliver = getattr(gateway, "deliver_pending", None) or gateway.flush_outbox
            deliver(ctx.customer_id)
        except Exception as exc:  # noqa: BLE001 - includes ImportError and delivery failures
            logger.warning(
                "deliver_pending_skipped", run_id=ctx.run_id, error_type=type(exc).__name__
            )
        finally:
            finished.set()

    threading.Thread(target=work, name="deliver-pending", daemon=True).start()
    return finished.wait(budget)


# ----------------------------------------------------------------------------- helpers


def _js(ctx: RunContext, value: Any) -> Any:
    return ctx.services.store.json_safe(value)


def _with_money(item: Mapping[str, Any], pairs: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    enriched = dict(item)
    for cents_key, text_key in pairs:
        text = ranking.money_text(item.get(cents_key))
        if text is not None:
            enriched[text_key] = text
    return enriched


def _order_summary(order: Mapping[str, Any]) -> dict[str, Any]:
    lines = [
        {
            "line_id": line.get("line_id"),
            "product_name": line.get("product_name"),
            "size": line.get("size"),
            "qty": line.get("purchased_qty", line.get("qty")),
            "final_sale": line.get("final_sale"),
        }
        for line in order.get("lines", [])
    ]
    return {
        "order_id": order.get("order_id"),
        "ordered_at": order.get("ordered_at"),
        "delivered_at": order.get("delivered_at"),
        "channel": order.get("channel"),
        "lines": lines,
    }


def _fit_evidence_text(insight: Mapping[str, Any]) -> str:
    """One sentence with the numbers the agent may quote (percentages come from the counts)."""
    n_fit = int(insight.get("n_fit") or 0)
    scope = "ALDO_CA" if insight.get("scope") == fit.SCOPE_ALDO_CA else "all banners"
    signal = insight.get("signal")
    if n_fit <= 0 or signal == fit.INSUFFICIENT_DATA:
        return (
            f"Not enough fit-related returns for a signal (n={n_fit}, minimum {fit.MIN_N}, {scope}); "
            f"{FIT_LABEL}."
        )
    small = round(100 * int(insight.get("too_small") or 0) / n_fit, 1)
    big = round(100 * int(insight.get("too_big") or 0) / n_fit, 1)
    if signal == fit.RUNS_SMALL:
        lead = f"{small}% of fit-related returns of this style were 'too small'"
    elif signal == fit.RUNS_LARGE:
        lead = f"{big}% of fit-related returns of this style were 'too big'"
    else:
        lead = f"Fit-related returns are mixed ({small}% 'too small', {big}% 'too big')"
    return f"{lead} (n={n_fit}, {scope}); {FIT_LABEL}."


def _unique(refs: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for ref in refs:
        if isinstance(ref, str) and ref:
            seen.setdefault(ref, None)
    return list(seen)


# -------------------------------------------------------------------------------- tools


def build_tools(ctx: RunContext) -> list[Callable[..., Any]]:
    """The ten tools bound to ``ctx``, in the order of ``TOOL_NAMES``."""

    @tool
    def list_my_orders() -> dict:
        """List the signed-in customer's orders, newest first. This includes website orders and orders placed by the customer's own shopping assistant (channel "agent").

        Use it only when no order is selected and you must ask which order the customer means. It returns order ids, dates, channel and the lines (line id, product, size, quantity, final-sale flag).
        """

        def call() -> Out:
            orders = _js(ctx, ctx.services.orders.list_orders(ctx.customer_id))
            summaries = [_order_summary(order) for order in orders]
            return Out(
                {"orders": summaries},
                refs=[f"ORDER#{o['order_id']}/META" for o in summaries if o.get("order_id")],
                evidence={"order_count": len(summaries)},
            )

        return _run_tool(ctx, "list_my_orders", {}, call, retry=True)

    @tool
    def get_order_context(order_id: str) -> dict:
        """Get the lines, quantities, price paid, delivery date, final-sale flag and already-returned quantity of one of the customer's orders.

        Args:
            order_id: The order id exactly as shown by list_my_orders or in [selected_order_id: ...], for example DEMO-1001 or DEMO-A2A-81B20E.
        """

        def call() -> Out:
            data = _js(ctx, ctx.services.orders.order_context(ctx.customer_id, order_id, run_id=ctx.run_id))
            lines = [
                _with_money(line, (("unit_paid_cents", "unit_paid_text"),))
                for line in data.get("lines", [])
            ]
            data = {**data, "lines": lines}
            first = lines[0] if lines else {}
            return Out(
                data,
                refs=list(data.get("source_refs") or [f"ORDER#{order_id}/META"]),
                evidence={
                    "delivered_at": data.get("delivered_at"),
                    "final_sale": first.get("final_sale"),
                    "remaining_qty": first.get("remaining_qty"),
                },
            )

        return _run_tool(ctx, "get_order_context", {"order_id": order_id}, call, retry=True)

    @tool
    def evaluate_return_policy(
        order_id: str,
        line_id: str,
        qty: int,
        reason_category: ReasonCategoryArg,
        condition: ConditionArg,
        size_direction: SizeDirectionArg = "unknown",
    ) -> dict:
        """Run the deterministic return policy for one order line. Price, dates and the final-sale flag come from the database and nothing the customer writes can change the result.

        Returns policy_check_id, decision (eligible, ineligible, needs_information or needs_review), rule_ids, missing_fields, allowed_option_types, policy_version and the gross refund. Required before find_resolution_options. Call it again with new facts if the customer corrects them.

        Args:
            order_id: The order id exactly as the tools return it.
            line_id: The line id from get_order_context.
            qty: Number of pairs to return or exchange, at least 1.
            reason_category: FIT_SIZE (wrong size, tight, loose), COMFORT, QUALITY_DAMAGE (defective or damaged), EXPECTATION (not as described), CHANGE_OF_MIND, DELIVERY, OTHER or UNKNOWN.
            condition: unworn, worn, damaged (arrived or became defective) or unknown when the customer has not said.
            size_direction: For FIT_SIZE only. too_small (too small or tight, so the replacement is a larger size), too_big (too big or loose, so the replacement is a smaller size) or unknown.
        """
        args = {
            "order_id": order_id,
            "line_id": line_id,
            "qty": qty,
            "reason_category": reason_category,
            "condition": condition,
            "size_direction": size_direction,
        }

        def call() -> Out:
            check = _js(
                ctx,
                ctx.services.orders.check_policy(
                    ctx.customer_id,
                    ctx.run_id,
                    order_id,
                    line_id,
                    qty,
                    reason_category,
                    condition,
                    size_direction,
                ),
            )
            result = check.get("result") or {}
            data = _with_money(
                {
                    "policy_check_id": check.get("policy_check_id"),
                    "decision": result.get("decision"),
                    "rule_ids": result.get("rule_ids", []),
                    "missing_fields": result.get("missing_fields", []),
                    "allowed_option_types": result.get("allowed_option_types", []),
                    "policy_version": check.get("policy_version") or result.get("policy_version"),
                    "refund_cents": check.get("refund_cents"),
                    "currency": check.get("currency", "CAD"),
                    "facts": result.get("facts", {}),
                },
                (("refund_cents", "refund_text"),),
            )
            return Out(
                data,
                refs=[f"PCHK#{data['policy_check_id']}/META", f"ORDER#{order_id}/LINE#{line_id}"],
                evidence={
                    "decision": data["decision"],
                    "rule_ids": data["rule_ids"],
                    "policy_version": data["policy_version"],
                    "policy_check_id": data["policy_check_id"],
                },
            )

        return _run_tool(ctx, "evaluate_return_policy", args, call, retry=True)

    @tool
    def get_fit_insight(product_name: str) -> dict:
        """Look up the fit signal of a product from real return data: the share of fit-related returns that said "too small" versus "too big". Use it for size problems, after the policy says eligible.

        Returns the signal (runs_small, runs_large, mixed or insufficient_data), the counts, the sample size n_fit and evidence_text, a ready sentence with the numbers you may quote. The data is derived from provided data and is advice only: the customer may choose any size that is in stock.

        Args:
            product_name: The product name exactly as shown on the order line.
        """

        def call() -> Out:
            insight = _js(ctx, fit.get_fit_insight(product_name))
            data = {**insight, "label": FIT_LABEL, "evidence_text": _fit_evidence_text(insight)}
            source = insight.get("source") or {}
            sha = str(source.get("sha256") or "")[:12]
            return Out(
                data,
                refs=[f"fit_insights.json@{sha}" if sha else "fit_insights.json"],
                evidence={
                    "signal": insight.get("signal"),
                    "too_small_share": insight.get("too_small_share"),
                    "n_fit": insight.get("n_fit"),
                    "scope": insight.get("scope"),
                },
                provenance=fit.PROVENANCE,
            )

        return _run_tool(ctx, "get_fit_insight", {"product_name": product_name}, call, retry=True)

    @tool
    def find_resolution_options(
        policy_check_id: str,
        replacement_size: str | None = None,
        deadline_date: str | None = None,
        preferred_channel: PreferredChannelArg | None = None,
    ) -> dict:
        """Find the eligible resolution options for an ELIGIBLE policy check: store-pickup, ship-from-store and ship-from-warehouse exchanges (only when stock exists) plus in-store and mail-in returns, each with fee, refund, location, ETA and conditions. Also returns ranking (order and reason) that follows the ranking rule, and other_sizes_in_stock as information only.

        Args:
            policy_check_id: The policy_check_id returned by evaluate_return_policy.
            replacement_size: A STRING with one decimal such as "8.5". Leave it out to use the server default (current size plus one half for too_small, minus one half for too_big). Pass it only when the customer asks for a specific different size.
            deadline_date: The customer's deadline as YYYY-MM-DD, if they gave one. Options then show meets_deadline.
            preferred_channel: ship, store_pickup or return, only if the customer stated a preference.
        """
        args = {
            "policy_check_id": policy_check_id,
            "replacement_size": replacement_size,
            "deadline_date": deadline_date,
            "preferred_channel": preferred_channel,
        }

        def call() -> Out:
            found = _js(
                ctx,
                ctx.services.options.find_options(
                    ctx.customer_id,
                    policy_check_id,
                    replacement_size,
                    deadline_date,
                    preferred_channel,
                    run_id=ctx.run_id,
                ),
            )
            offered = [
                ranking.add_money_text(option) for option in found.get("options", [])
            ]
            deadline = found.get("deadline_date") or deadline_date
            summary = ranking.ranking_summary(offered, deadline)
            data = {**found, "options": offered, "ranking": summary}
            candidate_set_id = found.get("candidate_set_id")
            if candidate_set_id:
                ctx.rank_orders[str(candidate_set_id)] = summary["order"]
            availability = found.get("availability") or {}
            refs = _unique(
                [ref for option in offered for ref in option.get("source_refs", [])]
            ) or [f"CAND#{candidate_set_id}/META"]
            return Out(
                data,
                refs=refs,
                evidence={
                    "candidate_set_id": candidate_set_id,
                    "option_types": [option.get("type") for option in offered],
                    "dc_atp": availability.get("dc_atp"),
                    "replacement_size": availability.get("replacement_size"),
                },
            )

        return _run_tool(ctx, "find_resolution_options", args, call, retry=True)

    @tool
    def create_quote(candidate_set_id: str, option_ids: list[str]) -> dict:
        """Create the quote the customer will review and approve. The server recomputes every amount from the database; you only choose which options to present. Present the options best first (follow ranking.order), include at least one return option whenever returns are allowed, and never hide an eligible refund option.

        Returns quote_id, expires_at (about 15 minutes) and the option ids in the order shown. After this, tell the customer to review the option cards and press Approve, then stop.

        Args:
            candidate_set_id: The candidate_set_id returned by find_resolution_options.
            option_ids: The option ids to present, for example ["OPT-3", "OPT-1", "OPT-4"].
        """
        args = {"candidate_set_id": candidate_set_id, "option_ids": list(option_ids)}

        def call() -> Out:
            ordered = ranking.reorder_ids(option_ids, ctx.rank_orders.get(candidate_set_id, []))
            quote = _js(
                ctx,
                ctx.services.quotes.create_quote(
                    ctx.customer_id, ctx.run_id, ctx.conv_id, candidate_set_id, ordered
                ),
            )
            data = {
                "quote_id": quote.get("quote_id"),
                "expires_at": quote.get("expires_at"),
                "option_ids": ordered,
                "status": quote.get("status", "open"),
            }
            return Out(
                data,
                refs=[f"QUOTE#{data['quote_id']}/META"],
                evidence={"quote_id": data["quote_id"]},
            )

        return _run_tool(ctx, "create_quote", args, call, retry=False)

    @tool
    def execute_approved_resolution(quote_id: str, option_id: str | None = None) -> dict:
        """Execute the option the customer approved. Use it ONLY after a message that starts with [server] says the customer approved an option on a quote. The server checks the stored approval; the executed option always comes from that approval, and a different option_id is refused with APPROVAL_REQUIRED. Calling it again returns the same transaction.

        Args:
            quote_id: The quote id from the [server] message.
            option_id: The approved option id from the [server] message.
        """
        # In an approval run the trusted payload already names the approved quote and option;
        # use those so a mistyped id can never derail the one step that matters.
        use_quote, use_option = quote_id, option_id
        if ctx.kind == "approval" and ctx.quote_id:
            use_quote, use_option = ctx.quote_id, ctx.option_id or option_id
        args = {"quote_id": use_quote, "option_id": use_option}

        def call() -> Out:
            result = _js(
                ctx,
                ctx.services.execution.execute(
                    ctx.customer_id, use_quote, use_option, run_id=ctx.run_id
                ),
            )
            return Out(
                result,
                refs=[f"TXN#{result.get('transaction_id')}/META", f"QUOTE#{use_quote}/META"],
                evidence={
                    "transaction_id": result.get("transaction_id"),
                    "replayed": result.get("replayed"),
                },
            )

        return _run_tool(
            ctx,
            "execute_approved_resolution",
            args,
            call,
            retry=True,
            after_ok=lambda: deliver_pending_best_effort(ctx),
        )

    @tool
    def get_resolution_status(transaction_id: str) -> dict:
        """Read the stored record of a transaction: state, option type, location, replacement size, refund, fee, ETA and next step. Always call it after execute_approved_resolution and report only what this record says.

        Args:
            transaction_id: The transaction_id returned by execute_approved_resolution.
        """

        def call() -> Out:
            view = _js(
                ctx,
                ctx.services.execution.get_status(ctx.customer_id, transaction_id, run_id=ctx.run_id),
            )
            data = _with_money(view, (("refund_cents", "refund_text"), ("fee_cents", "fee_text")))
            return Out(
                data,
                refs=[f"TXN#{transaction_id}/META"],
                evidence={"state": data.get("state"), "location_id": data.get("location_id")},
            )

        return _run_tool(
            ctx, "get_resolution_status", {"transaction_id": transaction_id}, call, retry=True
        )

    @tool
    def escalate_case(
        reason_code: CaseReasonArg,
        summary: str,
        order_id: str | None = None,
        line_id: str | None = None,
    ) -> dict:
        """Hand the case to a person on the ALDO team. Use it when the policy says needs_review, when a tool keeps failing, when the round limit is reached (once), when the status read disagrees with the execution, or when the customer asks for a person. Tell the customer a person will review it and give the case id. Never promise an outcome.

        Args:
            reason_code: POLICY_REVIEW, TOOL_FAILURE, LIMIT_REACHED, VERIFICATION_MISMATCH or CUSTOMER_REQUEST.
            summary: Facts only, at most 800 characters: order, line, rule ids and what the customer asked.
            order_id: The order id, if there is one.
            line_id: The line id, if there is one.
        """
        args = {
            "reason_code": reason_code,
            "summary": summary,
            "order_id": order_id,
            "line_id": line_id,
        }

        def call() -> Out:
            case = _js(
                ctx,
                ctx.services.cases.escalate(
                    ctx.customer_id, ctx.run_id, reason_code, summary, order_id, line_id
                ),
            )
            ctx.escalated = True
            ctx.case_id = case.get("case_id")
            return Out(
                {"case_id": case.get("case_id"), "status": case.get("status", "open")},
                refs=[f"CASE#{case.get('case_id')}/META"],
                evidence={"case_id": case.get("case_id"), "reason_code": reason_code},
            )

        return _run_tool(ctx, "escalate_case", args, call, retry=False)

    @tool
    def record_decision(summary: str) -> dict:
        """Record one short public sentence that explains your choice, for example why an option is ranked first. It is shown in the customer-visible trace. Do not put hidden reasoning, ids or personal data in it. Call it once, right before create_quote.

        Args:
            summary: At most 240 characters. Longer text is cut.
        """
        try:
            limit = config.get_settings().max_decision_chars
        except DomainError:
            limit = 240
        text = " ".join(str(summary).split())[:limit]

        def call() -> Out:
            if not text:
                raise DomainError(ErrorCode.VALIDATION_ERROR, "summary must not be empty")
            ctx.services.trace.emit(ctx.run_id, "decision", {"summary": text})
            return Out({"recorded": True}, evidence={"chars": len(text)})

        return _run_tool(ctx, "record_decision", {"summary": text}, call, retry=False)

    return [
        list_my_orders,
        get_order_context,
        evaluate_return_policy,
        get_fit_insight,
        find_resolution_options,
        create_quote,
        execute_approved_resolution,
        get_resolution_status,
        escalate_case,
        record_decision,
    ]
