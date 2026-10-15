"""orders.py: ownership-checked reads, the persisted policy check and gateway-placed orders."""

from __future__ import annotations

import json
import re

import pytest

from returns_domain import audit, orders, store
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN, CHLOE, make_agent_order


def pchk_items():
    return list(store.scan_keys(("PCHK#",)))


# -------------------------------------------------------------------------------- reads


@pytest.mark.parametrize(
    ("customer", "expected"),
    [(AVA, ["DEMO-1008", "DEMO-1002", "DEMO-1001"]),
     (BEN, ["DEMO-1010", "DEMO-1005", "DEMO-1003", "DEMO-1004"]),
     (CHLOE, ["DEMO-1007", "DEMO-1009", "DEMO-1006"])],
)
def test_list_orders_is_newest_first_and_only_the_customers(seeded, customer, expected):
    assert [o.order_id for o in orders.list_orders(customer)] == expected


def test_order_view_shape_has_no_customer_identity(seeded):
    view = orders.get_order(AVA, "DEMO-1001")
    body = view.model_dump()
    assert set(body) == {"order_id", "banner", "currency", "channel", "ordered_at", "delivered_at", "lines", "source", "fulfillment"}
    assert (body["banner"], body["currency"], body["channel"], body["delivered_at"]) == ("ALDO_CA", "CAD", "web", "2026-10-04")
    line = body["lines"][0]
    assert set(line) == {"line_id", "sku", "product_name", "style", "size", "color", "purchased_qty",
                         "returned_or_reserved_qty", "unit_paid_cents", "final_sale", "currency", "source"}
    assert (line["size"], line["unit_paid_cents"], line["final_sale"]) == ("7.0", 12999, False)
    assert isinstance(line["unit_paid_cents"], int)
    assert "DEMO-CUST" not in json.dumps(body)


def test_missing_delivery_date_and_quantities_are_visible(seeded):
    assert orders.get_order(CHLOE, "DEMO-1007").delivered_at is None
    line = orders.get_order(AVA, "DEMO-1008").lines[0]
    assert (line.purchased_qty, line.returned_or_reserved_qty) == (2, 1)


def test_get_order_other_customers_order_is_denied_and_audited(seeded):
    with pytest.raises(DomainError) as exc:
        orders.get_order(AVA, "DEMO-1006", run_id="RUN-X")  # Chloe's order
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    denied = audit.list_audit(action="access_denied")
    assert len(denied) == 1 and denied[0]["outcome"] == "denied" and denied[0]["actor"] == AVA
    assert denied[0]["refs"] == {"resource_type": "order", "resource_id": "DEMO-1006", "run_id": "RUN-X"}


def test_get_order_missing_is_order_not_found(seeded):
    with pytest.raises(DomainError) as exc:
        orders.get_order(AVA, "DEMO-9999")
    assert exc.value.code == ErrorCode.ORDER_NOT_FOUND
    assert audit.list_audit(action="access_denied") == []


def test_order_context_payload(seeded):
    context = orders.order_context(AVA, "DEMO-1008")
    assert set(context) == {"order_id", "delivered_at", "ordered_at", "currency", "lines", "source_refs"}
    line = context["lines"][0]
    assert set(line) == {"line_id", "sku", "product_name", "size", "color", "purchased_qty", "returned_or_reserved_qty",
                         "already_returned_qty", "remaining_qty", "unit_paid_cents", "final_sale"}
    assert (line["purchased_qty"], line["already_returned_qty"], line["remaining_qty"]) == (2, 1, 1)
    assert context["source_refs"] == ["ORDER#DEMO-1008/META", "ORDER#DEMO-1008/LINE#DEMO-1008-L1"]
    assert context["delivered_at"] == "2026-10-07" and context["ordered_at"] == "2026-10-03"
    assert "DEMO-CUST" not in json.dumps(context)
    store.json_safe(context)  # JSON-native


def test_order_context_is_owner_checked(seeded):
    with pytest.raises(DomainError) as exc:
        orders.order_context(BEN, "DEMO-1001")
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED


