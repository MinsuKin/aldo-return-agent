"""The Montreal store in the demo is a real ALDO store with its street address, and John works next to it."""

from __future__ import annotations

from returns_domain import fixtures
from returns_domain.options import Option

MTL = "DEMO-STORE-MTL-01"


def _option(**extra: object) -> Option:
    return Option(
        option_id="O1", type="exchange_store_pickup", sku="S", location_id=MTL, location_name="ALDO Sainte-Catherine",
        qty=1, refund_cents=0, fee_cents=0, eta_date="2026-10-17", eta_text="Sat Oct 17", conditions=[], evidence={},
        source_refs=[], **extra,
    )


def test_the_montreal_store_is_the_sainte_catherine_store_with_its_address() -> None:
    store = next(loc for loc in fixtures.load_bundle().locations if loc["location_id"] == MTL)
    assert store["name"] == "ALDO Sainte-Catherine"
    assert store["address"] == "1009 Sainte-Catherine St. West #1007, Montreal, QC H3B 1H1"


def test_john_is_at_the_bronfman_building_five_minutes_from_that_store() -> None:
    john = next(c for c in fixtures.load_bundle().customers if c["customer_id"] == "DEMO-CUST-JOHN")
    assert john["location_label"] == "Bronfman Building, Montreal"
    assert john["location_address"] == "1001 Rue Sherbrooke O, Montréal, QC H3A 1G5"
    assert john["travel_minutes"][MTL] == 5


def test_an_option_carries_the_address_only_when_the_store_has_one() -> None:
    assert _option(location_address="1009 Sainte-Catherine St. West #1007, Montreal, QC H3B 1H1").model_dump()[
        "location_address"
    ].startswith("1009 Sainte-Catherine")
    assert "location_address" not in _option().model_dump()
