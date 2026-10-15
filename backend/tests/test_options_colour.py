"""options.py, domain v2: colour exchange for a preference reason, ``needs_choice`` and ``minutes_away``.

Facts only (the ranking stays in the agent). John is a silver member 5 / 24 / 540 minutes from the
Montreal / Laval / Toronto stores; his black men's shoe in size 9.5 was delivered today. The world
comes from test builders (``tests/domain_v2.py``), not from the shipped v2 fixtures.
"""

from __future__ import annotations

import pytest

from returns_domain import fixtures, options, orders, quotes, store
from returns_domain.errors import DomainError, ErrorCode
from returns_domain.options import CandidateSet, Option
from tests.conftest import AVA, BEN
from tests.domain_v2 import DAVID, DC, JOHN, LAVAL, MEN_STYLE, MTL, TOR, john_order, men_sku, v2_world  # noqa: F401

BROWN_95, TAN_95, BLACK_95 = men_sku("BROWN", "9.5"), men_sku("TAN", "9.5"), men_sku("BLACK", "9.5")
ORDER, LINE = "DEMO-A2A-5A1B2C", "DEMO-A2A-5A1B2C-L1"


def find(customer=JOHN, *, reason="CHANGE_OF_MIND", color=None, size=None, condition="unworn", direction="unknown",
         deadline=None, order=ORDER, line=LINE, run_id="RUN-1") -> CandidateSet:
    check = orders.check_policy(customer, run_id, order, line, 1, reason, condition, direction)
    return options.find_options(customer, check.policy_check_id, size, deadline, None, color, run_id=run_id)


def types(cand):
    return [o.type for o in cand.options]


def dump(cand):
    return [o.model_dump() for o in cand.options]


@pytest.fixture
def john(v2_world):
    john_order(ORDER)
    return JOHN


# ------------------------------------------------------------------- the world builders


def test_the_test_world_matches_the_contract(john):
    profile = orders.customer_profile(JOHN)
    assert (profile["tier"], profile["tier_label"], profile["travel_minutes"]) == (
        "silver", "Silver member", {MTL: 5, LAVAL: 24, TOR: 540})
    stock = {(sku, loc): store.get_item(store.inventory_key(sku, loc))["atp"]
             for sku in (BLACK_95, BROWN_95, TAN_95) for loc in (DC, MTL, LAVAL, TOR)}
    assert [stock[BLACK_95, loc] for loc in (DC, MTL, LAVAL, TOR)] == [3, 0, 1, 1]
    assert [stock[BROWN_95, loc] for loc in (DC, MTL, LAVAL, TOR)] == [4, 2, 1, 0]
    assert [stock[TAN_95, loc] for loc in (DC, MTL, LAVAL, TOR)] == [2, 0, 0, 1]


# --------------------------------------------------------- a preference reason needs a colour


@pytest.mark.parametrize("reason", ["CHANGE_OF_MIND", "EXPECTATION"])
def test_without_a_colour_there_are_no_exchange_options_but_a_list_of_colours_to_ask_about(john, reason):
    cand = find(reason=reason)
    assert types(cand) == ["return_in_store", "return_mail"]  # the refund options stay visible
    assert cand.needs_choice == {
        "replacement_color": [
            {"color": "BROWN", "total_atp": 7, "nearest_store_minutes": 5},  # Montreal has it, 5 minutes away
            {"color": "TAN", "total_atp": 3, "nearest_store_minutes": None},  # only the DC and Toronto: no pickup nearby
        ]
    }
    assert cand.availability == {
        "replacement_size": "9.5", "replacement_sku": None, "replacement_color": None, "atp_by_location": {},
        "dc_atp": 0, "reason_no_exchange": "NEEDS_COLOR_CHOICE"}
    assert cand.inventory_versions == {} and cand.other_sizes_in_stock == []
    assert "needs_choice" in cand.model_dump()
    assert store.get_item(store.cand_key(cand.candidate_set_id))["needs_choice"] == cand.needs_choice


def test_the_current_colour_is_not_offered_and_sold_out_colours_are_left_out(john):
    for location in (DC, MTL, LAVAL):
        store.update_item(store.inventory_key(TAN_95, location), set={"atp": 0})
    store.update_item(store.inventory_key(TAN_95, TOR), set={"atp": 0})
    choices = find().needs_choice["replacement_color"]
    assert [c["color"] for c in choices] == ["BROWN"]  # BLACK is what he owns, TAN has no stock left