# ------------------------------------------------------------------------------ policy


def check(customer, order_id, line_id, *, qty=1, reason="FIT_SIZE", condition="unworn", direction="too_small", run_id="RUN-1"):
    return orders.check_policy(customer, run_id, order_id, line_id, qty, reason, condition, direction)


def test_hero_check_is_eligible_and_persisted(seeded):
    result = check(AVA, "DEMO-1001", "DEMO-1001-L1")
    assert re.fullmatch(r"PCHK-[0-9A-F]{8}", result.policy_check_id)
    assert result.result["decision"] == "eligible" and result.result["rule_ids"] == ["window.ok"]
    assert result.result["allowed_option_types"] == ["exchange_ship_from_dc", "exchange_ship_from_store",
                                                      "exchange_store_pickup", "return_in_store", "return_mail"]
    assert (result.refund_cents, result.currency, result.qty, result.size_direction) == (12999, "CAD", 1, "too_small")
    assert result.policy_version == "2026.10-demo"
    stored = store.get_item(store.pchk_key(result.policy_check_id))
    assert stored["customer_id"] == AVA and stored["run_id"] == "RUN-1" and stored["condition"] == "unworn"
    assert stored["result"] == result.result and stored["reason_category"] == "FIT_SIZE"
    assert "customer_id" not in result.model_dump()  # never handed to a tool


@pytest.mark.parametrize(
    ("customer", "order", "line", "kwargs", "decision", "rule"),
    [
        (AVA, "DEMO-1002", "DEMO-1002-L1", {"reason": "CHANGE_OF_MIND"}, "ineligible", "final_sale.no_return"),
        (AVA, "DEMO-1002", "DEMO-1002-L1", {"reason": "QUALITY_DAMAGE", "condition": "damaged"}, "needs_review", "final_sale.defect_review"),
        (BEN, "DEMO-1003", "DEMO-1003-L1", {"reason": "CHANGE_OF_MIND"}, "eligible", "window.ok"),  # day 30
        (BEN, "DEMO-1004", "DEMO-1004-L1", {"reason": "CHANGE_OF_MIND"}, "ineligible", "window.expired"),  # day 31
        (BEN, "DEMO-1005", "DEMO-1005-L1", {"reason": "CHANGE_OF_MIND"}, "needs_review", "authority.refund_limit"),
        (CHLOE, "DEMO-1007", "DEMO-1007-L1", {}, "needs_review", "window.missing_delivery_date"),
        (AVA, "DEMO-1008", "DEMO-1008-L1", {"qty": 2}, "ineligible", "qty.exceeds_remaining"),
        (AVA, "DEMO-1001", "DEMO-1001-L1", {"condition": "unknown"}, "needs_information", "condition.unknown"),
        (AVA, "DEMO-1001", "DEMO-1001-L1", {"condition": "worn"}, "ineligible", "condition.worn"),
    ],
)
def test_fixture_orders_hit_the_policy_boundaries(seeded, customer, order, line, kwargs, decision, rule):
    result = check(customer, order, line, **kwargs)
    assert result.result["decision"] == decision and rule in result.result["rule_ids"]
    # every check is persisted, whatever the decision
    assert store.get_item(store.pchk_key(result.policy_check_id))["result"]["decision"] == decision


def test_a_check_is_persisted_even_when_ineligible_and_ids_are_unique(seeded):
    one = check(AVA, "DEMO-1002", "DEMO-1002-L1", reason="CHANGE_OF_MIND")
    two = check(AVA, "DEMO-1002", "DEMO-1002-L1", reason="CHANGE_OF_MIND")
    assert one.policy_check_id != two.policy_check_id and len(pchk_items()) == 2


def test_needs_information_names_the_missing_field(seeded):
    result = check(AVA, "DEMO-1001", "DEMO-1001-L1", condition="unknown")
    assert result.result["missing_fields"] == ["condition"] and result.result["allowed_option_types"] == []


