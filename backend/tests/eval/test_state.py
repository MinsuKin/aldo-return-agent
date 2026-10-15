"""DynamoDB readers: snapshot views, diffs, tolerant item accessors, key parity with returns_domain.store."""

from __future__ import annotations

from decimal import Decimal

import pytest

from .conftest import SCRIPT


def seed(tables, items, pa_items=()):
    for item in items:
        tables.aldo.put_item(Item=item)
    for item in pa_items:
        tables.pa.put_item(Item=item)


@pytest.fixture
def tables(ev, aws):
    from tests.eval.fake_system import create_tables

    resource = ev.boto3.resource("dynamodb", region_name="us-east-1")
    create_tables(resource)
    return ev.Tables(resource)


def test_snapshot_reads_numbers_as_ints_and_builds_views(ev, tables):
    seed(tables, [
        {"pk": "INV#SKU-1", "sk": "LOC#L1", "atp": Decimal(3), "version": Decimal(1)},
        {"pk": "ORDER#O1", "sk": "LINE#O1-L1", "returned_or_reserved_qty": Decimal(1), "purchased_qty": Decimal(2)},
        {"pk": "ORDER#DEMO-A2A-ABC123", "sk": "META", "channel": "agent"},
        {"pk": "TXN#T1", "sk": "META", "state": "completed"}, {"pk": "IDEM#A1", "sk": "META", "transaction_id": "T1"},
        {"pk": "QUOTE#Q1", "sk": "META", "status": "open"}, {"pk": "QUOTE#Q1", "sk": "APPROVAL", "approval_id": "A1"},
        {"pk": "A2AOUT#MU-1", "sk": "META", "status": "pending"}, {"pk": "OUTBOX#MU-2", "sk": "META", "status": "pending"},
        {"pk": "RUN#R1", "sk": "META", "kind": "approval"}, {"pk": "RUN#A2A-DEMO-CUST-AVA", "sk": "META"},
        {"pk": "RUN#A2A-DEMO-CUST-AVA", "sk": "EVT#000002", "type": "b"}, {"pk": "RUN#A2A-DEMO-CUST-AVA", "sk": "EVT#000001", "type": "a"},
        {"pk": "CONV#C1", "sk": "META", "messages": "x" * 100},
    ], [{"pk": "OWNER#DEMO-CUST-AVA", "sk": "UPD#MU-1"}, {"pk": "OWNER#DEMO-CUST-AVA", "sk": "MEM#PM-1"}, {"pk": "OWNER#DEMO-CUST-AVA", "sk": "PCONV#P1"}])
    snap = tables.snapshot(pa=True)
    assert snap.inv()[("SKU-1", "L1")]["atp"] == 3 and isinstance(snap.inv()[("SKU-1", "L1")]["atp"], int)
    assert snap.lines()[("O1", "O1-L1")]["returned_or_reserved_qty"] == 1
    assert list(snap.agent_orders()) == ["DEMO-A2A-ABC123"] and list(snap.txns()) == ["T1"] and list(snap.idems()) == ["A1"]
    assert list(snap.quotes()) == ["Q1"] and list(snap.approvals()) == ["Q1"]
    assert sorted(snap.outbox()) == ["MU-1", "MU-2"]  # contract name A2AOUT# and the on-disk store name OUTBOX#
    assert list(snap.run_metas()) == ["R1"]  # the A2A stream is not a run
    assert [e["type"] for e in snap.a2a_events("DEMO-CUST-AVA")] == ["a", "b"]
    assert ("CONV#C1", "META") not in snap.aldo  # conversations are skipped
    assert list(snap.pa_updates("DEMO-CUST-AVA")) == ["MU-1"] and snap.pa_memory_owners() == {"DEMO-CUST-AVA"}
    assert tables.snapshot().pa == {}  # the personal-agent table is read only when asked


def test_diff_and_deltas(ev, tables):
    seed(tables, [{"pk": "INV#S", "sk": "LOC#L", "atp": 5}, {"pk": "ORDER#O", "sk": "LINE#O-L1", "returned_or_reserved_qty": 0},
                  {"pk": "AUDIT#2026-10-15", "sk": "a#1", "action": "x"}])
    before = tables.snapshot()
    tables.aldo.update_item(Key={"pk": "INV#S", "sk": "LOC#L"}, UpdateExpression="SET atp = :a", ExpressionAttributeValues={":a": 4})
    tables.aldo.update_item(Key={"pk": "ORDER#O", "sk": "LINE#O-L1"}, UpdateExpression="SET returned_or_reserved_qty = :a", ExpressionAttributeValues={":a": 1})
    seed(tables, [{"pk": "AUDIT#2026-10-15", "sk": "a#2", "action": "y"}])
    after = tables.snapshot()
    assert ev.inv_deltas(before, after) == {("S", "L"): -1} and ev.line_qty_deltas(before, after) == {("O", "O-L1"): 1}
    same, detail = ev.snapshots_equal(before, after)
    assert not same and "changed 2" in detail and "INV#S/LOC#L" in detail
    only_audit = tables.snapshot()
    seed(tables, [{"pk": "AUDIT#2026-10-15", "sk": "a#3", "action": "z"}])
    assert ev.snapshots_equal(only_audit, tables.snapshot())[0]  # audit events are allowed by "writes nothing"
    assert not ev.snapshots_equal(only_audit, tables.snapshot(), ignore_pk=())[0]