def test_a_colour_in_stock_only_where_no_pickup_is_possible_has_no_store_minutes(john):
    store.update_item(store.inventory_key(BROWN_95, MTL), set={"atp": 0})
    store.update_item(store.inventory_key(BROWN_95, LAVAL), set={"atp": 0})
    brown = next(c for c in find().needs_choice["replacement_color"] if c["color"] == "BROWN")
    assert brown == {"color": "BROWN", "total_atp": 4, "nearest_store_minutes": None}  # the DC has 4, no home store has any


def test_nearest_store_minutes_is_the_nearest_home_region_store_that_has_the_colour(john):
    store.update_item(store.inventory_key(BROWN_95, MTL), set={"atp": 0})  # Montreal (5) is out, Laval (24) still has 1
    brown = next(c for c in find().needs_choice["replacement_color"] if c["color"] == "BROWN")
    assert brown["nearest_store_minutes"] == 24 and brown["total_atp"] == 5


def test_no_colour_in_stock_gives_an_empty_choice_and_says_so(john):
    for sku in (BROWN_95, TAN_95):
        for location in (DC, MTL, LAVAL, TOR):
            store.update_item(store.inventory_key(sku, location), set={"atp": 0})
    cand = find()
    assert cand.needs_choice == {"replacement_color": []}
    assert cand.availability["reason_no_exchange"] == "OUT_OF_STOCK_EVERYWHERE" and types(cand) == ["return_in_store", "return_mail"]


def test_a_style_that_only_comes_in_one_colour_has_nothing_to_choose(seeded):
    """Ava's hero style is sold in black only: the refund options are all there is, and the reason says so."""
    check = orders.check_policy(AVA, "RUN-1", "DEMO-1001", "DEMO-1001-L1", 1, "CHANGE_OF_MIND", "unworn", "unknown")
    cand = options.find_options(AVA, check.policy_check_id, run_id="RUN-1")
    assert types(cand) == ["return_in_store", "return_mail"]
    assert cand.needs_choice == {"replacement_color": []}
    assert cand.availability["reason_no_exchange"] == "NO_OTHER_COLOR" and cand.availability["replacement_size"] == "7.0"
    asked = options.find_options(AVA, check.policy_check_id, None, None, None, "Brown", run_id="RUN-1")
    assert types(asked) == ["return_in_store", "return_mail"] and asked.availability["reason_no_exchange"] == "NO_SUCH_COLOR"
    assert asked.needs_choice == {"replacement_color": []}


def test_a_customer_without_travel_times_still_gets_the_colours_but_no_minutes(v2_world):
    john_order("DEMO-A2A-AAAAAA", customer_id=AVA)
    cand = find(AVA, order="DEMO-A2A-AAAAAA", line="DEMO-A2A-AAAAAA-L1")
    assert cand.needs_choice["replacement_color"] == [
        {"color": "BROWN", "total_atp": 7, "nearest_store_minutes": None},
        {"color": "TAN", "total_atp": 3, "nearest_store_minutes": None}]


def test_david_far_from_every_store_has_no_pickup_minutes_for_any_colour(v2_world):
    john_order("DEMO-A2A-DDDDDD", customer_id=DAVID)
    cand = find(DAVID, order="DEMO-A2A-DDDDDD", line="DEMO-A2A-DDDDDD-L1")
    assert [c["nearest_store_minutes"] for c in cand.needs_choice["replacement_color"]] == [None, None]  # NB has no store


# ----------------------------------------------------------------- with a colour


def test_with_a_colour_the_exchange_is_the_same_style_and_size_in_that_colour(john):
    cand = find(color="BROWN")
    assert types(cand) == ["exchange_ship_from_dc", "exchange_ship_from_store", "exchange_store_pickup",
                           "exchange_store_pickup", "return_in_store", "return_mail"]
    assert [o.option_id for o in cand.options] == [f"OPT-{n}" for n in range(1, 7)]
    dc, ship_store, pickup_mtl, pickup_laval, in_store, mail = cand.options
    for option in (dc, ship_store, pickup_mtl, pickup_laval):
        assert (option.replacement_size, option.sku, option.replacement_sku, option.replacement_color) == (
            "9.5", BROWN_95, BROWN_95, "BROWN")
        assert (option.qty, option.refund_cents, option.fee_cents) == (1, 0, 0)
    assert [o.location_id for o in (dc, ship_store, pickup_mtl, pickup_laval)] == [DC, MTL, MTL, LAVAL]
    # the returns are for the pair he owns, not for the replacement colour
    assert in_store.sku == mail.sku == BLACK_95 and in_store.replacement_color is None and in_store.replacement_sku is None
    assert (in_store.refund_cents, mail.refund_cents, mail.fee_cents) == (14999, 14404, 595)
    assert cand.needs_choice is None and "needs_choice" not in cand.model_dump()
    assert cand.availability == {
        "replacement_size": "9.5", "replacement_sku": BROWN_95, "replacement_color": "BROWN", "dc_atp": 4,
        "atp_by_location": {DC: 4, LAVAL: 1, MTL: 2, TOR: 0}, "reason_no_exchange": None}
    assert cand.inventory_versions == {f"{BROWN_95}|{DC}": 1, f"{BROWN_95}|{MTL}": 1, f"{BROWN_95}|{LAVAL}": 1}


