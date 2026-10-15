"""options.py: facts about exchanges and returns for an eligible check (requirement 6, G-14, G-15, G-26)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from returns_domain import audit, fixtures, options, orders, store
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import AVA, BEN, CHLOE, make_agent_order

MTL, LAVAL, TOR, DC = "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01", "DEMO-DC-QC"


def find(customer=AVA, order="DEMO-1001", line="DEMO-1001-L1", *, reason="FIT_SIZE", direction="too_small",
         condition="unworn", replacement=None, deadline="2026-10-17", channel=None, qty=1, run_id="RUN-1"):
    check = orders.check_policy(customer, run_id, order, line, qty, reason, condition, direction)
    return options.find_options(customer, check.policy_check_id, replacement, deadline, channel, run_id=run_id)


def types(cand):
    return [o.type for o in cand.options]


def by_type(cand, option_type):
    return next(o for o in cand.options if o.type == option_type)


# ------------------------------------------------------------------------------- hero


def test_default_variant_offers_dc_store_ship_pickup_and_both_returns(seeded):
    cand = find()
    assert types(cand) == ["exchange_ship_from_dc", "exchange_ship_from_store", "exchange_store_pickup",
                           "return_in_store", "return_mail"]
    assert [o.option_id for o in cand.options] == ["OPT-1", "OPT-2", "OPT-3", "OPT-4", "OPT-5"]
    dc, store_ship, pickup, in_store, mail = cand.options
    assert (dc.location_id, dc.eta_date, dc.eta_text, dc.meets_deadline) == (DC, "2026-10-20", "Tue Oct 20", False)
    assert (store_ship.location_id, store_ship.eta_date, store_ship.meets_deadline) == (MTL, "2026-10-19", False)
    assert (pickup.location_id, pickup.eta_date, pickup.eta_text, pickup.meets_deadline) == (MTL, "2026-10-17", "Sat Oct 17", True)
    for option in (dc, store_ship, pickup):
        assert (option.replacement_size, option.sku, option.refund_cents, option.fee_cents, option.qty) == (
            "7.5", "DEMO-SKU-HERO-BLACK-075", 0, 0, 1)
    assert (in_store.refund_cents, in_store.fee_cents, in_store.meets_deadline, in_store.replacement_size) == (12999, 0, None, None)
    assert (mail.refund_cents, mail.fee_cents, mail.eta_date, mail.location_id) == (12404, 595, "2026-10-26", DC)
    assert in_store.sku == mail.sku == "DEMO-SKU-HERO-BLACK-070"  # the returned line's own SKU


def test_meets_deadline_hero(seeded):
    """G-15: only store pickup meets Saturday 2026-10-17; without a deadline the flag is null."""
    cand = find()
    assert {o.type: o.meets_deadline for o in cand.options} == {
        "exchange_ship_from_dc": False, "exchange_ship_from_store": False, "exchange_store_pickup": True,
        "return_in_store": None, "return_mail": None}
    assert all(o.meets_deadline is None for o in find(deadline=None).options)
    later = find(deadline="2026-10-20")
    assert {o.type: o.meets_deadline for o in later.options if o.type.startswith("exchange")} == {
        "exchange_ship_from_dc": True, "exchange_ship_from_store": True, "exchange_store_pickup": True}


def test_dc_stockout_variant(seeded):
    fixtures.reset("dc_stockout")
    cand = find()
    assert types(cand) == ["exchange_ship_from_store", "exchange_store_pickup", "return_in_store", "return_mail"]
    assert by_type(cand, "exchange_ship_from_store").location_id == MTL
    assert by_type(cand, "exchange_ship_from_store").meets_deadline is False
    assert by_type(cand, "exchange_store_pickup").meets_deadline is True
    assert cand.availability["dc_atp"] == 0 and cand.availability["atp_by_location"][MTL] == 2


def test_all_stockout_leaves_only_the_two_returns(seeded):
    fixtures.reset("all_stockout")
    cand = find()
    assert types(cand) == ["return_in_store", "return_mail"]
    assert cand.availability["reason_no_exchange"] == "OUT_OF_STOCK_EVERYWHERE"
    assert cand.inventory_versions == {} and cand.availability["replacement_sku"] == "DEMO-SKU-HERO-BLACK-075"


def test_availability_evidence_and_provenance(seeded):
    cand = find()
    assert cand.availability == {
        "replacement_size": "7.5", "replacement_sku": "DEMO-SKU-HERO-BLACK-075",
        "atp_by_location": {DC: 5, LAVAL: 0, MTL: 1, TOR: 1}, "dc_atp": 5, "reason_no_exchange": None}
    pickup = by_type(cand, "exchange_store_pickup")
    assert pickup.evidence["atp"] == 1 and pickup.evidence["inventory_version"] == 1 and pickup.evidence["rate_id"] == "DEMO-RATE-XPICKUP"
    assert pickup.source_refs == [f"INV#DEMO-SKU-HERO-BLACK-075/LOC#{MTL}", "DEMO-RATE-XPICKUP", "DEMO-POLICY-CA@2026.10-demo"]
    assert by_type(cand, "return_mail").evidence["rate_id"] == "DEMO-RATE-MAIL"
    assert by_type(cand, "return_in_store").source_refs[0] == "ORDER#DEMO-1001/LINE#DEMO-1001-L1"
    assert all(o.provenance == "synthetic" and o.currency == "CAD" and o.conditions for o in cand.options)
    assert "Bring the original pair, unworn" in pickup.conditions


def test_inventory_versions_cover_only_the_locations_the_options_use(seeded):
    cand = find()
    assert cand.inventory_versions == {f"DEMO-SKU-HERO-BLACK-075|{DC}": 1, f"DEMO-SKU-HERO-BLACK-075|{MTL}": 1}
    returns_only = find(reason="COMFORT", direction="unknown")
    assert returns_only.inventory_versions == {}


def test_other_sizes_in_stock_is_information_only(seeded):
    cand = find()
    sizes = {entry["size"]: entry for entry in cand.other_sizes_in_stock}
    assert list(sizes) == ["6.0", "6.5", "8.0", "8.5", "9.0"]  # sorted; current 7.0 and chosen 7.5 are excluded
    assert sizes["8.0"] == {"size": "8.0", "sku": "DEMO-SKU-HERO-BLACK-080", "total_atp": 7}
    assert sizes["6.0"]["total_atp"] == 5
    fixtures.reset("all_stockout")
    assert [e["size"] for e in find().other_sizes_in_stock] == ["6.0", "6.5", "8.5", "9.0"]  # 7.5 and 8.0 are out


def test_find_options_returns_facts_only_and_does_not_mutate_inventory(seeded, snapshot):
    before = snapshot(exclude=("PCHK#", "CAND#", "AUDIT#"))
    cand = find()
    assert snapshot(exclude=("PCHK#", "CAND#", "AUDIT#")) == before
    assert not any(hasattr(cand, name) for name in ("ranking", "recommended", "best"))


def test_candidate_set_is_persisted_for_the_quote_step(seeded):
    cand = find(channel="store_pickup")
    stored = store.get_item(store.cand_key(cand.candidate_set_id))
    assert cand.candidate_set_id.startswith("CAND-") and len(cand.candidate_set_id) == len("CAND-") + 8
    assert stored["customer_id"] == AVA and stored["policy_check_id"] == cand.policy_check_id
    assert stored["deadline_date"] == "2026-10-17" and cand.preferred_channel == "store_pickup" == stored["preferred_channel"]
    assert [o["option_id"] for o in stored["options"]] == [o.option_id for o in cand.options]
    assert stored["inventory_versions"] == cand.inventory_versions and stored["order_id"] == "DEMO-1001"


# ------------------------------------------------------------------- replacement sizes (G-14)


def test_default_size_from_direction(seeded):
    assert find().availability["replacement_size"] == "7.5"  # 7.0 too small -> +0.5
    contrast = find(CHLOE, "DEMO-1009", "DEMO-1009-L1", direction="too_big", deadline=None)
    assert contrast.availability["replacement_size"] == "7.5"  # 8.0 too big -> -0.5
    assert contrast.options[0].sku == "DEMO-SKU-CONTRAST-BLACK-075"
    assert types(contrast)[0] == "exchange_ship_from_dc"


def test_unknown_direction_requires_size(seeded):
    with pytest.raises(DomainError) as exc:
        find(direction="unknown")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "replacement_size" in exc.value.message
    assert find(direction="unknown", replacement="8.0").availability["replacement_size"] == "8.0"
    assert audit.list_audit(action="access_denied") == []


def test_an_explicit_size_wins_and_floats_are_accepted(seeded):
    assert find(replacement="8.0").availability["replacement_size"] == "8.0"
    assert find(replacement=8.5).availability["replacement_size"] == "8.5"  # models pass floats
    assert find(replacement=8).availability["replacement_size"] == "8.0"
    assert find(replacement=" 7.5 ").availability["replacement_size"] == "7.5"


@pytest.mark.parametrize("bad", ["abc", "7.25", "7,5", -1, 0, True, "7.0", 7.0])
def test_invalid_or_identical_replacement_size_is_a_validation_error(seeded, bad):
    with pytest.raises(DomainError) as exc:
        find(replacement=bad)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_a_size_that_does_not_exist_gives_no_exchange_but_keeps_returns(seeded):
    cand = find(replacement="12.0")
    assert types(cand) == ["return_in_store", "return_mail"]
    assert cand.availability["reason_no_exchange"] == "NO_SUCH_SIZE" and cand.availability["replacement_sku"] is None
    assert cand.availability["atp_by_location"] == {}
    assert [e["size"] for e in cand.other_sizes_in_stock][:2] == ["6.0", "6.5"]  # alternatives are still listed


def test_other_orders_have_no_size_run(seeded):
    cand = find(AVA, "DEMO-1008", "DEMO-1008-L1", direction="too_small", deadline=None)
    assert types(cand) == ["return_in_store", "return_mail"] and cand.availability["reason_no_exchange"] == "NO_SUCH_SIZE"


def test_non_fit_reasons_only_get_returns_and_need_no_size(seeded):
    cand = find(reason="COMFORT", direction="unknown")
    assert types(cand) == ["return_in_store", "return_mail"]
    assert cand.availability == {"replacement_size": None, "replacement_sku": None, "atp_by_location": {},
                                 "dc_atp": 0, "reason_no_exchange": "NOT_FIT_REASON"}
    assert cand.other_sizes_in_stock == []


# ----------------------------------------------------------------- regions and locations


def test_pickup_is_limited_to_the_customers_home_region(seeded):
    store.update_item(store.customer_key(AVA), set={"home_region": "ON"})
    cand = find()
    assert [o.location_id for o in cand.options if o.type == "exchange_store_pickup"] == [TOR]
    assert by_type(cand, "exchange_ship_from_store").location_id == TOR  # tie on ship_days: home region first
    assert by_type(cand, "return_in_store").location_id == TOR


def test_store_ship_tie_break_and_pickup_list_by_location_id(seeded):
    store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", LAVAL), set={"atp": 2})
    cand = find()
    pickups = [o.location_id for o in cand.options if o.type == "exchange_store_pickup"]
    assert pickups == [LAVAL, MTL]  # every home-region store with stock, by location id
    assert by_type(cand, "exchange_ship_from_store").location_id == LAVAL  # home region, then lowest location id


def test_return_in_store_location_is_the_first_home_region_store(seeded):
    assert by_type(find(), "return_in_store").location_id == LAVAL
    assert by_type(find(BEN, "DEMO-1003", "DEMO-1003-L1", reason="CHANGE_OF_MIND", direction="unknown", deadline=None),
                   "return_in_store").location_id == TOR


def test_no_pickup_when_the_home_region_has_no_stock(seeded):
    store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", MTL), set={"atp": 0})
    cand = find()
    assert "exchange_store_pickup" not in types(cand)
    assert by_type(cand, "exchange_ship_from_store").location_id == TOR  # the only store with stock


# --------------------------------------------------------------------- policy gating


def test_options_require_an_eligible_check(seeded):
    ineligible = orders.check_policy(AVA, "RUN-1", "DEMO-1002", "DEMO-1002-L1", 1, "CHANGE_OF_MIND", "unworn")
    with pytest.raises(DomainError) as exc:
        options.find_options(AVA, ineligible.policy_check_id)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "decision=ineligible" in exc.value.message
    review = orders.check_policy(CHLOE, "RUN-1", "DEMO-1007", "DEMO-1007-L1", 1, "FIT_SIZE", "unworn")
    with pytest.raises(DomainError) as exc:
        options.find_options(CHLOE, review.policy_check_id)
    assert "decision=needs_review" in exc.value.message
    assert store.query("CAND#x") == [] and list(store.scan_keys(("CAND#",))) == []


def test_missing_and_foreign_checks(seeded):
    with pytest.raises(DomainError) as exc:
        options.find_options(AVA, "PCHK-NOPE")
    assert exc.value.code == ErrorCode.MISSING_POLICY
    check = orders.check_policy(AVA, "RUN-1", "DEMO-1001", "DEMO-1001-L1", 1, "FIT_SIZE", "unworn", "too_small")
    with pytest.raises(DomainError) as exc:
        options.find_options(BEN, check.policy_check_id)
    assert exc.value.code == ErrorCode.NOT_AUTHORIZED
    assert audit.list_audit(action="access_denied")[0]["actor"] == BEN
    assert list(store.scan_keys(("CAND#",))) == []


@pytest.mark.parametrize("deadline", ["Saturday", "2026-13-01", "2026-10-1", "10/17/2026", 20261017, ""])
def test_deadline_must_be_a_calendar_date(seeded, deadline):
    with pytest.raises(DomainError) as exc:
        find(deadline=deadline)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_preferred_channel_is_validated_and_echoed(seeded):
    assert find(channel="ship").preferred_channel == "ship"
    assert find(channel=None).preferred_channel is None
    with pytest.raises(DomainError):
        find(channel="drone")


def test_the_agent_cannot_change_which_options_exist_by_channel(seeded):
    assert types(find(channel="return")) == types(find(channel="ship")) == types(find())


# ----------------------------------------------------------- orders placed by the gateway


def test_options_work_on_an_agent_order_once_delivered(seeded):
    """Act 2: the hero bought size 8.0 through the gateway, it is delivered, and the owner asks for 8.5."""
    make_agent_order("DEMO-A2A-81B20E", delivered_at="2026-10-16")
    cand = find(AVA, "DEMO-A2A-81B20E", "DEMO-A2A-81B20E-L1", direction="too_small")
    assert cand.availability["replacement_size"] == "8.5"
    assert types(cand)[0] == "exchange_ship_from_dc" and "exchange_store_pickup" in types(cand)
    assert by_type(cand, "exchange_store_pickup").sku == "DEMO-SKU-HERO-BLACK-085"
    assert by_type(cand, "return_in_store").refund_cents == 12999


def test_an_undelivered_agent_order_has_no_options_yet(seeded):
    make_agent_order("DEMO-A2A-81B20E")
    check = orders.check_policy(AVA, "RUN-1", "DEMO-A2A-81B20E", "DEMO-A2A-81B20E-L1", 1, "FIT_SIZE", "unworn", "too_small")
    with pytest.raises(DomainError):
        options.find_options(AVA, check.policy_check_id)


# --------------------------------------------------------------------------- eta_for


DC_LOCATION = {"location_id": DC, "type": "DC", "ship_days": 4, "pickup_ready_days": None}
STORE_LOCATION = {"location_id": MTL, "type": "STORE", "ship_days": 3, "pickup_ready_days": 1}


def test_eta_for_uses_calendar_days_from_the_demo_date(seeded):
    assert options.eta_for(DC_LOCATION, "exchange_ship_from_dc") == date(2026, 10, 20)
    assert options.eta_for(STORE_LOCATION, "exchange_ship_from_store") == date(2026, 10, 19)
    assert options.eta_for(STORE_LOCATION, "exchange_store_pickup") == date(2026, 10, 17)
    assert options.eta_for(STORE_LOCATION, "return_in_store") == date(2026, 10, 16)
    assert options.eta_for(DC_LOCATION, "return_mail") == date(2026, 10, 26)
    assert options.eta_for(DC_LOCATION, "return_mail", mail_refund_days=5) == date(2026, 10, 21)


def test_eta_for_accepts_the_gateway_option_names(seeded):
    assert options.eta_for(DC_LOCATION, "ship_from_dc") == date(2026, 10, 20)
    assert options.eta_for(STORE_LOCATION, "ship_from_store") == date(2026, 10, 19)
    assert options.eta_for(STORE_LOCATION, "store_pickup") == date(2026, 10, 17)
    assert options.normalize_option_type("store_pickup") == "exchange_store_pickup"


def test_eta_for_now_can_be_a_date_datetime_or_text(seeded):
    assert options.eta_for(STORE_LOCATION, "store_pickup", date(2026, 11, 2)) == date(2026, 11, 3)
    assert options.eta_for(STORE_LOCATION, "store_pickup", "2026-10-30") == date(2026, 10, 31)
    late = datetime(2026, 10, 17, 2, 30, tzinfo=timezone.utc)  # still Oct 16 in Toronto
    assert options.eta_for(STORE_LOCATION, "store_pickup", late) == date(2026, 10, 17)


def test_eta_for_is_pure_and_validates(seeded):
    with pytest.raises(DomainError):
        options.eta_for(DC_LOCATION, "store_pickup")  # a DC has no pickup_ready_days
    with pytest.raises(DomainError):
        options.eta_for(STORE_LOCATION, "teleport")
    with pytest.raises(DomainError):
        options.eta_for({"ship_days": -1}, "exchange_ship_from_dc")
    with pytest.raises(DomainError):
        options.eta_for({"ship_days": "3"}, "exchange_ship_from_dc")


def test_eta_for_matches_what_find_options_promises(seeded):
    cand = find()
    locations = {loc["location_id"]: loc for loc in store.batch_get([store.location_key(i) for i in fixtures.location_ids()])}
    for option in cand.options:
        if option.type.startswith("exchange_"):
            assert options.eta_for(locations[option.location_id], option.type).isoformat() == option.eta_date
