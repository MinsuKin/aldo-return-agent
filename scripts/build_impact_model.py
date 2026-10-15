#!/usr/bin/env python3
"""Build the business impact model for the Agentic Returns Gateway prototype.

Spec: requirements.md R13, design.md section 12, tasks.md 2.3.

Streams the provided returns CSV and in-store exchange CSV with the stdlib csv module (no
pandas), keeps returns with store_banner ALDO_CA, last return status RETURN_COMPLETED and
return_creation_date in 2025-02-01..2026-01-31, applies the low/base/high assumption sets and
writes:

    docs/impact/impact-model.json
    docs/impact/impact-model.md

Every number carries a tag: measured, derived or assumed. The agent cost per run carries the
tag pending_measurement while it is only a placeholder; it becomes derived once
docs/eval/results.json provides a mean cost per run. Re-run this script after the live eval:

    python3.12 scripts/build_impact_model.py

Idempotent: no wall-clock values are written, the output depends only on the input files, and
files are replaced atomically (and left untouched when their content is unchanged).
Only aggregates are written; customer identifier columns are never read.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETURNS_CSV = ROOT / "dataset" / "OMS Data_Feb24-Jan26_new.csv"
EXCHANGES_CSV = ROOT / "dataset" / "McGill_Instore_return_exchanges.csv"
DATA_PROFILE = ROOT / "docs" / "evidence" / "data-profile.json"
COHORT_PROFILE = ROOT / "docs" / "evidence" / "cohort-profile.json"
EVAL_RESULTS = ROOT / "docs" / "eval" / "results.json"
OUT_DIR = ROOT / "docs" / "impact"

# ---- Scope (requirement 13.1) -------------------------------------------------------------
WINDOW_START = date(2025, 2, 1)  # common to both files; exactly 12 months
WINDOW_END = date(2026, 1, 31)
BANNER = "ALDO_CA"
STATUS = "RETURN_COMPLETED"
FIT_REASONS = ("too small", "too big")  # after strip, lower-case, whitespace collapse, prefix removal
REASON_PREFIX = "return.page.reasons."

# ---- Assumptions (requirement 13.2, design 12, handoff section 15) --------------------------
SCENARIOS = {
    "low": {"conversion_pct": 10, "repeat_reduction_pct": 0},
    "base": {"conversion_pct": 20, "repeat_reduction_pct": 5},
    "high": {"conversion_pct": 30, "repeat_reduction_pct": 10},
}
# Handoff section 15 synthetic unit economics: exchange (-28) minus refund (-82) = +54.
VALUE_PER_CONVERSION_CAD = Decimal("54")
# Design 12: reverse logistics 12 + handling 10.
AVOIDED_SECOND_RETURN_COST_CAD = Decimal("22")
# Pre-purchase prevention what-if (pilot hypothesis): share of measured fit lines that sizing guidance
# before purchase would stop from becoming returns. Values per prevented case reuse the same handoff
# section 15 / design 12 assumptions as the exchange model.
PREVENTION_PCT = {"low": 0, "base": 5, "high": 10}
AVOIDED_RETURN_COST_CAD = AVOIDED_SECOND_RETURN_COST_CAD  # CAD 22: reverse logistics 12 + handling 10
RETAINED_CASE_VALUE_CAD = VALUE_PER_CONVERSION_CAD  # CAD 54 per retained case
# Used only while docs/eval/results.json has no usable mean cost per run. Deliberately conservative.
PLACEHOLDER_AGENT_COST_PER_RUN_CAD = Decimal("0.50")
# Bedrock bills in USD. Used only when the eval reports its cost in USD.
USD_TO_CAD = Decimal("1.40")

CURRENCY_NOTE = "CAD assumed for ALDO_CA, unconfirmed"

# Normalized (strip, lower-case, single-spaced) source header names.
RETURNS_COLUMNS = {
    "banner": "store_banner",
    "return_id": "return_order_number",
    "date": "return_creation_date",
    "status": "last return status",
    "refund": "refund amount",
    "reason": "reason",
}
RETURNS_STORE_COLUMN = "instore return location"  # optional: used to attribute exchange stores to banners
EXCHANGE_COLUMNS = {
    "confidence": "exchange_confidence",
    "date": "transactions_businessdaydate",
    "qty": "exchange qty",
    "store": "return store",
}

CENT = Decimal(1)
TAG_LEGEND = {
    "measured": "Counted or summed directly from the provided CSV files.",
    "derived": "Calculated from measured numbers and/or assumptions with the formula shown.",
    "assumed": "An input chosen by the team, or a what-if result built only from such inputs and a measured "
               "baseline. It is not observed in ALDO data and must be validated in a pilot.",
    "pending_measurement": "Placeholder until the live eval measures it (docs/eval/results.json).",
}
FORMULA = (
    "annual_net_value = fit_lines x exchange_conversion x value_per_converted_case\n"
    "                 + fit_lines x exchange_conversion x repeat_return_reduction x avoided_second_return_cost\n"
    "                 - total_requests x agent_cost_per_run"
)
PREVENTION_FORMULA = (
    "annual_value = fit_lines x prevention_rate x (avoided_return_cost + value_per_retained_case)"
)


# ---- small helpers -------------------------------------------------------------------------
def rel(path: Path) -> str:
    """Repo-relative path for output (never leak absolute local paths into a served file)."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(CENT, rounding=ROUND_HALF_UP))


def quantized(value, places: int) -> Decimal:
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def jnum(value, places: int = 2) -> float:
    return float(quantized(value, places))


def fmt_int(n: int) -> str:
    return f"{n:,}"


def fmt_dec(value, places: int = 2) -> str:
    return f"{quantized(value, places):,}"


def fmt_cad(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}CAD {whole:,}.{frac:02d}"


def fmt_rate(value: Decimal) -> str:
    return f"CAD {value:.4f}"


def norm_header(name: str) -> str:
    return " ".join(name.strip().lower().split())


def resolve_columns(header, wanted: dict, source: str) -> tuple:
    """Map wanted keys to column positions; also returns the full normalized-name index."""
    index = {norm_header(h): i for i, h in enumerate(header)}
    missing = [col for col in wanted.values() if col not in index]
    if missing:
        raise SystemExit(f"{source}: missing expected column(s) {missing}; found {sorted(index)}")
    return {key: index[col] for key, col in wanted.items()}, index