def test_the_colour_is_matched_ignoring_case_and_spaces(john):
    reference = types(find(color="BROWN"))
    for spelling in ("brown", " Brown ", "bRoWn"):
        cand = find(color=spelling)
        assert types(cand) == reference and {o.replacement_color for o in cand.options if o.type.startswith("exchange")} == {"BROWN"}


def test_a_blank_colour_is_no_colour(john):
    assert find(color="   ").needs_choice is not None and find(color="").needs_choice is not None


def test_tan_has_no_pickup_because_the_only_store_with_it_is_outside_his_region(john):
    cand = find(color="TAN")
    assert types(cand) == ["exchange_ship_from_dc", "exchange_ship_from_store", "return_in_store", "return_mail"]
    ship_store = cand.options[1]
    assert (ship_store.location_id, ship_store.sku, ship_store.replacement_color, ship_store.minutes_away) == (TOR, TAN_95, "TAN", 540)


def test_the_distance_only_orders_and_breaks_ties_it_never_adds_or_removes_options(john):
    near = find(color="BROWN")
    store.update_item(store.customer_key(JOHN), set={"travel_minutes": {}})
    unknown = find(color="BROWN")
    assert types(unknown) == types(near)
    pickups = lambda cand: [o.location_id for o in cand.options if o.type == "exchange_store_pickup"]  # noqa: E731
    assert pickups(near) == [MTL, LAVAL] and pickups(unknown) == [LAVAL, MTL]  # nearest first / the old order: by id
    ship_store = lambda cand: next(o.location_id for o in cand.options if o.type == "exchange_ship_from_store")  # noqa: E731
    assert ship_store(near) == MTL and ship_store(unknown) == LAVAL  # equal ship days: nearest, else lowest id


def test_the_same_colour_unknown_colours_and_bad_values_are_refused_or_explained(john):
    with pytest.raises(DomainError) as exc:  # he already owns black
        find(color="Black")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "differ" in exc.value.message
    cand = find(color="Navy")  # not a colour of this style: explained, with the real colours to ask about
    assert types(cand) == ["return_in_store", "return_mail"]
    assert cand.availability["reason_no_exchange"] == "NO_SUCH_COLOR"
    assert [c["color"] for c in cand.needs_choice["replacement_color"]] == ["BROWN", "TAN"]
    for bad in (5, ["BROWN"], "x" * 41):
        with pytest.raises(DomainError) as exc:
            find(color=bad)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_a_colour_with_no_such_size_keeps_the_returns_and_says_so(john):
    products = fixtures.load_bundle().products
    assert not any(p["style"] == MEN_STYLE and p["size"] == "12.0" for p in products)
    john_order("DEMO-A2A-BBBBBB", size="10.0")
    store.update_item(store.line_key("DEMO-A2A-BBBBBB", "DEMO-A2A-BBBBBB-L1"), set={"size": "12.0"})  # a size the style never had
    cand = find(order="DEMO-A2A-BBBBBB", line="DEMO-A2A-BBBBBB-L1", color="BROWN")
    assert types(cand) == ["return_in_store", "return_mail"]
    assert cand.availability["reason_no_exchange"] == "NO_SUCH_SIZE" and cand.availability["replacement_sku"] is None


def test_a_colour_that_is_out_of_stock_everywhere_keeps_the_returns(john):
    for location in (DC, MTL, LAVAL, TOR):
        store.update_item(store.inventory_key(BROWN_95, location), set={"atp": 0})
    cand = find(color="BROWN")
    assert types(cand) == ["return_in_store", "return_mail"]
    assert cand.availability["reason_no_exchange"] == "OUT_OF_STOCK_EVERYWHERE" and cand.inventory_versions == {}


def test_a_colour_exchange_keeps_the_size(john):
    assert types(find(color="BROWN", size="9.5")) == types(find(color="BROWN"))  # the current size is fine
    with pytest.raises(DomainError) as exc:
        find(color="BROWN", size="10.0")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "keeps the current size" in exc.value.message
    for bad in ("abc", "9.25"):
        with pytest.raises(DomainError):
            find(color="BROWN", size=bad)


