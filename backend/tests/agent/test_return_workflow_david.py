"""Return workflow v2 for David (S19b): a multi-line bundle, the monthly shipping perk, the authority limit.

Fakes only (see fakes.py; the integration test on the real domain is in test_integration_david.py). The fakes follow the
bundle contract of the domain (S17B report): ``find_options(policy_check_ids=[...])`` returns return options only, each
with ``lines``, ONE fee, ``refund_total_cents``/``net_refund_cents``, a ``benefit`` only while the month has room and
``decision: needs_review`` above the tier limit; ``create_quote`` refuses a needs_review option.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent import line_match, prompt as agent_prompt, ranking
from agent import tools as agent_tools
from agent.context import RunContext
from returns_domain.errors import DomainError, ErrorCode

from .fakes import BUNDLE_ORDER, BUNDLE_SCRIPT, DAVID, FakeDomain, bundle_check_id

CAND = "CAND-0000000B"
SCRIPT_IDS = [bundle_check_id(n) for n in BUNDLE_SCRIPT]


@pytest.fixture
def david_ctx(domain: FakeDomain) -> RunContext:
    return RunContext(
        run_id="RUN-DAVID00001", conv_id="CONV-DAVID00001", customer_id=DAVID, order_id=BUNDLE_ORDER,
        kind="customer", max_rounds=8, services=domain.services,
    )


@pytest.fixture
def david(david_ctx: RunContext) -> dict[str, Any]:
    return {t.tool_spec["name"]: t for t in agent_tools.build_tools(david_ctx)}


def lines_of(domain: FakeDomain, david: dict[str, Any]) -> list[dict[str, Any]]:
    return david["get_order_context"](order_id=BUNDLE_ORDER)["data"]["lines"]


def find_bundle(david: dict[str, Any], ids: list[str] | None = None, **kwargs: Any) -> dict:
    return david["find_resolution_options"](policy_check_ids=ids or SCRIPT_IDS, **kwargs)


# ------------------------------------------------------------------ line resolution (code, no model)


def test_the_numbered_list_follows_the_line_ids_not_the_text_order(domain: FakeDomain, david: dict[str, Any]) -> None:
    lines = lines_of(domain, david)
    assert [l["line_id"] for l in lines] == [f"{BUNDLE_ORDER}-L{n}" for n in range(1, 11)]  # not L1, L10, L2 ...
    assert [l["number"] for l in lines] == list(range(1, 11))
    second = lines[1]
    assert (second["product_name"], second["color"], second["category"], second["size"]) == (
        "Brando", "TAN", "boot", "10.5",
    )
    assert second["unit_paid_text"] == "CAD 179.99"


def test_a_single_line_order_is_left_as_it_was(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    from .fakes import AGENT_ORDER

    line = tools_by_name["get_order_context"](order_id=AGENT_ORDER)["data"]["lines"][0]
    assert "number" not in line and "category" not in line


def names(selection: dict[str, Any]) -> list[tuple[str, str]]:
    return [(m["status"], m.get("line_id", "").rsplit("-", 1)[-1]) for m in selection["matches"]]


def test_colour_plus_category_resolves_the_script_items(domain: FakeDomain, david: dict[str, Any]) -> None:
    result = david["get_order_context"](
        order_id=BUNDLE_ORDER, items=["the tan boot", "the white sneaker", "the navy loafer"]
    )
    selection = result["data"]["selection"]
    assert names(selection) == [("matched", "L2"), ("matched", "L5"), ("matched", "L8")]
    assert selection["selected_line_ids"] == [f"{BUNDLE_ORDER}-L{n}" for n in (2, 5, 8)]
    assert selection["complete"] is True
    assert selection["matches"][0]["unit_paid_text"] == "CAD 179.99"


def test_the_items_reach_the_trace_only_when_given(domain: FakeDomain, david: dict[str, Any]) -> None:
    david["get_order_context"](order_id=BUNDLE_ORDER)
    david["get_order_context"](order_id=BUNDLE_ORDER, items=["tan boot"])
    calls = domain.trace.of_type("tool_call")
    assert calls[0]["args"] == {"order_id": BUNDLE_ORDER}
    assert calls[1]["args"] == {"order_id": BUNDLE_ORDER, "items": ["tan boot"]}


@pytest.fixture
def order_lines(domain: FakeDomain, david: dict[str, Any]) -> list[dict[str, Any]]:
    return lines_of(domain, david)


def test_a_category_that_fits_two_lines_is_ambiguous_and_lists_both(order_lines: list[dict[str, Any]]) -> None:
    sneaker = line_match.resolve(order_lines, ["the sneaker"])["matches"][0]
    assert sneaker["status"] == "ambiguous"
    assert [c["line_id"].rsplit("-", 1)[-1] for c in sneaker["candidates"]] == ["L5", "L10"]
    assert {c["color"] for c in sneaker["candidates"]} == {"WHITE", "GREY"}
    boot = line_match.resolve(order_lines, ["boot"])["matches"][0]  # the tan boot and the black chelsea boot
    assert boot["status"] == "ambiguous" and len(boot["candidates"]) == 2
    brown = line_match.resolve(order_lines, ["my brown shoes"])["matches"][0]  # "shoes" says nothing about which
    assert brown["status"] == "ambiguous" and len(brown["candidates"]) == 3


def test_an_ambiguous_phrase_makes_the_selection_incomplete(order_lines: list[dict[str, Any]]) -> None:
    selection = line_match.resolve(order_lines, ["tan boot", "sneaker"])
    assert selection["complete"] is False
    assert selection["selected_line_ids"] == [f"{BUNDLE_ORDER}-L2"]  # only what is certain


def test_colour_narrows_a_shared_category_and_plurals_match(order_lines: list[dict[str, Any]]) -> None:
    assert names(line_match.resolve(order_lines, ["the grey sneakers"])) == [("matched", "L10")]
    assert names(line_match.resolve(order_lines, ["black boot"])) == [("matched", "L4")]  # the black chelsea boot
    assert names(line_match.resolve(order_lines, ["Tan Boots"])) == [("matched", "L2")]


def test_the_product_name_resolves_when_the_owner_uses_it(order_lines: list[dict[str, Any]]) -> None:
    assert names(line_match.resolve(order_lines, ["Brando", "the Patten"])) == [("matched", "L2"), ("matched", "L8")]


def test_a_number_means_the_position_in_the_list(order_lines: list[dict[str, Any]]) -> None:
    result = line_match.resolve(order_lines, ["2", "number 5", "#8", "item 10"])
    assert names(result) == [("matched", "L2"), ("matched", "L5"), ("matched", "L8"), ("matched", "L10")]
    assert names(line_match.resolve(order_lines, ["11"])) == [("none", "")]


def test_unknown_words_and_wrong_colours_match_nothing(order_lines: list[dict[str, Any]]) -> None:
    for phrase in ("the pink boot", "the red sneaker", "my umbrella", "", "   "):
        assert not any(m["status"] == "matched" for m in line_match.resolve(order_lines, [phrase])["matches"])
    assert line_match.resolve(order_lines, [])["complete"] is False


def test_the_same_line_named_twice_is_a_duplicate_not_two_lines(order_lines: list[dict[str, Any]]) -> None:
    result = line_match.resolve(order_lines, ["tan boot", "the Brando"])
    assert [m["status"] for m in result["matches"]] == ["matched", "duplicate"]
    assert result["selected_line_ids"] == [f"{BUNDLE_ORDER}-L2"] and result["complete"] is False


def test_a_line_that_is_already_returned_cannot_be_selected(order_lines: list[dict[str, Any]]) -> None:
    lines = [dict(l, remaining_qty=0) if l["number"] == 2 else l for l in order_lines]
    assert names(line_match.resolve(lines, ["tan boot"])) == [("none", "")]


def test_at_most_ten_phrases_are_read(order_lines: list[dict[str, Any]]) -> None:
    result = line_match.resolve(order_lines, [str(n) for n in range(1, 11)] + ["tan boot"])
    assert len(result["matches"]) == 10 and result["complete"] is True


# ---------------------------------------------------------------------- one policy call per line


def test_the_policy_is_called_once_per_selected_line(domain: FakeDomain, david: dict[str, Any]) -> None:
    ids = []
    for n in BUNDLE_SCRIPT:
        result = david["evaluate_return_policy"](
            order_id=BUNDLE_ORDER, line_id=f"{BUNDLE_ORDER}-L{n}", qty=1, reason_category="CHANGE_OF_MIND",
            condition="unworn",
        )
        assert result["ok"] and result["data"]["decision"] == "eligible"
        ids.append(result["data"]["policy_check_id"])
    assert ids == SCRIPT_IDS and len(set(ids)) == 3
    calls = domain.calls_to("FakeOrders", "check_policy")
    assert [a[3] for a, _ in calls] == [f"{BUNDLE_ORDER}-L{n}" for n in BUNDLE_SCRIPT]
    assert all(a[0] == DAVID for a, _ in calls)  # always the signed-in owner


# ------------------------------------------------------------------------- bundle options


def test_the_policy_check_ids_go_to_the_domain_and_the_single_id_stays_out(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    assert find_bundle(david)["ok"]
    (args, kwargs), = domain.calls_to("FakeOptions", "find_options")
    assert args == (DAVID, None, None, None, None, None)
    assert kwargs == {"run_id": "RUN-DAVID00001", "policy_check_ids": SCRIPT_IDS}
    assert domain.trace.of_type("tool_call")[0]["args"]["policy_check_ids"] == SCRIPT_IDS


def test_one_of_the_two_id_arguments_is_required_and_not_both(domain: FakeDomain, david: dict[str, Any]) -> None:
    both = david["find_resolution_options"](policy_check_id=SCRIPT_IDS[0], policy_check_ids=SCRIPT_IDS)
    neither = david["find_resolution_options"]()
    for refused in (both, neither):
        assert refused["ok"] is False and refused["error"]["code"] == "VALIDATION_ERROR"
    assert domain.calls_to("FakeOptions", "find_options") == []  # the domain was never asked


def test_the_tool_schema_takes_the_list_and_keeps_the_single_id_optional(david: dict[str, Any]) -> None:
    schema = david["find_resolution_options"].tool_spec["inputSchema"]["json"]
    assert schema["properties"]["policy_check_ids"]["type"] == "array"
    assert schema["properties"]["policy_check_ids"]["items"] == {"type": "string"}
    assert "policy_check_id" not in schema.get("required", [])
    items = david["get_order_context"].tool_spec["inputSchema"]["json"]
    assert items["properties"]["items"]["type"] == "array" and items["required"] == ["order_id"]


def test_the_bundle_option_carries_lines_one_fee_totals_and_the_perk(david: dict[str, Any]) -> None:
    data = find_bundle(david)["data"]
    (option,) = data["options"]  # David has no store within reach: mail only
    assert option["type"] == "return_mail" and "minutes_away" not in option
    assert option["fee_cents"] == 0 and option["fee_text"] == "CAD 0.00"
    assert option["refund_total_cents"] == 41997 and option["refund_total_text"] == "CAD 419.97"
    assert option["net_refund_cents"] == 41997 and option["net_refund_text"] == "CAD 419.97"
    assert option["decision"] == "eligible" and option["authority_limit_text"] == "CAD 750.00"
    assert option["benefit"] == {
        "type": "free_return_shipping", "label": "Platinum free return shipping", "saved_cents": 595, "used": 1,
        "allowed": 1, "period": "2026-10", "saved_text": "CAD 5.95",
    }
    assert [l["product_name"] for l in option["lines"]] == ["Brando", "Oscar", "Patten"]
    first = option["lines"][0]
    assert first == {
        "line_id": f"{BUNDLE_ORDER}-L2", "product_name": "Brando", "color": "TAN", "category": "boot",
        "size": "10.5", "refund_cents": 17999, "refund_text": "CAD 179.99", "unit_paid_cents": 17999,
        "unit_paid_text": "CAD 179.99",
    }  # no sku and no policy check id for the model
    assert data["bundle"]["n_lines"] == 3 and data["bundle"]["refund_total_text"] == "CAD 419.97"
    assert data["bundle"]["decision"] == "eligible"
    assert data["policy_check_ids"] == SCRIPT_IDS
    assert data["availability"]["reason_no_exchange"] == "BUNDLE_RETURN_ONLY"


def test_the_ranking_says_no_store_is_within_reach_and_names_the_perk_from_the_option(david: dict[str, Any]) -> None:
    ranking_data = find_bundle(david)["data"]["ranking"]
    assert ranking_data["order"] == ["OPT-1"]
    assert ranking_data["reason"] == (
        "No store is within reach, so returning 3 items by mail is free with your Platinum free return shipping, "
        "saving CAD 5.95."
    )


def test_without_the_perk_the_standard_fee_applies_and_no_perk_is_invented(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    domain.options.bundle_perk = False
    data = find_bundle(david)["data"]
    (option,) = data["options"]
    assert "benefit" not in option and option["fee_cents"] == 595 and option["fee_text"] == "CAD 5.95"
    assert option["net_refund_cents"] == 41997 - 595 and option["net_refund_text"] == "CAD 414.02"
    assert option["refund_total_text"] == "CAD 419.97"
    assert "Platinum" not in data["ranking"]["reason"] and "free" not in data["ranking"]["reason"]
    assert "member_perk" not in data  # a tier without the perk hears nothing about it (ledger allowed 0)


def test_a_second_bundle_tells_the_model_the_monthly_perk_is_used(domain: FakeDomain, david: dict[str, Any]) -> None:
    domain.options.bundle_perk = False
    domain.benefits.usage.update(tier="platinum", used=1, allowed=1)
    data = find_bundle(david, [bundle_check_id(10)])["data"]
    (option,) = data["options"]
    assert option["fee_cents"] == 595 and option["fee_text"] == "CAD 5.95" and "benefit" not in option
    assert data["member_perk"] == {
        "type": "free_return_shipping", "label": "Platinum free return shipping", "status": "used_this_month",
        "used": 1, "allowed": 1, "period": "2026-10",
    }
    assert option["refund_total_text"] == "CAD 99.99" and [l["product_name"] for l in option["lines"]] == ["Finespec"]


def test_member_perk_is_absent_when_the_option_has_the_benefit_or_the_ledger_has_room_or_cannot_be_read(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    domain.benefits.usage.update(tier="platinum", used=0, allowed=1)
    assert "member_perk" not in find_bundle(david)["data"]  # the option carries the benefit
    domain.options.bundle_perk = False
    assert "member_perk" not in find_bundle(david)["data"]  # room in the ledger: nothing is "used"
    domain.benefits.usage.update(used=1)
    domain.fail("FakeBenefits.ledger_usage", RuntimeError("table down"))
    result = find_bundle(david)
    assert result["ok"] and "member_perk" not in result["data"]  # an unreadable ledger never breaks the options


def test_in_store_is_offered_only_when_the_domain_says_a_store_is_within_reach(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    domain.options.bundle_store = True
    data = find_bundle(david)["data"]
    assert [o["type"] for o in data["options"]] == ["return_in_store", "return_mail"]
    assert data["options"][0]["minutes_away"] == 5
    assert data["ranking"]["order"] == ["OPT-1", "OPT-2"]  # the store first, the mail option stays visible
    assert data["ranking"]["reason"].startswith("Returning 3 items in store at ALDO Sainte-Catherine, 5 minutes away")


def test_all_ten_lines_fit_and_the_result_stays_small(domain: FakeDomain, david: dict[str, Any]) -> None:
    domain.options.bundle_store = True
    ids = [bundle_check_id(n) for n in range(1, 11)]
    data = find_bundle(david, ids)["data"]
    assert [len(o["lines"]) for o in data["options"]] == [10, 10]
    assert all(l["product_name"] and l["color"] and l["category"] for l in data["options"][0]["lines"])
    assert data["options"][0]["refund_total_cents"] == 152990  # the real sum of the ten fixture prices
    assert len(json.dumps(data)) < 9000  # both options, ten lines each
    refunds = [o for o in data["options"] if o["type"].startswith("return_")]
    assert len(refunds) == 2 and data["ranking"]["order"][0] == "OPT-1"  # first option and refunds kept


# -------------------------------------------------------------------------------- quote


def test_the_quote_carries_the_bundle_fee_total_and_perk_with_the_owners_path_first(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    domain.options.bundle_store = True
    find_bundle(david)
    result = david["create_quote"](candidate_set_id=CAND, option_ids=["OPT-2", "OPT-1"], preferred_channel="return")
    assert result["ok"] and result["data"]["option_ids"] == ["OPT-1", "OPT-2"]  # ranking order, refunds all there
    bundle = result["data"]["bundle"]
    assert [l["product_name"] for l in bundle["lines"]] == ["Brando", "Oscar", "Patten"]
    mail = next(o for o in bundle["options"] if o["type"] == "return_mail")
    assert mail["fee_text"] == "CAD 0.00" and mail["refund_total_text"] == "CAD 419.97"
    assert mail["net_refund_text"] == "CAD 419.97" and mail["benefit"]["saved_text"] == "CAD 5.95"
    store_option = next(o for o in bundle["options"] if o["type"] == "return_in_store")
    assert "benefit" not in store_option and store_option["location_name"] == "ALDO Sainte-Catherine"


def test_a_forgotten_refund_option_is_added_to_a_bundle_quote_too(domain: FakeDomain, david: dict[str, Any]) -> None:
    find_bundle(david)
    result = david["create_quote"](candidate_set_id=CAND, option_ids=[], preferred_channel="return")
    assert result["ok"] and result["data"]["option_ids"] == ["OPT-1"]


def test_a_single_line_quote_has_no_bundle_key(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    tools_by_name["find_resolution_options"](policy_check_id="PCHK-0000000A", deadline_date="2026-10-17")
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=["OPT-3"], preferred_channel="store_pickup")
    assert result["ok"] and "bundle" not in result["data"]


# ------------------------------------------------------------------------- authority limit


def test_a_bundle_above_the_limit_is_marked_for_review_and_the_quote_is_refused(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    domain.options.bundle_review = True
    data = find_bundle(david)["data"]
    (option,) = data["options"]
    assert data["bundle"]["decision"] == "needs_review" and option["decision"] == "needs_review"
    assert option["rule_ids"] == ["authority.refund_limit"] and "benefit" not in option
    assert option["fee_text"] == "CAD 5.95"  # nobody can grant it, so no perk
    assert data["ranking"]["reason"] == (
        "A person must review this return: CAD 419.97 for 3 items is above the CAD 250.00 I can approve."
    )
    assert "member_perk" not in data
    refused = david["create_quote"](candidate_set_id=CAND, option_ids=["OPT-1"], preferred_channel="return")
    assert refused["ok"] is False and refused["error"]["code"] == "VALIDATION_ERROR"
    assert refused["error"]["details"] == {"decision": "needs_review", "rule_ids": ["authority.refund_limit"]}


def test_the_agent_escalates_a_review_bundle_through_the_case_tool_and_grants_nothing(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    domain.options.bundle_review = True
    find_bundle(david)
    case = david["escalate_case"](
        reason_code="POLICY_REVIEW", order_id=BUNDLE_ORDER, line_id=None,
        summary="Bundle of 3 items (Brando, Oscar, Patten), total CAD 419.97, above the CAD 250.00 limit.",
    )
    assert case["ok"] and case["data"]["case_id"].startswith("DEMO-CASE-")
    assert domain.calls_to("FakeQuotes", "create_quote") == []  # escalating creates no quote
    assert domain.calls_to("FakeExecution", "execute") == []


# ----------------------------------------------------------------------------- execution


def test_the_bundle_execution_reports_the_fee_refund_and_perk_from_the_record(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    domain.execution.bundle = True
    executed = david["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-1")["data"]
    assert executed["option_type"] == "return_mail" and executed["memory_update_id"] == "MU-5C0A12"
    assert executed["benefit"]["saved_text"] == "CAD 5.95"
    status = david["get_resolution_status"](transaction_id="DEMO-RET-ABC123")["data"]
    assert status["state"] == "completed" and status["option_type"] == "return_mail"
    assert status["fee_text"] == "CAD 0.00" and status["refund_total_text"] == "CAD 419.97"
    assert status["net_refund_text"] == "CAD 419.97" and status["benefit"]["label"] == "Platinum free return shipping"
    assert [l["product_name"] for l in status["lines"]] == ["Brando", "Oscar", "Patten"]
    assert all("sku" not in l and "policy_check_id" not in l for l in status["lines"])
    assert "Mail the 3 items" in status["next_step"] and status.get("store_notice") is None


def test_a_bundle_with_the_standard_fee_shows_no_perk(domain: FakeDomain, david: dict[str, Any]) -> None:
    domain.execution.bundle, domain.execution.bundle_fee = True, 595
    assert "benefit" not in david["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-1")["data"]
    status = david["get_resolution_status"](transaction_id="DEMO-RET-ABC123")["data"]
    assert "benefit" not in status and status["fee_text"] == "CAD 5.95" and status["net_refund_text"] == "CAD 414.02"


def test_a_perk_used_meanwhile_is_a_typed_error_and_a_new_find_has_the_fee_and_no_perk(
    domain: FakeDomain, david: dict[str, Any]
) -> None:
    find_bundle(david)
    quote = david["create_quote"](candidate_set_id=CAND, option_ids=["OPT-1"], preferred_channel="return")
    assert quote["data"]["bundle"]["options"][0]["fee_text"] == "CAD 0.00"
    domain.fail(
        "FakeExecution.execute",
        DomainError(ErrorCode.STOCK_CHANGED, "The free return shipping of this month was used.",
                    details={"reason": "BENEFIT_USED"}),
    )
    failed = david["execute_approved_resolution"](quote_id=quote["data"]["quote_id"], option_id="OPT-1")
    assert failed["ok"] is False and failed["error"]["code"] == "STOCK_CHANGED"
    assert failed["error"]["details"] == {"reason": "BENEFIT_USED"}  # the model reads this to re-quote

    # The re-quote: the domain now prices the same lines with the fee and no perk.
    domain.options.bundle_perk = False
    domain.benefits.usage.update(tier="platinum", used=1, allowed=1)
    again = find_bundle(david)["data"]
    (option,) = again["options"]
    assert option["fee_text"] == "CAD 5.95" and "benefit" not in option
    requote = david["create_quote"](candidate_set_id=CAND, option_ids=["OPT-1"], preferred_channel="return")
    assert requote["ok"] and requote["data"]["bundle"]["options"][0]["net_refund_text"] == "CAD 414.02"
    assert "benefit" not in requote["data"]["bundle"]["options"][0]


# ------------------------------------------------------------------------- ranking helper


def test_bundle_money_text_is_added_to_options_and_lines() -> None:
    option = {
        "option_id": "OPT-1", "type": "return_mail", "refund_cents": 41402, "fee_cents": 595,
        "refund_total_cents": 41997, "net_refund_cents": 41402, "authority_limit_cents": 75000,
        "lines": [{"line_id": "x", "refund_cents": 17999, "unit_paid_cents": 17999}],
    }
    shown = ranking.add_money_text(option)
    assert shown["refund_total_text"] == "CAD 419.97" and shown["net_refund_text"] == "CAD 414.02"
    assert shown["authority_limit_text"] == "CAD 750.00" and shown["fee_text"] == "CAD 5.95"
    assert shown["lines"][0]["refund_text"] == "CAD 179.99"
    assert ranking.add_money_text({"option_id": "OPT-1", "refund_cents": 100}) == {
        "option_id": "OPT-1", "refund_cents": 100, "refund_text": "CAD 1.00",
    }  # a single-line option gains nothing new


def test_bundle_options_rank_as_returns_in_store_first() -> None:
    options = [
        {"option_id": "OPT-2", "type": "return_mail", "fee_cents": 0, "eta_date": "2026-10-26", "lines": [{}]},
        {"option_id": "OPT-1", "type": "return_in_store", "fee_cents": 0, "eta_date": "2026-10-16", "lines": [{}]},
    ]
    assert ranking.ranking_summary(options)["order"] == ["OPT-1", "OPT-2"]


# ----------------------------------------------------------------------------------- prompt


@pytest.fixture(scope="module")
def text() -> str:
    return agent_prompt.build_system_prompt()


def test_the_prompt_has_the_bundle_dialogue_in_order(text: str) -> None:
    section = text.index("SEVERAL ITEMS OF ONE ORDER (a bundle)")
    assert text.index("FLOW") < section < text.index("CONTINUATIONS")
    steps = [text.index(marker, section) for marker in ("a. List.", "b. Choose.", "c. Policy.", "d. Options.", "e. After create_quote", "f. A second bundle", "g. Fees, limits and perks are fixed")]
    assert steps == sorted(steps)


@pytest.mark.parametrize(
    "rule",
    [
        "show the order as a numbered list",
        'call get_order_context with items set to their own words',
        "never pick a line yourself",
        "Ask for the reason once for the whole selection",
        "Call evaluate_return_policy once for EACH selected line",
        "Call find_resolution_options ONCE with policy_check_ids",
        "A bundle has no exchange: never offer one",
        "if there is no return_in_store option, no store is within reach, so it goes by mail",
        "never state a distance or a store that is not in an option",
        "If decision is needs_review, do NOT call create_quote",
        "Call escalate_case with POLICY_REVIEW",
        "You cannot grant it",
        "Mention the perk ONLY when the option carries benefit",
        "member_perk with status used_this_month",
        "Fees, limits and perks are fixed",
        'A later "also return the grey sneaker" is its own bundle of one item',
        "A size or colour exchange of one item is not a bundle",
        "BENEFIT_USED",
        "call find_resolution_options again with the same policy_check_ids",
        '"Return label created"',
    ],
)
def test_the_prompt_states_each_bundle_rule(text: str, rule: str) -> None:
    assert rule in text


def test_the_prompt_never_shows_a_second_agent_and_keeps_the_perk_rule_single(text: str) -> None:
    lowered = text.lower()
    assert "returns agent" not in lowered and "another agent" not in lowered and "hand-off" not in lowered
    assert text.count("ONLY when the chosen option carries a benefit field") == 1  # John's rule is untouched


def test_the_agent_still_has_exactly_ten_tools(david: dict[str, Any]) -> None:
    assert tuple(david) == agent_tools.TOOL_NAMES and len(david) == 10