@pytest.mark.parametrize("qty", [0, -1, 1.5, True, "1", None])
def test_bad_quantity_is_rejected_before_anything_is_written(seeded, qty):
    with pytest.raises(DomainError) as exc:
        check(AVA, "DEMO-1001", "DEMO-1001-L1", qty=qty)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and pchk_items() == []


@pytest.mark.parametrize("kwargs", [{"reason": "WHATEVER"}, {"condition": "pristine"}, {"direction": "sideways"}, {"reason": None}])
def test_free_text_vocabularies_are_rejected(seeded, kwargs):
    with pytest.raises(DomainError) as exc:
        check(AVA, "DEMO-1001", "DEMO-1001-L1", **kwargs)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and pchk_items() == []


def test_check_policy_ownership_and_unknown_line(seeded):
    with pytest.raises(DomainError) as exc:
        check(AVA, "DEMO-1006", "DEMO-1006-L1")
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED and pchk_items() == []
    assert len(audit.list_audit(action="access_denied")) == 1
    with pytest.raises(DomainError) as exc:
        check(AVA, "DEMO-1001", "DEMO-1002-L1")  # a line of another order
    assert exc.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(DomainError) as exc:
        check(AVA, "DEMO-9999", "L")
    assert exc.value.code == ErrorCode.ORDER_NOT_FOUND


def test_policy_reads_price_and_date_from_the_store_not_from_the_caller(seeded):
    """Refund and the authority limit come from the stored line; there is no argument to change them."""
    result = check(BEN, "DEMO-1005", "DEMO-1005-L1", reason="CHANGE_OF_MIND")
    assert result.refund_cents == 28900 and result.result["facts"]["refund_cents"] == 28900
    store.update_item(store.line_key("DEMO-1005", "DEMO-1005-L1"), set={"unit_paid_cents": 20000})
    cheaper = check(BEN, "DEMO-1005", "DEMO-1005-L1", reason="CHANGE_OF_MIND")
    assert cheaper.refund_cents == 20000 and cheaper.result["decision"] == "eligible"


