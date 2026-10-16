"""Fixtures v2 (PIVOT_ADDENDUM 11.3): John, David, the men's style, DEMO-3001, reviews and the policy tiers.

The files are fixtures/demo/{customers,orders,products,inventory}_v2.json, reviews.json, fit_aliases.json and
the new keys of policy.json; ``fixtures.load_bundle`` merges the v2 files behind the base ones. Real product
names are never hard-coded: they are read from the data and checked against fit_insights.json. The men's
style is the one exception by design: its name is the demo name ``Toney`` everywhere, and its fit
numbers are the real counts of the style named in fit_aliases.json (copied into fit_insights.json by
scripts/add_fit_aliases.py). The base-file contract stays in test_fixtures_contract.py.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from returns_domain import clock, fit, fixtures, store
from returns_domain.errors import DomainError, ErrorCode
from tests.conftest import FIXTURES_ROOT

JOHN, DAVID = "DEMO-CUST-JOHN", "DEMO-CUST-DAVID"
DC, MTL, LAVAL, TOR = "DEMO-DC-QC", "DEMO-STORE-MTL-01", "DEMO-STORE-LAVAL-01", "DEMO-STORE-TOR-01"
LOCATIONS = (DC, MTL, LAVAL, TOR)
MEN_STYLE, HERO, CONTRAST = "DEMO-STYLE-MEN001", "DEMO-STYLE-HERO", "DEMO-STYLE-CONTRAST"
MEN_NAME = "Toney"  # the men's product now carries its real ALDO style name, shown everywhere
MEN_ALIAS = "ALDO Men 001"  # the legacy demo phrase, kept as an alias so old queries still resolve
# Real ALDO men's styles that are not in the ladies'-only 24-month OMS fit extract (see fixtures/demo/README.md).
NOT_IN_OMS_EXTRACT = {"Stark", "Patten"}
V2_NAMES = ("customers_v2.json", "orders_v2.json", "products_v2.json", "inventory_v2.json", "reviews.json")
DEMO = FIXTURES_ROOT / "demo"
REPO_ROOT = FIXTURES_ROOT.parent
FIT_FILE = FIXTURES_ROOT / "derived" / "fit_insights.json"
SCRIPT = REPO_ROOT / "scripts" / "add_fit_aliases.py"

# FIXTURES_DIR must name the repo fixtures in every test: without it the loader would read a stale backend/fixtures_bundle
pytestmark = pytest.mark.usefixtures("aws_env")


@pytest.fixture
def bundle(aws_env):
    return fixtures.load_bundle()


def men_sku(color: str, size: str) -> str:
    return f"DEMO-SKU-MEN001-{color}-{round(float(size) * 10):03d}"


def name_of(bundle, style: str) -> str:
    return next(p["product_name"] for p in bundle.products if p["style"] == style)


def atp_of(items, sku: str) -> list[int]:
    """ATP of ``sku`` at DC, Montreal, Laval, Toronto from the items ``fixtures.build_items`` returns."""
    rows = {i["sk"]: i["atp"] for i in items if i["pk"] == f"INV#{sku}"}
    return [rows[f"LOC#{loc}"] for loc in LOCATIONS]


def copy_demo(tmp_path: Path, *, v2: bool = True) -> Path:
    """A private copy of fixtures/demo; ``v2=False`` leaves out the additive files (an older bundle)."""
    root = tmp_path / "fx"
    ignore = None if v2 else shutil.ignore_patterns(*V2_NAMES)
    shutil.copytree(DEMO, root / "demo", ignore=ignore)
    return root


def edit(root: Path, name: str, change) -> None:
    path = root / "demo" / name
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------------------ file style


@pytest.mark.parametrize("name", [*V2_NAMES, "policy.json", "fit_aliases.json"])
def test_files_follow_the_file_rules(name):
    """contracts.md D.0: UTF-8, 2-space indent, trailing newline, top-level schema_version 1."""
    text = (DEMO / name).read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["schema_version"] == 1
    assert text == json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def test_the_v2_files_hold_no_contact_data_or_links():
    text = "".join((DEMO / name).read_text(encoding="utf-8") for name in V2_NAMES)
    assert "@" not in text and "http" not in text.lower()


# ----------------------------------------------------------------------------- customers


def test_john_and_david_carry_the_v2_attributes(bundle):
    people = {c["customer_id"]: c for c in bundle.customers}
    john, david = people[JOHN], people[DAVID]
    assert (john["display_name"], john["tier"], john["home_region"]) == ("John", "silver", "QC")
    assert (john["annual_spend_cents"], john["location_label"]) == (180000, "Bronfman Building, Montreal")
    assert john["travel_minutes"] == {MTL: 5, LAVAL: 24, TOR: 540}
    assert (david["display_name"], david["tier"], david["home_region"]) == ("David", "platinum", "NB")
    assert (david["annual_spend_cents"], david["location_label"]) == (1240000, "Rural New Brunswick")
    assert set(david["travel_minutes"]) == {MTL, LAVAL, TOR} and min(david["travel_minutes"].values()) >= 150
    for person in (john, david):
        assert person["source"] == "synthetic" and "@" not in json.dumps(person)


def test_the_base_customers_get_no_new_required_fields(bundle):
    base = json.loads((DEMO / "customers.json").read_text(encoding="utf-8"))["customers"]
    assert [c["customer_id"] for c in base] == ["DEMO-CUST-AVA", "DEMO-CUST-BEN", "DEMO-CUST-CHLOE"]
    new_fields = {"tier", "annual_spend_cents", "travel_minutes", "location_label"}
    assert all(not (new_fields & set(c)) for c in base)


def test_customer_items_get_the_new_attributes_and_tier_defaults_to_none(bundle):
    items = {(i["pk"], i["sk"]): i for i in fixtures.build_items(bundle, "default")}
    john = items[("CUST#DEMO-CUST-JOHN", "PROFILE")]
    assert (john["tier"], john["annual_spend_cents"], john["location_label"]) == ("silver", 180000, "Bronfman Building, Montreal")
    assert john["travel_minutes"] == {MTL: 5, LAVAL: 24, TOR: 540}
    assert items[("CUST#DEMO-CUST-DAVID", "PROFILE")]["tier"] == "platinum"
    for base in ("AVA", "BEN", "CHLOE"):
        item = items[(f"CUST#DEMO-CUST-{base}", "PROFILE")]
        assert item["tier"] == "none" and "travel_minutes" not in item and "location_label" not in item


def test_persona_rows_carry_tier_label_and_location(aws_env):
    rows = {p["customer_id"]: p for p in fixtures.persona_list()}
    tiers = fixtures.load_policy_json()["tiers"]
    assert (rows[JOHN]["tier"], rows[JOHN]["tier_label"], rows[JOHN]["location_label"]) == (
        "silver", tiers["silver"]["label"], "Bronfman Building, Montreal")
    assert (rows[DAVID]["tier"], rows[DAVID]["tier_label"], rows[DAVID]["location_label"]) == (
        "platinum", tiers["platinum"]["label"], "Rural New Brunswick")
    for base in ("AVA", "BEN", "CHLOE"):
        assert rows[f"DEMO-CUST-{base}"] == {
            **rows[f"DEMO-CUST-{base}"], "tier": "none", "tier_label": tiers["none"]["label"], "location_label": ""}
    assert all(isinstance(v, str) for row in rows.values() for v in row.values())


def test_persona_tier_labels_fall_back_when_policy_json_has_no_tiers(aws_env, tmp_path, monkeypatch):
    root = copy_demo(tmp_path)
    edit(root, "policy.json", lambda d: d.pop("tiers"))
    monkeypatch.setenv("FIXTURES_DIR", str(root))
    fixtures.clear_cache()
    rows = {p["customer_id"]: p for p in fixtures.persona_list()}
    assert rows[JOHN]["tier_label"] == "Silver member" and rows["DEMO-CUST-AVA"]["tier_label"] == "Member"


# --------------------------------------------------------------------------- policy.json


def test_policy_json_has_the_v2_keys_and_keeps_its_old_values(bundle):
    data = fixtures.load_policy_json()
    assert data["exchange_reasons"] == ["FIT_SIZE", "EXPECTATION", "CHANGE_OF_MIND"]
    assert data["tiers"] == {
        "none": {"label": "Member", "authority_refund_limit_cents": 25000},
        "silver": {"label": "Silver member", "authority_refund_limit_cents": 25000},
        "platinum": {
            "label": "Platinum member",
            "authority_refund_limit_cents": 75000,
            "benefits": {"free_return_shipping": {"per_month": 1, "applies_to": ["return_mail"], "waives_fee": True}},
        },
    }
    assert data["tiers"]["none"]["authority_refund_limit_cents"] == data["authority_refund_limit_cents"] == 25000
    assert data["label"] == "Demo assumption, not official ALDO policy"
    assert (data["window_days"], data["window_inclusive"], data["policy_id"], data["version"]) == (
        30, True, "DEMO-POLICY-CA", "2026.10-demo")
    assert [r["rule_id"] for r in data["rules"]][:2] == ["final_sale.no_return", "final_sale.defect_review"]


# ------------------------------------------------------------------------ the men's style


def fit_aliases() -> dict[str, str]:
    return json.loads((DEMO / "fit_aliases.json").read_text(encoding="utf-8"))["aliases"]


def test_the_mens_product_is_called_aldo_men_001_everywhere_and_never_by_its_real_style_name(bundle):
    # the men's product now carries its real ALDO style name (Toney) everywhere in the v2 data
    assert name_of(bundle, MEN_STYLE) == MEN_NAME and {p["product_name"] for p in bundle.products if p["style"] == MEN_STYLE} == {MEN_NAME}
    # the legacy demo phrase survives only as an alias, so old queries still resolve to this one style
    assert all(MEN_ALIAS in p["aliases"] for p in bundle.products if p["style"] == MEN_STYLE)
    assert fit_aliases() == {MEN_ALIAS: MEN_NAME}


def test_the_demo_name_reaches_the_real_numbers_of_a_runs_large_style(bundle):
    insight = fit.get_fit_insight(MEN_NAME)
    assert insight["found"] and insight["scope"] == "ALDO_CA"
    assert insight["signal"] == "runs_large" and insight["n_fit"] >= 100 and insight["too_small_share"] < 0.35
    # the lookup is an exact name match, so the legacy demo alias reaches the very same real numbers
    legacy = fit.get_fit_insight(next(iter(fit_aliases())))
    assert legacy["found"] and {**legacy, "product_name": MEN_NAME} == insight
    # the pick with its numbers is documented in the README
    readme = (DEMO / "README.md").read_text(encoding="utf-8")
    assert MEN_NAME in readme
    assert str(insight["n_fit"]) in readme and str(insight["too_big"]) in readme
    # the data caveat travels with the pick: the OMS extract is ladies' only, the POS file has men's sizes 7 to 14
    assert "ladies' categories only" in readme and "sizes 7 to 14" in readme and "24 months of OMS returns" in readme


def test_the_mens_style_runs_from_8_to_12_in_black_brown_and_tan(bundle):
    products = [p for p in bundle.products if p["style"] == MEN_STYLE]
    sizes = [f"{8 + 0.5 * n:.1f}" for n in range(9)]
    assert sizes[0] == "8.0" and sizes[-1] == "12.0"
    assert sorted({p["color"] for p in products}) == ["BLACK", "BROWN", "TAN"]
    for color in ("BLACK", "BROWN", "TAN"):
        assert sorted((p["size"] for p in products if p["color"] == color), key=float) == sizes
    assert len(products) == 27 and len({p["sku"] for p in products}) == 27
    assert {p["sku"] for p in products} == {men_sku(c, s) for c in ("BLACK", "BROWN", "TAN") for s in sizes}
    assert {p["price_cents"] for p in products} == {14999} and {p["currency"] for p in products} == {"CAD"}
    assert len({p["product_name"] for p in products}) == 1
    assert all(p["aliases"] == ["Men 001", MEN_ALIAS, "ALDO Toney"] and p["source"] == "synthetic" for p in products)


def test_both_the_full_name_and_the_short_alias_find_only_the_mens_style(bundle):
    # resolution matches product_name OR any alias; legacy phrases and the real name all find one style
    haystack: dict[str, set[str]] = {}
    for p in bundle.products:
        bucket = haystack.setdefault(p["style"], set())
        bucket.add(p["product_name"].casefold())
        bucket.update(a.casefold() for a in p.get("aliases", []))
    for query in ("aldo men 001", "men 001", "ALDO MEN", "toney"):
        q = query.casefold()
        assert [s for s, names in haystack.items() if any(q in n for n in names)] == [MEN_STYLE], query
    # the men's product keeps its legacy demo aliases so old queries still resolve
    for p in (p for p in bundle.products if p["style"] == MEN_STYLE):
        assert "Men 001" in p["aliases"] and MEN_ALIAS in p["aliases"]


def test_product_names_stay_unique_per_style_and_never_contain_each_other(bundle):
    names = {}
    for p in bundle.products:
        names.setdefault(p["style"], p["product_name"].casefold())
    assert len(set(names.values())) == len(names)
    assert not [(a, b) for a in names.values() for b in names.values() if a != b and a in b]
    assert [s for s, n in names.items() if "stessy" in n] == [HERO]  # the pre-purchase query "Stessy" stays unambiguous


@pytest.mark.parametrize(
    ("color", "stock"),
    [("BLACK", [3, 0, 1, 1]), ("BROWN", [4, 2, 1, 0]), ("TAN", [2, 0, 0, 1])],
)
def test_size_9_5_stock_in_the_default_variant_is_binding(bundle, color, stock):
    assert atp_of(fixtures.build_items(bundle, "default"), men_sku(color, "9.5")) == stock  # DC, MTL, LAVAL, TOR


def test_every_mens_sku_has_all_four_locations_and_stock_at_the_dc(bundle):
    items = fixtures.build_items(bundle, "default")
    for p in (p for p in bundle.products if p["style"] == MEN_STYLE):
        stock = atp_of(items, p["sku"])
        assert stock[0] >= 1, p["sku"]  # every SKU is at the DC
        assert all(isinstance(n, int) and 0 <= n <= 5 for n in stock)
    # Montreal is sold out of black in the sizes John would ask for (the stockout he meets)
    assert [atp_of(items, men_sku("BLACK", s))[1] for s in ("9.0", "9.5", "10.0", "10.5")] == [0, 0, 0, 0]


@pytest.mark.parametrize(
    ("variant", "black_95"),
    [("default", [3, 0, 1, 1]), ("dc_stockout", [0, 0, 1, 1]), ("all_stockout", [0, 0, 0, 0])],
)
def test_variants_override_only_black_9_5_of_the_mens_style(bundle, variant, black_95):
    items, default = fixtures.build_items(bundle, variant), fixtures.build_items(bundle, "default")
    assert atp_of(items, men_sku("BLACK", "9.5")) == black_95
    for p in (p for p in bundle.products if p["style"] == MEN_STYLE and p["sku"] != men_sku("BLACK", "9.5")):
        assert atp_of(items, p["sku"]) == atp_of(default, p["sku"]), p["sku"]


def test_variants_leave_the_hero_overrides_and_david_alone(bundle):
    default = fixtures.build_items(bundle, "default")
    for variant in ("dc_stockout", "all_stockout"):
        items = fixtures.build_items(bundle, variant)
        david = [line["sku"] for line in bundle.orders[-1]["lines"]]
        assert all(atp_of_dc(items, sku) == atp_of_dc(default, sku) for sku in david)
    assert atp_of(fixtures.build_items(bundle, "dc_stockout"), "DEMO-SKU-HERO-BLACK-075") == [0, 2, 0, 0]


def atp_of_dc(items, sku: str) -> int:
    return next(i["atp"] for i in items if i["pk"] == f"INV#{sku}" and i["sk"] == f"LOC#{DC}")


def test_v2_variant_overrides_must_match_a_base_row(aws_env, tmp_path):
    root = copy_demo(tmp_path)
    edit(root, "inventory_v2.json",
         lambda d: d["variants"]["dc_stockout"]["overrides"].append({"sku": "DEMO-SKU-NOPE", "location_id": DC, "atp": 0}))
    bad = fixtures.load_bundle(root)
    with pytest.raises(DomainError) as exc:
        fixtures.build_items(bad, "dc_stockout")
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    fixtures.build_items(bad, "default")
    fixtures.build_items(bad, "all_stockout")


# ----------------------------------------------------------------------- the fit alias


def load_script():
    spec = importlib.util.spec_from_file_location("add_fit_aliases_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_products(doc: dict) -> dict:
    return {name: entry for name, entry in doc["products"].items() if "alias_of" not in entry}


def test_fit_aliases_json_maps_the_demo_name_to_a_real_product_of_the_fit_file():
    data = json.loads((DEMO / "fit_aliases.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1 and data["source"] == "synthetic"
    doc = json.loads(FIT_FILE.read_text(encoding="utf-8"))
    assert list(data["aliases"]) == [MEN_ALIAS]
    for alias, real in data["aliases"].items():
        assert alias != real and real in real_products(doc) and alias not in real_products(doc)


def test_the_alias_entry_copies_the_real_counts_and_signals():
    doc = json.loads(FIT_FILE.read_text(encoding="utf-8"))
    for alias, real in fit_aliases().items():
        entry, source = doc["products"][alias], doc["products"][real]
        assert set(entry) == {"ALDO_CA", "ALL", "alias_of", "note"} and entry["alias_of"] == real
        assert entry["ALDO_CA"] == source["ALDO_CA"] and entry["ALL"] == source["ALL"]
        assert entry["note"].strip() == entry["note"] and "\n" not in entry["note"] and real in entry["note"]
        assert "alias_of" not in source


def test_adding_the_alias_left_the_file_metadata_and_every_real_product_alone():
    text = FIT_FILE.read_text(encoding="utf-8")
    doc = json.loads(text)
    render = load_script().render  # the one-product-per-line layout of scripts/build_fit_insights.py
    assert render(doc) == text
    plain = {**doc, "products": real_products(doc)}
    alias_lines = tuple(f"    {json.dumps(alias)}: " for alias in fit_aliases())
    assert [line for line in text.split("\n") if not line.startswith(alias_lines)] == render(plain).split("\n")
    assert doc["totals"]["ALL"]["products"] == len(plain["products"])  # the totals describe the real data only
    assert list(doc["products"]) == sorted(doc["products"])  # the alias sits where a sorted file holds it
    profile = REPO_ROOT / "docs" / "evidence" / "data-profile.json"
    if profile.exists():  # sha and row count still equal the independent record of the source CSV
        recorded = json.loads(profile.read_text(encoding="utf-8"))["files"]["OMS Data_Feb24-Jan26_new.csv"]
        assert doc["source"]["sha256"] == recorded["sha256"] and doc["source"]["rows_read"] == recorded["rows"]


def test_the_committed_fit_file_is_up_to_date_with_the_mapping(capsys):
    assert load_script().main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out


def fit_scope(too_small: int, too_big: int) -> dict:
    n_fit = too_small + too_big
    return {"too_small": too_small, "too_big": too_big, "total_returns": n_fit + 20,
            "too_small_share": round(too_small / n_fit, 4), "n_fit": n_fit,
            "signal": "runs_small" if too_small * 100 >= 65 * n_fit else "runs_large" if too_small * 100 <= 35 * n_fit else "mixed"}


SAMPLE_FIT = {
    "generated_at": "2026-01-01T00:00:00+00:00",
    "provenance": "derived",
    "source": {"kind": "provided_csv", "file": "x.csv", "sha256": "ab" * 32, "rows_read": 7},
    "filter": "a filter text",
    "thresholds": {"min_n": 30},
    "totals": {"ALL": {"products": 3}},
    "products": {
        "Alpha": {"ALDO_CA": fit_scope(5, 95), "ALL": fit_scope(9, 191)},
        "Mid": {"ALDO_CA": fit_scope(50, 50), "ALL": fit_scope(60, 60)},
        "Zulu": {"ALDO_CA": fit_scope(80, 20), "ALL": fit_scope(150, 40)},
    },
}


@pytest.fixture
def fit_files(tmp_path):
    script = load_script()
    insights, aliases = tmp_path / "fit.json", tmp_path / "aliases.json"
    insights.write_text(script.render(SAMPLE_FIT), encoding="utf-8")

    def run(mapping: dict | None, *extra: str) -> int:
        if mapping is not None:
            aliases.write_text(json.dumps({"schema_version": 1, "aliases": mapping}), encoding="utf-8")
        return script.main(["--insights", str(insights), "--aliases", str(aliases), *extra])

    return insights, run


def test_the_script_adds_only_the_alias_entry_and_is_idempotent(fit_files, capsys):
    insights, run = fit_files
    before = insights.read_text(encoding="utf-8")
    assert run({"Demo Name": "Zulu"}, "--check") == 1 and insights.read_text(encoding="utf-8") == before  # nothing written
    assert run({"Demo Name": "Zulu"}) == 0
    first = insights.read_text(encoding="utf-8")
    doc = json.loads(first)
    assert list(doc["products"]) == ["Alpha", "Demo Name", "Mid", "Zulu"]  # sorted position, others in their order
    assert {k: v for k, v in doc.items() if k != "products"} == {k: v for k, v in SAMPLE_FIT.items() if k != "products"}
    assert {n: e for n, e in doc["products"].items() if n != "Demo Name"} == SAMPLE_FIT["products"]
    alias = doc["products"]["Demo Name"]
    assert alias == {**SAMPLE_FIT["products"]["Zulu"], "alias_of": "Zulu", "note": alias["note"]} and "Zulu" in alias["note"]
    stamp = insights.stat().st_mtime_ns
    assert run({"Demo Name": "Zulu"}) == 0 and insights.read_text(encoding="utf-8") == first  # the same bytes
    assert insights.stat().st_mtime_ns == stamp  # and the file is not even rewritten
    assert run({"Demo Name": "Zulu"}, "--check") == 0 and "up to date" in capsys.readouterr().out


def test_an_alias_dropped_from_the_mapping_disappears_from_the_file(fit_files):
    insights, run = fit_files
    assert run({"Demo Name": "Zulu"}) == 0
    assert run({"Other Name": "Alpha"}) == 0
    products = json.loads(insights.read_text(encoding="utf-8"))["products"]
    assert list(products) == ["Alpha", "Mid", "Other Name", "Zulu"] and products["Other Name"]["alias_of"] == "Alpha"


@pytest.mark.parametrize(
    "mapping",
    [
        {"Mid": "Zulu"},  # an alias must never replace a real product
        {"Demo Name": "No Such Style"},  # the real style must exist
        {"Demo Name": "Demo Name"},  # not an alias of itself
        {"Demo Name": ""},  # blank names
        {" Demo Name": "Zulu"},  # names are exact, so they must be trimmed
        {},  # an empty mapping is a mistake, not a request to remove
    ],
)
def test_the_script_refuses_a_bad_mapping_and_writes_nothing(fit_files, mapping):
    insights, run = fit_files
    before = insights.read_text(encoding="utf-8")
    assert run(mapping) == 2 and insights.read_text(encoding="utf-8") == before


def test_an_alias_cannot_point_at_another_alias(fit_files):
    insights, run = fit_files
    assert run({"Demo Name": "Zulu"}) == 0
    before = insights.read_text(encoding="utf-8")
    assert run({"Demo Name": "Zulu", "Second": "Demo Name"}) == 2 and insights.read_text(encoding="utf-8") == before


def test_the_script_reports_unusable_input_with_exit_code_2(tmp_path, capsys):
    script = load_script()
    missing = tmp_path / "nope.json"
    assert script.main(["--insights", str(missing), "--aliases", str(DEMO / "fit_aliases.json")]) == 2
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert script.main(["--insights", str(FIT_FILE), "--aliases", str(broken), "--check"]) == 2
    assert "add_fit_aliases" in capsys.readouterr().err


# -------------------------------------------------------------------------- David's order


def test_davids_order_has_ten_one_unit_lines_of_ten_real_styles(bundle):
    order = next(o for o in bundle.orders if o["order_id"] == "DEMO-3001")
    assert (order["customer_id"], order["banner"], order["currency"], order["channel"]) == (
        DAVID, "ALDO_CA", "CAD", "web")
    assert [line["line_id"] for line in order["lines"]] == [f"DEMO-3001-L{n}" for n in range(1, 11)]
    names = [line["product_name"] for line in order["lines"]]
    assert len(set(names)) == 10 and len({line["style"] for line in order["lines"]}) == 10
    base_names = {p["product_name"] for p in json.loads((DEMO / "products.json").read_text(encoding="utf-8"))["products"]}
    assert not set(names) & base_names and name_of(bundle, MEN_STYLE) not in names
    for name in names:
        # every name is a real ALDO men's style; most appear in the fit file. Two real styles
        # (Stark, Patten) are not in the ladies'-only 24-month OMS extract, as the README notes,
        # so the fit module returns insufficient_data for them rather than a signal.
        insight = fit.get_fit_insight(name)
        assert insight["found"] or name in NOT_IN_OMS_EXTRACT, name
    products = {p["sku"]: p for p in bundle.products}
    stocked = {r["sku"] for r in bundle.inventory_base if r["location_id"] == DC and r["atp"] >= 1}
    for line in order["lines"]:
        assert (line["purchased_qty"], line["returned_or_reserved_qty"], line["final_sale"]) == (1, 0, False)
        assert 8999 <= line["unit_paid_cents"] <= 21999 and isinstance(line["unit_paid_cents"], int)
        assert store.size_str(line["size"]) == line["size"] and line["source"] == "synthetic"
        product = products[line["sku"]]  # the SKU exists in products and at the DC
        assert line["sku"] in stocked
        assert (product["product_name"], product["style"], product["size"], product["color"], product["price_cents"]) == (
            line["product_name"], line["style"], line["size"], line["color"], line["unit_paid_cents"])
    assert len({line["color"] for line in order["lines"]}) >= 4 and len({line["size"] for line in order["lines"]}) >= 3


def test_davids_script_lines_are_the_tan_boot_white_sneaker_and_navy_loafer(bundle):
    order = next(o for o in bundle.orders if o["order_id"] == "DEMO-3001")
    lines = {line["line_id"]: line for line in order["lines"]}
    picked = [lines["DEMO-3001-L2"], lines["DEMO-3001-L5"], lines["DEMO-3001-L8"]]
    assert [(line["color"], line["category"]) for line in picked] == [("TAN", "boot"), ("WHITE", "sneaker"), ("NAVY", "loafer")]
    total = sum(line["unit_paid_cents"] for line in picked)
    limit = fixtures.load_policy_json()["authority_refund_limit_cents"]
    assert 25000 < total < 75000 and total > limit  # a silver customer would need a person, platinum would not
    assert max(line["unit_paid_cents"] for line in order["lines"]) < limit  # no single line does
    # the colours of the script are unique, so "the tan boot" resolves by colour alone
    colors = [line["color"] for line in order["lines"]]
    assert all(colors.count(c) == 1 for c in ("TAN", "WHITE", "NAVY", "GREY"))
    grey = lines["DEMO-3001-L10"]
    assert (grey["color"], grey["category"]) == ("GREY", "sneaker")  # "Also return the grey sneaker"


def test_davids_order_was_ordered_a_week_ago_and_delivered_three_days_ago(bundle):
    order = next(o for o in bundle.orders if o["order_id"] == "DEMO-3001")
    today = clock.demo_today()
    assert (today - date.fromisoformat(order["ordered_at"])).days == 7
    assert (today - date.fromisoformat(order["delivered_at"])).days == 3


def test_the_other_orders_are_untouched_by_v2(bundle):
    base = json.loads((DEMO / "orders.json").read_text(encoding="utf-8"))["orders"]
    assert bundle.orders[: len(base)] == base and len(base) == 10


# ---------------------------------------------------------------------------- reviews


def reviews_of(style: str) -> list[dict]:
    return fixtures.load_reviews(style)


def test_review_counts_shapes_and_text_limits(bundle):
    assert [len(reviews_of(s)) for s in (MEN_STYLE, HERO, CONTRAST)] == [16, 8, 4]
    everything = fixtures.load_reviews()
    assert len(everything) == 28 == len(bundle.reviews)
    assert len({r["review_id"] for r in everything}) == 28
    fields = {"review_id", "style_id", "rating", "title", "text", "language", "verified_purchase", "helpful_votes", "created_at", "source"}
    for r in everything:
        assert set(r) == fields and r["source"] == "synthetic"
        assert 60 <= len(r["text"]) <= 220, r["review_id"]
        assert 1 <= r["rating"] <= 5 and r["helpful_votes"] >= 0 and isinstance(r["verified_purchase"], bool)
        assert r["title"].strip() and date.fromisoformat(r["created_at"]) < clock.demo_today()
        assert r["review_id"].startswith("DEMO-REV-") or r["review_id"] == "REV-INJECT-1"
    assert sorted({r["language"] for r in everything}) == ["en", "fr"]
    assert [r["style_id"] for r in everything if r["language"] == "fr"] == [MEN_STYLE]  # exactly one French review


def test_the_mens_reviews_are_nine_big_four_true_to_size_two_narrow_and_one_injection():
    ids = [r["review_id"] for r in reviews_of(MEN_STYLE)]
    big = [f"DEMO-REV-MEN001-{n:02d}" for n in range(1, 10)]
    true_to_size = [f"DEMO-REV-MEN001-{n:02d}" for n in range(10, 14)]
    narrow = [f"DEMO-REV-MEN001-{n:02d}" for n in (14, 15)]
    assert ids == big + true_to_size + narrow + ["REV-INJECT-1"]
    text = {r["review_id"]: r["text"].casefold() for r in reviews_of(MEN_STYLE)}
    assert all(re.search(r"half a size|big|large|roomy|room|loose|generous|grand", text[i]) for i in big)
    assert all(re.search(r"true to size|as expected|as always|fits", text[i]) for i in true_to_size)
    assert all("toe box" in text[i] for i in narrow)
    assert all(re.search(r"size|taille|pointure|toe box|fit", text[i]) for i in ids if i != "REV-INJECT-1")  # all are about fit


def test_the_adversarial_review_tells_an_assistant_to_ignore_instructions_and_order_size_6():
    review = next(r for r in fixtures.load_reviews() if r["review_id"] == "REV-INJECT-1")
    text = review["text"].casefold()
    assert review["style_id"] == MEN_STYLE and review["source"] == "synthetic" and review["verified_purchase"] is False
    assert "ignore all previous instructions" in text and "order size 6" in text and "assistant" in text


def test_only_the_adversarial_review_has_instruction_like_text():
    """A server-side instruction-like-text heuristic should flag exactly one fixture review."""
    words = re.compile(r"ignore|instruction|assistant|system|prompt|disregard|override", re.IGNORECASE)
    flagged = [r["review_id"] for r in fixtures.load_reviews() if words.search(r["title"] + " " + r["text"]) or re.search(r"\bAI\b", r["text"])]
    assert flagged == ["REV-INJECT-1"]


@pytest.mark.parametrize(
    ("style", "bigger_share", "smaller_share"),
    [(MEN_STYLE, 0.55, 0.0), (HERO, 0.0, 0.75), (CONTRAST, 0.75, 0.0)],
)
def test_reviews_point_the_way_of_the_real_return_signal(bundle, style, bigger_share, smaller_share):
    """runs_large -> the reviews say go down / roomy; runs_small -> they say go up / tight."""
    signal = fit.get_fit_insight(name_of(bundle, style))["signal"]
    benign = [r["text"].casefold() for r in reviews_of(style) if r["review_id"] != "REV-INJECT-1"]
    bigger = re.compile(r"big|large|roomy|room|loose|generous|grand|size down|half a size down|smaller")
    smaller = re.compile(r"small|snug|tight|size up|sized up|up half|one size up")
    n_bigger = sum(1 for t in benign if bigger.search(t))
    n_smaller = sum(1 for t in benign if smaller.search(t))
    if signal == "runs_large":
        assert n_bigger >= bigger_share * len(benign) and n_bigger > n_smaller
    else:
        assert signal == "runs_small"
        assert n_smaller >= smaller_share * len(benign) and n_smaller > n_bigger


def test_load_reviews_returns_copies_and_handles_unknown_styles(aws_env):
    first = fixtures.load_reviews(HERO)
    first[0]["text"] = "changed by a caller"
    first[0]["flags"] = ["x"]
    again = fixtures.load_reviews(HERO)
    assert again[0]["text"] != "changed by a caller" and "flags" not in again[0]
    assert fixtures.load_reviews("DEMO-STYLE-NOPE") == [] and fixtures.load_reviews("") == []
    assert [r["review_id"] for r in fixtures.load_reviews()][:1] == ["DEMO-REV-MEN001-01"]  # file order


def test_load_reviews_without_the_file_is_empty(aws_env, tmp_path):
    root = copy_demo(tmp_path, v2=False)
    assert fixtures.load_reviews(fixtures_dir=root) == [] and fixtures.load_bundle(root).reviews == []


# ---------------------------------------------------------------- loader: optional and strict


def test_without_the_v2_files_the_base_bundle_loads_unchanged(aws_env, tmp_path, monkeypatch):
    root = copy_demo(tmp_path, v2=False)
    old = fixtures.load_bundle(root)
    assert (len(old.customers), len(old.orders), len(old.products), len(old.inventory_base)) == (3, 10, 19, 52)
    assert {k: len(v["overrides"]) for k, v in old.inventory_variants.items()} == {"default": 0, "dc_stockout": 8, "all_stockout": 8}
    assert len(fixtures.build_items(old, "default")) == 98
    monkeypatch.setenv("FIXTURES_DIR", str(root))
    fixtures.clear_cache()
    rows = fixtures.persona_list()
    assert [p["customer_id"] for p in rows] == ["DEMO-CUST-AVA", "DEMO-CUST-BEN", "DEMO-CUST-CHLOE"]
    assert {p["tier"] for p in rows} == {"none"}
    assert all(item["tier"] == "none" for item in fixtures.build_items(old, "default") if item["pk"].startswith("CUST#"))


def test_the_merge_keeps_base_records_first_and_does_not_touch_the_cache(aws_env):
    first = fixtures.load_bundle()
    assert [c["customer_id"] for c in first.customers][:3] == ["DEMO-CUST-AVA", "DEMO-CUST-BEN", "DEMO-CUST-CHLOE"]
    first.customers.append({"customer_id": "DEMO-CUST-X"})
    first.inventory_variants["dc_stockout"]["overrides"].append({"sku": "x"})
    second = fixtures.load_bundle()
    assert len(second.customers) == 5 and len(second.inventory_variants["dc_stockout"]["overrides"]) == 10


BAD_V2 = [
    ("orders_v2.json", lambda d: d["orders"][0].update(customer_id="DEMO-CUST-NOBODY"), "unknown customer"),
    ("orders_v2.json", lambda d: d["orders"][0]["lines"][0].update(sku="DEMO-SKU-NOPE-BLACK-100"), "unknown sku"),
    ("orders_v2.json", lambda d: d["orders"][0]["lines"][0].update(size="10"), "size"),
    ("inventory_v2.json", lambda d: d["base"][0].update(location_id="DEMO-STORE-NOPE"), "dangling"),
    ("inventory_v2.json", lambda d: d["base"][0].update(sku="DEMO-SKU-NOPE-BLACK-100"), "dangling"),
    ("inventory_v2.json", lambda d: d["variants"].update(bogus={"overrides": []}), "variants"),
    ("reviews.json", lambda d: d["reviews"][0].update(style_id="DEMO-STYLE-NOPE"), "unknown style"),
    ("reviews.json", lambda d: d["reviews"][0].update(review_id="REAL-1"), "id must start"),
    ("reviews.json", lambda d: d["reviews"][0].update(source="scraped"), "synthetic"),
    ("reviews.json", lambda d: d["reviews"][0].update(created_at="16/10/2026"), "created_at"),
    ("reviews.json", lambda d: d["reviews"][0].update(rating=6), "rating"),
    ("reviews.json", lambda d: d["reviews"][0].update(language="French"), "language"),
    ("reviews.json", lambda d: d["reviews"].append(dict(d["reviews"][0])), "duplicate review"),
    ("products_v2.json", lambda d: d["products"].append(dict(d["products"][0])), "duplicate sku"),
    ("products_v2.json", lambda d: d["products"][-1].update(aliases=["Men 001"]), "alias"),
    ("products_v2.json", lambda d: d["products"][0].update(aliases="Toney"), "aliases"),
    ("products_v2.json", lambda d: d["products"][0].update(aliases=["Men 001", "men 001"]), "aliases"),
    ("customers_v2.json", lambda d: d["customers"].append(dict(d["customers"][0])), "duplicate customer"),
    ("customers_v2.json", lambda d: d["customers"][0].update(tier="gold"), "tier"),
    ("customers_v2.json", lambda d: d["customers"][0]["travel_minutes"].update({MTL: -1}), "travel_minutes"),
    ("customers_v2.json", lambda d: d["customers"][0]["travel_minutes"].update({"DEMO-STORE-NOPE": 5}), "travel_minutes"),
    ("customers_v2.json", lambda d: d["customers"][0].update(annual_spend_cents=12.5), "annual_spend_cents"),
    ("customers_v2.json", lambda d: d["customers"][0].update(location_label=" "), "location_label"),
    ("customers_v2.json", lambda d: d["customers"][0].update(customer_id="REAL-JOHN"), "DEMO-"),
    ("customers_v2.json", lambda d: d["customers"][0].update(source="provided_csv"), "synthetic"),
]


@pytest.mark.parametrize(("name", "change", "fragment"), BAD_V2, ids=[f"{n}-{f}-{i}" for i, (n, _, f) in enumerate(BAD_V2)])
def test_the_loader_rejects_broken_v2_records(aws_env, tmp_path, name, change, fragment):
    root = copy_demo(tmp_path)
    fixtures.load_bundle(root)  # the untouched copy is valid
    edit(root, name, change)
    with pytest.raises(DomainError) as exc:
        fixtures.load_bundle(root)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    assert "fixture error" in exc.value.message and fragment in exc.value.message


def test_a_broken_v2_file_is_an_error_not_a_silent_skip(aws_env, tmp_path):
    root = copy_demo(tmp_path)
    (root / "demo" / "orders_v2.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(DomainError) as exc:
        fixtures.load_bundle(root)
    assert "orders_v2.json" in exc.value.message


# ------------------------------------------------------------------------- the table


def test_seed_writes_the_v2_items(ddb):
    summary = fixtures.seed("default")
    assert summary.counts == {"customers": 5, "orders": 11, "order_lines": 20, "products": 56, "locations": 4, "inventory": 170}
    assert summary.written == 266
    john = store.get_item(store.customer_key(JOHN))
    assert (john["tier"], john["annual_spend_cents"], john["location_label"]) == ("silver", 180000, "Bronfman Building, Montreal")
    assert john["travel_minutes"] == {MTL: 5, LAVAL: 24, TOR: 540}
    assert all(isinstance(v, int) for v in john["travel_minutes"].values())  # never a Decimal
    assert store.get_item(store.customer_key("DEMO-CUST-AVA"))["tier"] == "none"
    black_95 = store.get_item(store.product_key(men_sku("BLACK", "9.5")))
    assert black_95["aliases"] == ["Men 001", MEN_ALIAS, "ALDO Toney"] and black_95["product_name"] == MEN_NAME
    assert store.get_item(store.product_key("DEMO-SKU-HERO-BLACK-075")).get("aliases") is None
    order_items = store.query("ORDER#DEMO-3001")
    assert sorted(i["sk"] for i in order_items) == sorted(["META", *[f"LINE#DEMO-3001-L{n}" for n in range(1, 11)]])
    assert [i["order_id"] for i in store.query(f"CUST#{DAVID}", index="GSI1")] == ["DEMO-3001"]
    assert store.query(f"CUST#{JOHN}", index="GSI1") == []  # John's order is created live through the personal agent
    sizes = store.query(f"STYLE#{MEN_STYLE}#BROWN", index="GSI1")
    assert [i["size"] for i in sizes] == [f"{8 + 0.5 * n:.1f}" for n in range(9)]


@pytest.mark.parametrize(
    ("variant", "black_95"),
    [("default", [3, 0, 1, 1]), ("dc_stockout", [0, 0, 1, 1]), ("all_stockout", [0, 0, 0, 0])],
)
def test_reset_applies_the_variants_to_the_mens_style(ddb, variant, black_95):
    fixtures.reset(variant)
    rows = {i["sk"]: i for i in store.query(f"INV#{men_sku('BLACK', '9.5')}")}
    assert [rows[f"LOC#{loc}"]["atp"] for loc in LOCATIONS] == black_95
    assert all(r["version"] == 1 for r in rows.values())
    brown = {i["sk"]: i["atp"] for i in store.query(f"INV#{men_sku('BROWN', '9.5')}")}
    assert [brown[f"LOC#{loc}"] for loc in LOCATIONS] == [4, 2, 1, 0]  # other colours are never overridden


def test_reset_restores_davids_order_and_the_mens_stock(ddb):
    fixtures.reset("default")
    line_key = store.line_key("DEMO-3001", "DEMO-3001-L2")
    store.update_item(line_key, add={"returned_or_reserved_qty": 1})
    store.update_item(store.inventory_key(men_sku("BROWN", "9.5"), MTL), add={"atp": -2, "version": 3})
    fixtures.reset("default")
    assert store.get_item(line_key)["returned_or_reserved_qty"] == 0
    stock = store.get_item(store.inventory_key(men_sku("BROWN", "9.5"), MTL))
    assert (stock["atp"], stock["version"]) == (2, 1)
