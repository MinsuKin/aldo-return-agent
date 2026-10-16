"""Return workflow v2 at the tool level: colour exchange, needs_choice, minutes away, perks, store notice, owner path.

Fakes only (see fakes.py). The fakes mirror the domain v2 contract (S17A report): a preference reason without a
colour gets ``needs_choice`` and no exchange options, with a colour it gets same-style same-size options in that
colour, store options carry ``minutes_away``, and ``store_notice`` comes back from execution and status.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent import owner_context
from agent import tools as agent_tools
from agent.context import RunContext

from .fakes import CUSTOMER, FakeDomain, _opt

PCHK = "PCHK-0000000A"
CAND = "CAND-0000000B"
MEMORY = {"size_system": "US_M", "typical_size": "10.0", "preferred_colors": ["WHITE", "BROWN"]}


@pytest.fixture
def john(domain: FakeDomain, ctx: RunContext) -> FakeDomain:
    """A colour (preference) return: the fake options follow the domain's v2 behaviour."""
    domain.options.preference = True
    return domain


def find(tools: dict[str, Any], **kwargs: Any) -> dict:
    return tools["find_resolution_options"](policy_check_id=PCHK, **kwargs)


# ------------------------------------------------------------------------- tool specs


def test_find_options_schema_has_the_colour_and_create_quote_the_owner_path(tools_by_name: dict[str, Any]) -> None:
    find_props = tools_by_name["find_resolution_options"].tool_spec["inputSchema"]["json"]["properties"]
    assert find_props["replacement_color"]["type"] == "string"
    assert "replacement_color" not in tools_by_name["find_resolution_options"].tool_spec["inputSchema"]["json"].get(
        "required", []
    )
    quote_props = tools_by_name["create_quote"].tool_spec["inputSchema"]["json"]["properties"]
    assert quote_props["preferred_channel"]["enum"] == ["ship", "store_pickup", "return"]
    assert tools_by_name["create_quote"].tool_spec["inputSchema"]["json"]["required"] == [
        "candidate_set_id", "option_ids",
    ]


def test_still_exactly_ten_tools(tools_by_name: dict[str, Any]) -> None:
    assert tuple(tools_by_name) == agent_tools.TOOL_NAMES and len(tools_by_name) == 10


# --------------------------------------------------------------- replacement_color passthrough