def test_get_policy_check_round_trip_and_ownership(seeded):
    created = check(AVA, "DEMO-1001", "DEMO-1001-L1")
    assert orders.get_policy_check(AVA, created.policy_check_id) == created
    with pytest.raises(DomainError) as exc:
        orders.get_policy_check(BEN, created.policy_check_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert audit.list_audit(action="access_denied")[0]["refs"] == {"resource_type": "policy_check", "resource_id": created.policy_check_id}
    with pytest.raises(DomainError) as exc:
        orders.get_policy_check(AVA, "PCHK-MISSING")
    assert exc.value.code == ErrorCode.MISSING_POLICY


def test_latest_policy_check_for_a_line_in_a_run(seeded, fixed_now):
    first = check(AVA, "DEMO-1002", "DEMO-1002-L1", reason="CHANGE_OF_MIND", run_id="RUN-A")
    fixed_now.advance(seconds=5)
    second = check(AVA, "DEMO-1002", "DEMO-1002-L1", reason="QUALITY_DAMAGE", condition="damaged", run_id="RUN-A")
    check(AVA, "DEMO-1001", "DEMO-1001-L1", run_id="RUN-A")
    check(AVA, "DEMO-1002", "DEMO-1002-L1", reason="CHANGE_OF_MIND", run_id="RUN-B")
    assert orders.latest_policy_check(AVA, "RUN-A", "DEMO-1002", "DEMO-1002-L1").policy_check_id == second.policy_check_id
    assert orders.latest_policy_check(AVA, "RUN-A", "DEMO-1001", "DEMO-1001-L1") is not None
    assert orders.latest_policy_check(AVA, "RUN-C", "DEMO-1002", "DEMO-1002-L1") is None
    assert orders.latest_policy_check(BEN, "RUN-A", "DEMO-1002", "DEMO-1002-L1") is None  # not the intruder's
    assert first.policy_check_id != second.policy_check_id


# ------------------------------------------------------------ domain v2: the customer profile


MTL, LAVAL, TOR = "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01"


def test_a_base_persona_is_a_plain_member_with_no_travel_times(seeded):
    assert orders.customer_profile(AVA) == {
        "display_name": "Ava", "home_region": "QC", "tier": "none", "tier_label": "Member",
        "authority_limit_cents": 25000, "travel_minutes": {}}
    assert orders.customer_profile(BEN)["home_region"] == "ON"


@pytest.mark.parametrize(
    ("tier", "label", "limit"),
    [("none", "Member", 25000), ("silver", "Silver member", 25000), ("platinum", "Platinum member", 75000),
     ("Platinum", "Platinum member", 75000), (" silver ", "Silver member", 25000)],
)
def test_the_profile_reads_the_tier_from_the_customer_record(seeded, tier, label, limit):
    store.update_item(store.customer_key(AVA), set={"tier": tier})
    profile = orders.customer_profile(AVA)
    assert (profile["tier"], profile["tier_label"], profile["authority_limit_cents"]) == (tier.strip().lower(), label, limit)


@pytest.mark.parametrize("stored", ["diamond", "", None, 7, ["platinum"]])
def test_an_unknown_or_unusable_tier_is_a_plain_member(seeded, stored):
    store.update_item(store.customer_key(AVA), set={"tier": stored})
    profile = orders.customer_profile(AVA)
    assert (profile["tier"], profile["tier_label"], profile["authority_limit_cents"]) == ("none", "Member", 25000)


def test_the_profile_reads_the_travel_minutes_per_store(seeded):
    store.update_item(store.customer_key(AVA), set={"travel_minutes": {MTL: 5, LAVAL: 24, TOR: 540}})
    profile = orders.customer_profile(AVA)
    assert profile["travel_minutes"] == {MTL: 5, LAVAL: 24, TOR: 540}
    assert all(type(minutes) is int for minutes in profile["travel_minutes"].values())  # never Decimal
    store.update_item(store.customer_key(AVA), set={"travel_minutes": {MTL: 0}})
    assert orders.customer_profile(AVA)["travel_minutes"] == {MTL: 0}  # 0 minutes is a known distance


@pytest.mark.parametrize(
    "stored",
    [None, "far", 5, [5, 24], {}, {MTL: -5}, {MTL: True}, {MTL: "5"}, {MTL: 5.5}, {MTL: None}],
)
def test_unusable_travel_minutes_mean_unknown_not_an_error(seeded, stored):
    store.update_item(store.customer_key(AVA), set={"travel_minutes": stored})
    assert orders.customer_profile(AVA)["travel_minutes"] == {}


def test_a_customer_without_a_record_is_the_default_profile(ddb):
    assert orders.customer_profile("DEMO-CUST-NOBODY") == {
        "display_name": None, "home_region": None, "tier": "none", "tier_label": "Member",
        "authority_limit_cents": 25000, "travel_minutes": {}}


def test_the_profile_holds_no_customer_id_and_is_json_safe(seeded):
    store.update_item(store.customer_key(AVA), set={"tier": "platinum", "travel_minutes": {MTL: 5}})
    profile = orders.customer_profile(AVA)
    assert store.json_safe(profile) == profile and "DEMO-CUST" not in json.dumps(profile)
    assert set(profile) == {"display_name", "home_region", "tier", "tier_label", "authority_limit_cents", "travel_minutes"}


def test_the_tier_labels_and_limits_follow_policy_json(seeded, tmp_path, monkeypatch):
    from returns_domain import fixtures

    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "policy.json").write_text(json.dumps(
        {"tiers": {"gold": {"label": "Gold member", "authority_refund_limit_cents": 50000}}}))
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path))
    fixtures.clear_cache()
    store.update_item(store.customer_key(AVA), set={"tier": "gold"})
    profile = orders.customer_profile(AVA)
    assert (profile["tier"], profile["tier_label"], profile["authority_limit_cents"]) == ("gold", "Gold member", 50000)
    monkeypatch.setenv("FIXTURES_DIR", str(tmp_path / "nowhere"))  # no policy file at all: the constants apply
    fixtures.clear_cache()
    store.update_item(store.customer_key(AVA), set={"tier": "platinum"})
    assert orders.customer_profile(AVA)["authority_limit_cents"] == 75000


