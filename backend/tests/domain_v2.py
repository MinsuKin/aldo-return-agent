"""A test world for domain v2 (PIVOT_ADDENDUM 11.3): John, David and the men's style, from test builders.

The fixtures v2 stream writes the real files (``fixtures/demo/*_v2.json``); the tests that use this
module do NOT depend on them. ``build_v2_fixtures`` copies the seven BASE fixture files to a temp
directory and appends the v2 records to them, so the standard loader reads them whatever way the
real v2 files end up being merged. ``tests/test_domain_v2_fixtures.py`` runs the same checks
against the shipped files once they exist.

Contract used (addendum 11.3): John is a silver member in QC, 5 / 24 / 540 minutes from the
Montreal / Laval / Toronto stores; David is a platinum member in NB, 150+ minutes from every store.
Style ``DEMO-STYLE-MEN001`` comes in BLACK, BROWN and TAN; stock in size 9.5 (DC, Montreal, Laval,
Toronto) is BLACK 3/0/1/1, BROWN 4/2/1/0 and TAN 2/0/0/1.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from returns_domain import fixtures
from tests.conftest import FIXTURES_ROOT, make_agent_order

BASE_FILES = (
    "customers.json",
    "orders.json",
    "products.json",
    "locations.json",
    "inventory.json",
    "logistics.json",
    "policy.json",
)

JOHN = "DEMO-CUST-JOHN"
DAVID = "DEMO-CUST-DAVID"
MTL, LAVAL, TOR, DC = "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01", "DEMO-DC-QC"
LOCATIONS = (DC, MTL, LAVAL, TOR)

MEN_STYLE = "DEMO-STYLE-MEN001"
MEN_NAME = "Demo Men Oxford"
MEN_PRICE = 14999
MEN_COLORS = ("BLACK", "BROWN", "TAN")
MEN_SIZES = ("9.0", "9.5", "10.0")
#: Size 9.5 stock per colour at (DC, MTL, LAVAL, TOR), addendum 11.3 default variant.
STOCK_95 = {"BLACK": (3, 0, 1, 1), "BROWN": (4, 2, 1, 0), "TAN": (2, 0, 0, 1)}
#: Any other size: two pairs at the DC only.
STOCK_OTHER = (2, 0, 0, 0)

TIERS = {
    "none": {"label": "Member", "authority_refund_limit_cents": 25000},
    "silver": {"label": "Silver member", "authority_refund_limit_cents": 25000},
    "platinum": {
        "label": "Platinum member",
        "authority_refund_limit_cents": 75000,
        "benefits": {"free_return_shipping": {"per_month": 1, "applies_to": ["return_mail"], "waives_fee": True}},
    },
}


def men_sku(color: str, size: str) -> str:
    return f"DEMO-SKU-MEN001-{color}-{round(float(size) * 10):03d}"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def build_v2_fixtures(dest: Path) -> Path:
    """Write the augmented fixture tree under ``dest`` and return the directory ``FIXTURES_DIR`` must name."""
    source, demo = FIXTURES_ROOT / "demo", dest / "demo"
    demo.mkdir(parents=True)
    for name in BASE_FILES:
        shutil.copy(source / name, demo / name)

    customers = _load(demo / "customers.json")
    customers["customers"] += [
        {
            "customer_id": JOHN, "display_name": "John", "home_region": "QC", "tier": "silver",
            "annual_spend_cents": 180000, "location_label": "Bronfman Building, Montreal",
            "travel_minutes": {MTL: 5, LAVAL: 24, TOR: 540},
            "description": "Silver member, buys the men's style and exchanges the colour", "source": "synthetic",
        },
        {
            "customer_id": DAVID, "display_name": "David", "home_region": "NB", "tier": "platinum",
            "annual_spend_cents": 1240000, "location_label": "Rural New Brunswick",
            "travel_minutes": {MTL: 400, LAVAL: 410, TOR: 700},
            "description": "Platinum member far from every store", "source": "synthetic",
        },
    ]
    _dump(demo / "customers.json", customers)

    products = _load(demo / "products.json")
    inventory = _load(demo / "inventory.json")
    for color in MEN_COLORS:
        for size in MEN_SIZES:
            sku = men_sku(color, size)
            products["products"].append(
                {"sku": sku, "product_name": MEN_NAME, "style": MEN_STYLE, "size": size, "color": color,
                 "price_cents": MEN_PRICE, "currency": "CAD", "source": "synthetic"}
            )
            stock = STOCK_95[color] if size == "9.5" else STOCK_OTHER
            for location, atp in zip(LOCATIONS, stock):
                inventory["base"].append(
                    {"sku": sku, "location_id": location, "atp": atp, "version": 1, "source": "synthetic"}
                )
    black_95 = men_sku("BLACK", "9.5")
    inventory["variants"]["dc_stockout"]["overrides"] += [
        {"sku": black_95, "location_id": DC, "atp": 0},
        {"sku": black_95, "location_id": LAVAL, "atp": 1},
    ]
    inventory["variants"]["all_stockout"]["overrides"] += [
        {"sku": black_95, "location_id": location, "atp": 0} for location in LOCATIONS
    ]
    _dump(demo / "products.json", products)
    _dump(demo / "inventory.json", inventory)

    policy = _load(demo / "policy.json")
    policy["exchange_reasons"] = ["FIT_SIZE", "EXPECTATION", "CHANGE_OF_MIND"]
    policy["tiers"] = TIERS
    _dump(demo / "policy.json", policy)
    return dest


@pytest.fixture
def v2_world(ddb: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The moto table seeded with the base fixtures plus John, David and the men's style."""
    root = build_v2_fixtures(tmp_path / "fixtures")
    monkeypatch.setenv("FIXTURES_DIR", str(root))
    fixtures.clear_cache()
    fixtures.reset("default")
    return root


def john_order(
    order_id: str = "DEMO-A2A-5A1B2C",
    *,
    customer_id: str = JOHN,
    color: str = "BLACK",
    size: str = "9.5",
    delivered_at: str | None = "2026-10-16",
) -> str:
    """John's purchase through the gateway (channel agent), delivered by default, line ``{order_id}-L1``."""
    return make_agent_order(
        order_id, customer_id, sku=men_sku(color, size), size=size, delivered_at=delivered_at, price_cents=MEN_PRICE
    )
