"""The eval's assertions against state written by the REAL domain code (store, execution, gateway on moto).

The simulator proves the scenarios' logic; this file proves the item shapes the assertions read are the
shapes the domain really writes (TXN, IDEM, approval, SIZEREQ, orders, A2A events, outbox).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from returns_domain import auth, execution, gateway

from ..conftest import AVA


@pytest.fixture
def tables(ev, ddb):
    return ev.Tables(ev.boto3.resource("dynamodb", region_name="us-east-1"))


def failures(rec) -> list[tuple[str, str]]:
    return [(c.name, c.detail) for c in rec.failed_checks]


def test_exchange_assertions_hold_for_real_execution_state(ev, flow, no_flush, tables):
    base = tables.snapshot()
    hq = flow.hero_quote()
    flow.approve(hq)
    execution.execute(AVA, hq.quote_id)
    after = tables.snapshot()
    rec = ev.Recorder("A", 1, "default")
    option = hq.option("exchange_store_pickup").model_dump(mode="json")
    tid = ev.check_exchange_committed(rec, base, after, customer_id=AVA, order_id="DEMO-1001", line_id="DEMO-1001-L1",
                                      quote_id=hq.quote_id, option=option, run=None)
    assert tid and not rec.failed_checks, failures(rec)
    assert len(after.outbox()) == 1, "an exchange writes exactly one write-back outbox item"
    payload = next(iter(after.outbox().values()))["payload"]
    assert (payload["owner_ref"], payload["outcome"], payload["provisional"], payload["reason"]) == (AVA, "exchange_created", True, "TOO_SMALL")
    assert (payload["original_size"], payload["new_size"]) == ("7.0", "7.5")


def test_writeback_row_in_the_personal_agent_table_has_the_fields_p4_reads(ev, flow, no_flush, tables, monkeypatch):
    memory = pytest.importorskip("personal_agent.memory")
    monkeypatch.setenv("PERSONAL_AGENT_TABLE_NAME", "aldo-personal-agent-demo")
    repo = memory.get_repo()
    repo.create_table()
    memory.seed_memory()
    from ..conftest import make_agent_order

    order_id = make_agent_order(delivered_at="2026-10-16")
    hq = flow.hero_quote(order_id=order_id, line_id=f"{order_id}-L1", replacement_size="8.5")
    flow.approve(hq)
    execution.execute(AVA, hq.quote_id)
    snap = tables.snapshot(pa=True)
    update_id, item = next(iter(snap.outbox().items()))
    result = memory.apply_update(item["payload"])
    assert result["applied"] is True and memory.apply_update(item["payload"])["duplicate"] is True
    snap = tables.snapshot(pa=True)
    row = snap.pa_updates(AVA)[update_id]
    assert (row["original_size"], row["new_size"], row["outcome"], row["provisional"], row["status"]) == ("8.0", "8.5", "exchange_created", True, "active")
    assert len(snap.pa_updates(AVA)) == 1 and snap.pa_memory_owners() >= {AVA}
    assert ev.check_reset_state.__name__  # the reset check reads MEM# rows through pa_memory_owners()


def test_noop_calls_are_seen_as_no_commit(ev, flow, no_flush, tables):
    base = tables.snapshot()
    flow.hero_quote()  # a quote only: nothing may be committed
    rec = ev.Recorder("A", 1, "default")
    ev.check_no_commit(rec, base, tables.snapshot(), prefix="before_approval_", allow_quotes=True)
    assert not rec.failed_checks, failures(rec)
    bad = ev.Recorder("A", 1, "default")
    ev.check_no_commit(bad, base, tables.snapshot(), prefix="x_", allow_quotes=False)
    assert [c.name for c in bad.failed_checks] == ["x_no_quote"]


def test_purchase_assertions_hold_for_the_real_gateway(ev, seeded, no_flush, tables):
    facts = ev.load_fixture_facts()
    ctx = SimpleNamespace(facts=facts)
    base = tables.snapshot()
    claims = auth.verify_claims(auth.sign_token(AVA, scope="delegated_gateway", actor="personal_agent").token, "delegated_gateway")
    assist = gateway.size_assist(claims, {"intent": "size_assist", "product_query": facts.hero_query, "color": "BLACK",
                                          "size_system": "US_W", "deadline_date": "2026-10-17", "channel_pref": None}).model_dump(mode="json")
    size, option = ev.find_offer(assist, size="8.0", type_="store_pickup", location_id="DEMO-STORE-MTL-01")
    rid = assist["size_request_id"]
    gateway.decide_size(claims, {"size_request_id": rid, "option_id": option["option_id"], "chosen_size": size,
                                 "rationale_public": "runs small per ALDO signal", "confidence": "high"})
    proposed = tables.snapshot()
    rec = ev.Recorder("P1", 1, "default")
    found = ev.check_proposal(rec, base, proposed, ctx, customer_id=AVA, want_montreal_pickup=True)
    assert found is not None and not rec.failed_checks, failures(rec)
    _rid, sr = found
    result = gateway.confirm_purchase(AVA, rid, option["option_id"])
    final = tables.snapshot()
    resp = ev.ApiResponse(200, result.model_dump(mode="json"))
    done = ev.Recorder("P1", 1, "default")
    ev.check_purchase_confirmed(done, base, final, ctx, customer_id=AVA, rid=rid, sr=sr, resp=resp)
    assert not done.failed_checks, failures(done)
    assert final.sizereqs()[rid]["approved_by"] == "owner_click"
    # replaying the click writes nothing: the same proof P3 uses against the deployed API
    again = gateway.confirm_purchase(AVA, rid, option["option_id"])
    assert again.replayed is True and ev.snapshots_equal(final, tables.snapshot())[0]