def test_the_candidate_set_is_persisted_with_the_colour_fields(john):
    cand = find(color="BROWN")
    stored = store.get_item(store.cand_key(cand.candidate_set_id))
    pickup = next(o for o in stored["options"] if o["type"] == "exchange_store_pickup")
    assert (pickup["replacement_color"], pickup["replacement_sku"], pickup["minutes_away"]) == ("BROWN", BROWN_95, 5)
    assert "needs_choice" not in stored  # nothing to choose any more
    assert stored["availability"]["replacement_color"] == "BROWN"


# ---------------------------------------------- the colour only belongs to a preference reason


def test_a_fit_problem_changes_the_size_not_the_colour(john):
    with pytest.raises(DomainError) as exc:
        find(reason="FIT_SIZE", direction="too_small", color="BROWN")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert "EXPECTATION or CHANGE_OF_MIND" in exc.value.message and "leave it out" in exc.value.message
    assert find(reason="FIT_SIZE", direction="too_small", color="black").availability["replacement_size"] == "10.0"  # a no-op


def test_other_reasons_refuse_a_new_colour_too(john):
    with pytest.raises(DomainError) as exc:
        find(reason="COMFORT", color="BROWN")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    cand = find(reason="COMFORT")
    assert types(cand) == ["return_in_store", "return_mail"] and cand.availability["reason_no_exchange"] == "NOT_FIT_REASON"
    assert cand.needs_choice is None


def test_fit_size_options_are_exactly_what_they_were(john):
    """A fit problem for a customer with travel times: the same size options; only the distances are new."""
    cand = find(reason="FIT_SIZE", direction="too_small", deadline="2026-10-17")
    assert cand.availability["replacement_size"] == "10.0" and cand.needs_choice is None
    assert types(cand) == ["exchange_ship_from_dc", "return_in_store", "return_mail"]  # size 10.0 only exists at the DC
    for option in dump(cand):
        assert "replacement_color" not in option and "replacement_sku" not in option


def test_a_customer_without_travel_times_sees_exactly_the_old_option_shape(seeded):
    check = orders.check_policy(AVA, "RUN-1", "DEMO-1001", "DEMO-1001-L1", 1, "FIT_SIZE", "unworn", "too_small")
    cand = options.find_options(AVA, check.policy_check_id, None, "2026-10-17", run_id="RUN-1")
    assert not any(name in option for option in dump(cand) for name in ("minutes_away", "replacement_color", "replacement_sku"))
    assert "needs_choice" not in cand.model_dump()
    assert set(dump(cand)[0]) == {
        "option_id", "type", "sku", "replacement_size", "location_id", "location_name", "qty", "refund_cents", "fee_cents",
        "currency", "eta_date", "eta_text", "meets_deadline", "conditions", "evidence", "source_refs", "provenance"}
    stored = store.get_item(store.cand_key(cand.candidate_set_id))
    assert "needs_choice" not in stored and "replacement_color" not in stored["availability"]


def test_ben_in_ontario_gets_the_toronto_store_for_a_colour_change(v2_world):
    john_order("DEMO-A2A-CCCCCC", customer_id=BEN, color="BLACK")
    store.update_item(store.customer_key(BEN), set={"travel_minutes": {TOR: 12}})
    cand = find(BEN, order="DEMO-A2A-CCCCCC", line="DEMO-A2A-CCCCCC-L1", color="TAN")
    pickup = next(o for o in cand.options if o.type == "exchange_store_pickup")
    assert (pickup.location_id, pickup.minutes_away, pickup.replacement_color) == (TOR, 12, "TAN")


# ----------------------------------------------------------------- minutes_away and the order


def test_every_store_option_has_minutes_away_and_the_others_do_not(john):
    cand = find(color="BROWN")
    minutes = {(o.type, o.location_id): o.minutes_away for o in cand.options}
    assert minutes == {
        ("exchange_ship_from_dc", DC): None,
        ("exchange_ship_from_store", MTL): 5,
        ("exchange_store_pickup", MTL): 5,
        ("exchange_store_pickup", LAVAL): 24,
        ("return_in_store", MTL): 5,
        ("return_mail", DC): None,
    }
    for option in dump(cand):  # unknown means ABSENT, never null
        assert ("minutes_away" in option) == (option["location_id"] != DC)
        assert option.get("minutes_away", 0) is not None


