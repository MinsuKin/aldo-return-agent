"""Domain v2 against the SHIPPED fixtures (``fixtures/demo/*_v2.json``, PIVOT_ADDENDUM 11.3).

``tests/test_options_colour.py`` and ``tests/test_execution_v2.py`` prove the code against test
builders; this file proves that the real files written by the fixtures v2 stream satisfy the
contract the code relies on: John and David, the men's style with BLACK / BROWN / TAN, the stock
in size 9.5 and the tiers of policy.json. It skips itself when the v2 files do not exist.
"""

from __future__ import annotations

import pytest

from returns_domain import execution, fixtures, options, orders, quotes, store, trace
from tests.conftest import FIXTURES_ROOT, Flow, HeroQuote, make_agent_order

pytestmark = [
    pytest.mark.usefixtures("no_flush"),
    pytest.mark.skipif(not (FIXTURES_ROOT / "demo" / "customers_v2.json").exists(), reason="fixtures v2 not generated yet"),
]

JOHN, DAVID = "DEMO-CUST-JOHN", "DEMO-CUST-DAVID"
MTL, LAVAL, TOR, DC = "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01", "DEMO-DC-QC"
MEN_STYLE = "DEMO-STYLE-MEN001"
BLACK_95, BROWN_95, TAN_95 = ("DEMO-SKU-MEN001-%s-095" % color for color in ("BLACK", "BROWN", "TAN"))
ORDER, LINE = "DEMO-A2A-5A1B2C", "DEMO-A2A-5A1B2C-L1"


@pytest.fixture
def john(seeded):
    """John after the purchase of the black men's shoe in 9.5 (placed through ALDO bot, delivered today)."""
    make_agent_order(ORDER, JOHN, sku=BLACK_95, size="9.5", delivered_at="2026-10-16", price_cents=14999)
    return Flow()


def find(customer, color=None, *, order=ORDER, line=LINE, reason="CHANGE_OF_MIND"):
    check = orders.check_policy(customer, "RUN-1", order, line, 1, reason, "unworn", "unknown")
    return options.find_options(customer, check.policy_check_id, None, None, None, color, run_id="RUN-1")


# ------------------------------------------------------------------------ the people


def test_john_is_a_silver_member_five_minutes_from_the_montreal_store(seeded):
    profile = orders.customer_profile(JOHN)
    assert profile == {
        "display_name": "John", "home_region": "QC", "tier": "silver", "tier_label": "Silver member",
        "authority_limit_cents": 25000, "travel_minutes": {MTL: 5, LAVAL: 24, TOR: 540}}


def test_david_is_a_platinum_member_far_from_every_store(seeded):
    profile = orders.customer_profile(DAVID)
    assert (profile["tier"], profile["tier_label"], profile["authority_limit_cents"], profile["home_region"]) == (
        "platinum", "Platinum member", 75000, "NB")
    assert set(profile["travel_minutes"]) == {MTL, LAVAL, TOR} and min(profile["travel_minutes"].values()) >= 150


def test_the_base_personas_are_plain_members_with_no_travel_times(seeded):
    for customer in ("DEMO-CUST-AVA", "DEMO-CUST-BEN", "DEMO-CUST-CHLOE"):
        profile = orders.customer_profile(customer)
        assert (profile["tier"], profile["tier_label"], profile["authority_limit_cents"], profile["travel_minutes"]) == (
            "none", "Member", 25000, {})


def test_the_policy_json_tiers_and_exchange_reasons_are_what_the_code_falls_back_to(seeded):
    from returns_domain import policy

    assert policy.exchange_reasons() == policy.EXCHANGE_REASONS == ("FIT_SIZE", "EXPECTATION", "CHANGE_OF_MIND")
    tiers = policy.load_tiers()
    assert {name: (t["label"], t["authority_refund_limit_cents"]) for name, t in tiers.items()} == {
        name: (t["label"], t["authority_refund_limit_cents"]) for name, t in policy.TIERS_DEFAULT.items()}
    assert tiers["platinum"]["benefits"]["free_return_shipping"] == {
        "per_month": 1, "applies_to": ["return_mail"], "waives_fee": True}  # phase 2 reads this


# ------------------------------------------------------------------- John's colour change


def test_the_shipped_style_has_the_three_colours_and_the_default_stock_in_size_95(seeded):
    products = [p for p in fixtures.load_bundle().products if p["style"] == MEN_STYLE]
    assert sorted({p["color"] for p in products}) == ["BLACK", "BROWN", "TAN"] and "9.5" in {p["size"] for p in products}
    stock = {sku: [store.get_item(store.inventory_key(sku, loc))["atp"] for loc in (DC, MTL, LAVAL, TOR)]
             for sku in (BLACK_95, BROWN_95, TAN_95)}
    assert stock == {BLACK_95: [3, 0, 1, 1], BROWN_95: [4, 2, 1, 0], TAN_95: [2, 0, 0, 1]}


def test_without_a_colour_john_is_offered_brown_and_tan(john):
    cand = find(JOHN)
    assert [o.type for o in cand.options] == ["return_in_store", "return_mail"]
    assert cand.needs_choice == {"replacement_color": [
        {"color": "BROWN", "total_atp": 7, "nearest_store_minutes": 5},
        {"color": "TAN", "total_atp": 3, "nearest_store_minutes": None}]}
    assert cand.availability["replacement_size"] == "9.5" and cand.availability["reason_no_exchange"] == "NEEDS_COLOR_CHOICE"
    assert [o for o in cand.options if o.type == "return_in_store"][0].minutes_away == 5


