"""Every scenario against the scripted simulator: a correct system passes, each injected defect is caught."""

from __future__ import annotations

import pytest

SCENARIOS = ["A", "B", "C", "D", "E", "H", "P1", "P2", "P3", "P4"]


def failed(row) -> set[str]:
    return {c["name"] for c in row["failed_checks"]}


@pytest.mark.parametrize("sid", SCENARIOS)
def test_correct_system_passes(run_scenario, sid):
    rows, _ctx, _system = run_scenario(sid)
    row = rows[0]
    assert row["passed"], (row["error"], row["failed_checks"])
    assert row["checks_failed"] == 0 and row["checks_passed"] > 10


def test_three_runs_reset_between_runs_and_all_pass(run_scenario):
    rows, _ctx, _system = run_scenario("P1", runs=3)
    assert [r["run"] for r in rows] == [1, 2, 3]
    assert all(r["passed"] for r in rows), [r["failed_checks"] for r in rows]


# (scenario, fault, a check name that must fail)
FAULT_MATRIX = [
    ("A", "double_txn", "exactly_one_transaction"),
    ("A", "no_atp_decrement", "atp_down_by_one_at_chosen_location"),
    ("A", "early_side_effect", "before_approval_atp_unchanged"),
    ("B", "double_txn", "exactly_one_transaction"),
    ("D", "no_case", "d_cases_created"),
    ("E", "approve_replay_new_run", "double_click_returns_same_approval_and_run"),
    ("E", "double_txn", "exactly_one_transaction"),
    ("H", "no_audit_on_denied", "agent_attempt_audited_as_access_denied"),
    ("P1", "bot_places_order", "no_order_before_owner_click"),
    ("P1", "leak_retailer", "after_click_a2a_stream_is_public_only"),
    ("P2", "bot_places_order", "atp_unchanged_before_owner_click"),
    ("P3", "accept_invalid_choice", "p3_option_not_offered_error"),
    ("P3", "no_audit_on_denied", "p3_option_not_offered_audited"),
    ("P4", "dup_writeback", "p4_still_one_update_after_replays"),
    ("P4", "dup_writeback", "p4_replayed_update_reported_as_duplicate"),
    ("P4", "double_txn", "exactly_one_transaction"),
]


@pytest.mark.parametrize(("sid", "fault", "check"), FAULT_MATRIX, ids=[f"{s}-{f}" for s, f, _c in FAULT_MATRIX])
def test_injected_defect_is_caught(run_scenario, sid, fault, check):
    rows, _ctx, _system = run_scenario(sid, faults=(fault,))
    assert not rows[0]["passed"]
    assert check in failed(rows[0]), failed(rows[0])


def test_reset_that_leaves_runtime_items_aborts_the_next_run(run_scenario):
    rows, _ctx, _system = run_scenario("A", faults=("skip_reset_cleanup",), runs=2)
    assert rows[0]["passed"]
    assert not rows[1]["passed"] and "reset_runtime_partitions_empty" in failed(rows[1])
    assert rows[1]["error"].startswith("aborted") and rows[1]["llm_runs"] == []  # no tokens spent on a dirty table


def test_each_scenario_uses_the_variant_it_needs(run_scenario):
    rows, ctx, _system = run_scenario("B")
    inv = ctx.tables.snapshot().inv()
    # dc_stockout: the DC has no replacement stock left to sell, so DC atp is still 0 after the exchange
    assert inv[("DEMO-SKU-HERO-BLACK-075", "DEMO-DC-QC")]["atp"] == 0
    assert rows[0]["variant"] == "dc_stockout"


def test_llm_runs_record_tokens_model_and_cost(run_scenario):
    rows, _ctx, _system = run_scenario("A")
    row = rows[0]
    assert [r["label"] for r in row["llm_runs"]] == ["customer_run", "approval_run"]
    assert row["input_tokens"] == 9600 and row["output_tokens"] == 1040 and row["tokens_reported"]
    assert row["cost_usd"] == pytest.approx((9600 * 3 + 1040 * 15) / 1e6, abs=1e-6)  # assumed prices 3 / 15 USD per Mtok
    assert row["cost_cad"] == pytest.approx(row["cost_usd"] * 1.38, abs=1e-6)


def test_p3_writes_nothing_for_every_rejected_call(run_scenario):
    rows, _ctx, _system = run_scenario("P3")
    passed = set(rows[0]["passed_checks"])
    for case in ("p3_option_not_offered", "p3_over_limit_delegation", "p3_stale_request_proposal", "p3_expired_request_confirm",
                 "p3_confirm_before_any_proposal", "p3_other_customers_confirm", "p3_confirm_with_a_different_option",
                 "p3_delegated_token_at_purchase_confirm", "p3_session_token_at_size_decision"):
        assert f"{case}_error" in passed and f"{case}_wrote_nothing" in passed, case
    assert "p3_double_confirm_returns_same_order" in passed and "p3_double_confirm_wrote_nothing" in passed