def test_txn_view_accepts_flat_and_nested_option_snapshots(ev):
    nested = {"state": "completed", "customer_id": "C", "quote_id": "Q", "order_id": "O", "line_id": "L",
              "option": {"type": "exchange_store_pickup", "location_id": "LOC", "sku": "S", "replacement_size": "7.5", "eta_date": "2026-10-17"}}
    flat = {"state": "completed", "customer_id": "C", "quote_id": "Q", "order_id": "O", "line_id": "L", "option_type": "exchange_store_pickup",
            "location_id": "LOC", "sku": "S", "replacement_size": "7.5", "eta_date": "2026-10-17"}
    assert ev.txn_view(nested) == ev.txn_view(flat) and ev.txn_view(flat)["option_type"] == "exchange_store_pickup"
    assert ev.dig({"a": {"b": None}}, "a.b", "c", default=5) == 5


def test_backdate_expiry_needs_the_attribute(ev, tables):
    seed(tables, [{"pk": "SIZEREQ#SZR-AAAAAA", "sk": "META", "expires_at": "2099-01-01T00:00:00+00:00"}, {"pk": "SIZEREQ#SZR-BBBBBB", "sk": "META"}])
    assert ev.Tables.backdate_expiry(tables, "SZR-AAAAAA").startswith("2020")
    assert tables.snapshot().sizereqs()["SZR-AAAAAA"]["expires_at"].startswith("2020")
    with pytest.raises(ev.ScenarioAbort):
        tables.backdate_expiry("SZR-BBBBBB")
    with pytest.raises(ev.ScenarioAbort):
        tables.backdate_expiry("SZR-MISSING")


def test_key_formats_match_returns_domain_store(ev):
    store = pytest.importorskip("returns_domain.store")
    assert store.txn_key("T") == ("TXN#T", "META") and store.idem_key("A") == ("IDEM#A", "META")
    assert store.quote_key("Q") == ("QUOTE#Q", "META") and store.approval_key("Q") == ("QUOTE#Q", "APPROVAL")
    assert store.case_key("C") == ("CASE#C", "META") and store.pchk_key("P") == ("PCHK#P", "META")
    assert store.inventory_key("S", "L") == ("INV#S", "LOC#L") and store.line_key("O", "L1") == ("ORDER#O", "LINE#L1")
    assert store.order_key("O") == ("ORDER#O", "META") and store.run_key("R") == ("RUN#R", "META")
    assert store.event_key("R", 3) == ("RUN#R", "EVT#000003") and store.sizereq_key("Z") == ("SIZEREQ#Z", "META")
    assert store.outbox_key("MU-1")[0].startswith(ev.OUTBOX_PREFIXES) and store.outbox_key("MU-1")[1] == "META"
    # STORENOTE# (domain v2 S17A: the store-desk notice of an in-store exchange or return) is deleted by
    # fixtures.reset through store.RUNTIME_PREFIXES; scripts/eval_scenarios.py should list it in
    # ALDO_RUNTIME_PREFIXES with the J2 scenario (eval v2). Tolerated here until then.
    not_yet_in_the_eval_script = {"STORENOTE#", "BENEFIT#"}  # BENEFIT#: the monthly perk ledger (domain v2 S17B)
    assert set(store.RUNTIME_PREFIXES) - {"AUDIT#"} - not_yet_in_the_eval_script <= set(ev.ALDO_RUNTIME_PREFIXES), (
        "store has a runtime partition the eval's reset check does not know")


def test_fixture_facts_come_from_fixtures_not_from_code(ev):
    facts = ev.load_fixture_facts()
    assert facts.hero_query in facts.hero_product_name and facts.hero_price_cents == 12999
    sku = ev.hero_sku("7.5")
    assert sku == "DEMO-SKU-HERO-BLACK-075"
    assert facts.inventory["default"][(sku, "DEMO-DC-QC")]["atp"] == 5 and facts.inventory["dc_stockout"][(sku, "DEMO-DC-QC")]["atp"] == 0
    assert facts.inventory["dc_stockout"][(sku, "DEMO-STORE-MTL-01")]["atp"] == 2 and facts.inventory["all_stockout"][(sku, "DEMO-STORE-MTL-01")]["atp"] == 0
    assert facts.lines[("DEMO-1001", "DEMO-1001-L1")] == 0 and facts.lines[("DEMO-1008", "DEMO-1008-L1")] == 1
    assert facts.typical_sizes["DEMO-CUST-AVA"] == "7.5" and facts.home_regions["DEMO-CUST-AVA"] == "QC"
    assert {"Retailer A (synthetic)", "Retailer A"} <= set(facts.leak_terms("DEMO-CUST-AVA"))
    assert facts.hero_product_name not in SCRIPT.read_text(encoding="utf-8"), "product names must come from fixtures, not from the script"