def test_brown_in_his_size_is_a_store_pickup_five_minutes_away(john):
    cand = find(JOHN, "Brown")
    summary = [(o.type, o.location_id, o.minutes_away) for o in cand.options]
    assert summary == [
        ("exchange_ship_from_dc", DC, None),
        ("exchange_ship_from_store", MTL, 5),
        ("exchange_store_pickup", MTL, 5),
        ("exchange_store_pickup", LAVAL, 24),
        ("return_in_store", MTL, 5),
        ("return_mail", DC, None)]
    for option in cand.options[:4]:
        assert (option.sku, option.replacement_sku, option.replacement_color, option.replacement_size) == (
            BROWN_95, BROWN_95, "BROWN", "9.5")
        assert (option.refund_cents, option.fee_cents) == (0, 0)
    assert cand.options[2].eta_date == "2026-10-17" and cand.options[4].refund_cents == 14999


def test_the_whole_colour_exchange_in_store_with_the_shipped_fixtures(john):
    conv_id, run_id = john.start_run(JOHN)
    check = orders.check_policy(JOHN, run_id, ORDER, LINE, 1, "CHANGE_OF_MIND", "unworn", "unknown")
    assert check.result["decision"] == "eligible" and "exchange_store_pickup" in check.result["allowed_option_types"]
    cand = options.find_options(JOHN, check.policy_check_id, None, None, None, "BROWN", run_id=run_id)
    ids = [o.option_id for kind in ("exchange_store_pickup", "return_in_store", "return_mail") for o in cand.options if o.type == kind]
    quote = quotes.create_quote(JOHN, run_id, conv_id, cand.candidate_set_id, ids)
    hq = HeroQuote(JOHN, conv_id, run_id, check, cand, quote)
    john.approve(hq, "exchange_store_pickup")
    result = execution.execute(JOHN, quote.quote_id, run_id=run_id)

    assert result.option_type == "exchange_store_pickup" and result.store_notice == {
        "store_id": MTL, "store_name": "ALDO Sainte-Catherine", "display_name": "John", "tier_label": "Silver member",
        "eta_minutes": 5,
        "summary": "ALDO Sainte-Catherine will have your Brown pair ready, John is about 5 minutes away"}
    txn = store.get_item(store.txn_key(result.transaction_id))
    assert (txn["sku"], txn["replacement_color"], txn["replacement_size"], txn["memory_reason"]) == (BROWN_95, "BROWN", "9.5", "PREFERENCE")
    update = store.get_item(store.outbox_key(result.memory_update_id))["payload"]
    assert (update["reason"], update["fit_feedback"], update["original_size"], update["new_size"], update["provisional"]) == (
        "PREFERENCE", "size_ok", "9.5", "9.5", True)
    assert store.get_item(store.inventory_key(BROWN_95, MTL))["atp"] == 1
    assert [(e["type"], e["actor"]) for e in trace.list_a2a_events(JOHN).events] == [
        ("quote_created", "aldo_agent"), ("transaction_created", "aldo_agent"), ("store_notified", "aldo_store")]
    assert trace.list_a2a_events(JOHN).events[0]["payload"]["primary_option_type"] == "exchange_store_pickup"


@pytest.mark.parametrize("variant", ["dc_stockout", "all_stockout"])
def test_the_stockout_variants_do_not_break_the_colour_choice(john, variant):
    """The variants only touch BLACK 9.5, the pair John owns; the colours he can swap to are unchanged."""
    fixtures.reset(variant)
    make_agent_order(ORDER, JOHN, sku=BLACK_95, size="9.5", delivered_at="2026-10-16", price_cents=14999)
    cand = find(JOHN)
    assert [c["color"] for c in cand.needs_choice["replacement_color"]] == ["BROWN", "TAN"]
    assert [o.type for o in find(JOHN, "TAN").options] == ["exchange_ship_from_dc", "exchange_ship_from_store", "return_in_store", "return_mail"]


# ------------------------------------------------------------------------ David


def test_davids_ten_item_order_passes_the_policy_with_the_platinum_limit(seeded):
    check = orders.check_policy(DAVID, "RUN-D", "DEMO-3001", "DEMO-3001-L2", 1, "CHANGE_OF_MIND", "unworn", "unknown")
    assert check.result["decision"] == "eligible" and check.result["facts"]["authority_limit_cents"] == 75000
    assert check.result["facts"]["days_since_delivery"] == 3 and check.refund_cents == 17999
    assert "exchange_ship_from_dc" in check.result["allowed_option_types"]  # a preference reason allows exchanges


def test_davids_collection_styles_come_in_one_colour_and_he_has_no_store_to_visit(seeded):
    cand = find(DAVID, order="DEMO-3001", line="DEMO-3001-L2")  # a preference reason on the tan boot
    assert cand.needs_choice == {"replacement_color": []}
    assert cand.availability["reason_no_exchange"] == "NO_OTHER_COLOR"
    assert [o.type for o in cand.options] == ["return_in_store", "return_mail"]  # the refund options stay visible
    mail = cand.options[-1]
    assert (mail.type, mail.refund_cents, mail.fee_cents) == ("return_mail", 17999 - 595, 595)  # the fee the platinum perk will waive
    assert "minutes_away" not in mail.model_dump()