def parse_date(text: str):
    """ISO YYYY-MM-DD (returns file) or M/D/YYYY (exchange file); None when unparseable."""
    s = text.strip()
    if not s:
        return None
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return date.fromisoformat(s[:10])
        return datetime.strptime(s.split(" ")[0], "%m/%d/%Y").date()
    except ValueError:
        return None


def normalize_reason(text: str) -> str:
    reason = " ".join(text.strip().lower().split())
    if reason.startswith(REASON_PREFIX):
        reason = reason[len(REASON_PREFIX):].strip()
    return reason


def percentile(sorted_values, q: Decimal) -> Decimal:
    """Linear-interpolation percentile (numpy default); q in [0, 1]."""
    n = len(sorted_values)
    if n == 0:
        return Decimal(0)
    pos = Decimal(n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return Decimal(sorted_values[lo]) + (Decimal(sorted_values[hi]) - Decimal(sorted_values[lo])) * (pos - lo)


def median_cents(values) -> int:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0
    if n % 2:
        return ordered[n // 2]
    return int((Decimal(ordered[n // 2 - 1] + ordered[n // 2]) / 2).quantize(CENT, rounding=ROUND_HALF_UP))


def metric(label, value, unit, tag, display, note=None) -> dict:
    out = {"label": label, "value": value, "unit": unit, "tag": tag, "display": display}
    if note:
        out["note"] = note
    return out


def scenario_metric(label, unit, tag, values: dict, display: dict, note=None) -> dict:
    out = {"label": label, "unit": unit, "tag": tag, "values": values, "display": display}
    if note:
        out["note"] = note
    return out


def write_if_changed(path: Path, text: str) -> str:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return "written"


# ---- CSV scans -----------------------------------------------------------------------------
def scan_returns(path: Path) -> dict:
    rows_read = malformed = bad_dates = refund_unparseable = 0
    banner_rows = banner_completed = banner_fit_all = total_lines = 0
    fit_by_reason: Counter = Counter()
    refund_cents: list = []
    per_day: Counter = Counter()
    return_ids: set = set()
    fit_return_ids: set = set()
    store_banners: dict = defaultdict(Counter)  # in-store return location -> banner counts

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh)
        cols, index = resolve_columns(next(reader), RETURNS_COLUMNS, path.name)
        i_banner, i_ret, i_date = cols["banner"], cols["return_id"], cols["date"]
        i_status, i_refund, i_reason = cols["status"], cols["refund"], cols["reason"]
        i_store = index.get(RETURNS_STORE_COLUMN)
        need = max(cols.values())
        for row in reader:
            rows_read += 1
            if len(row) <= need:
                malformed += 1
                continue
            banner = row[i_banner].strip()
            if i_store is not None and len(row) > i_store:
                store = row[i_store].strip()
                if store:
                    store_banners[store][banner] += 1
            if banner != BANNER:
                continue
            banner_rows += 1
            reason = normalize_reason(row[i_reason])
            is_fit = reason in FIT_REASONS
            if is_fit:
                banner_fit_all += 1
            if row[i_status].strip() != STATUS:
                continue
            banner_completed += 1
            day = parse_date(row[i_date])
            if day is None:
                bad_dates += 1
                continue
            if day < WINDOW_START or day > WINDOW_END:
                continue
            total_lines += 1
            per_day[day] += 1
            return_id = row[i_ret].strip()
            return_ids.add(return_id)
            if not is_fit:
                continue
            fit_by_reason[reason] += 1
            fit_return_ids.add(return_id)
            try:
                amount = Decimal(row[i_refund].strip())
                if not amount.is_finite():
                    raise InvalidOperation
                refund_cents.append(to_cents(amount))
            except InvalidOperation:
                refund_unparseable += 1

    return {
        "rows_read": rows_read, "malformed_rows": malformed, "unparseable_dates_in_scope": bad_dates,
        "banner_rows": banner_rows, "banner_completed_rows": banner_completed,
        "banner_fit_rows_all": banner_fit_all,
        "total_lines": total_lines, "fit_by_reason": fit_by_reason,
        "refund_cents": refund_cents, "refund_unparseable": refund_unparseable,
        "per_day": per_day, "return_ids": len(return_ids), "fit_return_ids": len(fit_return_ids),
        "store_banners": store_banners,
    }


def scan_exchanges(path: Path, store_banners: dict) -> dict:
    rows_read = malformed = bad_dates = bad_qty = in_window = units = 0
    by_conf: Counter = Counter()
    by_banner: dict = defaultdict(Counter)
    first_day = last_day = None

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh)
        cols, _ = resolve_columns(next(reader), EXCHANGE_COLUMNS, path.name)
        i_conf, i_date, i_qty, i_store = cols["confidence"], cols["date"], cols["qty"], cols["store"]
        need = max(cols.values())
        for row in reader:
            rows_read += 1
            if len(row) <= need:
                malformed += 1
                continue
            day = parse_date(row[i_date])
            if day is None:
                bad_dates += 1
                continue
            first_day = day if first_day is None or day < first_day else first_day
            last_day = day if last_day is None or day > last_day else last_day
            if day < WINDOW_START or day > WINDOW_END:
                continue
            in_window += 1
            conf = row[i_conf].strip().upper()
            by_conf[conf] += 1
            try:
                units += int(Decimal(row[i_qty].strip()))
            except (InvalidOperation, ValueError):
                bad_qty += 1
            counts = store_banners.get(row[i_store].strip())
            banner = counts.most_common(1)[0][0] if counts else "UNMAPPED"
            by_banner[banner][conf] += 1

    return {
        "rows_read": rows_read, "malformed_rows": malformed, "unparseable_dates": bad_dates,
        "unparseable_qty": bad_qty, "rows_in_window": in_window, "units_in_window": units,
        "by_conf": by_conf, "by_banner": by_banner, "first_day": first_day, "last_day": last_day,
    }


# ---- agent cost input (docs/eval/results.json) -----------------------------------------------
# Accepted field names (any nesting depth, shallowest level wins) for the MEAN cost of one agent
# run, for example est_cost_per_run_usd, mean_cost_usd, cost_per_run_cad. Per-scenario values,
# totals, percentiles and token-component costs are ignored.
COST_BAD_PARTS = ("total", "p50", "p90", "p95", "p99", "median", "max", "min", "token", "input", "output", "cache")
COST_GOOD_PARTS = ("perrun", "mean", "avg", "average")


def cost_key_currency(key):
    """'usd', 'cad' or '' when the key names the mean cost of one run, else None."""
    k = "".join(ch for ch in str(key).lower() if ch.isalnum())
    if "cost" not in k or any(p in k for p in COST_BAD_PARTS) or not any(p in k for p in COST_GOOD_PARTS):
        return None
    return "usd" if "usd" in k else "cad" if "cad" in k else ""


def find_cost_candidates(data) -> list:
    """Candidates at the shallowest nesting level that has any: (path, currency, raw value, parent dict)."""
    level = [("", data)]
    while level:
        hits, nxt = [], []
        for path, node in level:
            if isinstance(node, dict):
                for key, value in node.items():
                    child = f"{path}.{key}" if path else str(key)
                    if isinstance(value, (dict, list)):
                        nxt.append((child, value))
                    elif isinstance(value, (int, float, str)) and not isinstance(value, bool):
                        currency = cost_key_currency(key)
                        if currency is not None:
                            hits.append((child, currency, value, node))
            elif isinstance(node, list):
                nxt.extend((f"{path}[{i}]", v) for i, v in enumerate(node) if isinstance(v, (dict, list)))
        if hits:
            return hits
        level = nxt
    return []


def load_agent_cost(path: Path) -> dict:
    info = {
        "status": "pending_measurement", "reason": "", "file": rel(path), "sha256": None,
        "field": None, "currency_in_file": None, "usd_to_cad_used": False,
        "cost_per_run_cad": PLACEHOLDER_AGENT_COST_PER_RUN_CAD,
    }
    if not path.is_file():
        info["reason"] = "file not found"
        return info
    raw = path.read_bytes()
    info["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        info["reason"] = "file is not valid JSON"
        return info
    hits = find_cost_candidates(data)
    if not hits:
        info["reason"] = "no mean-cost-per-run field found"
        return info

    parsed = []
    for field_path, currency, raw_value, parent in hits:
        try:
            value = Decimal(str(raw_value).strip())
            if not value.is_finite() or value <= 0:
                raise InvalidOperation
        except InvalidOperation:
            info["reason"] = f"field {field_path} is not a positive number"
            return info
        if not currency:  # unsuffixed key: use a sibling currency field, default USD (Bedrock bills in USD)
            sibling = next((str(v).strip().lower() for k, v in parent.items()
                            if "".join(c for c in str(k).lower() if c.isalnum()) in
                            ("currency", "costcurrency", "pricecurrency") and isinstance(v, str)), "usd")
            currency = sibling
        if currency not in ("usd", "cad"):
            info["reason"] = f"field {field_path} has unsupported currency {currency!r}"
            return info
        cad = value * USD_TO_CAD if currency == "usd" else value
        parsed.append((field_path, currency, cad.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)))

    if len({cad for _, _, cad in parsed}) > 1:
        info["reason"] = "ambiguous: several different mean-cost fields at " + ", ".join(p for p, _, _ in parsed)
        return info
    field_path, currency, cad = parsed[0]
    info.update(status="measured", field=field_path, currency_in_file=currency,
                usd_to_cad_used=currency == "usd", cost_per_run_cad=cad)
    return info


# ---- model ---------------------------------------------------------------------------------
def scenario_builders(calc: dict, names: list):
    """Row builders for the low/base/high tables: per() varies by scenario, const() repeats one value."""

    def per(label, unit, tag, key, fmt, as_json=lambda v: v, note=None):
        return scenario_metric(
            label, unit, tag, {n: as_json(calc[n][key]) for n in names},
            {n: fmt(calc[n][key]) for n in names}, note,
        )

    def const(label, unit, tag, value, shown, note=None):
        return scenario_metric(label, unit, tag, {n: value for n in names}, {n: shown for n in names}, note)

    return per, const


def build_prevention(fit_lines: int) -> dict:
    """Pre-purchase prevention what-if: a pilot hypothesis against the measured fit-line baseline.

    Prevented lines = fit_lines x prevention rate. Each prevented case avoids a return (CAD 22) and keeps the
    sale (CAD 54). Everything except the measured baseline is an assumption, so results are tagged assumed.
    No sizing-assistant operating cost is modeled.
    """
    names = list(PREVENTION_PCT)
    calc = {}
    for name, pct in PREVENTION_PCT.items():
        prevented = Decimal(fit_lines) * pct / 100
        avoided_value = to_cents(prevented * AVOIDED_RETURN_COST_CAD)
        retained_value = to_cents(prevented * RETAINED_CASE_VALUE_CAD)
        calc[name] = {
            "pct": pct, "prevented": prevented,
            "avoided_value": avoided_value, "retained_value": retained_value,
            "total": avoided_value + retained_value,
        }
    per, const = scenario_builders(calc, names)
    return {
        "tag": "assumed",
        "framing": "Pilot hypothesis against a measured baseline: sizing guidance before purchase could stop some "
                   "size-related returns from happening at all. The baseline is measured; the prevention rates "
                   "and unit values are assumptions to validate in a pilot. This is not a forecast.",
        "columns": names,
        "inputs": {
            "baseline_fit_lines": const(
                "Baseline: size-related return lines (Too small + Too big), window", "lines", "measured",
                fit_lines, fmt_int(fit_lines), note="Measured, same window and base as the exchange scenarios."),
            "prevention_rate_pct": per(
                "Share of size-related return lines prevented before purchase", "percent", "assumed", "pct",
                lambda v: f"{v}%", note="Pilot hypothesis. Not observed in ALDO data."),
            "avoided_return_cost": const(
                "Avoided return cost per prevented case", "CAD_cents", "assumed",
                to_cents(AVOIDED_RETURN_COST_CAD), fmt_cad(to_cents(AVOIDED_RETURN_COST_CAD)),
                note="Reverse logistics 12 + handling 10 (design section 12, handoff section 15)."),
            "retained_case_value": const(
                "Value per retained case", "CAD_cents", "assumed",
                to_cents(RETAINED_CASE_VALUE_CAD), fmt_cad(to_cents(RETAINED_CASE_VALUE_CAD)),
                note="Handoff section 15 synthetic unit economics (exchange versus refund = +54). "
                     "Incremental economic value, not gross margin."),
        },
        "outputs": {
            "prevented_lines": per(
                "Prevented return lines", "lines", "assumed", "prevented", lambda v: fmt_dec(v, 0),
                as_json=lambda v: jnum(v), note="Baseline x prevention rate (expected value, rounded for display)."),
            "avoided_return_cost_value": per(
                "Avoided return cost", "CAD_cents", "assumed", "avoided_value", fmt_cad,
                note="Prevented lines x avoided return cost."),
            "retained_sale_value": per(
                "Retained-sale value", "CAD_cents", "assumed", "retained_value", fmt_cad,
                note="Prevented lines x value per retained case."),
            "total_value": per(
                "Annual value (what-if)", "CAD_cents", "assumed", "total", fmt_cad,
                note="The 12-month window is used as the annual figure. No sizing-assistant operating cost "
                     "is subtracted."),
        },
        "formula": PREVENTION_FORMULA,
    }


def build_scenarios(fit_lines: int, total_requests: int, cost: dict) -> dict:
    names = list(SCENARIOS)
    per_run = cost["cost_per_run_cad"]
    cost_tag = "derived" if cost["status"] == "measured" else "pending_measurement"
    calc = {}
    for name, a in SCENARIOS.items():
        conv = Decimal(a["conversion_pct"]) / 100
        rep = Decimal(a["repeat_reduction_pct"]) / 100
        converted = Decimal(fit_lines) * conv
        avoided = converted * rep
        exchange_value = to_cents(converted * VALUE_PER_CONVERSION_CAD)
        avoided_value = to_cents(avoided * AVOIDED_SECOND_RETURN_COST_CAD)
        agent_total = to_cents(Decimal(total_requests) * per_run)
        calc[name] = {
            "conv": a["conversion_pct"], "rep": a["repeat_reduction_pct"],
            "converted": converted, "avoided": avoided,
            "exchange_value": exchange_value, "avoided_value": avoided_value,
            "gross": exchange_value + avoided_value, "agent_total": agent_total,
            "net": exchange_value + avoided_value - agent_total,
        }

    per, const = scenario_builders(calc, names)

    inputs = {
        "exchange_conversion_pct": per(
            "Exchange conversion of fit-reason lines", "percent", "assumed", "conv", lambda v: f"{v}%",
            note="Share of fit-reason lines that become exchanges. Assumption to validate in a pilot."),
        "repeat_return_reduction_pct": per(
            "Repeat-return reduction on converted cases", "percent", "assumed", "rep", lambda v: f"{v}%",
            note="Share of converted cases where a second return is avoided thanks to the fit insight."),
        "value_per_converted_case": const(
            "Incremental value per converted case", "CAD_cents", "assumed",
            to_cents(VALUE_PER_CONVERSION_CAD), fmt_cad(to_cents(VALUE_PER_CONVERSION_CAD)),
            note="Handoff section 15 synthetic unit economics: exchange (-28) minus refund (-82) = +54. "
                 "Incremental economic value, not gross margin."),
        "avoided_second_return_cost": const(
            "Avoided second-return cost", "CAD_cents", "assumed",
            to_cents(AVOIDED_SECOND_RETURN_COST_CAD), fmt_cad(to_cents(AVOIDED_SECOND_RETURN_COST_CAD)),
            note="Reverse logistics 12 + handling 10 (design section 12, handoff section 15)."),
        "fit_lines": const(
            "Fit-reason lines (window)", "lines", "measured", fit_lines, fmt_int(fit_lines)),
        "total_requests": const(
            "Agent runs charged (total return lines in window)", "runs", "derived", total_requests,
            fmt_int(total_requests),
            note="Conservative: one run is charged for every return line in scope, not only fit-reason lines."),
        "agent_cost_per_run": const(
            "Agent cost per run", "CAD_per_run", cost_tag, jnum(per_run, 4), fmt_rate(per_run),
            note=("Mean cost per run from the live eval (measured tokens x assumed token price)."
                  if cost["status"] == "measured" else
                  "Placeholder until the live eval measures it. Re-run this script after the eval.")),
    }
    outputs = {
        "converted_cases": per(
            "Converted cases", "cases", "derived", "converted", lambda v: fmt_dec(v, 0),
            as_json=lambda v: jnum(v), note="Fit lines x exchange conversion (expected value, rounded for display)."),
        "exchange_value": per(
            "Exchange value", "CAD_cents", "derived", "exchange_value", fmt_cad,
            note="Converted cases x incremental value per converted case."),
        "avoided_second_returns": per(
            "Avoided second returns", "cases", "derived", "avoided", lambda v: fmt_dec(v, 0),
            as_json=lambda v: jnum(v), note="Converted cases x repeat-return reduction."),
        "avoided_second_return_value": per(
            "Avoided second-return value", "CAD_cents", "derived", "avoided_value", fmt_cad),
        "gross_value": per(
            "Gross annual value (before agent cost)", "CAD_cents", "derived", "gross", fmt_cad),
        "agent_cost_total": per(
            "Agent cost (total requests x cost per run)", "CAD_cents", cost_tag, "agent_total", fmt_cad),
        "annual_net_value": per(
            "Annual net value", "CAD_cents", cost_tag, "net", fmt_cad,
            note="The 12-month window is used as the annual figure (no scaling)."),
    }
    return {"columns": names, "inputs": inputs, "outputs": outputs}


def build_slide_summary(total_lines, fit_lines, fit_share, scenarios, prevention, cost) -> dict:
    """Three headline numbers (one measured, one derived, one assumed), key assumptions and the currency note."""
    names = scenarios["columns"]
    net = scenarios["outputs"]["annual_net_value"]
    what_if = prevention["outputs"]["total_value"]
    cost_measured = cost["status"] == "measured"
    conversions = "/".join(str(SCENARIOS[n]["conversion_pct"]) for n in names)
    repeats = "/".join(str(SCENARIOS[n]["repeat_reduction_pct"]) for n in names)
    prevent = "/".join(str(PREVENTION_PCT[n]) for n in prevention["columns"])
    per_run = cost["cost_per_run_cad"]
    headline = [
        {"key": "fit_lines", "label": "Size-related return lines (Too small + Too big)",
         "value": fit_lines, "unit": "lines", "tag": "measured", "display": fmt_int(fit_lines),
         "basis": f"{fmt_dec(fit_share)}% of {fmt_int(total_lines)} completed ALDO_CA return lines, "
                  f"{WINDOW_START.isoformat()} to {WINDOW_END.isoformat()}. Lines, not customers."},
        {"key": "annual_net_value_base", "label": "Annual net value, post-purchase exchange (base case)",
         "value": net["values"]["base"], "unit": "CAD_cents", "tag": net["tag"], "display": net["display"]["base"],
         "basis": f"Range {net['display']['low']} (low) to {net['display']['high']} (high). Rests on assumed "
                  f"{conversions}% exchange conversion"
                  + (" and the measured agent cost per run." if cost_measured
                     else " and a placeholder agent cost (pending_measurement).")},
        {"key": "prevention_value_base", "label": "Pre-purchase prevention what-if (base case)",
         "value": what_if["values"]["base"], "unit": "CAD_cents", "tag": "assumed",
         "display": what_if["display"]["base"],
         "basis": f"{PREVENTION_PCT['base']}% of the measured size-related lines prevented. Range "
                  f"{what_if['display']['low']} ({PREVENTION_PCT['low']}%) to {what_if['display']['high']} "
                  f"({PREVENTION_PCT['high']}%). Pilot hypothesis, separate lever, not additive."},
    ]
    assumptions = [
        f"Exchange conversion of size-related lines {conversions}% and prevention before purchase {prevent}% "
        "(low/base/high, assumed). Both are assumptions to validate in a pilot.",
        f"Value per converted or retained case {fmt_cad(to_cents(VALUE_PER_CONVERSION_CAD))} and avoided return cost "
        f"{fmt_cad(to_cents(AVOIDED_RETURN_COST_CAD))} (assumed, synthetic unit economics from handoff section 15).",
        f"Repeat-return reduction on converted cases {repeats}% (assumed).",
        (f"Agent cost per run {fmt_rate(per_run)} (derived from measured tokens at an assumed token price)."
         if cost_measured else
         f"Agent cost per run {fmt_rate(per_run)} is a placeholder (pending_measurement) until the live eval."),
        "Fit lines are return lines, not customers.",
    ]
    return {"currency_note": CURRENCY_NOTE, "headline_numbers": headline, "key_assumptions": assumptions}


def build_model(returns: dict, exchanges: dict, cost: dict, returns_path: Path, exchanges_path: Path) -> dict:
    days = (WINDOW_END - WINDOW_START).days + 1
    total_lines = returns["total_lines"]
    fit_by_reason = returns["fit_by_reason"]
    fit_lines = sum(fit_by_reason.values())
    refund_sum = sum(returns["refund_cents"])
    refund_median = median_cents(returns["refund_cents"])

    daily = [returns["per_day"].get(WINDOW_START + timedelta(days=i), 0) for i in range(days)]
    daily_mean = Decimal(total_lines) / days
    daily_p95 = percentile(sorted(daily), Decimal("0.95"))
    daily_max = max(daily)
    days_with_lines = sum(1 for v in daily if v)
    fit_share = Decimal(fit_lines) * 100 / Decimal(total_lines) if total_lines else Decimal(0)
    peak_ratio = daily_p95 / daily_mean if daily_mean else Decimal(0)

    window_text = f"{WINDOW_START.isoformat()}..{WINDOW_END.isoformat()}"
    filter_text = (
        f"store_banner = '{BANNER}' AND last return status = '{STATUS}' AND return_creation_date in {window_text}; "
        "fit line = reason equals 'too small' or 'too big' after strip, lower-case, whitespace collapse "
        f"and removal of the '{REASON_PREFIX}' prefix"
    )

    returns_window = {
        "total_return_lines": metric(
            "Return lines in scope", total_lines, "lines", "measured", fmt_int(total_lines),
            note="One line is one CSV row. The source has duplicate (order, SKU) groups, so a line is not "
                 "guaranteed to be an independent return unit."),
        "fit_lines": metric(
            "Fit-reason lines (Too small + Too big)", fit_lines, "lines", "measured", fmt_int(fit_lines),
            note="Return lines, not customers."),
        "fit_lines_too_small": metric(
            "Fit lines: Too small", fit_by_reason["too small"], "lines", "measured",
            fmt_int(fit_by_reason["too small"])),
        "fit_lines_too_big": metric(
            "Fit lines: Too big", fit_by_reason["too big"], "lines", "measured",
            fmt_int(fit_by_reason["too big"])),
        "fit_share_of_lines": metric(
            "Fit lines as share of all lines in scope", jnum(fit_share), "percent", "derived",
            f"{fmt_dec(fit_share)}%", note="Not an exchange conversion rate."),
        "fit_refund_sum": metric(
            "Refund amount of fit lines, sum", refund_sum, "CAD_cents", "measured", fmt_cad(refund_sum),
            note="Column summed as provided. Context for the funnel, not an input to the annual-value "
                 "formula and not a realized loss. Line versus transaction grain is unconfirmed."),
        "fit_refund_median": metric(
            "Refund amount of fit lines, median", refund_median, "CAD_cents", "measured", fmt_cad(refund_median)),
        "distinct_return_numbers_in_scope": metric(
            "Distinct return numbers in scope", returns["return_ids"], "returns", "measured",
            fmt_int(returns["return_ids"]), note="Several lines can share one return number."),
        "distinct_return_numbers_fit": metric(
            "Distinct return numbers with a fit line", returns["fit_return_ids"], "returns", "measured",
            fmt_int(returns["fit_return_ids"])),
    }

    scale = {
        "days_in_window": metric("Calendar days in window", days, "days", "derived", fmt_int(days)),
        "days_with_lines": metric("Days with at least one line", days_with_lines, "days", "measured",
                                  fmt_int(days_with_lines)),
        "lines_per_day_mean": metric(
            "Lines per day, mean", jnum(daily_mean), "lines_per_day", "measured", fmt_dec(daily_mean),
            note="Mean over all calendar days in the window (days without lines count as 0)."),
        "lines_per_day_p95": metric(
            "Lines per day, p95", jnum(daily_p95), "lines_per_day", "measured", fmt_dec(daily_p95),
            note="95th percentile of the daily counts, linear interpolation between closest ranks."),
        "lines_per_day_max": metric("Lines per day, busiest day", daily_max, "lines_per_day", "measured",
                                    fmt_int(daily_max)),
        "peak_to_mean_ratio": metric(
            "Peak multiplier (p95 / mean)", jnum(peak_ratio), "ratio", "derived", f"{fmt_dec(peak_ratio)}x"),
    }

    conf = exchanges["by_conf"]
    by_banner = exchanges["by_banner"]
    aldo = by_banner.get(BANNER, Counter())
    unmapped = sum(by_banner.get("UNMAPPED", Counter()).values())
    other_banners = sum(sum(c.values()) for b, c in by_banner.items() if b not in (BANNER, "UNMAPPED"))
    other_labels = sorted(k for k in conf if k not in ("CONFIRMED", "ASSUMED"))
    exchange_evidence = {
        "confirmed_all_stores": metric(
            "CONFIRMED in-store exchanges, all stores in file", conf["CONFIRMED"], "rows", "measured",
            fmt_int(conf["CONFIRMED"]), note="Replacement sale recorded inside the return transaction."),
        "assumed_all_stores": metric(
            "ASSUMED in-store exchanges, all stores in file", conf["ASSUMED"], "rows", "measured",
            fmt_int(conf["ASSUMED"]),
            note="Inferred when a separate in-store sale followed within five minutes. Never merge with CONFIRMED."),
        "confirmed_aldo_ca_stores": metric(
            "CONFIRMED in-store exchanges, stores attributed to ALDO_CA", aldo["CONFIRMED"], "rows", "derived",
            fmt_int(aldo["CONFIRMED"]),
            note="The exchange file has no banner column and covers several banners. Attribution maps Return "
                 "store to the banner seen for the same in-store return location in the returns file."),
        "assumed_aldo_ca_stores": metric(
            "ASSUMED in-store exchanges, stores attributed to ALDO_CA", aldo["ASSUMED"], "rows", "derived",
            fmt_int(aldo["ASSUMED"])),
        "rows_other_banners": metric(
            "Exchange rows in stores attributed to other banners", other_banners, "rows", "derived",
            fmt_int(other_banners)),
        "rows_unmapped_store": metric(
            "Exchange rows whose store could not be attributed to a banner", unmapped, "rows", "derived",
            fmt_int(unmapped)),
        "units_in_window": metric(
            "Exchanged units (sum of Exchange QTY) in window", exchanges["units_in_window"], "units", "measured",
            fmt_int(exchanges["units_in_window"])),
    }

    # Integrity checks against the earlier pandas profile (all statuses and dates for the banner).
    reconciliation = []
    try:
        expected = json.loads(COHORT_PROFILE.read_text(encoding="utf-8"))["aldo_ca"]
    except (OSError, ValueError, KeyError):
        expected = None
    if expected:
        for key, label, got in (
            ("rows", f"{BANNER} rows, all statuses and dates", returns["banner_rows"]),
            ("completed_rows", f"{BANNER} {STATUS} rows, all dates", returns["banner_completed_rows"]),
            ("fit_size_rows", f"{BANNER} fit-reason rows, all statuses and dates", returns["banner_fit_rows_all"]),
        ):
            reconciliation.append({"check": label, "script": got, "cohort_profile": expected.get(key),
                                   "match": got == expected.get(key), "tag": "measured"})

    def profile_sha(name):
        try:
            return json.loads(DATA_PROFILE.read_text(encoding="utf-8"))["files"][name]["sha256"]
        except (OSError, ValueError, KeyError):
            return None

    returns_sha, exchanges_sha = sha256_of(returns_path), sha256_of(exchanges_path)
    returns_expected, exchanges_expected = profile_sha(returns_path.name), profile_sha(exchanges_path.name)
    sources = {
        "tag": "measured",
        "returns_csv": {
            "file": rel(returns_path), "sha256": returns_sha,
            "matches_data_profile": (returns_sha == returns_expected) if returns_expected else None,
            "rows_read": returns["rows_read"], "malformed_rows": returns["malformed_rows"],
            "unparseable_dates_in_scope": returns["unparseable_dates_in_scope"],
            "unparseable_refund_amounts_in_fit_lines": returns["refund_unparseable"],
        },
        "exchanges_csv": {
            "file": rel(exchanges_path), "sha256": exchanges_sha,
            "matches_data_profile": (exchanges_sha == exchanges_expected) if exchanges_expected else None,
            "rows_read": exchanges["rows_read"], "rows_in_window": exchanges["rows_in_window"],
            "malformed_rows": exchanges["malformed_rows"], "unparseable_dates": exchanges["unparseable_dates"],
            "unparseable_qty_in_window": exchanges["unparseable_qty"],
            "first_business_day": exchanges["first_day"].isoformat() if exchanges["first_day"] else None,
            "last_business_day": exchanges["last_day"].isoformat() if exchanges["last_day"] else None,
            "other_confidence_labels_in_window": other_labels,
        },
        "eval_results": {
            "file": cost["file"], "status": cost["status"], "reason": cost["reason"] or None,
            "sha256": cost["sha256"], "field": cost["field"],
        },
    }

    cost_measured = cost["status"] == "measured"
    if cost_measured:
        cost_text = (
            f"Agent cost per run is {fmt_rate(cost['cost_per_run_cad'])}, read from `{cost['file']}` "
            f"(field `{cost['field']}`). Tokens are measured by the live eval; the token price is an assumption "
            "set in the eval config."
            + (f" The eval reports USD, converted at an assumed {USD_TO_CAD} CAD per USD." if cost["usd_to_cad_used"] else "")
        )
    else:
        cost_text = (
            f"Agent cost per run is a deliberately conservative placeholder of "
            f"{fmt_rate(PLACEHOLDER_AGENT_COST_PER_RUN_CAD)}, tagged pending_measurement "
            f"({cost['reason']}: `{cost['file']}`). It is not a measurement. Run the live eval, then re-run "
            "`python3.12 scripts/build_impact_model.py` so the cost becomes derived from measured tokens. "
            "The agent cost and annual net value are provisional until then."
        )

    caveats = [
        "Fit lines are return lines, not customers. A customer can appear on several lines and some rows have "
        "no customer identifier (see the data profile), so customers cannot be counted from this file.",
        "Exchange conversion rates (10, 20 and 30 percent) are assumptions to validate in a pilot. The share of "
        "fit-reason lines is not a conversion rate and must not be used as one.",
        "CAD assumed for ALDO_CA, unconfirmed: the source files have no currency column.",
        "Value per converted case (CAD 54) comes from the synthetic unit economics in handoff section 15 "
        "(CAD 100 order, cost 40, refund path -82 versus exchange path -28). It is incremental economic value, "
        "not accounting gross profit and not ALDO-measured.",
        "Refund amounts are summed as provided. Whether the column is line or transaction grain is unconfirmed, so "
        "the sum is context for the funnel only and is not a realized loss or a revenue figure.",
        "Total requests is set to the number of return lines in scope (one agent run per line). This is "
        "conservative because one return can have several lines and the agent would not run on every line.",
        "The exchange file has no banner column. Store attribution to ALDO_CA is inferred from the returns file "
        "and is tagged derived. No exchange rate is computed: only about 26 percent of exchange rows match an "
        "original order (handoff section 5), so dividing by returns would be misleading.",
        "CONFIRMED means the replacement sale is inside the return transaction. ASSUMED is inferred from a "
        "separate in-store sale within five minutes. The two are reported separately and no causal effect is claimed.",
        "The window covers 12 months, so window totals are used as annual values without seasonality scaling.",
        "Pre-purchase prevention is a pilot hypothesis, not a forecast. The baseline (size-related return lines) "
        "is measured; the prevention rates ("
        + ", ".join(str(p) for p in PREVENTION_PCT.values())
        + " percent) and both unit values are assumptions to validate in a pilot, so every what-if result is "
        "tagged assumed.",
        "The prevention what-if is a separate lever and is not additive with the exchange-conversion scenarios: "
        "a prevented return cannot also be converted to an exchange. Do not add the two tables together.",
        f"Per prevented case the what-if adds the avoided return cost (CAD {AVOIDED_RETURN_COST_CAD}) and the "
        f"retained-sale value (CAD {RETAINED_CASE_VALUE_CAD}). Both come from the synthetic unit economics, and "
        f"part of the CAD {RETAINED_CASE_VALUE_CAD} may already include return handling cost, so the sum may "
        "overstate. No sizing-assistant operating cost is subtracted.",
        "The prevention baseline counts size-related lines from every return channel in the returns file. The "
        "share that pre-purchase guidance could reach is not measured.",
    ]

    scenarios = build_scenarios(fit_lines, total_lines, cost)
    prevention = build_prevention(fit_lines)
    slide_summary = build_slide_summary(total_lines, fit_lines, fit_share, scenarios, prevention, cost)

    model = {
        "schema_version": 1,
        "title": "Agentic Returns Gateway impact model: fit-related returns, ALDO_CA",
        "script": "scripts/build_impact_model.py",
        "currency": "CAD",
        "currency_note": CURRENCY_NOTE,
        "slide_summary": slide_summary,
        "tag_legend": TAG_LEGEND,
        "agent_cost_status": "measured" if cost_measured else "pending_measurement",
        "agent_cost_note": cost_text,
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat(), "days": days, "tag": "derived"},
        "filter": {"text": filter_text, "banner": BANNER, "last_return_status": STATUS,
                   "date_field": "return_creation_date", "fit_reasons": ["Too small", "Too big"]},
        "returns_window": returns_window,
        "scale": scale,
        "exchange_evidence": exchange_evidence,
        "formula": FORMULA,
        "scenarios": scenarios,
        "prevention_what_if": prevention,
        "caveats": caveats,
        "reconciliation": reconciliation,
        "sources": sources,
    }
    if cost["usd_to_cad_used"]:
        model["usd_to_cad"] = metric("USD to CAD rate", float(USD_TO_CAD), "CAD_per_USD", "assumed",
                                     f"{USD_TO_CAD}", note="Used only because the eval reports its cost in USD.")
    return model


# ---- markdown ------------------------------------------------------------------------------
def md_metric_table(items: dict) -> list:
    lines = ["| Metric | Value | Tag | Note |", "| --- | ---: | --- | --- |"]
    for item in items.values():
        lines.append(f"| {item['label']} | {item['display']} | {item['tag']} | {item.get('note', '')} |")
    return lines


def md_scenario_table(items: dict, names: list) -> list:
    lines = ["| Item | " + " | ".join(n.capitalize() for n in names) + " | Tag |",
             "| --- | " + " | ".join("---:" for _ in names) + " | --- |"]
    for item in items.values():
        cells = " | ".join(item["display"][n] for n in names)
        lines.append(f"| {item['label']} | {cells} | {item['tag']} |")
    return lines


def render_markdown(m: dict) -> str:
    sc = m["scenarios"]
    pw = m["prevention_what_if"]
    ss = m["slide_summary"]
    names = sc["columns"]
    window = m["window"]
    src = m["sources"]
    out = [
        f"# {m['title']}",
        "",
        "## Slide summary",
        "",
        "| Headline number | Value | Tag | Basis |",
        "| --- | ---: | --- | --- |",
        *[f"| {h['label']} | {h['display']} | {h['tag']} | {h['basis']} |" for h in ss["headline_numbers"]],
        "",
        "Key assumptions",
        "",
        *[f"- {a}" for a in ss["key_assumptions"]],
        f"- Currency: {ss['currency_note']}.",
        "",
        f"**{m['currency_note']}.** The source files have no currency column.",
        "",
        f"Window: {window['start']} to {window['end']} ({window['days']} days, 12 months). "
        f"Scope: store_banner {m['filter']['banner']}, last return status {m['filter']['last_return_status']}.",
        "",
        "## Headline",
        "",
        f"{m['returns_window']['fit_lines']['display']} of {m['returns_window']['total_return_lines']['display']} "
        "completed return lines in scope (both measured) have a fit reason (Too small or Too big). "
        "These are lines, not customers. "
        "Annual value below rests on assumed conversion rates (validate in a pilot)"
        + ("" if m["agent_cost_status"] == "measured" else " and a placeholder agent cost (provisional)") + ".",
        "",
        *md_scenario_table({k: sc["outputs"][k] for k in ("gross_value", "agent_cost_total", "annual_net_value")}, names),
        "",
        "## How to read this",
        "",
        "Every number carries a tag.",
        "",
        "| Tag | Meaning |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in m["tag_legend"].items()],
        "",
        "- Fit lines are return lines, not customers.",
        "- Exchange conversion rates are assumptions to validate in a pilot. The fit share of returns is not a conversion rate.",
        "- The value per converted case is incremental economic value versus a refund (synthetic unit economics from the handoff), not gross margin.",
        "",
        "## 1. Measured from the returns file",
        "",
        *md_metric_table(m["returns_window"]),
        "",
        "## 2. Daily volume (scale)",
        "",
        *md_metric_table(m["scale"]),
        "",
        "## 3. In-store exchange evidence",
        "",
        *md_metric_table(m["exchange_evidence"]),
        "",
        "Behavioral context only: it shows exchanges already happen in store. It is not used in the dollar model, no exchange rate is computed and no causal effect is claimed.",
        "",
        "## 4. Scenarios: annual value",
        "",
        "Inputs",
        "",
        *md_scenario_table(sc["inputs"], names),
        "",
        "Results",
        "",
        *md_scenario_table(sc["outputs"], names),
        "",
        "Formula:",
        "",
        "```text",
        m["formula"],
        "```",
        "",
        "## 5. Agent cost input",
        "",
        m["agent_cost_note"],
        "",
        "## Pre-purchase prevention what-if (pilot hypothesis)",
        "",
        pw["framing"],
        "",
        "Inputs",
        "",
        *md_scenario_table(pw["inputs"], pw["columns"]),
        "",
        "Results (all tagged assumed)",
        "",
        *md_scenario_table(pw["outputs"], pw["columns"]),
        "",
        "Formula:",
        "",
        "```text",
        pw["formula"],
        "```",
        "",
        "This is a separate lever from section 4. Do not add the two together: a prevented return cannot also be "
        "converted to an exchange.",
        "",
        "## 6. Caveats",
        "",
        *[f"- {c}" for c in m["caveats"]],
        "",
        "## 7. Provenance and checks",
        "",
        f"- Filter: {m['filter']['text']}.",
        f"- Returns file: `{src['returns_csv']['file']}`, SHA-256 `{src['returns_csv']['sha256']}`, "
        f"{fmt_int(src['returns_csv']['rows_read'])} rows read, matches data profile: {src['returns_csv']['matches_data_profile']}.",
        f"- Exchange file: `{src['exchanges_csv']['file']}`, SHA-256 `{src['exchanges_csv']['sha256']}`, "
        f"{fmt_int(src['exchanges_csv']['rows_read'])} rows read, business days "
        f"{src['exchanges_csv']['first_business_day']} to {src['exchanges_csv']['last_business_day']}, "
        f"matches data profile: {src['exchanges_csv']['matches_data_profile']}.",
        f"- Eval results: `{src['eval_results']['file']}`, status {src['eval_results']['status']}"
        + (f", SHA-256 `{src['eval_results']['sha256']}`" if src["eval_results"]["sha256"] else "") + ".",
    ]
    if m["reconciliation"]:
        out.append("- Reconciliation with `docs/evidence/cohort-profile.json` (earlier pandas profile):")
        for r in m["reconciliation"]:
            out.append(f"  - {r['check']}: script {fmt_int(r['script'])}, profile {fmt_int(r['cohort_profile'])}, "
                       f"match: {r['match']}")
    out.append("")
    return "\n".join(out)


# ---- main ----------------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build docs/impact/impact-model.{json,md} from the provided CSVs.")
    parser.add_argument("--returns-csv", type=Path, default=RETURNS_CSV)
    parser.add_argument("--exchanges-csv", type=Path, default=EXCHANGES_CSV)
    parser.add_argument("--eval-results", type=Path, default=EVAL_RESULTS)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)

    for path in (args.returns_csv, args.exchanges_csv):
        if not path.is_file():
            print(f"error: input file not found: {rel(path)}", file=sys.stderr)
            return 2

    returns = scan_returns(args.returns_csv)
    exchanges = scan_exchanges(args.exchanges_csv, returns["store_banners"])
    cost = load_agent_cost(args.eval_results)
    model = build_model(returns, exchanges, cost, args.returns_csv, args.exchanges_csv)

    status_json = write_if_changed(args.out_dir / "impact-model.json",
                                   json.dumps(model, indent=2, ensure_ascii=False) + "\n")
    status_md = write_if_changed(args.out_dir / "impact-model.md", render_markdown(model))

    rw, out, sc, ex = model["returns_window"], model["scenarios"]["outputs"], model["scale"], model["exchange_evidence"]
    print(f"Window {model['window']['start']}..{model['window']['end']} ({model['window']['days']} days), {BANNER}, {STATUS}")
    print(f"Return lines: {rw['total_return_lines']['display']}; fit lines: {rw['fit_lines']['display']} "
          f"(too small {rw['fit_lines_too_small']['display']}, too big {rw['fit_lines_too_big']['display']}, "
          f"{rw['fit_share_of_lines']['display']} of lines)")
    print(f"Fit refund sum {rw['fit_refund_sum']['display']}, median {rw['fit_refund_median']['display']}")
    print(f"Lines/day mean {sc['lines_per_day_mean']['display']}, p95 {sc['lines_per_day_p95']['display']}, "
          f"max {sc['lines_per_day_max']['display']}")
    print(f"Exchanges in window, all stores: CONFIRMED {ex['confirmed_all_stores']['display']}, "
          f"ASSUMED {ex['assumed_all_stores']['display']}; ALDO_CA-attributed: "
          f"CONFIRMED {ex['confirmed_aldo_ca_stores']['display']}, ASSUMED {ex['assumed_aldo_ca_stores']['display']}")
    reason = f" - {cost['reason']}" if cost["reason"] else (f" - {cost['field']}" if cost["field"] else "")
    print(f"Agent cost per run {fmt_rate(cost['cost_per_run_cad'])} ({model['agent_cost_status']}){reason}")
    for name in model["scenarios"]["columns"]:
        print(f"  {name:>4}: gross {out['gross_value']['display'][name]}, agent cost "
              f"{out['agent_cost_total']['display'][name]}, annual net {out['annual_net_value']['display'][name]}")
    pw = model["prevention_what_if"]
    print("Pre-purchase prevention what-if (assumed): " + "; ".join(
        f"{pw['inputs']['prevention_rate_pct']['display'][n]} -> {pw['outputs']['total_value']['display'][n]} "
        f"({pw['outputs']['prevented_lines']['display'][n]} lines)" for n in pw["columns"]))
    for r in model["reconciliation"]:
        print(f"check {'OK' if r['match'] else 'MISMATCH'}: {r['check']} script={r['script']} profile={r['cohort_profile']}")
    print(f"impact-model.json {status_json}; impact-model.md {status_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
