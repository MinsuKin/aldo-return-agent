"""Contract tests for the ten tools: names, schemas, envelope, events, retries, gateway hook.

Run with fakes only (see fakes.py); the policy decision inside the fake comes from the real
``returns_domain.policy``, and ``store.json_safe`` is the real one.
"""

from __future__ import annotations

import importlib
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from agent import services as services_module
from agent import tools as agent_tools
from agent.context import RunContext
from returns_domain import config, taxonomy
from returns_domain.errors import DomainError, ErrorCode

from .fakes import AGENT_ORDER, CUSTOMER, OTHER_ORDER, SEEDED_ORDER, FakeDomain

HERO_LINE = f"{AGENT_ORDER}-L1"


def has_decimal(value: Any) -> bool:
    if isinstance(value, Decimal):
        return True
    if isinstance(value, dict):
        return any(has_decimal(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_decimal(v) for v in value)
    return False


def evaluate(tools: dict[str, Any], **overrides: Any) -> dict:
    args = {
        "order_id": AGENT_ORDER,
        "line_id": HERO_LINE,
        "qty": 1,
        "reason_category": "FIT_SIZE",
        "condition": "unworn",
        "size_direction": "too_small",
    }
    args.update(overrides)
    return tools["evaluate_return_policy"](**args)


# ------------------------------------------------------------------------ the tool set


def test_exactly_ten_tools_in_contract_order(tools_by_name: dict[str, Any]) -> None:
    assert tuple(tools_by_name) == agent_tools.TOOL_NAMES
    assert len(tools_by_name) == 10
    assert set(tools_by_name) == {
        "list_my_orders", "get_order_context", "evaluate_return_policy", "get_fit_insight",
        "find_resolution_options", "create_quote", "execute_approved_resolution",
        "get_resolution_status", "escalate_case", "record_decision",
    }


def test_no_pre_purchase_or_approval_tool(tools_by_name: dict[str, Any]) -> None:
    """PIVOT_ADDENDUM C6: the ALDO agent keeps exactly its ten tools; none creates an approval."""
    forbidden = (
        "size_assist", "ask_aldo_size_assist", "size_decision", "send_size_decision", "recall_size_memory",
        "memory", "gateway", "purchase", "place_order", "create_approval", "approve", "approve_quote",
        "approve_resolution", "grant_approval",
    )
    for name in tools_by_name:
        assert name not in forbidden, name
        assert not name.startswith(("approve", "create_approval", "grant_")), name
    # The only tool that acts on an approval executes it; the approval row is written by the API.
    assert [n for n in tools_by_name if "approv" in n] == ["execute_approved_resolution"]


def test_no_tool_takes_a_customer_id(tools_by_name: dict[str, Any]) -> None:
    for name, tool in tools_by_name.items():
        properties = tool.tool_spec["inputSchema"]["json"].get("properties", {})
        assert not any("customer" in key for key in properties), name


def test_schema_enums_come_from_the_taxonomy(tools_by_name: dict[str, Any]) -> None:
    props = tools_by_name["evaluate_return_policy"].tool_spec["inputSchema"]["json"]["properties"]
    assert props["reason_category"]["enum"] == list(taxonomy.REASON_CATEGORIES)
    assert props["condition"]["enum"] == list(taxonomy.CONDITIONS)
    assert props["size_direction"]["enum"] == list(taxonomy.SIZE_DIRECTIONS)
    assert props["qty"]["type"] == "integer"
    find = tools_by_name["find_resolution_options"].tool_spec["inputSchema"]["json"]["properties"]
    assert find["preferred_channel"]["enum"] == ["ship", "store_pickup", "return"]
    assert find["replacement_size"]["type"] == "string"  # sizes are strings with one decimal
    escalate = tools_by_name["escalate_case"].tool_spec["inputSchema"]["json"]["properties"]
    assert escalate["reason_code"]["enum"] == list(agent_tools.CASE_REASONS)


def test_case_reasons_match_the_cases_module_when_it_exists() -> None:
    cases = pytest.importorskip("returns_domain.cases")
    assert tuple(cases.CASE_REASONS) == agent_tools.CASE_REASONS


# ------------------------------------------------------------------------- envelope


def test_success_envelope_shape(tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert set(result) == {"ok", "data", "source_refs", "provenance"}
    assert result["ok"] is True and result["provenance"] == "synthetic"
    assert result["source_refs"] == [f"ORDER#{AGENT_ORDER}/META"]
    assert result["data"]["order_id"] == AGENT_ORDER


def test_error_envelope_never_raises_to_the_model(tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["get_order_context"](order_id=OTHER_ORDER)  # another customer's order
    assert result["ok"] is False
    assert result["error"] == {
        "code": "NOT_AUTHORIZED", "message": "Order not found for this customer.", "retryable": False,
    }
    assert result["source_refs"] == []


def test_unexpected_exception_becomes_a_generic_internal_error(
    domain: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    domain.fail("FakeOrders.order_context", RuntimeError("secret table name aldo-returns-demo"))
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert result["ok"] is False and result["error"]["code"] == "INTERNAL_ERROR"
    assert "aldo-returns-demo" not in json.dumps(result)  # internals stay in the log
    assert result["error"]["retryable"] is False


def test_no_decimal_in_results(tools_by_name: dict[str, Any]) -> None:
    """G-03: the fakes hand back Decimals (as boto3 would); nothing of that reaches the model."""
    context = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    options = tools_by_name["find_resolution_options"](policy_check_id="PCHK-0000000A", deadline_date="2026-10-17")
    for envelope in (context, options):
        assert not has_decimal(envelope)
        json.dumps(envelope)  # must not raise
    assert context["data"]["lines"][0]["remaining_qty"] == 1
    assert options["data"]["availability"]["dc_atp"] == 5


def test_domain_error_details_pass_through_when_present(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    error = DomainError(ErrorCode.VALIDATION_ERROR, "replacement_size is required", details={"allowed": ["8.0"]})
    domain.fail("FakeOptions.find_options", error)
    result = tools_by_name["find_resolution_options"](policy_check_id="PCHK-0000000A")
    assert result["error"]["code"] == "VALIDATION_ERROR"
    assert result["error"]["details"] == {"allowed": ["8.0"]}


# ----------------------------------------------------------------------------- events


def test_tool_call_then_tool_result_events(domain: FakeDomain, ctx: RunContext, tools_by_name: dict[str, Any]) -> None:
    ctx.rounds = 3
    tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    call, result = domain.trace.of_type("tool_call")[0], domain.trace.of_type("tool_result")[0]
    assert domain.trace.types == ["tool_call", "tool_result"]
    assert call == {"tool": "get_order_context", "args": {"order_id": AGENT_ORDER}, "round": 3}
    assert result["tool"] == "get_order_context" and result["ok"] is True and result["code"] is None
    assert result["round"] == 3 and isinstance(result["latency_ms"], int)
    assert result["evidence"] == {"delivered_at": "2026-10-16", "final_sale": False, "remaining_qty": 1}
    assert result["provenance"] == "synthetic" and result["source_refs"] == [f"ORDER#{AGENT_ORDER}/META"]
    assert ctx.tool_calls == 1


def test_failed_tool_result_event_carries_the_code(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["get_order_context"](order_id=OTHER_ORDER)
    result = domain.trace.of_type("tool_result")[0]
    assert result["ok"] is False and result["code"] == "NOT_AUTHORIZED" and result["evidence"] == {}


def test_a_failing_trace_never_breaks_a_tool(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.fail("FakeTrace.emit", *[RuntimeError("trace down")] * 6)
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert result["ok"] is True


def test_events_never_contain_customer_ids_or_tokens(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["list_my_orders"]()
    evaluate(tools_by_name)
    text = json.dumps(domain.trace.events)
    assert CUSTOMER not in text
    assert "token" not in text.lower() and "secret" not in text.lower()


# ---------------------------------------------------------------- identity and ownership


def test_customer_id_comes_from_the_context_only(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["list_my_orders"]()
    tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    evaluate(tools_by_name)
    for cls, method in (("FakeOrders", "list_orders"), ("FakeOrders", "order_context"), ("FakeOrders", "check_policy")):
        (args, _), = domain.calls_to(cls, method)
        assert args[0] == CUSTOMER


def test_list_my_orders_includes_agent_placed_orders(tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["list_my_orders"]()
    orders = {o["order_id"]: o for o in result["data"]["orders"]}
    assert set(orders) == {AGENT_ORDER, SEEDED_ORDER, "DEMO-1002"}  # own orders only, newest first
    assert orders[AGENT_ORDER]["channel"] == "agent"
    assert orders[AGENT_ORDER]["lines"] == [{
        "line_id": HERO_LINE, "product_name": "Stessy2.0", "size": "8.0", "qty": 1, "final_sale": False,
    }]
    assert list(orders)[0] == AGENT_ORDER


@pytest.mark.parametrize("order_id", [AGENT_ORDER, SEEDED_ORDER])
def test_agent_placed_orders_work_like_seeded_orders(
    domain: FakeDomain, tools_by_name: dict[str, Any], order_id: str
) -> None:
    """PIVOT C6 delta 3: DEMO-A2A-XXXXXX ids go through every tool unchanged."""
    line_id = f"{order_id}-L1"
    context = tools_by_name["get_order_context"](order_id=order_id)
    policy = evaluate(tools_by_name, order_id=order_id, line_id=line_id)
    assert context["ok"] and policy["ok"] and policy["data"]["decision"] == "eligible"
    (args, _), = domain.calls_to("FakeOrders", "check_policy")
    assert args[2] == order_id and args[3] == line_id


def test_other_customers_orders_stay_unreadable(tools_by_name: dict[str, Any]) -> None:
    assert evaluate(tools_by_name, order_id=OTHER_ORDER, line_id=f"{OTHER_ORDER}-L1")["error"]["code"] == "NOT_AUTHORIZED"


# ------------------------------------------------------------------------- the policy


def test_policy_result_mapping_for_the_hero_return(tools_by_name: dict[str, Any]) -> None:
    result = evaluate(tools_by_name)
    data = result["data"]
    assert data["decision"] == "eligible" and data["rule_ids"] == ["window.ok"]
    assert data["policy_check_id"] == "PCHK-0000000A" and data["policy_version"] == "2026.10-demo"
    assert "exchange_store_pickup" in data["allowed_option_types"]
    assert data["refund_cents"] == 12999 and data["refund_text"] == "CAD 129.99" and data["currency"] == "CAD"
    assert result["source_refs"][0] == "PCHK#PCHK-0000000A/META"


def test_size_direction_is_passed_to_the_domain(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    evaluate(tools_by_name, size_direction="too_small")
    (args, _), = domain.calls_to("FakeOrders", "check_policy")
    assert args[-1] == "too_small" and args[5] == "FIT_SIZE" and args[6] == "unworn"


def test_unknown_condition_asks_for_information(tools_by_name: dict[str, Any]) -> None:
    data = evaluate(tools_by_name, condition="unknown")["data"]
    assert data["decision"] == "needs_information" and data["missing_fields"] == ["condition"]
    assert data["allowed_option_types"] == []


def test_final_sale_defect_needs_review(tools_by_name: dict[str, Any]) -> None:
    data = evaluate(
        tools_by_name, order_id="DEMO-1002", line_id="DEMO-1002-L1",
        reason_category="QUALITY_DAMAGE", condition="damaged", size_direction="unknown",
    )["data"]
    assert data["decision"] == "needs_review" and "final_sale.defect_review" in data["rule_ids"]


def test_policy_is_not_influenced_by_free_text(tools_by_name: dict[str, Any]) -> None:
    """There is no parameter that carries customer prose into the policy."""
    props = tools_by_name["evaluate_return_policy"].tool_spec["inputSchema"]["json"]["properties"]
    assert set(props) == {"order_id", "line_id", "qty", "reason_category", "condition", "size_direction"}


# -------------------------------------------------------------------------- fit insight


def test_fit_insight_for_the_hero_uses_real_derived_data(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["get_fit_insight"](product_name="Stessy2.0")
    data = result["data"]
    assert result["ok"] and result["provenance"] == "derived"
    assert data["label"] == "derived from provided data"
    assert data["signal"] == "runs_small" and data["scope"] == "ALDO_CA" and data["n_fit"] == 582
    assert "79.9%" in data["evidence_text"] and "n=582" in data["evidence_text"]
    assert "'too small'" in data["evidence_text"]
    evidence = domain.trace.of_type("tool_result")[0]["evidence"]
    assert evidence == {"signal": "runs_small", "too_small_share": 0.799, "n_fit": 582, "scope": "ALDO_CA"}


def test_fit_insight_for_a_runs_large_style_says_too_big(tools_by_name: dict[str, Any]) -> None:
    data = tools_by_name["get_fit_insight"](product_name="Clementinne")["data"]
    assert data["signal"] == "runs_large"
    assert "'too big'" in data["evidence_text"] and "n=417" in data["evidence_text"]


def test_fit_insight_for_an_unknown_product_is_insufficient_data(tools_by_name: dict[str, Any]) -> None:
    data = tools_by_name["get_fit_insight"](product_name="No Such Shoe")["data"]
    assert data["signal"] == "insufficient_data" and data["found"] is False
    assert "Not enough fit-related returns" in data["evidence_text"]


# ------------------------------------------------------------------------------ options


def test_find_options_returns_a_ranking_that_follows_the_pivot_rule(
    ctx: RunContext, tools_by_name: dict[str, Any]
) -> None:
    result = tools_by_name["find_resolution_options"](policy_check_id="PCHK-0000000A", deadline_date="2026-10-17")
    ranking = result["data"]["ranking"]
    assert ranking["order"] == ["OPT-3", "OPT-2", "OPT-1", "OPT-4", "OPT-5"]
    assert "Store pickup at ALDO Sainte-Catherine is free and ready Sat Oct 17" in ranking["reason"]
    assert "meets your Sat Oct 17 deadline" in ranking["reason"]
    assert "Refund options stay available" in ranking["reason"]
    assert ctx.rank_orders["CAND-0000000B"] == ranking["order"]


def test_every_eligible_refund_option_stays_visible(tools_by_name: dict[str, Any]) -> None:
    data = tools_by_name["find_resolution_options"](policy_check_id="PCHK-0000000A")["data"]
    types = [o["type"] for o in data["options"]]
    assert "return_in_store" in types and "return_mail" in types
    in_store = next(o for o in data["options"] if o["type"] == "return_in_store")
    assert in_store["refund_text"] == "CAD 129.99" and in_store["fee_text"] == "CAD 0.00"
    exchange = next(o for o in data["options"] if o["type"] == "exchange_store_pickup")
    assert exchange["fee_cents"] == 0 and exchange["fee_text"] == "CAD 0.00"  # in-store exchange is free


def test_find_options_passes_the_arguments_to_the_domain(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["find_resolution_options"](
        policy_check_id="PCHK-0000000A", replacement_size="9.0", deadline_date="2026-10-17",
        preferred_channel="store_pickup",
    )
    (args, kwargs), = domain.calls_to("FakeOptions", "find_options")
    assert args == (CUSTOMER, "PCHK-0000000A", "9.0", "2026-10-17", "store_pickup", None)  # no colour for a fit problem
    assert kwargs == {"run_id": "RUN-TEST000001"}


def test_find_options_evidence_and_refs(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["find_resolution_options"](policy_check_id="PCHK-0000000A", deadline_date="2026-10-17")
    evidence = domain.trace.of_type("tool_result")[0]["evidence"]
    assert evidence["candidate_set_id"] == "CAND-0000000B" and evidence["dc_atp"] == 5
    assert evidence["replacement_size"] == "8.5" and "exchange_store_pickup" in evidence["option_types"]
    assert "INV#DEMO-SKU-HERO-BLACK-085/LOC#DEMO-STORE-MTL-01" in result["source_refs"]


def test_dc_stockout_ranking_still_leads_with_store_pickup(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.options.dc_stock = False
    ranking = tools_by_name["find_resolution_options"](
        policy_check_id="PCHK-0000000A", deadline_date="2026-10-17"
    )["data"]["ranking"]
    assert ranking["order"] == ["OPT-3", "OPT-2", "OPT-4", "OPT-5"]


# -------------------------------------------------------------------------------- quote


def test_create_quote_presents_options_in_ranking_order(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["find_resolution_options"](policy_check_id="PCHK-0000000A", deadline_date="2026-10-17")
    result = tools_by_name["create_quote"](
        candidate_set_id="CAND-0000000B", option_ids=["OPT-5", "OPT-1", "OPT-4", "OPT-3"]
    )
    assert result["ok"] and result["data"]["option_ids"] == ["OPT-3", "OPT-1", "OPT-4", "OPT-5"]
    (args, _), = domain.calls_to("FakeQuotes", "create_quote")
    assert args == (CUSTOMER, "RUN-TEST000001", "CONV-TEST000001", "CAND-0000000B", ["OPT-3", "OPT-1", "OPT-4", "OPT-5"])
    assert result["data"]["quote_id"].startswith("QUO-") and result["data"]["status"] == "open"


def test_create_quote_without_a_known_ranking_keeps_the_given_order(
    domain: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    tools_by_name["create_quote"](candidate_set_id="CAND-0000000B", option_ids=["OPT-4", "OPT-3"])
    (args, _), = domain.calls_to("FakeQuotes", "create_quote")
    assert args[-1] == ["OPT-4", "OPT-3"]


# ---------------------------------------------------------------------------- retries


def test_read_tool_retries_twice_then_succeeds(
    domain: FakeDomain, tools_by_name: dict[str, Any], no_real_backoff: list[float]
) -> None:
    retryable = DomainError(ErrorCode.UPSTREAM_TIMEOUT, "slow")
    domain.fail("FakeOrders.order_context", retryable, retryable)
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert result["ok"] is True
    assert len(domain.calls_to("FakeOrders", "order_context")) == 3
    assert no_real_backoff == [0.1, 0.3]  # 100 ms then 300 ms


def test_read_tool_gives_up_after_two_retries(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    retryable = DomainError(ErrorCode.UPSTREAM_TIMEOUT, "slow")
    domain.fail("FakeOrders.order_context", retryable, retryable, retryable)
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert result["ok"] is False and result["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert result["error"]["retryable"] is True
    assert len(domain.calls_to("FakeOrders", "order_context")) == 3


def test_non_retryable_errors_are_not_retried(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.fail("FakeOrders.order_context", DomainError(ErrorCode.ORDER_NOT_FOUND, "missing"))
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert result["error"]["code"] == "ORDER_NOT_FOUND"
    assert len(domain.calls_to("FakeOrders", "order_context")) == 1


def test_transient_aws_errors_are_retried_then_mapped_to_upstream_timeout(
    domain: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    throttle = ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "Query")
    timeout = ReadTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")
    domain.fail("FakeOrders.order_context", throttle, timeout, throttle)
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert result["error"] == {
        "code": "UPSTREAM_TIMEOUT", "message": agent_tools.UPSTREAM_MESSAGE, "retryable": True,
    }
    assert len(domain.calls_to("FakeOrders", "order_context")) == 3


def test_no_retry_on_create_quote(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    """G-17: create_quote is not retried by the wrapper."""
    domain.fail("FakeQuotes.create_quote", DomainError(ErrorCode.UPSTREAM_TIMEOUT, "slow"))
    result = tools_by_name["create_quote"](candidate_set_id="CAND-0000000B", option_ids=["OPT-3"])
    assert result["error"]["code"] == "UPSTREAM_TIMEOUT"
    assert len(domain.calls_to("FakeQuotes", "create_quote")) == 1


def test_no_retry_on_escalate_case(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.fail("FakeCases.escalate", DomainError(ErrorCode.UPSTREAM_TIMEOUT, "slow"))
    result = tools_by_name["escalate_case"](reason_code="POLICY_REVIEW", summary="needs a person")
    assert result["ok"] is False
    assert len(domain.calls_to("FakeCases", "escalate")) == 1


def test_execute_is_retried_on_a_retryable_failure(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.fail("FakeExecution.execute", DomainError(ErrorCode.TRANSACTION_CONFLICT, "conflict"))
    result = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["ok"] is True and len(domain.calls_to("FakeExecution", "execute")) == 2


# ----------------------------------------------------------------------- execution


def test_execute_passes_ids_and_returns_the_record(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["ok"]
    assert result["data"]["transaction_id"] == "DEMO-RMA-ABC123" and result["data"]["replayed"] is False
    (args, kwargs), = domain.calls_to("FakeExecution", "execute")
    assert args == (CUSTOMER, "QUO-X", "OPT-3") and kwargs == {"run_id": "RUN-TEST000001"}
    evidence = domain.trace.of_type("tool_result")[0]["evidence"]
    assert evidence == {"transaction_id": "DEMO-RMA-ABC123", "replayed": False}


def test_approval_runs_execute_the_server_named_quote_and_option(domain: FakeDomain, ctx: RunContext) -> None:
    ctx.kind, ctx.quote_id, ctx.option_id = "approval", "QUO-APPROVED", "OPT-3"
    tools = {t.tool_spec["name"]: t for t in agent_tools.build_tools(ctx)}
    tools["execute_approved_resolution"](quote_id="QUO-MISTYPED", option_id="OPT-1")
    (args, _), = domain.calls_to("FakeExecution", "execute")
    assert args == (CUSTOMER, "QUO-APPROVED", "OPT-3")
    assert domain.trace.of_type("tool_call")[0]["args"] == {"quote_id": "QUO-APPROVED", "option_id": "OPT-3"}


def test_customer_runs_keep_the_model_supplied_ids(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["execute_approved_resolution"](quote_id="QUO-Y", option_id=None)
    (args, _), = domain.calls_to("FakeExecution", "execute")
    assert args == (CUSTOMER, "QUO-Y", None)


def test_execution_errors_reach_the_model_as_codes(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.fail("FakeExecution.execute", DomainError(ErrorCode.STOCK_CHANGED, "stock changed"))
    result = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["error"]["code"] == "STOCK_CHANGED"


def test_status_adds_money_text_and_evidence(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["get_resolution_status"](transaction_id="DEMO-RMA-ABC123")
    data = result["data"]
    assert data["state"] == "completed" and data["simulated"] is True
    assert data["refund_text"] == "CAD 0.00" and data["fee_text"] == "CAD 0.00"
    assert data["next_step"].startswith("Pick up size 8.5 at ALDO Sainte-Catherine")
    assert domain.trace.of_type("tool_result")[0]["evidence"] == {"state": "completed", "location_id": "DEMO-STORE-MTL-01"}


# --------------------------------------------------- write-back delivery (pivot delta 4)


def test_deliver_pending_runs_after_a_successful_execution(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["ok"]
    assert domain.gateway.delivered_for == [CUSTOMER]
    (args, _), = domain.calls_to("FakeGateway", "deliver_pending")
    assert args == (CUSTOMER,)


def test_deliver_pending_is_skipped_when_execution_fails(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.fail("FakeExecution.execute", DomainError(ErrorCode.APPROVAL_REQUIRED, "no approval"))
    result = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["ok"] is False
    assert domain.calls_to("FakeGateway", "deliver_pending") == []


def test_the_gateway_may_call_it_flush_outbox(ctx: RunContext, domain: FakeDomain) -> None:
    """The addendum says deliver_pending; the module on disk exposes flush_outbox. Either works."""
    sent: list[Any] = []

    class OutboxOnly:
        def flush_outbox(self, customer_id: str | None = None, limit: int = 10, client: Any = None) -> int:
            sent.append(customer_id)
            return 1

    ctx.services = domain.services.override(gateway=OutboxOnly())
    tools = {t.tool_spec["name"]: t for t in agent_tools.build_tools(ctx)}
    assert tools["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")["ok"] is True
    assert sent == [CUSTOMER]


def test_deliver_pending_failure_never_fails_the_tool(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    domain.fail("FakeGateway.deliver_pending", RuntimeError("personal agent unreachable"))
    result = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["ok"] is True and result["data"]["transaction_id"] == "DEMO-RMA-ABC123"


def test_missing_gateway_module_never_fails_the_tool(
    ctx: RunContext, domain: FakeDomain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway module is written by another stream; without it the tool still works."""

    real_import = importlib.import_module

    def import_or_miss_gateway(name: str, package: str | None = None) -> Any:
        if name.endswith(".gateway"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, package)

    monkeypatch.setattr(services_module, "importlib", SimpleNamespace(import_module=import_or_miss_gateway))
    ctx.services = domain.services_without("gateway")
    tools = {t.tool_spec["name"]: t for t in agent_tools.build_tools(ctx)}
    result = tools["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["ok"] is True and result["data"]["transaction_id"] == "DEMO-RMA-ABC123"


def test_slow_delivery_cannot_slow_the_tool_beyond_the_budget(
    domain: FakeDomain, tools_by_name: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_tools, "DELIVER_BUDGET_SECONDS", 0.2)
    domain.gateway.delay = 3.0
    started = time.monotonic()
    result = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-3")
    assert result["ok"] is True
    assert time.monotonic() - started < 1.5


def test_the_delivery_budget_is_three_seconds() -> None:
    assert agent_tools.DELIVER_BUDGET_SECONDS == 3.0


# ------------------------------------------------------------- escalation and decisions


def test_escalate_case_returns_the_case_id_and_marks_the_context(
    domain: FakeDomain, ctx: RunContext, tools_by_name: dict[str, Any]
) -> None:
    result = tools_by_name["escalate_case"](
        reason_code="POLICY_REVIEW", summary="Final-sale defect", order_id="DEMO-1002", line_id="DEMO-1002-L1"
    )
    assert result["ok"] and result["data"]["status"] == "open"
    assert result["data"]["case_id"].startswith("DEMO-CASE-")
    assert ctx.escalated is True and ctx.case_id == result["data"]["case_id"]
    (args, _), = domain.calls_to("FakeCases", "escalate")
    assert args == (CUSTOMER, "RUN-TEST000001", "POLICY_REVIEW", "Final-sale defect", "DEMO-1002", "DEMO-1002-L1")


def test_a_failed_escalation_does_not_mark_the_context(ctx: RunContext, domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    long_summary = "x" * 801
    result = tools_by_name["escalate_case"](reason_code="POLICY_REVIEW", summary=long_summary)
    assert result["ok"] is False and result["error"]["code"] == "VALIDATION_ERROR"
    assert ctx.escalated is False and ctx.case_id is None


def test_record_decision_emits_a_decision_event(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["record_decision"](summary="  Store pickup   is free\nand ready Sat Oct 17.  ")
    assert result["ok"] and result["data"] == {"recorded": True}
    assert domain.trace.of_type("decision") == [{"summary": "Store pickup is free and ready Sat Oct 17."}]
    assert domain.trace.types == ["tool_call", "decision", "tool_result"]


def test_record_decision_is_cut_at_240_characters(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["record_decision"](summary="a" * 400)
    assert len(domain.trace.of_type("decision")[0]["summary"]) == 240
    assert config.get_settings().max_decision_chars == 240


def test_record_decision_rejects_an_empty_summary(tools_by_name: dict[str, Any]) -> None:
    result = tools_by_name["record_decision"](summary="   ")
    assert result["ok"] is False and result["error"]["code"] == "VALIDATION_ERROR"


# ------------------------------------------------------------------- limit in wrapper


def test_wrapper_refuses_over_limit_calls_when_used_without_the_hooks(
    domain: FakeDomain, ctx: RunContext, tools_by_name: dict[str, Any]
) -> None:
    ctx.rounds = ctx.max_rounds + 1
    result = tools_by_name["get_order_context"](order_id=AGENT_ORDER)
    assert result["ok"] is False and result["error"]["code"] == "LIMIT_REACHED"
    assert domain.calls_to("FakeOrders", "order_context") == []  # the domain was never touched
    assert domain.trace.of_type("tool_result")[0]["code"] == "LIMIT_REACHED"
    assert tools_by_name["record_decision"](summary="still allowed")["ok"] is True
