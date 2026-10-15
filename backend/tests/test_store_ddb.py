"""store.py: Decimal/float layer (G-03), sizes, ids, key builders, helpers, transactions (G-04).

The first half is pure (no AWS). The second half runs against moto DynamoDB.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

import pytest
from botocore.stub import Stubber
from pydantic import BaseModel

from returns_domain import store
from returns_domain.errors import DomainError, ErrorCode


def walk(obj):
    """Yield every scalar inside nested dicts and lists."""
    if isinstance(obj, dict):
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from walk(value)
    else:
        yield obj


# ------------------------------------------------------------------ Decimal / float layer


def test_to_ddb_turns_floats_into_decimals_everywhere():
    stored = store.to_ddb({"size": 7.5, "share": 0.799, "nested": [{"x": 0.1}], "n": 3, "t": (1.5, 2)})
    assert stored["size"] == Decimal("7.5")
    assert stored["share"] == Decimal("0.799")
    assert stored["nested"][0]["x"] == Decimal("0.1")
    assert stored["n"] == 3 and isinstance(stored["n"], int)
    assert stored["t"] == [Decimal("1.5"), 2]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), Decimal("NaN")])
def test_to_ddb_refuses_nan_and_infinity(bad):
    with pytest.raises(DomainError) as exc:
        store.to_ddb({"x": [bad]})
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_to_ddb_handles_models_enums_datetimes_and_sets():
    class Color(StrEnum):
        BLACK = "BLACK"

    class Model(BaseModel):
        name: str
        price: float
        color: Color

    @dataclass
    class Point:
        x: float

    stamp = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    stored = store.to_ddb(
        {"m": Model(name="a", price=1.25, color=Color.BLACK), "p": Point(0.5), "when": stamp,
         "day": date(2026, 10, 17), "tags": {"b", "a"}, "raw": b"hi", "c": Color.BLACK}
    )
    assert stored["m"] == {"name": "a", "price": Decimal("1.25"), "color": "BLACK"}
    assert type(stored["m"]["color"]) is str
    assert stored["p"] == {"x": Decimal("0.5")}
    assert stored["when"] == "2026-10-16T12:00:00+00:00"
    assert stored["day"] == "2026-10-17"
    assert stored["tags"] == ["a", "b"]
    assert stored["raw"] == "aGk="
    assert type(stored["c"]) is str


def test_to_ddb_rejects_unknown_types():
    with pytest.raises(DomainError):
        store.to_ddb({"x": object()})


def test_from_ddb_returns_int_when_integral_else_float():
    out = store.from_ddb({"a": Decimal("12999"), "b": Decimal("7.50"), "c": Decimal("7.0"),
                          "d": [Decimal("0.799"), {"e": Decimal("3")}]})
    assert out == {"a": 12999, "b": 7.5, "c": 7, "d": [0.799, {"e": 3}]}
    assert isinstance(out["a"], int) and isinstance(out["b"], float)


def test_json_safe_never_returns_decimal_and_is_json_dumpable():
    class Model(BaseModel):
        cents: Decimal
        share: Decimal

    payload = {
        "m": Model(cents=Decimal("12999"), share=Decimal("0.799")),
        "nan": float("nan"),
        "when": datetime(2026, 10, 16, 8, 0, tzinfo=UTC),
        "ids": {"b", "a"},
        "raw": b"\x00\x01",
        "d": Decimal("7.5"),
    }
    safe = store.json_safe(payload)
    assert not any(isinstance(v, Decimal) for v in walk(safe))
    assert safe["m"] == {"cents": 12999, "share": 0.799}
    assert safe["nan"] is None
    assert safe["when"] == "2026-10-16T08:00:00+00:00"
    assert safe["ids"] == ["a", "b"]
    assert safe["raw"] == "AAE="
    assert safe["d"] == 7.5
    json.dumps(safe)  # must not raise


def test_json_safe_rejects_unknown_types():
    with pytest.raises(DomainError) as exc:
        store.json_safe({"x": object()})
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


def test_canonical_json_is_order_independent_and_compact():
    a = store.canonical_json({"b": 1, "a": [Decimal("2"), 7.5]})
    b = store.canonical_json({"a": [2, 7.5], "b": 1})
    assert a == b == '{"a":[2,7.5],"b":1}'


# ---------------------------------------------------------------------------- sizes and ids


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7", "7.0"), (7, "7.0"), (7.0, "7.0"), (7.5, "7.5"), ("7.5", "7.5"), (Decimal("7.50"), "7.5"),
     (" 10 ", "10.0"), ("10.0", "10.0"), (Decimal(6), "6.0"), ("8.500", "8.5")],
)
def test_size_str_normalizes(raw, expected):
    assert store.size_str(raw) == expected


@pytest.mark.parametrize(
    "bad", ["", "abc", "7,5", "7.25", 7.25, None, True, float("nan"), float("inf"), "-7", 0, 31, "0.2",
            [7], Decimal("NaN"), "7.5.1", "1e1"],
)
def test_size_str_rejects_invalid(bad):
    with pytest.raises(DomainError) as exc:
        store.size_str(bad)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_new_id_and_derived_id_formats():
    one = store.new_id("DEMO-RMA-")
    assert one.startswith("DEMO-RMA-") and len(one) == len("DEMO-RMA-") + 6
    assert one.split("-")[-1] == one.split("-")[-1].upper()
    assert store.new_id("X-", 8) != store.new_id("X-", 8)
    assert len(store.new_id("X-", 10)) == 12
    first = store.derived_id("QUO-", "RUN-1", "CAND-1", "OPT-1,OPT-2")
    assert first == store.derived_id("QUO-", "RUN-1", "CAND-1", "OPT-1,OPT-2")
    assert first != store.derived_id("QUO-", "RUN-1", "CAND-1", "OPT-1")
    assert len(first) == len("QUO-") + 10 and first[4:] == first[4:].upper()
    assert len(store.derived_id("DEMO-CASE-", "RUN-1", "POLICY_REVIEW", n=6)) == len("DEMO-CASE-") + 6


# ---------------------------------------------------------------------------- key builders


def test_key_builders_follow_the_design():
    assert store.customer_key("C") == ("CUST#C", "PROFILE")
    assert store.order_key("O") == ("ORDER#O", "META")
    assert store.line_key("O", "L") == ("ORDER#O", "LINE#L")
    assert store.product_key("S") == ("PRODUCT#S", "META")
    assert store.location_key("L") == ("LOC#L", "META")
    assert store.inventory_key("S", "L") == ("INV#S", "LOC#L")
    assert store.pchk_key("P") == ("PCHK#P", "META")
    assert store.cand_key("C") == ("CAND#C", "META")
    assert store.quote_key("Q") == ("QUOTE#Q", "META")
    assert store.approval_key("Q") == ("QUOTE#Q", "APPROVAL")
    assert store.txn_key("T") == ("TXN#T", "META")
    assert store.idem_key("A") == ("IDEM#A", "META")
    assert store.case_key("C") == ("CASE#C", "META")
    assert store.conv_key("V") == ("CONV#V", "META")
    assert store.run_key("R") == ("RUN#R", "META")
    assert store.event_key("R", 7) == ("RUN#R", "EVT#000007")
    assert store.audit_key("2026-10-16", "2026-10-16T16:00:00+00:00", "u") == (
        "AUDIT#2026-10-16", "2026-10-16T16:00:00+00:00#u")
    assert store.sizereq_key("SZR-1") == ("SIZEREQ#SZR-1", "META")
    assert store.outbox_key("MU-1") == ("A2AOUT#MU-1", "META")
    assert store.storenote_key("DEMO-RMA-3F9A1C") == ("STORENOTE#DEMO-RMA-3F9A1C", "META")  # domain v2
    assert store.gsi1_order("C", "2026-09-30", "O") == ("CUST#C", "ORDER#2026-09-30#O")
    assert store.gsi1_product("ST", "BLACK", "7.5") == ("STYLE#ST#BLACK", "SIZE#007.5")
    assert store.gsi1_product("ST", "BLACK", "10") == ("STYLE#ST#BLACK", "SIZE#010.0")
    assert store.gsi1_case("2026-10-16T16:00:00+00:00") == ("CASES", "2026-10-16T16:00:00+00:00")
    assert store.gsi1_outbox_pending("t") == ("A2AOUT#pending", "t")


@pytest.mark.parametrize("builder", [store.order_key, store.customer_key, store.quote_key])
@pytest.mark.parametrize("bad", ["", None, 5, "x" * 300])
def test_key_builders_reject_bad_ids(builder, bad):
    with pytest.raises(DomainError) as exc:
        builder(bad)
    assert exc.value.code == ErrorCode.VALIDATION_ERROR


def test_event_keys_sort_lexicographically_by_seq():
    keys = [store.event_key("R", n)[1] for n in (1, 2, 10, 100, 999999)]
    assert keys == sorted(keys)


def test_runtime_prefixes_cover_every_runtime_partition():
    for prefix in ("CONV#", "RUN#", "PCHK#", "CAND#", "QUOTE#", "TXN#", "IDEM#", "CASE#", "AUDIT#",
                   "SIZEREQ#", "A2AOUT#", "STORENOTE#", "ORDER#DEMO-A2A-"):
        assert prefix in store.RUNTIME_PREFIXES
    # fixture partitions must never be matched by a runtime prefix
    for fixture_pk in ("CUST#DEMO-CUST-AVA", "ORDER#DEMO-1001", "PRODUCT#DEMO-SKU-X", "LOC#DEMO-DC-QC",
                       "INV#DEMO-SKU-X"):
        assert not fixture_pk.startswith(store.RUNTIME_PREFIXES)
    assert "ORDER#DEMO-A2A-81B20E".startswith(store.RUNTIME_PREFIXES)


def test_order_items_builds_meta_and_lines_with_keys():
    order = {"order_id": "DEMO-A2A-AAAAAA", "customer_id": "DEMO-CUST-AVA", "ordered_at": "2026-10-16",
             "delivered_at": None, "channel": "agent", "lines": [{"line_id": "x"}]}
    items = store.order_items(order, [{"line_id": "DEMO-A2A-AAAAAA-L1", "sku": "S", "size": "8.0"}])
    meta, line = items
    assert (meta["pk"], meta["sk"]) == ("ORDER#DEMO-A2A-AAAAAA", "META")
    assert (meta["gsi1pk"], meta["gsi1sk"]) == ("CUST#DEMO-CUST-AVA", "ORDER#2026-10-16#DEMO-A2A-AAAAAA")
    assert "lines" not in meta and meta["delivered_at"] is None
    assert (line["pk"], line["sk"]) == ("ORDER#DEMO-A2A-AAAAAA", "LINE#DEMO-A2A-AAAAAA-L1")
    assert line["order_id"] == "DEMO-A2A-AAAAAA"


# ------------------------------------------------------------- expression compilation (pure)


def test_expressions_use_only_referenced_names_and_values():
    built = store._build_expressions(
        condition="#status = :open AND #size <> :other", values={"open": "open", "other": "x", "unused": 1},
        set_values={"status": "executed"}, add_values={"seq": 1},
    )
    assert built["ExpressionAttributeNames"] == {"#seq": "seq", "#size": "size", "#status": "status"}
    assert set(built["ExpressionAttributeValues"]) == {":open", ":other", ":setv0", ":addv0"}
    assert built["UpdateExpression"] == "SET #status = :setv0 ADD #seq :addv0"


def test_expressions_accept_values_with_a_leading_colon_and_must_exist():
    built = store._build_expressions(condition="#a = :v", values={":v": 1}, set_values={"a": 2}, must_exist=True)
    assert built["ConditionExpression"] == "attribute_exists(#pk) AND (#a = :v)"
    assert built["ExpressionAttributeNames"]["#pk"] == "pk"


def test_expressions_report_missing_values_and_bad_names():
    with pytest.raises(DomainError):
        store._build_expressions(condition="#a = :missing")
    with pytest.raises(DomainError):
        store._build_expressions(set_values={"bad-name": 1})
    with pytest.raises(DomainError):  # :setv0 is reserved for the first SET value
        store._build_expressions(condition="#a = :setv0", values={"setv0": 1}, set_values={"a": 2})


def test_transaction_canceled_helpers():
    exc = store.TransactionCanceled(["ConditionalCheckFailed", None, "TransactionConflict"], ["idem", "line", "txn"])
    assert exc.failed() == ["idem"]
    assert exc.transient() is True
    assert store.TransactionCanceled([None, "ConditionalCheckFailed"], ["a", "b"]).transient() is False
    assert store.TransactionCanceled(["ThrottlingError"], ["a"]).transient() is True


# ============================================================================ moto-backed


def test_table_schema_and_create_table_is_idempotent(ddb):
    assert store.TABLE_SCHEMA["BillingMode"] == "PAY_PER_REQUEST"
    gsi = store.TABLE_SCHEMA["GlobalSecondaryIndexes"][0]
    assert gsi["IndexName"] == "GSI1" and gsi["Projection"] == {"ProjectionType": "ALL"}
    store.create_table()  # already exists: no error
    description = store.get_client().describe_table(TableName=store.table_name())["Table"]
    assert description["KeySchema"] == store.TABLE_SCHEMA["KeySchema"]
    assert [g["IndexName"] for g in description["GlobalSecondaryIndexes"]] == ["GSI1"]


def test_put_get_roundtrip_keeps_ints_floats_strings_and_none(ddb):
    store.put_item({"pk": "T#1", "sk": "META", "cents": 12999, "share": 0.799, "size": "7.5",
                    "nothing": None, "flag": False, "nested": {"qty": [1, 2.5]}})
    item = store.get_item(("T#1", "META"))
    assert item == {"pk": "T#1", "sk": "META", "cents": 12999, "share": 0.799, "size": "7.5",
                    "nothing": None, "flag": False, "nested": {"qty": [1, 2.5]}}
    assert isinstance(item["cents"], int) and item["size"] == "7.5"
    assert not any(isinstance(v, Decimal) for v in walk(item))
    assert store.get_item(("T#missing", "META")) is None


def test_put_item_condition_raises_condition_failed(ddb):
    store.put_item({"pk": "T#1", "sk": "META", "v": 1}, condition="attribute_not_exists(#pk)")
    with pytest.raises(store.ConditionFailed):
        store.put_item({"pk": "T#1", "sk": "META", "v": 2}, condition="attribute_not_exists(#pk)")
    assert store.get_item(("T#1", "META"))["v"] == 1


def test_reserved_words_work_in_conditions_and_updates(ddb):
    store.put_item({"pk": "T#1", "sk": "META", "status": "open", "name": "n", "size": "7.5", "type": "t",
                    "region": "QC", "source": "synthetic", "result": {}, "location": "L", "timestamp": "x"})
    item = store.update_item(("T#1", "META"), set={"status": "executed", "location": "M"},
                             condition="#status = :open AND #region = :qc", values={"open": "open", "qc": "QC"})
    assert item["status"] == "executed" and item["location"] == "M"
    with pytest.raises(store.ConditionFailed):
        store.update_item(("T#1", "META"), set={"status": "again"}, condition="#status = :open",
                          values={"open": "open"})
    assert store.get_item(("T#1", "META"))["status"] == "executed"


def test_update_item_add_remove_and_return_new(ddb):
    store.put_item({"pk": "T#1", "sk": "META", "seq": 0, "tmp": "x"})
    one = store.update_item(("T#1", "META"), add={"seq": 1})
    two = store.update_item(("T#1", "META"), add={"seq": 1}, remove=["tmp"])
    assert (one["seq"], two["seq"]) == (1, 2) and "tmp" not in two
    assert store.update_item(("T#1", "META"), add={"seq": -2}, return_new=False) is None
    assert store.get_item(("T#1", "META"))["seq"] == 0


def test_update_item_never_creates_a_stray_item_by_default(ddb):
    with pytest.raises(store.ConditionFailed):
        store.update_item(("T#ghost", "META"), set={"a": 1})
    assert store.get_item(("T#ghost", "META")) is None
    with pytest.raises(store.ConditionFailed):
        store.update_item(("T#ghost", "META"), set={"a": 1}, condition="attribute_not_exists(#a)")
    assert store.get_item(("T#ghost", "META")) is None
    store.update_item(("T#ghost", "META"), set={"a": 1}, must_exist=False)  # explicit upsert
    assert store.get_item(("T#ghost", "META"))["a"] == 1


def test_update_item_float_values_are_stored_as_numbers(ddb):
    store.put_item({"pk": "T#1", "sk": "META"})
    store.update_item(("T#1", "META"), set={"share": 0.799})
    assert store.get_item(("T#1", "META"))["share"] == 0.799


def test_delete_item(ddb):
    store.put_item({"pk": "T#1", "sk": "META"})
    store.delete_item(("T#1", "META"))
    assert store.get_item(("T#1", "META")) is None
    store.delete_item(("T#1", "META"))  # deleting a missing item is fine


def _put_events(count, pk="RUN#R"):
    for n in range(1, count + 1):
        store.put_item({"pk": pk, "sk": store.event_key("R", n)[1], "seq": n})
    store.put_item({"pk": pk, "sk": "META", "seq": count})


def test_query_conditions_order_and_limit(ddb):
    _put_events(12)
    assert [i["seq"] for i in store.query("RUN#R", sk_begins_with="EVT#")] == list(range(1, 13))
    assert [i["seq"] for i in store.query("RUN#R", sk_begins_with="EVT#", forward=False)][:3] == [12, 11, 10]
    assert [i["seq"] for i in store.query("RUN#R", sk_begins_with="EVT#", limit=5)] == [1, 2, 3, 4, 5]
    # sk > "EVT#000010" also matches META, which is why callers use sk_between for event cursors
    gt = store.query("RUN#R", sk_gt="EVT#000010")
    assert [i["sk"] for i in gt] == ["EVT#000011", "EVT#000012", "META"]
    between = store.query("RUN#R", sk_between=("EVT#000011", "EVT#999999"))
    assert [i["seq"] for i in between] == [11, 12]
    assert len(store.query("RUN#R")) == 13
    assert store.query("RUN#none") == []


def test_query_rejects_two_sort_key_conditions(ddb):
    with pytest.raises(DomainError):
        store.query("RUN#R", sk_begins_with="E", sk_gt="A")


def test_query_paginates_internally(ddb, monkeypatch):
    _put_events(30)
    client = store.get_client()
    real = client.query
    calls: list[dict] = []

    def small_pages(**kwargs):
        kwargs["Limit"] = 4  # force DynamoDB to return LastEvaluatedKey
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(client, "query", small_pages)
    items = store.query("RUN#R", sk_begins_with="EVT#")
    assert [i["seq"] for i in items] == list(range(1, 31))
    assert len(calls) >= 8


def test_query_gsi_is_ordered_and_not_consistent(ddb):
    for oid, day in (("O1", "2026-09-30"), ("O2", "2026-10-02"), ("O3", "2026-10-01")):
        pk, sk = store.order_key(oid)
        g_pk, g_sk = store.gsi1_order("C1", day, oid)
        store.put_item({"pk": pk, "sk": sk, "gsi1pk": g_pk, "gsi1sk": g_sk, "order_id": oid})
    newest_first = store.query("CUST#C1", index="GSI1", forward=False)
    assert [i["order_id"] for i in newest_first] == ["O2", "O3", "O1"]
    assert [i["order_id"] for i in store.query("CUST#C1", index="GSI1", sk_begins_with="ORDER#2026-10")] == ["O3", "O2"]


def test_batch_put_get_delete_beyond_the_batch_limits(ddb):
    items = [{"pk": f"B#{n:03d}", "sk": "META", "n": n} for n in range(130)]
    assert store.batch_put(items) == 130
    fetched = store.batch_get([(i["pk"], i["sk"]) for i in items])
    assert sorted(i["n"] for i in fetched) == list(range(130))
    assert store.batch_get([("B#missing", "META")]) == []
    assert store.batch_delete([(i["pk"], i["sk"]) for i in items]) == 130
    assert store.batch_get([(i["pk"], i["sk"]) for i in items]) == []
    with pytest.raises(DomainError):
        store.batch_put([{"pk": "D", "sk": "1"}, {"pk": "D", "sk": "1"}])


def test_batch_delete_retries_unprocessed_items(ddb, monkeypatch):
    store.batch_put([{"pk": f"U#{n}", "sk": "META"} for n in range(5)])
    client = store.get_client()
    real = client.batch_write_item
    state = {"calls": 0}

    def flaky(**kwargs):
        state["calls"] += 1
        table = store.table_name()
        requests = kwargs["RequestItems"][table]
        if state["calls"] == 1:  # process none, return everything as unprocessed
            return {"UnprocessedItems": {table: requests}}
        return real(**kwargs)

    monkeypatch.setattr(client, "batch_write_item", flaky)
    monkeypatch.setattr(store, "_backoff", lambda attempt: None)
    assert store.batch_delete([(f"U#{n}", "META") for n in range(5)]) == 5
    assert state["calls"] == 2
    assert store.batch_get([(f"U#{n}", "META") for n in range(5)]) == []


def test_scan_keys_filters_by_pk_prefix(ddb):
    for pk in ("RUN#1", "RUN#2", "QUOTE#1", "CUST#1", "ORDER#DEMO-1001", "ORDER#DEMO-A2A-X"):
        store.put_item({"pk": pk, "sk": "META"})
    found = set(store.scan_keys(("RUN#", "QUOTE#", "ORDER#DEMO-A2A-")))
    assert found == {("RUN#1", "META"), ("RUN#2", "META"), ("QUOTE#1", "META"), ("ORDER#DEMO-A2A-X", "META")}
    assert list(store.scan_keys(())) == []


# ---------------------------------------------------------------------------- transactions


def test_transact_write_all_or_nothing_with_labelled_reasons(ddb):
    store.put_item({"pk": "A#1", "sk": "META", "atp": 1})
    store.put_item({"pk": "A#2", "sk": "META", "n": 0})
    store.put_item({"pk": "A#taken", "sk": "META"})
    ops = [
        store.TxPut({"pk": "A#new", "sk": "META"}, condition="attribute_not_exists(#pk)", label="new"),
        store.TxPut({"pk": "A#taken", "sk": "META"}, condition="attribute_not_exists(#pk)", label="taken"),
        store.TxUpdate(("A#1", "META"), add={"atp": -1}, condition="#atp >= :qty", values={"qty": 2}, label="stock"),
        store.TxUpdate(("A#2", "META"), add={"n": 1}, label="counter"),
    ]
    with pytest.raises(store.TransactionCanceled) as exc:
        store.transact_write(ops)
    assert exc.value.labels == ["new", "taken", "stock", "counter"]
    assert set(exc.value.failed()) == {"taken", "stock"}
    assert exc.value.transient() is False
    # nothing was written
    assert store.get_item(("A#new", "META")) is None
    assert store.get_item(("A#1", "META"))["atp"] == 1 and store.get_item(("A#2", "META"))["n"] == 0


def test_transact_write_commits_every_operation(ddb):
    store.put_item({"pk": "A#1", "sk": "META", "atp": 3})
    store.put_item({"pk": "A#chk", "sk": "META", "status": "open"})
    store.transact_write([
        store.TxPut({"pk": "A#new", "sk": "META", "x": 1.5}, condition="attribute_not_exists(#pk)"),
        store.TxUpdate(("A#1", "META"), add={"atp": -1}, condition="#atp >= :qty", values={"qty": 1}),
        store.TxCheck(("A#chk", "META"), condition="#status = :open", values={"open": "open"}),
    ])
    assert store.get_item(("A#new", "META"))["x"] == 1.5
    assert store.get_item(("A#1", "META"))["atp"] == 2


def test_transact_update_on_a_missing_item_is_cancelled_not_upserted(ddb):
    with pytest.raises(store.TransactionCanceled) as exc:
        store.transact_write([store.TxUpdate(("A#ghost", "META"), set={"a": 1}, label="ghost")])
    assert exc.value.failed() == ["ghost"]
    assert store.get_item(("A#ghost", "META")) is None


def test_transact_write_needs_operations(ddb):
    with pytest.raises(DomainError):
        store.transact_write([])


def _stub_cancellation(codes):
    return {"CancellationReasons": [{"Code": c, "Message": "m"} for c in codes]}


def test_botocore_transaction_conflict_maps_to_a_transient_cancellation(ddb):
    client = store.get_client()
    with Stubber(client) as stub:
        stub.add_client_error("transact_write_items", service_error_code="TransactionCanceledException",
                              service_message="cancelled", http_status_code=400,
                              modeled_fields=_stub_cancellation(["None", "TransactionConflict", "ConditionalCheckFailed"]))
        with pytest.raises(store.TransactionCanceled) as exc:
            store.transact_write([store.TxPut({"pk": "X", "sk": "1"}, label="a"),
                                  store.TxPut({"pk": "X", "sk": "2"}, label="b"),
                                  store.TxPut({"pk": "X", "sk": "3"}, label="c")])
    assert exc.value.reasons == [None, "TransactionConflict", "ConditionalCheckFailed"]
    assert exc.value.transient() is True and exc.value.failed() == ["c"]


def test_top_level_conflict_and_throttling_errors(ddb):
    client = store.get_client()
    with Stubber(client) as stub:
        stub.add_client_error("transact_write_items", service_error_code="TransactionConflictException",
                              http_status_code=400)
        with pytest.raises(store.TransactionCanceled) as exc:
            store.transact_write([store.TxPut({"pk": "X", "sk": "1"}, label="a")])
        assert exc.value.transient() and exc.value.reasons == ["TransactionConflict"]
        stub.add_client_error("transact_write_items", service_error_code="ProvisionedThroughputExceededException",
                              http_status_code=400)
        with pytest.raises(store.TransactionCanceled) as throttled:
            store.transact_write([store.TxPut({"pk": "X", "sk": "1"}, label="a")])
        assert throttled.value.transient() and throttled.value.reasons == ["ThrottlingError"]
        stub.add_client_error("transact_write_items", service_error_code="InternalServerError",
                              http_status_code=500)
        with pytest.raises(DomainError) as upstream:
            store.transact_write([store.TxPut({"pk": "X", "sk": "1"}, label="a")])
        assert upstream.value.code == ErrorCode.UPSTREAM_TIMEOUT and upstream.value.retryable is True


def test_throttling_on_reads_is_a_retryable_upstream_error(ddb):
    client = store.get_client()
    with Stubber(client) as stub:
        stub.add_client_error("get_item", service_error_code="ThrottlingException", http_status_code=400)
        with pytest.raises(DomainError) as exc:
            store.get_item(("T#1", "META"))
    assert exc.value.code == ErrorCode.UPSTREAM_TIMEOUT and exc.value.retryable


def test_concurrent_counter_updates_do_not_lose_increments(ddb):
    store.put_item({"pk": "T#1", "sk": "META", "seq": 0})
    seen: list[int] = []
    lock = threading.Lock()

    def bump():
        item = store.update_item(("T#1", "META"), add={"seq": 1})
        with lock:
            seen.append(item["seq"])

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == list(range(1, 11))


def test_reset_clients_drops_the_cached_client(ddb):
    first = store.get_client()
    assert store.get_client() is first
    store.reset_clients()
    assert store.get_client() is not first
