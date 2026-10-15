"""Statistics, pricing, results.json / results.md, impact-model compatibility and the CLI."""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal

import pytest

from .conftest import REPO_ROOT


def test_percentile_is_nearest_rank(ev):
    assert ev.percentile([], 50) is None
    assert ev.percentile([7], 95) == 7
    assert (ev.percentile([30, 10, 20], 50), ev.percentile([30, 10, 20], 95)) == (20, 30)  # N=3: p95 is the slowest run
    ordered = list(range(1, 11))
    assert (ev.percentile(ordered, 50), ev.percentile(ordered, 95), ev.percentile(ordered, 100)) == (5, 10, 10)


def pricing(ev):
    return ev.Pricing("primary", "fallback", Decimal("3"), Decimal("15"), Decimal("5"), Decimal("25"), Decimal("1.38"))


def test_cost_uses_the_fallback_price_only_for_the_fallback_model(ev):
    p = pricing(ev)
    assert p.cost_usd("primary", 1_000_000, 1_000_000) == Decimal(18)
    assert p.cost_usd("fallback", 1_000_000, 1_000_000) == Decimal(30)
    assert p.cost_usd(None, 1_000_000, 0) == Decimal(3)
    assert p.to_dict()["basis"] == "assumed" and p.to_dict()["usd_to_cad_basis"] == "assumed"


def make_row(ev, sid="A", run=1, passed=True, latency=1000.0, tokens=(1000, 100), model="primary", returns_agent=True):
    rec = ev.Recorder(sid, run, "default")
    rec.latency_ms = latency
    rec.llm_runs.append({"label": "customer_run", "agent": "aldo", "run_id": "RUN-1", "status": "completed", "model_id": model, "fallback_used": False,
                         "rounds": 3, "tool_count": 4, "input_tokens": tokens[0], "output_tokens": tokens[1], "agent_latency_ms": 800,
                         "wall_ms": latency, "error_code": None, "tokens_reported": True})
    rec.check("txn_exactly_one", passed, "details here" if not passed else "")
    spec = ev.SCENARIOS[sid]
    return ev.run_summary(rec, ev.ScenarioSpec(spec.sid, spec.title, spec.variant, spec.fn, returns_agent), pricing(ev))


def test_results_aggregate_pass_rate_latency_tokens_and_cost(ev):
    rows = [make_row(ev, run=1, latency=1000), make_row(ev, run=2, latency=3000), make_row(ev, run=3, latency=2000, passed=False)]
    results = ev.build_results(rows, mode="live", target="https://x.invalid", pricing=pricing(ev), runs_per_scenario=3)
    a = results["scenarios"]["A"]
    assert (a["runs"], a["passed"], a["failed"], a["pass_rate"]) == (3, 2, 1, 0.6667)
    assert a["latency_ms"]["p50"] == 2000 and a["latency_ms"]["p95"] == 3000
    assert a["tokens"]["mean_total"] == 1100 and a["failing_checks"] == {"txn_exactly_one": 1}
    cost = (1000 * 3 + 100 * 15) / 1e6
    assert a["cost_usd"]["mean_per_run"] == pytest.approx(cost, abs=1e-6)
    assert results["est_cost_per_run_usd"] == pytest.approx(cost, abs=1e-6)
    assert results["summary"]["failed"] == 1 and results["failures"][0]["failed_checks"][0]["name"] == "txn_exactly_one"
    assert results["pricing"]["basis"] == "assumed"


def test_purchase_only_runs_do_not_feed_the_returns_agent_cost(ev):
    rows = [make_row(ev, sid="P1", returns_agent=False)]
    assert "est_cost_per_run_usd" not in ev.build_results(rows, mode="live", target="t", pricing=pricing(ev), runs_per_scenario=1)


def test_markdown_labels_prices_assumed_and_lists_failures(ev):
    rows = [make_row(ev, run=1), make_row(ev, run=2, passed=False)]
    text = ev.render_markdown(ev.build_results(rows, mode="live", target="https://x.invalid", pricing=pricing(ev), runs_per_scenario=2))
    assert "ASSUMED" in text and "p50 s" in text and "| A " in text and "`txn_exactly_one`" in text and "details here" in text
    dry = ev.render_markdown(ev.build_results(rows, mode="dry-run", target="sim", pricing=pricing(ev), runs_per_scenario=2))
    assert "DRY RUN" in dry and "nothing here is a measurement" in dry


def run_cli(ev, tmp_path, *args):
    lines: list[str] = []
    code = ev.main(["--dry-run", *args, "--out-dir", str(tmp_path)], out=lines.append)
    return code, lines


def test_cli_dry_run_writes_results_and_exits_zero_when_green(ev, tmp_path):
    code, lines = run_cli(ev, tmp_path, "--runs", "2", "--scenarios", "c, d")
    assert code == 0 and any(line.startswith("SUMMARY: 4/4 runs passed") for line in lines)
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["simulated"] is True and set(results["scenarios"]) == {"C", "D"} and results["summary"]["pass_rate"] == 1.0
    assert "Scenario eval results" in (tmp_path / "results.md").read_text()


def test_dry_run_writes_nothing_without_out_dir(ev, tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "DEFAULT_OUT_DIR", tmp_path / "must-stay-empty")
    lines: list[str] = []
    assert ev.main(["--dry-run", "--runs", "1", "--scenarios", "C"], out=lines.append) == 0
    assert not (tmp_path / "must-stay-empty").exists()
    assert not any("wrote " in line for line in lines)


@pytest.mark.parametrize("argv", [["--scenarios", "I"], ["--scenarios", "Z"], ["--runs", "0"]])
def test_cli_rejects_bad_arguments_with_exit_2(ev, argv):
    lines: list[str] = []
    assert ev.main(["--dry-run", *argv], out=lines.append) == 2 and lines[0].startswith("error:")


def test_live_run_needs_a_base_url(ev):
    lines: list[str] = []
    assert ev.main(["--scenarios", "A"], out=lines.append) == 2 and "--base-url" in lines[0]


def test_failing_run_gives_exit_code_1(ev, build):
    ctx, _system = build(("double_txn",))
    rows = ev.run_suite(ctx, ["A"], 1, ev.Pricing.from_config(), out=lambda _l: None)
    assert not rows[0]["passed"]
    results = ev.build_results(rows, mode="live", target="x", pricing=ev.Pricing.from_config(), runs_per_scenario=1)
    assert results["summary"]["failed"] == 1 and results["failures"]


def test_results_json_is_readable_by_the_impact_model_script(ev, tmp_path):
    path = REPO_ROOT / "scripts" / "build_impact_model.py"
    if not path.is_file():
        pytest.skip("build_impact_model.py not present")
    code, _lines = run_cli(ev, tmp_path, "--runs", "1", "--scenarios", "A,C")
    assert code == 0
    spec = importlib.util.spec_from_file_location("build_impact_model", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_impact_model"] = module
    spec.loader.exec_module(module)
    cost = module.load_agent_cost(tmp_path / "results.json")
    assert cost["status"] == "measured", cost["reason"]
    assert cost["field"] == "est_cost_per_run_usd" and cost["cost_per_run_cad"] > 0