def test_policy_checks_use_the_tier_limit_and_report_it(seeded):
    """The CAD 289.00 boot: over the default limit, within a platinum member's."""
    plain = check(BEN, "DEMO-1005", "DEMO-1005-L1", reason="CHANGE_OF_MIND")
    assert plain.result["decision"] == "needs_review" and plain.result["facts"]["authority_limit_cents"] == 25000
    store.update_item(store.customer_key(BEN), set={"tier": "silver"})
    assert check(BEN, "DEMO-1005", "DEMO-1005-L1", reason="CHANGE_OF_MIND").result["decision"] == "needs_review"
    store.update_item(store.customer_key(BEN), set={"tier": "platinum"})
    rich = check(BEN, "DEMO-1005", "DEMO-1005-L1", reason="CHANGE_OF_MIND")
    assert rich.result["decision"] == "eligible" and rich.result["facts"]["authority_limit_cents"] == 75000
    assert rich.refund_cents == 28900 and rich.result["rule_ids"] == ["window.ok"]
    # the preference reason allows exchanges now; the tier does not change what is allowed
    assert rich.result["allowed_option_types"] == ["exchange_ship_from_dc", "exchange_ship_from_store",
                                                    "exchange_store_pickup", "return_in_store", "return_mail"]


# ------------------------------------------------- orders placed by the agent-to-agent purchase


def test_agent_orders_appear_in_list_orders_newest_first(seeded):
    make_agent_order("DEMO-A2A-81B20E")
    listed = orders.list_orders(AVA)
    assert [o.order_id for o in listed] == ["DEMO-A2A-81B20E", "DEMO-1008", "DEMO-1002", "DEMO-1001"]
    agent = listed[0]
    assert (agent.channel, agent.delivered_at, agent.ordered_at) == ("agent", None, "2026-10-16")
    assert agent.fulfillment == {"type": "store_pickup", "location_id": "DEMO-STORE-MTL-01",
                                 "location_name": "ALDO Demo Store Montreal", "eta_date": "2026-10-17"}
    assert agent.lines[0].line_id == "DEMO-A2A-81B20E-L1" and agent.lines[0].size == "8.0"
    assert all(o.fulfillment is None and o.channel == "web" for o in listed[1:])


def test_agent_orders_work_with_get_order_context_and_ownership(seeded):
    make_agent_order("DEMO-A2A-81B20E")
    assert orders.get_order(AVA, "DEMO-A2A-81B20E").channel == "agent"
    context = orders.order_context(AVA, "DEMO-A2A-81B20E")
    assert context["delivered_at"] is None and context["lines"][0]["remaining_qty"] == 1
    with pytest.raises(DomainError) as exc:
        orders.get_order(BEN, "DEMO-A2A-81B20E")
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert orders.list_orders(BEN)[0].order_id != "DEMO-A2A-81B20E"


def test_agent_order_policy_waits_for_the_simulated_delivery(seeded):
    make_agent_order("DEMO-A2A-81B20E")
    before = check(AVA, "DEMO-A2A-81B20E", "DEMO-A2A-81B20E-L1")
    assert before.result["decision"] == "needs_review" and "window.missing_delivery_date" in before.result["rule_ids"]
    store.update_item(store.order_key("DEMO-A2A-81B20E"), set={"delivered_at": "2026-10-16"})  # simulate-delivery
    after = check(AVA, "DEMO-A2A-81B20E", "DEMO-A2A-81B20E-L1")
    assert after.result["decision"] == "eligible" and after.result["facts"]["days_since_delivery"] == 0
    assert after.refund_cents == 12999