def test_replacement_color_goes_to_the_domain_as_the_sixth_argument(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    result = find(tools_by_name, deadline_date="2026-10-17", replacement_color="BROWN")
    assert result["ok"]
    (args, kwargs), = john.calls_to("FakeOptions", "find_options")
    assert args == (CUSTOMER, PCHK, None, "2026-10-17", None, "BROWN")
    assert kwargs == {"run_id": "RUN-TEST000001"}
    data = result["data"]
    assert data["availability"]["replacement_color"] == "BROWN"
    exchanges = [o for o in data["options"] if o["type"].startswith("exchange_")]
    assert exchanges and all(o["replacement_color"] == "BROWN" and o["replacement_size"] == "9.5" for o in exchanges)
    assert "needs_choice" not in data  # the key is absent when no choice is needed


def test_a_fit_problem_sends_no_colour(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    data = find(tools_by_name, deadline_date="2026-10-17")["data"]
    (args, _), = domain.calls_to("FakeOptions", "find_options")
    assert args[5] is None
    assert "needs_choice" not in data
    assert all("replacement_color" not in o for o in data["options"])


def test_the_colour_reaches_the_trace_evidence(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    find(tools_by_name, replacement_color="BROWN")
    assert john.trace.of_type("tool_result")[0]["evidence"]["replacement_color"] == "BROWN"


def test_the_domain_refusing_a_colour_for_a_fit_problem_reaches_the_model_as_an_error(
    domain: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    from returns_domain.errors import DomainError, ErrorCode

    domain.fail("FakeOptions.find_options", DomainError(ErrorCode.VALIDATION_ERROR, "replacement_color only applies"))
    result = find(tools_by_name, replacement_color="BROWN")
    assert result["ok"] is False and result["error"]["code"] == "VALIDATION_ERROR"


# ----------------------------------------------------------------------- needs_choice


def test_a_preference_return_without_a_colour_returns_needs_choice_and_no_exchange(
    john: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    data = find(tools_by_name)["data"]
    assert data["availability"]["reason_no_exchange"] == "NEEDS_COLOR_CHOICE"
    assert [o["type"] for o in data["options"]] == ["return_in_store", "return_mail"]
    colours = data["needs_choice"]["replacement_color"]
    assert [c["color"] for c in colours] == ["BROWN", "TAN", "WHITE"]  # the domain's order without a context
    assert colours[0] == {"color": "BROWN", "total_atp": 7, "nearest_store_minutes": 5}
    assert colours[1]["nearest_store_minutes"] is None
    assert not any("preferred" in c for c in colours)


def test_usual_colours_come_first_and_are_marked_when_the_owner_context_has_them(
    john: FakeDomain, ctx: RunContext, tools_by_name: dict[str, Any]
) -> None:
    ctx.memory_context = owner_context.sanitize(MEMORY)
    colours = find(tools_by_name)["data"]["needs_choice"]["replacement_color"]
    assert [c["color"] for c in colours] == ["WHITE", "BROWN", "TAN"]
    assert [c.get("preferred") for c in colours] == [True, True, None]
    assert colours[1]["total_atp"] == 7  # the domain's numbers are untouched


def test_a_context_colour_that_is_not_in_stock_is_never_offered(
    john: FakeDomain, ctx: RunContext, tools_by_name: dict[str, Any]
) -> None:
    ctx.memory_context = owner_context.sanitize({**MEMORY, "preferred_colors": ["PINK", "NEON", "BROWN"]})
    colours = find(tools_by_name)["data"]["needs_choice"]["replacement_color"]
    assert [c["color"] for c in colours] == ["BROWN", "TAN", "WHITE"]  # only what the domain says is in stock


def test_the_colour_list_is_capped(john: FakeDomain, tools_by_name: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.agent import fakes

    monkeypatch.setattr(fakes, "COLOR_CHOICES", [{"color": f"C{i}", "total_atp": 1, "nearest_store_minutes": None}
                                                 for i in range(12)])
    colours = find(tools_by_name)["data"]["needs_choice"]["replacement_color"]
    assert len(colours) == agent_tools.MAX_COLOR_CHOICES


# --------------------------------------------------------------------------- minutes away


def test_minutes_away_is_passed_through_for_stores_and_absent_elsewhere(
    john: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    options = {o["option_id"]: o for o in find(tools_by_name, replacement_color="BROWN")["data"]["options"]}
    assert options["OPT-2"]["minutes_away"] == 5 and options["OPT-3"]["minutes_away"] == 24
    assert options["OPT-4"]["minutes_away"] == 5  # the in-store return too
    assert "minutes_away" not in options["OPT-1"] and "minutes_away" not in options["OPT-5"]  # DC and mail: absent


def test_nearest_store_leads_the_ranking_and_the_reason_says_how_far(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    ranking = find(tools_by_name, replacement_color="BROWN")["data"]["ranking"]
    assert ranking["order"][:3] == ["OPT-2", "OPT-3", "OPT-1"]  # MTL (5 min), LAVAL (24 min), then the DC
    assert ranking["order"][-2:] == ["OPT-4", "OPT-5"]
    assert "Store pickup of the Brown pair at ALDO Sainte-Catherine is free and ready Sat Oct 17, 5 minutes away" in (
        ranking["reason"]
    )
    assert "Refund options stay available" in ranking["reason"]


# ------------------------------------------------------------------------------- benefit


def test_a_benefit_is_shown_only_when_the_domain_attached_one(
    john: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    plain = find(tools_by_name, replacement_color="BROWN")["data"]["options"]
    assert all("benefit" not in o for o in plain)  # nothing is invented

    perk = {"type": "free_return_shipping", "tier_label": "Platinum member", "saved_cents": 595, "fee_cents": 0}
    john.options.extra_options = [
        _opt("OPT-6", "return_mail", "ALDO Demo Distribution Centre", "DEMO-DC-QC", "2026-10-26", "Mon Oct 26",
             refund=14999)
        .model_copy(update={"benefit": perk})
    ]
    shown = {o["option_id"]: o for o in find(tools_by_name, replacement_color="BROWN")["data"]["options"]}
    assert shown["OPT-6"]["benefit"] == {**perk, "saved_text": "CAD 5.95"}
    assert "benefit" not in shown["OPT-5"] and "benefit" not in shown["OPT-2"]


# --------------------------------------------------------------------------- compact result


def test_the_result_leaves_out_what_the_model_does_not_need(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    data = find(tools_by_name, replacement_color="BROWN")["data"]
    for option in data["options"]:
        for dropped in ("evidence", "source_refs", "provenance", "sku", "replacement_sku", "qty", "currency"):
            assert dropped not in option
    assert "inventory_versions" not in data and "atp_by_location" not in data["availability"]
    assert data["availability"]["dc_atp"] == 4
    assert data["candidate_set_id"] == CAND and data["policy_check_id"] == PCHK


def test_a_very_long_list_is_capped_but_keeps_the_first_option_and_every_refund(
    john: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    john.options.extra_options = [
        _opt(f"OPT-{i}", "exchange_store_pickup", f"ALDO Store {i}", f"DEMO-STORE-X{i}", "2026-10-17", "Sat Oct 17",
             color="BROWN", minutes=100 + i)
        for i in range(6, 16)
    ]
    data = find(tools_by_name, replacement_color="BROWN")["data"]
    ids = [o["option_id"] for o in data["options"]]
    assert len(ids) == agent_tools.MAX_OPTIONS_IN_RESULT
    assert data["ranking"]["order"][0] == "OPT-2" and "OPT-2" in ids  # the first option stays
    assert {"OPT-4", "OPT-5"} <= set(ids)  # so does every refund option
    assert set(data["ranking"]["order"]) == set(ids)  # the order only lists what the model can see


def test_other_sizes_are_capped(domain: FakeDomain, tools_by_name: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    original = domain.options.find_options

    def many(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        return result.model_copy(update={"other_sizes_in_stock": [
            {"size": f"{9 + i / 2:.1f}", "sku": f"S{i}", "total_atp": 1} for i in range(9)
        ]})

    monkeypatch.setattr(domain.options, "find_options", many)
    data = find(tools_by_name)["data"]
    assert len(data["other_sizes_in_stock"]) == agent_tools.MAX_OTHER_SIZES_IN_RESULT


# ------------------------------------------------------------ create_quote: the owner's path


def option_types(domain: FakeDomain) -> list[str]:
    (args, _), = domain.calls_to("FakeQuotes", "create_quote")
    ids = args[-1]
    known = {o.option_id: o.type for o in domain.options.candidates[CAND]}
    return [known[i] for i in ids]


ALL_IDS = ["OPT-1", "OPT-2", "OPT-3", "OPT-4", "OPT-5"]


def test_in_store_puts_the_nearest_pickup_first(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    find(tools_by_name, replacement_color="BROWN")
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=list(reversed(ALL_IDS)),
                                           preferred_channel="store_pickup")
    assert result["ok"] and result["data"]["option_ids"] == ["OPT-2", "OPT-3", "OPT-1", "OPT-4", "OPT-5"]
    assert option_types(john)[0] == "exchange_store_pickup"


def test_by_mail_puts_the_shipped_exchange_first_and_keeps_the_refunds(
    john: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    find(tools_by_name, replacement_color="BROWN")
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=ALL_IDS, preferred_channel="ship")
    assert result["data"]["option_ids"] == ["OPT-1", "OPT-2", "OPT-3", "OPT-4", "OPT-5"]
    assert option_types(john)[0] == "exchange_ship_from_dc"
    assert option_types(john)[-2:] == ["return_in_store", "return_mail"]


def test_a_refund_choice_puts_the_returns_first_and_still_lists_the_exchanges(
    john: FakeDomain, tools_by_name: dict[str, Any]
) -> None:
    find(tools_by_name, replacement_color="BROWN")
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=ALL_IDS, preferred_channel="return")
    assert result["data"]["option_ids"][:2] == ["OPT-4", "OPT-5"]  # in-store before mail-in
    assert len(result["data"]["option_ids"]) == 5


def test_without_a_stated_path_the_default_order_applies(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    find(tools_by_name, replacement_color="BROWN")
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=list(reversed(ALL_IDS)))
    assert result["data"]["option_ids"] == ["OPT-2", "OPT-3", "OPT-1", "OPT-4", "OPT-5"]  # R6.5: store pickup first


def test_the_path_given_to_find_is_kept_for_the_quote(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    find(tools_by_name, replacement_color="BROWN", preferred_channel="ship")
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=ALL_IDS)
    assert result["data"]["option_ids"][0] == "OPT-1"


def test_a_forgotten_refund_option_is_added_never_left_out(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    find(tools_by_name, replacement_color="BROWN")
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=["OPT-2"], preferred_channel="store_pickup")
    assert result["data"]["option_ids"] == ["OPT-2", "OPT-4", "OPT-5"]


def test_no_quote_before_the_owner_named_a_colour(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    find(tools_by_name)  # needs_choice only
    refused = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=["OPT-1", "OPT-2"])
    assert refused["ok"] is False and refused["error"]["code"] == "VALIDATION_ERROR"
    assert "Ask the owner which colour" in refused["error"]["message"]
    assert john.calls_to("FakeQuotes", "create_quote") == []
    still_refused = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=["OPT-1", "OPT-2"],
                                                  preferred_channel="store_pickup")
    assert still_refused["ok"] is False and john.calls_to("FakeQuotes", "create_quote") == []


def test_a_refund_without_a_colour_is_allowed(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    find(tools_by_name)
    result = tools_by_name["create_quote"](candidate_set_id=CAND, option_ids=["OPT-1", "OPT-2"],
                                           preferred_channel="return")
    assert result["ok"] and result["data"]["option_ids"] == ["OPT-1", "OPT-2"]


def test_the_quote_step_in_a_later_run_ranks_from_the_stored_candidate_set(
    john: FakeDomain, domain: FakeDomain
) -> None:
    """Each owner message is its own run: the colour is chosen in one run and the quote made in the next."""
    first = RunContext(run_id="RUN-ONE", conv_id="CONV-TEST000001", customer_id=CUSTOMER, services=john.services)
    tools_one = {t.tool_spec["name"]: t for t in agent_tools.build_tools(first)}
    find(tools_one, replacement_color="BROWN")
    second = RunContext(run_id="RUN-TWO", conv_id="CONV-TEST000001", customer_id=CUSTOMER, services=john.services)
    assert second.offered == {} and second.rank_orders == {}
    tools_two = {t.tool_spec["name"]: t for t in agent_tools.build_tools(second)}
    result = tools_two["create_quote"](candidate_set_id=CAND, option_ids=list(reversed(ALL_IDS)),
                                       preferred_channel="ship")
    assert result["ok"] and result["data"]["option_ids"][0] == "OPT-1"


def test_another_customers_stored_candidate_set_is_not_used_for_ordering(john: FakeDomain, ctx: RunContext) -> None:
    other = RunContext(run_id="RUN-X", conv_id="CONV-X", customer_id="DEMO-CUST-BEN", services=john.services)
    tools_a = {t.tool_spec["name"]: t for t in agent_tools.build_tools(ctx)}
    find(tools_a, replacement_color="BROWN")
    tools_b = {t.tool_spec["name"]: t for t in agent_tools.build_tools(other)}
    tools_b["create_quote"](candidate_set_id=CAND, option_ids=["OPT-5", "OPT-1"], preferred_channel="store_pickup")
    (args, _), = john.calls_to("FakeQuotes", "create_quote")
    assert args[-1] == ["OPT-5", "OPT-1"]  # the model's order, unchanged: nothing was learned from the other's set


# -------------------------------------------------------------------- store notice surfaced


def test_execute_surfaces_the_store_notice_without_names_or_tier(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    john.execution.colour_exchange = True
    data = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-2")["data"]
    assert data["store_notice"] == {
        "store_name": "ALDO Sainte-Catherine",
        "eta_minutes": 5,
        "summary": "ALDO Sainte-Catherine will have your Brown pair ready, John is about 5 minutes away",
    }
    assert data["memory_update_id"] == "MU-5C0A11"


def test_status_surfaces_the_store_notice_and_the_colour(john: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    john.execution.colour_exchange = True
    data = tools_by_name["get_resolution_status"](transaction_id="DEMO-RMA-ABC123")["data"]
    assert set(data["store_notice"]) == {"store_name", "eta_minutes", "summary"}
    assert data["replacement_color"] == "BROWN" and data["original_size"] == "9.5"
    assert data["eta_text"] == "Sat Oct 17"  # what the data says, not "today"
    assert "ready from Sat Oct 17" in data["next_step"]


def test_no_store_notice_for_a_shipped_exchange(domain: FakeDomain, tools_by_name: dict[str, Any]) -> None:
    data = tools_by_name["execute_approved_resolution"](quote_id="QUO-X", option_id="OPT-1")["data"]
    assert data.get("store_notice") is None
