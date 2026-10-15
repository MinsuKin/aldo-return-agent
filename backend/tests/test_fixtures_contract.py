"""fixtures.py and the JSON files under fixtures/demo/ against contracts.md section (d).

Product names are never hard-coded: tests use ids and SKUs and read names from the data.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from returns_domain import clock, fit, fixtures, policy, store
from returns_domain.errors import DomainError, ErrorCode

HERO_SKU = "DEMO-SKU-HERO-BLACK-{size}"
LOCATIONS = ("DEMO-DC-QC", "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01")


@pytest.fixture
def bundle(aws_env):
    return fixtures.load_bundle()


def hero_atp(size: str) -> dict[str, int]:
    sku = HERO_SKU.format(size=f"{int(float(size) * 10):03d}")
    return {i["location_id"]: i["atp"] for i in store.query(f"INV#{sku}")}


# ------------------------------------------------------------------------ file contract


def test_bundle_counts_match_the_contract(bundle):
    """The loaded bundle is the base files plus fixtures v2 (John, David, DEMO-3001); the base counts are unchanged."""
    assert len(bundle.customers) == 5
    assert {c["customer_id"]: c["home_region"] for c in bundle.customers} == {
        "DEMO-CUST-AVA": "QC", "DEMO-CUST-BEN": "ON", "DEMO-CUST-CHLOE": "QC",
        "DEMO-CUST-JOHN": "QC", "DEMO-CUST-DAVID": "NB"}
    assert len(bundle.orders) == 11
    assert [o["order_id"] for o in bundle.orders] == [f"DEMO-{n}" for n in range(1001, 1011)] + ["DEMO-3001"]
    assert all(len(o["lines"]) == 1 for o in bundle.orders if o["order_id"] != "DEMO-3001")
    assert [loc["location_id"] for loc in bundle.locations] == list(LOCATIONS)
    hero_contrast_rows = [r for r in bundle.inventory_base if "HERO" in r["sku"] or "CONTRAST" in r["sku"]]
    assert len(hero_contrast_rows) == 44  # (7 hero + 4 contrast sizes) x 4 locations
    assert set(bundle.inventory_variants) == set(fixtures.VARIANTS)


def test_every_record_is_synthetic_demo_data_with_string_sizes(bundle):
    records = [*bundle.customers, *bundle.locations, *bundle.products, *bundle.inventory_base]
    for order in bundle.orders:
        records.append(order)
        records.extend(order["lines"])
    for record in records:
        assert record["source"] == "synthetic"
    ids = [c["customer_id"] for c in bundle.customers] + [o["order_id"] for o in bundle.orders]
    ids += [line["line_id"] for o in bundle.orders for line in o["lines"]]
    ids += [p["sku"] for p in bundle.products] + [p["style"] for p in bundle.products]
    ids += [loc["location_id"] for loc in bundle.locations]
    assert all(i.startswith("DEMO-") for i in ids)
    for record in (*bundle.products, *(line for o in bundle.orders for line in o["lines"])):
        assert isinstance(record["size"], str) and store.size_str(record["size"]) == record["size"]
    text = json.dumps([bundle.customers, bundle.orders])
    assert "@" not in text  # no emails


def test_order_table_matches_the_binding_values(bundle):
    expected = {
        "DEMO-1001": ("DEMO-CUST-AVA", "DEMO-SKU-HERO-BLACK-070", "7.0", 12999, False, -12),
        "DEMO-1002": ("DEMO-CUST-AVA", "DEMO-SKU-FINALSALE-TAN-080", "8.0", 5999, True, -10),
        "DEMO-1003": ("DEMO-CUST-BEN", "DEMO-SKU-STD1-BLACK-100", "10.0", 8999, False, -30),
        "DEMO-1004": ("DEMO-CUST-BEN", "DEMO-SKU-STD2-BROWN-100", "10.0", 8999, False, -31),
        "DEMO-1005": ("DEMO-CUST-BEN", "DEMO-SKU-BOOT-BLACK-100", "10.0", 28900, False, None),
        "DEMO-1006": ("DEMO-CUST-CHLOE", "DEMO-SKU-STD3-WHITE-070", "7.0", 7999, False, None),
        "DEMO-1007": ("DEMO-CUST-CHLOE", "DEMO-SKU-STD4-NAVY-075", "7.5", 6999, False, "null"),
        "DEMO-1008": ("DEMO-CUST-AVA", "DEMO-SKU-STD5-BLACK-080", "8.0", 5999, False, None),
        "DEMO-1009": ("DEMO-CUST-CHLOE", "DEMO-SKU-CONTRAST-BLACK-080", "8.0", 11999, False, None),
        "DEMO-1010": ("DEMO-CUST-BEN", "DEMO-SKU-STD6-GREY-100", "10.0", 7499, False, None),
    }
    today = clock.demo_today()
    assert today == date(2026, 10, 16) and today.strftime("%A") == "Friday"
    for order in bundle.orders:
        if order["order_id"] not in expected:  # DEMO-3001 (fixtures v2) is checked in test_fixtures_v2.py
            continue
        customer, sku, size, cents, final_sale, offset = expected[order["order_id"]]
        line = order["lines"][0]
        assert (order["customer_id"], line["sku"], line["size"], line["unit_paid_cents"], line["final_sale"]) == (
            customer, sku, size, cents, final_sale)
        assert order["banner"] == "ALDO_CA" and order["currency"] == "CAD"
        assert isinstance(line["unit_paid_cents"], int)
        if offset == "null":
            assert order["delivered_at"] is None
        else:
            assert order["delivered_at"] is not None
            if isinstance(offset, int):  # G-16: absolute Toronto dates, offsets from DEMO_NOW
                assert (today - date.fromisoformat(order["delivered_at"])).days == -offset
            assert date.fromisoformat(order["ordered_at"]) <= date.fromisoformat(order["delivered_at"])
        assert line["style"] == "DEMO-STYLE-" + sku.split("-")[2]
    # the boundaries the policy tests rely on
    assert [o["order_id"] for o in bundle.orders if o["lines"][0]["final_sale"] is True] == ["DEMO-1002"]
    assert [o["order_id"] for o in bundle.orders if o["lines"][0]["final_sale"] is None] == []
    assert [o["order_id"] for o in bundle.orders if o["lines"][0]["unit_paid_cents"] > 25000] == ["DEMO-1005"]
    two = [o for o in bundle.orders if o["lines"][0]["purchased_qty"] == 2]
    assert [(o["order_id"], o["lines"][0]["returned_or_reserved_qty"]) for o in two] == [("DEMO-1008", 1)]


def test_offsets(bundle):
    """G-16: delivered_at is an absolute Toronto date -12/-10/-30/-31 days from DEMO_NOW, null only on DEMO-1007."""
    today = clock.demo_today()
    delivered = {o["order_id"]: o["delivered_at"] for o in bundle.orders}
    assert [(today - date.fromisoformat(delivered[oid])).days for oid in ("DEMO-1001", "DEMO-1002", "DEMO-1003", "DEMO-1004")] == [12, 10, 30, 31]
    assert [oid for oid, value in delivered.items() if value is None] == ["DEMO-1007"]
    assert all(len(value) == 10 and value[4] == "-" for value in delivered.values() if value)


def test_seed_demo_script_resets_the_table_and_reports_counts(ddb, capsys):
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
    spec = importlib.util.spec_from_file_location("seed_demo_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store.put_item({"pk": "RUN#STALE", "sk": "META"})
    assert module.main(["--variant", "dc_stockout", "--create-table"]) == 0  # the table already exists: left alone
    out = capsys.readouterr().out
    assert "variant: dc_stockout" in out and "deleted: 1 runtime items" in out and "written: 266 fixture items" in out
    assert "customers: 5" in out and "orders: 11" in out and "inventory: 170" in out
    assert store.get_item(("RUN#STALE", "META")) is None
    assert hero_atp("7.5")["DEMO-DC-QC"] == 0
    assert module.main(["--variant", "default"]) == 0
    assert hero_atp("7.5")["DEMO-DC-QC"] == 5


def test_policy_json_equals_the_module_constants(bundle):
    data = fixtures.load_policy_json()
    assert data["window_days"] == policy.WINDOW_DAYS
    assert data["authority_refund_limit_cents"] == policy.AUTHORITY_REFUND_LIMIT_CENTS
    assert data["version"] == policy.POLICY_VERSION
    assert data["policy_id"] == policy.POLICY_ID
    assert tuple(r["rule_id"] for r in data["rules"]) == policy.RULE_IDS
    assert tuple(data["option_types"]) == policy.ALL_OPTION_TYPES
    assert data["currency"] == "CAD"


def test_logistics_values(bundle):
    data = fixtures.load_logistics()
    assert data["mail_return"]["fee_cents"] == 595 and data["mail_return"]["refund_eta_days_after_dropoff"] == 10
    assert data["in_store_return"]["fee_cents"] == 0
    assert data["exchange_ship"]["fee_cents"] == 0 and data["exchange_pickup"]["fee_cents"] == 0


def test_hero_and_contrast_products_have_real_fit_signals(bundle):
    names = {p["style"]: p["product_name"] for p in bundle.products}
    hero = fit.get_fit_insight(names["DEMO-STYLE-HERO"])
    assert hero["found"] and hero["scope"] == "ALDO_CA"
    assert hero["signal"] == "runs_small" and hero["n_fit"] >= 100
    contrast = fit.get_fit_insight(names["DEMO-STYLE-CONTRAST"])
    assert contrast["found"] and contrast["signal"] in ("runs_large", "mixed")
    # all sizes of one style and colour share the product name and price
    by_style = {}
    for p in bundle.products:
        by_style.setdefault((p["style"], p["color"]), set()).add((p["product_name"], p["price_cents"]))
    assert all(len(v) == 1 for v in by_style.values())


def test_size_runs_and_inventory_coverage(bundle):
    hero = sorted(p["size"] for p in bundle.products if p["style"] == "DEMO-STYLE-HERO")
    contrast = sorted(p["size"] for p in bundle.products if p["style"] == "DEMO-STYLE-CONTRAST")
    assert hero == ["6.0", "6.5", "7.0", "7.5", "8.0", "8.5", "9.0"]
    assert contrast == ["7.0", "7.5", "8.0", "8.5"]
    skus = {p["sku"] for p in bundle.products}
    assert {line["sku"] for o in bundle.orders for line in o["lines"]} <= skus
    inventoried = {r["sku"] for r in bundle.inventory_base}
    assert skus == inventoried  # every product has stock rows
    pairs = [(r["sku"], r["location_id"]) for r in bundle.inventory_base]
    assert len(pairs) == len(set(pairs))
    assert all(r["version"] == 1 for r in bundle.inventory_base)


# ----------------------------------------------------------------- variants and key shapes


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("default", {"DEMO-DC-QC": 5, "DEMO-STORE-MTL-01": 1, "DEMO-STORE-LAVAL-01": 0, "DEMO-STORE-TOR-01": 1}),
        ("dc_stockout", {"DEMO-DC-QC": 0, "DEMO-STORE-MTL-01": 2, "DEMO-STORE-LAVAL-01": 0, "DEMO-STORE-TOR-01": 0}),
        ("all_stockout", {loc: 0 for loc in LOCATIONS}),
    ],
)
def test_variant_stock_for_the_hero_replacement_sizes(ddb, variant, expected):
    fixtures.reset(variant)
    assert hero_atp("7.5") == expected
    assert hero_atp("8.0") == expected
    # variants never touch the other hero sizes
    assert hero_atp("8.5") == {"DEMO-DC-QC": 3, "DEMO-STORE-MTL-01": 1, "DEMO-STORE-LAVAL-01": 1, "DEMO-STORE-TOR-01": 0}


def test_variant_overrides_must_match_a_base_row(aws_env, tmp_path, monkeypatch):
    root = tmp_path / "fx"
    shutil.copytree(Path(fixtures.config.get_settings().fixtures_dir) / "demo", root / "demo")
    inv = json.loads((root / "demo" / "inventory.json").read_text())
    inv["variants"]["dc_stockout"]["overrides"].append({"sku": "DEMO-SKU-NOPE", "location_id": "DEMO-DC-QC", "atp": 0})
    (root / "demo" / "inventory.json").write_text(json.dumps(inv))
    bad = fixtures.load_bundle(root)
    with pytest.raises(DomainError) as exc:
        fixtures.build_items(bad, "dc_stockout")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    fixtures.build_items(bad, "default")  # untouched variants still load


def test_loader_rejects_non_demo_ids_and_non_synthetic_records(aws_env, tmp_path):
    root = tmp_path / "fx"
    shutil.copytree(Path(fixtures.config.get_settings().fixtures_dir) / "demo", root / "demo")
    path = root / "demo" / "customers.json"
    data = json.loads(path.read_text())
    data["customers"][0]["customer_id"] = "REAL-CUSTOMER"
    path.write_text(json.dumps(data))
    with pytest.raises(DomainError) as exc:
        fixtures.load_bundle(root)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR and "DEMO-" in exc.value.message
    data["customers"][0]["customer_id"] = "DEMO-CUST-AVA"
    data["customers"][0]["source"] = "provided_csv"
    path.write_text(json.dumps(data))
    with pytest.raises(DomainError):
        fixtures.load_bundle(root)


def test_missing_fixture_file_is_a_clear_error(aws_env, tmp_path):
    with pytest.raises(DomainError) as exc:
        fixtures.load_bundle(tmp_path)
    assert "fixture error" in exc.value.message


def test_build_items_derives_every_key_with_the_store_builders(bundle):
    items = {(i["pk"], i["sk"]): i for i in fixtures.build_items(bundle, "default")}
    order = items[("ORDER#DEMO-1001", "META")]
    assert (order["gsi1pk"], order["gsi1sk"]) == ("CUST#DEMO-CUST-AVA", "ORDER#2026-09-30#DEMO-1001")
    assert "lines" not in order and order["delivered_at"] == "2026-10-04"
    line = items[("ORDER#DEMO-1001", "LINE#DEMO-1001-L1")]
    assert line["order_id"] == "DEMO-1001" and line["size"] == "7.0" and line["final_sale"] is False
    product = items[("PRODUCT#DEMO-SKU-HERO-BLACK-075", "META")]
    assert (product["gsi1pk"], product["gsi1sk"]) == ("STYLE#DEMO-STYLE-HERO#BLACK", "SIZE#007.5")
    assert items[("INV#DEMO-SKU-HERO-BLACK-075", "LOC#DEMO-DC-QC")]["version"] == 1
    assert items[("CUST#DEMO-CUST-AVA", "PROFILE")]["home_region"] == "QC"
    assert items[("LOC#DEMO-STORE-MTL-01", "META")]["pickup_ready_days"] == 1
    assert items[("ORDER#DEMO-1007", "META")]["delivered_at"] is None
    assert len(items) == len(fixtures.build_items(bundle, "default"))  # no duplicate keys


def test_build_items_rejects_an_unknown_variant(bundle):
    with pytest.raises(DomainError) as exc:
        fixtures.build_items(bundle, "nope")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


# --------------------------------------------------------------------------- seed / reset


def test_seed_writes_fixture_items_only_and_never_deletes(ddb):
    store.put_item({"pk": "RUN#KEEP", "sk": "META"})
    summary = fixtures.seed("default")
    assert summary.deleted == 0 and summary.variant == "default"
    assert summary.counts["customers"] == 5 and summary.counts["orders"] == 11 and summary.counts["order_lines"] == 20
    assert summary.counts["locations"] == 4 and summary.counts["products"] == 56 and summary.counts["inventory"] == 170
    assert summary.written == sum(summary.counts.values())
    assert store.get_item(("RUN#KEEP", "META")) is not None
    assert store.get_item(("ORDER#DEMO-1001", "LINE#DEMO-1001-L1"))["unit_paid_cents"] == 12999
    assert store.get_item(("CUST#DEMO-CUST-AVA", "PROFILE"))["display_name"] == "Ava"


def test_reset_deletes_runtime_partitions(ddb):
    """G-27 and PIVOT_ADDENDUM 9.1: every runtime prefix goes, fixture partitions stay."""
    fixtures.seed("default")
    runtime_pks = [
        "CONV#1", "RUN#1", "RUN#A2A-DEMO-CUST-AVA", "PCHK#1", "CAND#1", "QUOTE#1", "TXN#1", "IDEM#1", "CASE#1",
        "AUDIT#2026-10-16", "SIZEREQ#SZR-1", "A2AOUT#MU-1", "ORDER#DEMO-A2A-81B20E",
    ]
    for pk in runtime_pks:
        store.put_item({"pk": pk, "sk": "META"})
        store.put_item({"pk": pk, "sk": "EVT#000001"})
    store.put_item({"pk": "ORDER#DEMO-A2A-81B20E", "sk": "LINE#DEMO-A2A-81B20E-L1"})
    summary = fixtures.reset("default")
    assert summary.deleted == len(runtime_pks) * 2 + 1
    for pk in runtime_pks:
        assert store.query(pk) == []
    # fixtures survive and were rewritten
    assert store.get_item(("ORDER#DEMO-1001", "META")) is not None
    assert len(store.query("CUST#DEMO-CUST-AVA", index="GSI1")) == 3


def test_reset_handles_more_runtime_items_than_one_batch(ddb):
    fixtures.seed("default")
    for n in range(130):
        store.put_item({"pk": f"RUN#BULK{n:03d}", "sk": "META"})
        store.put_item({"pk": f"RUN#BULK{n:03d}", "sk": "EVT#000001"})
    assert fixtures.reset("default").deleted == 260
    assert list(store.scan_keys(("RUN#",))) == []


def test_reset_restores_inventory_order_lines_and_removes_agent_orders(ddb):
    fixtures.reset("default")
    store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", "DEMO-DC-QC"), add={"atp": -2, "version": 3})
    store.update_item(store.line_key("DEMO-1001", "DEMO-1001-L1"), add={"returned_or_reserved_qty": 1})
    for item in store.order_items(
        {"order_id": "DEMO-A2A-AAAAAA", "customer_id": "DEMO-CUST-AVA", "ordered_at": "2026-10-16", "delivered_at": None},
        [{"line_id": "DEMO-A2A-AAAAAA-L1", "sku": "DEMO-SKU-HERO-BLACK-080"}],
    ):
        store.put_item(item)
    assert len(store.query("CUST#DEMO-CUST-AVA", index="GSI1")) == 4
    fixtures.reset("default")
    inv = store.get_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", "DEMO-DC-QC"))
    assert (inv["atp"], inv["version"]) == (5, 1)
    assert store.get_item(store.line_key("DEMO-1001", "DEMO-1001-L1"))["returned_or_reserved_qty"] == 0
    assert store.query("ORDER#DEMO-A2A-AAAAAA") == []
    assert len(store.query("CUST#DEMO-CUST-AVA", index="GSI1")) == 3


def test_reset_switches_variants_and_resets_versions(ddb):
    fixtures.reset("dc_stockout")
    assert hero_atp("7.5")["DEMO-DC-QC"] == 0
    store.update_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", "DEMO-STORE-MTL-01"), add={"version": 4})
    fixtures.reset("default")
    assert hero_atp("7.5")["DEMO-DC-QC"] == 5
    assert store.get_item(store.inventory_key("DEMO-SKU-HERO-BLACK-075", "DEMO-STORE-MTL-01"))["version"] == 1


def test_reset_with_an_unknown_variant_deletes_nothing(ddb):
    fixtures.seed("default")
    store.put_item({"pk": "RUN#KEEP", "sk": "META"})
    with pytest.raises(DomainError) as exc:
        fixtures.reset("bogus")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert store.get_item(("RUN#KEEP", "META")) is not None


def test_reset_summary_json_shape(ddb):
    summary = fixtures.reset("all_stockout").model_dump()
    assert set(summary) == {"variant", "deleted", "written", "counts"}
    assert summary["variant"] == "all_stockout"


def test_gsi_lookups_the_services_depend_on(ddb):
    fixtures.reset("default")
    sizes = store.query("STYLE#DEMO-STYLE-HERO#BLACK", index="GSI1")
    assert [i["size"] for i in sizes] == ["6.0", "6.5", "7.0", "7.5", "8.0", "8.5", "9.0"]
    exact = store.query("STYLE#DEMO-STYLE-HERO#BLACK", index="GSI1", sk_begins_with="SIZE#007.5")
    assert [i["sku"] for i in exact] == ["DEMO-SKU-HERO-BLACK-075"]
    orders = store.query("CUST#DEMO-CUST-AVA", index="GSI1", forward=False)
    assert [i["order_id"] for i in orders] == ["DEMO-1008", "DEMO-1002", "DEMO-1001"]  # ordered 10-03, 10-02, 09-30


def test_persona_list_comes_from_the_file(aws_env):
    personas = fixtures.persona_list()
    assert [p["customer_id"] for p in personas] == [
        "DEMO-CUST-AVA", "DEMO-CUST-BEN", "DEMO-CUST-CHLOE", "DEMO-CUST-JOHN", "DEMO-CUST-DAVID"]
    assert all(
        set(p) == {"customer_id", "display_name", "description", "tier", "tier_label", "location_label"}
        for p in personas
    )
    assert [p["display_name"] for p in personas] == ["Ava", "Ben", "Chloe", "John", "David"]


def test_location_ids_follow_the_file(aws_env):
    assert fixtures.location_ids() == list(LOCATIONS)


def test_runtime_prefixes_are_shared_with_the_store():
    assert fixtures.RUNTIME_PREFIXES is store.RUNTIME_PREFIXES
    assert "SIZEREQ#" in fixtures.RUNTIME_PREFIXES and "A2AOUT#" in fixtures.RUNTIME_PREFIXES
    assert "OUTBOX#" not in fixtures.RUNTIME_PREFIXES
    assert "ORDER#DEMO-A2A-" in fixtures.RUNTIME_PREFIXES


def test_hero_etas_follow_from_the_demo_clock(aws_env):
    """Contract D.4: pickup Sat 10-17, ship from store Mon 10-19, ship from DC Tue 10-20."""
    today = clock.demo_today()
    assert clock.format_day(today + timedelta(days=1)) == "Sat Oct 17"
    assert clock.format_day(today + timedelta(days=3)) == "Mon Oct 19"
    assert clock.format_day(today + timedelta(days=4)) == "Tue Oct 20"