def test_store_options_are_nearest_first_then_by_location_id(john):
    store.update_item(store.customer_key(JOHN), set={"travel_minutes": {MTL: 30, LAVAL: 10, TOR: 540}})
    cand = find(color="BROWN")
    assert [o.location_id for o in cand.options if o.type == "exchange_store_pickup"] == [LAVAL, MTL]
    assert next(o for o in cand.options if o.type == "exchange_ship_from_store").location_id == LAVAL  # same ship days: nearest
    assert next(o for o in cand.options if o.type == "return_in_store").location_id == LAVAL
    store.update_item(store.customer_key(JOHN), set={"travel_minutes": {MTL: 10, LAVAL: 10}})  # a tie: location id decides
    cand = find(color="BROWN")
    assert [o.location_id for o in cand.options if o.type == "exchange_store_pickup"] == [LAVAL, MTL]
    assert [o.minutes_away for o in cand.options if o.type == "exchange_store_pickup"] == [10, 10]


def test_a_store_with_an_unknown_distance_sorts_after_the_known_ones_and_has_no_minutes(john):
    store.update_item(store.customer_key(JOHN), set={"travel_minutes": {MTL: 5}})
    cand = find(color="BROWN")
    pickups = [o for o in cand.options if o.type == "exchange_store_pickup"]
    assert [o.location_id for o in pickups] == [MTL, LAVAL]
    assert pickups[0].minutes_away == 5 and pickups[1].minutes_away is None and "minutes_away" not in pickups[1].model_dump()


def test_the_return_drop_off_is_the_nearest_store_not_the_first_id(john):
    """Laval sorts before Montreal by id; John lives 5 minutes from Montreal."""
    cand = find()
    in_store = next(o for o in cand.options if o.type == "return_in_store")
    assert (in_store.location_id, in_store.minutes_away) == (MTL, 5)


def test_invalid_travel_entries_are_ignored_as_unknown(john):
    store.update_item(store.customer_key(JOHN), set={"travel_minutes": {MTL: -5, LAVAL: True, TOR: "far", "DEMO-STORE-X": 3}})
    assert orders.customer_profile(JOHN)["travel_minutes"] == {"DEMO-STORE-X": 3}
    cand = find(color="BROWN")
    assert all(o.minutes_away is None for o in cand.options)


# ----------------------------------------------------------- the models (shape on the wire)


def base_option(**extra):
    option = {
        "option_id": "OPT-1", "type": "exchange_store_pickup", "sku": "S", "replacement_size": "9.5", "location_id": MTL,
        "location_name": "L", "qty": 1, "refund_cents": 0, "fee_cents": 0, "eta_date": "2026-10-17", "eta_text": "Sat Oct 17",
        "conditions": [], "evidence": {}, "source_refs": []}
    option.update(extra)
    return option


def test_unknown_optional_option_fields_are_left_out_of_the_json_never_null():
    plain = Option(**base_option()).model_dump()
    assert not {"minutes_away", "replacement_sku", "replacement_color"} & set(plain)
    rich = Option(**base_option(minutes_away=0, replacement_sku="S", replacement_color="BROWN")).model_dump()
    assert (rich["minutes_away"], rich["replacement_sku"], rich["replacement_color"]) == (0, "S", "BROWN")  # 0 minutes is known
    assert Option(**base_option(minutes_away=None)).model_dump() == plain
    assert Option(**base_option()).minutes_away is None  # attribute access still works
    assert "minutes_away" not in Option(**base_option()).model_dump_json()


def test_the_quote_carries_the_colour_and_distance_fields_and_the_hash_still_binds_the_sku(john):
    from tests.conftest import Flow

    flow = Flow()
    conv_id, run_id = flow.start_run(JOHN)
    check = orders.check_policy(JOHN, run_id, ORDER, LINE, 1, "CHANGE_OF_MIND", "unworn", "unknown")
    cand = options.find_options(JOHN, check.policy_check_id, None, None, None, "BROWN", run_id=run_id)
    chosen = [o.option_id for o in cand.options if o.type in ("exchange_store_pickup", "return_in_store")]
    quote = quotes.create_quote(JOHN, run_id, conv_id, cand.candidate_set_id, chosen)
    first = quote.options[0]
    assert (first.type, first.location_id, first.replacement_color, first.minutes_away) == ("exchange_store_pickup", MTL, "BROWN", 5)
    body = store.json_safe(quote)
    assert body["options"][0]["replacement_sku"] == BROWN_95
    assert body["options"][-1]["type"] == "return_in_store" and "replacement_color" not in body["options"][-1]
    assert first.payload_hash == quotes.compute_payload_hash(quote.quote_id, first, quote.policy_version)
    other_colour = first.model_copy(update={"sku": TAN_95})
    assert quotes.compute_payload_hash(quote.quote_id, other_colour, quote.policy_version) != first.payload_hash
