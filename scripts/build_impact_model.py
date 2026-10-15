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

    python3.12 scripts/build_impact_model.py [--eval-results <results.json>]

Impact model v2 (Demo v2 levers, additive): all in-window ALDO_CA lines are classified by reason group
(fit, preference, quality or damage, delivery or wrong item, other) with the product taxonomy; a
preference-return exchange lever, multi-line (bundle) return metrics, a tier perk what-if (inputs only),
agent-load numbers in the scale block (from the eval results) and an agent cost sensitivity are added next
to the original blocks. The USD to CAD rate is read from backend/returns_domain/config.py (one source of
truth, parsed as text).

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
import re
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
TXN_PROFILE = ROOT / "docs" / "evidence" / "transaction-profile.json"  # scripts/profile_transactions.py (margin lever)
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
# Bedrock bills in USD. The USD to CAD rate has ONE source of truth: backend/returns_domain/config.py (used by the
# eval and the runtime metrics too). It is parsed as text here (nothing is imported, the file is never edited) and
# the fallback below is used, with a printed warning, only when the constant cannot be found.
CONFIG_PY = ROOT / "backend" / "returns_domain" / "config.py"
USD_TO_CAD_FALLBACK = Decimal("1.40")
USD_TO_CAD_PATTERNS = (
    # usd_to_cad=_decimal(source, "USD_TO_CAD", "1.38")
    re.compile(r"""["']USD_TO_CAD["']\s*,\s*["'](\d+(?:\.\d+)?)["']"""),
    # DEFAULT_USD_TO_CAD = Decimal("1.38")   or   USD_TO_CAD = 1.38
    re.compile(r"""^\s*(?:DEFAULT_)?USD_TO_CAD\s*(?::[^=\n]+)?=\s*(?:Decimal\(\s*)?["']?(\d+(?:\.\d+)?)""", re.M),
)

# ---- Demo v2 levers (impact model v2) -----------------------------------------------------------
# Reason groups. The product taxonomy (backend/returns_domain/taxonomy.py, categorize()) is the single mapping from
# raw return reasons to categories; this table only folds its categories into the five reporting groups. The fit group
# is kept EXACTLY equal to the earlier fit definition (Too small, Too big), so a fit-type spelling outside it (the French
# 'trop grand') goes to other. Comfort complaints and process reasons are ambiguous or not a product problem: other.
GROUPS = ("fit", "preference", "quality_damage", "delivery_wrong_item", "other")
GROUP_LABELS = {
    "fit": "Fit (Too small, Too big)",
    "preference": "Preference (changed mind or idea, not as expected, colour)",
    "quality_damage": "Quality or damage",
    "delivery_wrong_item": "Delivery or wrong item",
    "other": "Other or unclear",
}
CATEGORY_TO_GROUP = {
    "FIT_SIZE": "fit",  # only for the exact fit reasons (FIT_REASONS), see group_for_reason
    "EXPECTATION": "preference",
    "CHANGE_OF_MIND": "preference",
    "QUALITY_DAMAGE": "quality_damage",
    "DELIVERY": "delivery_wrong_item",
    "COMFORT": "other",
    "OTHER": "other",
    "UNKNOWN": "other",
}
TAXONOMY_SOURCE = "backend/returns_domain/taxonomy.py"
STORE_CHANNEL = "instore"  # return_channel value of in-store returns (compared lower-case)
# 'Changed my mind / Other' and its 20-character truncation ('...Changed my mind / Ot'): one label that mixes change of
# mind with unspecified reasons.
COMBINED_LABEL_PREFIX = "changed my mind / ot"
PROCESS_NOTE = "Process reason, no product problem stated: other."
REASON_NOTES = {
    "changed my mind / other": "Combined label: change of mind and unspecified reasons share one option. Counted as "
                               "preference because the product taxonomy maps it to CHANGE_OF_MIND, so the preference "
                               "group is an upper bound.",
    "changed my mind / ot": "Truncated spelling of 'Changed my mind / Other' (see above).",
    "uncomfortable": "Comfort complaint, fit-adjacent but not a size reason and not a preference: other.",
    "inconfortable": "French spelling of 'uncomfortable' (see above): other.",
    "trop grand": "French for 'too big'. Kept out of fit so that the fit group equals the earlier definition "
                  "(Too small and Too big only): other.",
    "return without merch": PROCESS_NOTE,
    "return without merchandise": PROCESS_NOTE,
    "return extension": PROCESS_NOTE,
    "gift return": PROCESS_NOTE,
}

# Preference-return exchange lever (what-if). Conversion is the share of preference lines, where the alternative
# colour or style is in stock, that end as an exchange instead of a refund. All assumed, to validate in a pilot.
PREFERENCE_CONVERSION_PCT = {"low": 5, "base": 10, "high": 15}
ALT_COLOUR_AVAILABILITY_PCT = 60
# Unit economics behind the CAD 54 (handoff section 15, synthetic CAD 100 order): item cost 40 (40% of price), refund
# path processing 12, exchange path processing 10 + reshipping 8. Exchange versus refund therefore equals
# (price - cost) - (10 + 8 - 12) = 0.60 x price - 6, which gives exactly CAD 54 at CAD 100. The preference lever applies
# the same structure to the MEASURED median refund of the preference lines instead of the synthetic CAD 100.
SYNTHETIC_ORDER_CAD = Decimal("100")
UNIT_COST_RATIO = Decimal("0.40")
NET_EXTRA_HANDLING_CAD = Decimal("6")

# Tier perk what-if (inputs only). The fee and the monthly allowance are read from the demo fixtures when present.
PLATINUM_SHARE_PCT = {"low": 1, "base": 2, "high": 3}  # assumed: Platinum members per 100 customers
CUSTOMERS_BASIS = 10_000
PERK_USES_MIN_PER_YEAR = 1
PERK_FEE_FALLBACK_CENTS = 595
PERK_PER_MONTH_FALLBACK = 1
POLICY_JSON = ROOT / "fixtures" / "demo" / "policy.json"
LOGISTICS_JSON = ROOT / "fixtures" / "demo" / "logistics.json"

# Agent load (scale block). Both are assumptions, tagged assumed wherever they feed a number.
ACTIVE_WINDOW_HOURS = 12
BUSIEST_HOUR_SHARE_PCT = 15
# In-store capture gap: the smallest ratio between neighbouring sorted monthly in-store counts that counts as a break.
CAPTURE_GAP_MIN_BREAK = 5

CURRENCY_NOTE = "CAD assumed for ALDO_CA, unconfirmed"

# Normalized (strip, lower-case, single-spaced) source header names.
RETURNS_COLUMNS = {
    "banner": "store_banner",
    "return_id": "return_order_number",
    "date": "return_creation_date",
    "status": "last return status",
    "refund": "refund amount",
    "reason": "reason",
    "channel": "return_channel",  # in-store share per reason group (v2)
    "sku": "sku",  # distinct items inside a multi-line return (v2); counted, never written out
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


def parse_refund_cents(text: str):
    """Refund amount in integer cents, or None when it is not a finite number."""
    try:
        amount = Decimal(text.strip())
    except InvalidOperation:
        return None
    return to_cents(amount) if amount.is_finite() else None


def load_taxonomy():
    """Import the product taxonomy without side effects: stdlib only, and the package __init__ imports nothing.

    No bytecode is written next to the backend sources (this script does not own that folder)."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    added = None
    try:
        try:
            from returns_domain import taxonomy
        except ImportError:
            added = str(ROOT / "backend")
            sys.path.insert(0, added)
            from returns_domain import taxonomy
    except ImportError as exc:
        raise SystemExit(f"error: cannot import {TAXONOMY_SOURCE} (needed for the reason groups): {exc}")
    finally:
        sys.dont_write_bytecode = previous
        if added and added in sys.path:
            sys.path.remove(added)
    return taxonomy


def group_for_reason(reason: str, taxonomy) -> tuple:
    """(group, taxonomy category) for a normalized reason.

    The fit group is exactly the earlier fit definition; a fit-type spelling outside it goes to other."""
    category = taxonomy.categorize(reason).value
    if category == "FIT_SIZE":
        return ("fit" if reason in FIT_REASONS else "other"), category
    return CATEGORY_TO_GROUP.get(category, "other"), category


def read_usd_to_cad(path: Path = CONFIG_PY) -> dict:
    """USD to CAD from backend/returns_domain/config.py: the one source of truth shared with the eval and the runtime.

    The default of the USD_TO_CAD setting is parsed as text; nothing is imported and the file is not edited. When it
    cannot be found, USD_TO_CAD_FALLBACK is used and a warning is printed."""
    shown = rel(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for pattern in USD_TO_CAD_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                rate = Decimal(match.group(1))
            except InvalidOperation:
                continue
            if Decimal(0) < rate < Decimal(100):
                return {"rate": rate, "source": f"{shown} (USD_TO_CAD default)", "found": True}
    print(f"warning: USD_TO_CAD not found in {shown}; using the fallback {USD_TO_CAD_FALLBACK} CAD per USD",
          file=sys.stderr)
    return {"rate": USD_TO_CAD_FALLBACK, "source": f"fallback {USD_TO_CAD_FALLBACK} (could not read {shown})",
            "found": False}


def read_perk_inputs() -> dict:
    """Return-mail fee and Platinum free-shipping allowance from the demo fixtures; documented fallbacks otherwise."""
    fee_cents, per_month = PERK_FEE_FALLBACK_CENTS, PERK_PER_MONTH_FALLBACK
    sources, warnings = [], []
    try:
        fee = json.loads(LOGISTICS_JSON.read_text(encoding="utf-8"))["mail_return"]["fee_cents"]
        if isinstance(fee, int) and not isinstance(fee, bool) and fee > 0:
            fee_cents = fee
            sources.append(f"{rel(LOGISTICS_JSON)} (mail_return.fee_cents)")
        else:
            raise ValueError("not a positive integer")
    except (OSError, ValueError, KeyError, TypeError):
        warnings.append(f"fee: could not read {rel(LOGISTICS_JSON)}; using {PERK_FEE_FALLBACK_CENTS} cents")
    try:
        allowance = (json.loads(POLICY_JSON.read_text(encoding="utf-8"))["tiers"]["platinum"]["benefits"]
                     ["free_return_shipping"]["per_month"])
        if isinstance(allowance, int) and not isinstance(allowance, bool) and allowance > 0:
            per_month = allowance
            sources.append(f"{rel(POLICY_JSON)} (tiers.platinum.benefits.free_return_shipping.per_month)")
        else:
            raise ValueError("not a positive integer")
    except (OSError, ValueError, KeyError, TypeError):
        warnings.append(f"allowance: could not read {rel(POLICY_JSON)}; using {PERK_PER_MONTH_FALLBACK} per month")
    for text in warnings:
        print(f"warning: tier perk input {text}", file=sys.stderr)
    return {"fee_cents": fee_cents, "per_month": per_month, "sources": sources, "warnings": warnings}


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
    taxonomy = load_taxonomy()
    rows_read = malformed = bad_dates = refund_unparseable = 0
    banner_rows = banner_completed = banner_fit_all = total_lines = 0
    fit_by_reason: Counter = Counter()
    refund_cents: list = []
    per_day: Counter = Counter()
    lines_per_return: Counter = Counter()  # return number -> lines in scope
    return_skus: dict = defaultdict(set)  # return number -> distinct SKUs (counted only, never written out)
    fit_return_ids: set = set()
    store_banners: dict = defaultdict(Counter)  # in-store return location -> banner counts
    # v2: every in-scope line belongs to exactly one reason group
    group_cache: dict = {}  # normalized reason -> (group, taxonomy category)
    group_lines: Counter = Counter()
    group_store_lines: Counter = Counter()
    group_refunds: dict = defaultdict(list)
    group_refund_bad: Counter = Counter()
    reason_lines: Counter = Counter()  # (reason, taxonomy category, group) -> lines
    reason_store_lines: Counter = Counter()
    month_channel: Counter = Counter()  # (yyyy-mm, returned in store) -> lines

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh)
        cols, index = resolve_columns(next(reader), RETURNS_COLUMNS, path.name)
        i_banner, i_ret, i_date = cols["banner"], cols["return_id"], cols["date"]
        i_status, i_refund, i_reason = cols["status"], cols["refund"], cols["reason"]
        i_channel, i_sku = cols["channel"], cols["sku"]
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
            lines_per_return[return_id] += 1
            return_skus[return_id].add(row[i_sku].strip())
            cents = parse_refund_cents(row[i_refund])
            grouped = group_cache.get(reason)
            if grouped is None:
                grouped = group_cache[reason] = group_for_reason(reason, taxonomy)
            group, category = grouped
            in_store = row[i_channel].strip().lower() == STORE_CHANNEL
            month_channel[(f"{day.year:04d}-{day.month:02d}", in_store)] += 1
            group_lines[group] += 1
            reason_lines[(reason, category, group)] += 1
            if in_store:
                group_store_lines[group] += 1
                reason_store_lines[(reason, category, group)] += 1
            if cents is None:
                group_refund_bad[group] += 1
            else:
                group_refunds[group].append(cents)
            if not is_fit:
                continue
            fit_by_reason[reason] += 1
            fit_return_ids.add(return_id)
            if cents is None:
                refund_unparseable += 1
            else:
                refund_cents.append(cents)

    return {
        "rows_read": rows_read, "malformed_rows": malformed, "unparseable_dates_in_scope": bad_dates,
        "banner_rows": banner_rows, "banner_completed_rows": banner_completed,
        "banner_fit_rows_all": banner_fit_all,
        "total_lines": total_lines, "fit_by_reason": fit_by_reason,
        "refund_cents": refund_cents, "refund_unparseable": refund_unparseable,
        "per_day": per_day, "return_ids": len(lines_per_return), "fit_return_ids": len(fit_return_ids),
        "store_banners": store_banners,
        "lines_per_return": lines_per_return,
        "multi_line_returns_with_distinct_skus": sum(
            1 for rid, n in lines_per_return.items() if n >= 2 and len(return_skus[rid]) >= 2),
        "group_lines": group_lines, "group_store_lines": group_store_lines, "group_refunds": group_refunds,
        "group_refund_unparseable": group_refund_bad,
        "reason_lines": reason_lines, "reason_store_lines": reason_store_lines,
        "month_channel": month_channel,
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


def dig(data, *keys):
    """data[k1][k2]... or None when any step is missing."""
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def load_agent_cost(path: Path, rate: dict = None) -> dict:
    rate = rate or read_usd_to_cad()
    usd_to_cad = rate["rate"]
    info = {
        "status": "pending_measurement", "reason": "", "file": rel(path), "sha256": None,
        "field": None, "currency_in_file": None, "usd_to_cad_used": False,
        "cost_per_run_cad": PLACEHOLDER_AGENT_COST_PER_RUN_CAD,
        "usd_to_cad": usd_to_cad, "usd_to_cad_source": rate["source"], "eval_usd_to_cad": None,
        "raw_value": None, "mode": None, "runs_costed": None,
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
    mode = dig(data, "mode")
    info["mode"] = mode if isinstance(mode, str) else None
    runs_costed = dig(data, "summary", "returns_agent_runs_costed")
    info["runs_costed"] = runs_costed if isinstance(runs_costed, int) and not isinstance(runs_costed, bool) \
        and runs_costed > 0 else None
    eval_rate = dig(data, "pricing", "usd_to_cad")
    if isinstance(eval_rate, (int, float)) and not isinstance(eval_rate, bool) and eval_rate > 0:
        info["eval_usd_to_cad"] = Decimal(str(eval_rate))
        if info["eval_usd_to_cad"] != usd_to_cad:
            print(f"warning: the eval file priced CAD at {eval_rate} per USD but {rate['source']} says {usd_to_cad}; "
                  f"this model uses {usd_to_cad}", file=sys.stderr)
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
        cad = value * usd_to_cad if currency == "usd" else value
        parsed.append((field_path, currency, cad.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), value))

    if len({cad for _, _, cad, _ in parsed}) > 1:
        info["reason"] = "ambiguous: several different mean-cost fields at " + ", ".join(p for p, _, _, _ in parsed)
        return info
    field_path, currency, cad, value = parsed[0]
    info.update(status="measured", field=field_path, currency_in_file=currency,
                usd_to_cad_used=currency == "usd", cost_per_run_cad=cad, raw_value=value)
    return info


def load_eval_profile(path: Path):
    """Per-scenario wall-clock latency, mean tokens and mean cost of the ALDO returns-agent runs in the eval results.

    Reads scenarios.<id>.latency_ms.p50, tokens.mean_input, tokens.mean_output and cost_usd.mean_per_run (the layout
    written by scripts/eval_scenarios.py), skipping scenarios flagged returns_agent false. Means are weighted by the
    number of runs per scenario. Returns None when the file or these fields are missing."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scenarios = dig(data, "scenarios")
    if not isinstance(scenarios, dict):
        return None
    rows = []
    for sid, sc in scenarios.items():
        if not isinstance(sc, dict) or sc.get("returns_agent") is False:
            continue
        try:
            runs = int(sc.get("runs", 1))
            row = {
                "id": str(sid), "runs": runs,
                "p50_ms": Decimal(str(sc["latency_ms"]["p50"])),
                "tokens_in": Decimal(str(sc["tokens"]["mean_input"])),
                "tokens_out": Decimal(str(sc["tokens"]["mean_output"])),
                "cost_usd": Decimal(str(sc["cost_usd"]["mean_per_run"])),
            }
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if runs < 1 or row["p50_ms"] <= 0 or min(row["tokens_in"], row["tokens_out"], row["cost_usd"]) < 0:
            continue
        rows.append(row)
    if not rows:
        return None
    rows.sort(key=lambda r: r["id"])
    total_runs = sum(r["runs"] for r in rows)

    def wmean(key):
        return sum(r[key] * r["runs"] for r in rows) / total_runs

    return {
        "scenario_ids": [r["id"] for r in rows], "runs": total_runs,
        "duration_s_mean": wmean("p50_ms") / 1000,
        "duration_s_min": min(r["p50_ms"] for r in rows) / 1000,
        "duration_s_max": max(r["p50_ms"] for r in rows) / 1000,
        "input_tokens_mean": wmean("tokens_in"), "output_tokens_mean": wmean("tokens_out"),
        "cost_usd_max": max(rows, key=lambda r: (r["cost_usd"], r["id"])),
        "cost_usd_min": min(rows, key=lambda r: (r["cost_usd"], r["id"])),
    }


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
            note=(f"Measured on {runs_label(cost)} at assumed model prices (prices in returns_domain.config)."
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


# ---- Demo v2 blocks (impact model v2; all additive) ---------------------------------------------
def jint(value) -> int:
    return int(quantized(value, 0))


def pct_of(part, whole) -> Decimal:
    return Decimal(part) * 100 / Decimal(whole) if whole else Decimal(0)


def cost_tag_of(cost: dict) -> str:
    return "derived" if cost["status"] == "measured" else "pending_measurement"


def runs_label(cost: dict) -> str:
    """'6 live runs' style label for the measured agent cost (never claims 'live' unless the eval file says so)."""
    n, mode = cost.get("runs_costed"), cost.get("mode")
    if n and mode == "live":
        return f"{n} live runs"
    if n:
        return f"{n} eval runs"
    return "live eval runs" if mode == "live" else "eval runs"


def build_reason_groups(returns: dict, total_lines: int, fit_lines: int, fit_refund_sum: int,
                        fit_refund_median: int) -> dict:
    """Every in-scope line belongs to exactly one reason group (measured), plus the raw-reason mapping for audit."""
    group_lines, store_by_group = returns["group_lines"], returns["group_store_lines"]
    store_lines = sum(store_by_group.values())
    groups = {}
    for g in GROUPS:
        n, n_store = group_lines[g], store_by_group[g]
        cents = returns["group_refunds"].get(g, [])
        share, store_share = pct_of(n, total_lines), pct_of(n_store, n)
        groups[g] = {
            "label": GROUP_LABELS[g],
            "lines": metric("Lines", n, "lines", "measured", fmt_int(n)),
            "share_of_lines": metric("Share of lines in scope", jnum(share), "percent", "measured",
                                     f"{fmt_dec(share)}%", note="Ratio of two measured counts."),
            "refund_sum": metric("Refund amount, sum", sum(cents), "CAD_cents", "measured", fmt_cad(sum(cents)),
                                 note="Column summed as provided. Line versus transaction grain is unconfirmed."),
            "refund_median": metric("Refund amount, median", median_cents(cents), "CAD_cents", "measured",
                                    fmt_cad(median_cents(cents))),
            "lines_in_store": metric("Lines returned in store", n_store, "lines", "measured", fmt_int(n_store)),
            "share_in_store": metric("Share of the group returned in store", jnum(store_share), "percent",
                                     "measured", f"{fmt_dec(store_share)}%"),
        }

    order = {g: i for i, g in enumerate(GROUPS)}
    mapping = []
    for key, n in sorted(returns["reason_lines"].items(), key=lambda kv: (order[kv[0][2]], -kv[1], kv[0][0])):
        reason, category, group = key
        row = {"reason": reason or "(blank)", "lines": n, "lines_in_store": returns["reason_store_lines"].get(key, 0),
               "taxonomy_category": category, "group": group}
        if reason in REASON_NOTES:
            row["note"] = REASON_NOTES[reason]
        mapping.append(row)
    other_rows = [r for r in mapping if r["group"] == "other"]
    other_note = ("Reasons placed in other because they are ambiguous or not a product problem: "
                  + ", ".join(f"{r['reason']} ({fmt_int(r['lines'])})" for r in other_rows) + "."
                  ) if other_rows else "No reason was placed in other."

    # The combined label 'Changed my mind / Other' dominates the preference group, mostly through store returns.
    combined = [(k, n) for k, n in returns["reason_lines"].items() if k[0].startswith(COMBINED_LABEL_PREFIX)]
    if any(k[2] != "preference" for k, _ in combined):
        raise SystemExit("error: the combined label 'Changed my mind / Other' is no longer in the preference group")
    combined_lines = sum(n for _, n in combined)
    combined_store = sum(returns["reason_store_lines"].get(k, 0) for k, _ in combined)
    pref_lines = group_lines["preference"]
    composition = {
        "combined_label_lines": metric(
            "Preference lines with the combined label 'Changed my mind / Other'", combined_lines, "lines", "measured",
            fmt_int(combined_lines), note="One label for change of mind and unspecified reasons."),
        "combined_label_share_of_preference": metric(
            "Combined label as share of the preference lines", jnum(pct_of(combined_lines, pref_lines)), "percent",
            "measured", f"{fmt_dec(pct_of(combined_lines, pref_lines))}%"),
        "specific_reason_lines": metric(
            "Preference lines with a specific reason (not as expected, changed idea, found something else, colour, "
            "advertised)", pref_lines - combined_lines, "lines", "measured", fmt_int(pref_lines - combined_lines),
            note="Preference lines without the combined label. Floor for the preference lever."),
        "store_lines": metric("Lines returned in store (channel InStore)", store_lines, "lines", "measured",
                              fmt_int(store_lines)),
        "store_share_of_all_lines": metric(
            "Lines returned in store, share of all lines in scope", jnum(pct_of(store_lines, total_lines)), "percent",
            "measured", f"{fmt_dec(pct_of(store_lines, total_lines))}%"),
        "combined_label_lines_in_store": metric(
            "Lines with the combined label that were returned in store", combined_store, "lines", "measured",
            fmt_int(combined_store)),
        "combined_label_share_of_store_lines": metric(
            "In-store lines that carry the combined label", jnum(pct_of(combined_store, store_lines)), "percent",
            "measured", f"{fmt_dec(pct_of(combined_store, store_lines))}%",
            note="In store the label may be entered as a default, so it is weaker evidence of a taste-based return."),
        "combined_label_share_of_other_channel_lines": metric(
            "Lines from other channels (web, mail, call centre) that carry the combined label",
            jnum(pct_of(combined_lines - combined_store, total_lines - store_lines)), "percent", "measured",
            f"{fmt_dec(pct_of(combined_lines - combined_store, total_lines - store_lines))}%"),
    }

    # Lines per month and channel (aggregates). The returns file is known to miss most in-store returns for some
    # months. A capture gap is reported when the sorted monthly in-store counts have a break of at least
    # CAPTURE_GAP_MIN_BREAK (a ratio between neighbouring months); the months below the largest break form the gap.
    months = sorted({month for month, _ in returns["month_channel"]})
    monthly = [{"month": month, "in_store_lines": returns["month_channel"][(month, True)],
                "other_channel_lines": returns["month_channel"][(month, False)]} for month in months]
    ordered = sorted(monthly, key=lambda r: (r["in_store_lines"], r["month"]))
    break_ratio, split = 0.0, None
    for i in range(len(ordered) - 1):
        ratio = ordered[i + 1]["in_store_lines"] / max(ordered[i]["in_store_lines"], 1)
        if ratio > break_ratio:
            break_ratio, split = ratio, i
    capture_gap = None
    if split is not None and break_ratio >= CAPTURE_GAP_MIN_BREAK:
        gap, rest = ordered[:split + 1], ordered[split + 1:]
        capture_gap = {
            "months": sorted(r["month"] for r in gap),
            "in_store_lines_range": [min(r["in_store_lines"] for r in gap), max(r["in_store_lines"] for r in gap)],
            "in_store_lines_range_other_months": [min(r["in_store_lines"] for r in rest),
                                                  max(r["in_store_lines"] for r in rest)],
            "other_channel_lines_range": [min(r["other_channel_lines"] for r in monthly),
                                          max(r["other_channel_lines"] for r in monthly)],
            "break_ratio": round(break_ratio, 1),
        }

    checks = [
        ("Group lines sum to the return lines in scope", sum(group_lines[g] for g in GROUPS), total_lines),
        ("Mapping rows sum to the return lines in scope", sum(r["lines"] for r in mapping), total_lines),
        ("Monthly channel mix sums to the return lines in scope",
         sum(r["in_store_lines"] + r["other_channel_lines"] for r in monthly), total_lines),
        ("Fit group lines equal the fit lines of the exchange scenarios (Too small + Too big)",
         group_lines["fit"], fit_lines),
        ("Fit group refund sum equals the fit refund sum", sum(returns["group_refunds"].get("fit", [])),
         fit_refund_sum),
        ("Fit group refund median equals the fit refund median", median_cents(returns["group_refunds"].get("fit", [])),
         fit_refund_median),
    ]
    checks = [{"check": c, "script": got, "expected": want, "match": got == want, "tag": "measured"}
              for c, got, want in checks]
    failed = [c["check"] for c in checks if not c["match"]]
    if failed:
        raise SystemExit("error: reason group reconciliation failed: " + "; ".join(failed))

    return {
        "tag": "measured",
        "framing": "All completed ALDO_CA return lines in the window, each in exactly one reason group. Counts, sums "
                   "and medians come straight from the returns file; the grouping uses the product taxonomy.",
        "mapping_source": f"{TAXONOMY_SOURCE} categorize() gives the category; CATEGORY_TO_GROUP in "
                          "scripts/build_impact_model.py folds categories into groups. The fit group stays identical "
                          "to the earlier definition (Too small and Too big only).",
        "normalization": f"strip, lower-case, whitespace collapse and removal of the '{REASON_PREFIX}' prefix",
        "groups": groups,
        "preference_composition": composition,
        "mapping": mapping,
        "other_group_note": other_note,
        "monthly_channel_mix": monthly,
        "capture_gap": capture_gap,
        "checks": checks,
    }


def unit_value_cad(price_cad: Decimal) -> Decimal:
    """Exchange versus refund for an item of this price: (price - cost) - net extra handling (handoff section 15)."""
    return (1 - UNIT_COST_RATIO) * price_cad - NET_EXTRA_HANDLING_CAD


def preference_value(lines: int, avail: Decimal, conv: Decimal, rep: Decimal, unit_cad: Decimal) -> dict:
    converted = Decimal(lines) * avail * conv
    avoided = converted * rep
    exchange_value = to_cents(converted * unit_cad)
    avoided_value = to_cents(avoided * AVOIDED_SECOND_RETURN_COST_CAD)
    return {"converted": converted, "avoided": avoided, "exchange_value": exchange_value,
            "avoided_value": avoided_value, "gross": exchange_value + avoided_value}


PREFERENCE_FORMULA = (
    "preference_gross_value = preference_lines x alt_colour_availability x exchange_conversion x value_per_converted_case\n"
    "                       + preference_lines x alt_colour_availability x exchange_conversion x repeat_return_reduction"
    " x avoided_second_return_cost\n"
    "value_per_converted_case = (1 - item_cost_ratio) x median_refund_of_preference_lines - net_extra_handling\n"
    "standalone_net  = preference_gross_value - total_requests x agent_cost_per_run\n"
    "combined_net    = fit_gross_value + preference_gross_value - total_requests x agent_cost_per_run"
    "   (agent cost counted once)"
)


def build_preference_lever(reason_groups: dict, scenarios: dict, cost: dict, total_requests: int) -> dict:
    """Preference-return exchange what-if next to the fit scenarios (same low/base/high shape, same agent cost)."""
    if unit_value_cad(SYNTHETIC_ORDER_CAD) != VALUE_PER_CONVERSION_CAD:
        raise SystemExit("error: the unit economics no longer reproduce the CAD 54 of the fit scenarios")
    names = list(PREFERENCE_CONVERSION_PCT)
    pref = reason_groups["groups"]["preference"]
    pref_lines = pref["lines"]["value"]
    specific_lines = reason_groups["preference_composition"]["specific_reason_lines"]["value"]
    median = pref["refund_median"]["value"]
    unit_cents = to_cents(unit_value_cad(Decimal(median) / 100))
    unit = Decimal(unit_cents) / 100
    avail = Decimal(ALT_COLOUR_AVAILABILITY_PCT) / 100
    cost_tag = cost_tag_of(cost)
    agent_total = to_cents(Decimal(total_requests) * cost["cost_per_run_cad"])
    fit_gross = scenarios["outputs"]["gross_value"]["values"]
    fit_net = scenarios["outputs"]["annual_net_value"]["values"]
    calc = {}
    for name in names:
        conv_pct, rep_pct = PREFERENCE_CONVERSION_PCT[name], SCENARIOS[name]["repeat_reduction_pct"]
        conv, rep = Decimal(conv_pct) / 100, Decimal(rep_pct) / 100
        main = preference_value(pref_lines, avail, conv, rep, unit)
        flat = preference_value(pref_lines, avail, conv, rep, VALUE_PER_CONVERSION_CAD)
        floor = preference_value(specific_lines, avail, conv, rep, unit)
        combined_gross = fit_gross[name] + main["gross"]
        calc[name] = {
            "conv": conv_pct, "rep": rep_pct, **main,
            "agent_total": agent_total, "net": main["gross"] - agent_total,
            "fit_gross": fit_gross[name], "combined_gross": combined_gross, "combined_net": combined_gross - agent_total,
            "flat_gross": flat["gross"], "floor_gross": floor["gross"],
        }
        if calc[name]["combined_net"] != fit_net[name] + main["gross"]:
            raise SystemExit("error: combined net does not equal fit net plus preference gross (agent cost counted twice?)")

    per, const = scenario_builders(calc, names)
    inputs = {
        "preference_lines": const(
            "Preference lines (baseline, window)", "lines", "measured", pref_lines, fmt_int(pref_lines),
            note="Reason group preference: change of mind or idea, not as expected, colour. Upper bound, see "
                 "specific_reason_lines."),
        "specific_reason_lines": const(
            "Preference lines with a specific reason (floor baseline)", "lines", "measured", specific_lines,
            fmt_int(specific_lines), note="Preference lines without the combined label 'Changed my mind / Other'."),
        "median_refund_preference": const(
            "Median refund of the preference lines (price of the typical returned item)", "CAD_cents", "measured",
            median, fmt_cad(median), note="Refund column as provided; line versus transaction grain is unconfirmed."),
        "alt_colour_availability_pct": const(
            "Availability of the alternative colour or style (same price band, customer's size)", "percent",
            "assumed", ALT_COLOUR_AVAILABILITY_PCT, f"{ALT_COLOUR_AVAILABILITY_PCT}%",
            note="Assumption with no stock data behind it. Validate against ALDO inventory in a pilot."),
        "exchange_conversion_pct": per(
            "Exchange conversion of preference lines where the alternative is available", "percent", "assumed",
            "conv", lambda v: f"{v}%", note="Share that end as an exchange instead of a refund. Additional to the "
                                            "in-store exchanges that already happen. Assumption to validate in a pilot."),
        "repeat_return_reduction_pct": per(
            "Repeat-return reduction on converted cases", "percent", "assumed", "rep", lambda v: f"{v}%",
            note="Reused unchanged from the fit scenarios."),
        "item_cost_ratio_pct": const(
            "Item cost as share of price", "percent", "assumed", jint(UNIT_COST_RATIO * 100),
            f"{jint(UNIT_COST_RATIO * 100)}%", note="Handoff section 15 synthetic unit economics (cost 40 on a CAD 100 order)."),
        "net_extra_handling_per_exchange": const(
            "Net extra handling of an exchange versus a refund", "CAD_cents", "assumed",
            to_cents(NET_EXTRA_HANDLING_CAD), fmt_cad(to_cents(NET_EXTRA_HANDLING_CAD)),
            note="Exchange processing 10 + reshipping 8 minus refund processing 12 (handoff section 15)."),
        "value_per_converted_case": const(
            "Incremental value per converted case (from the measured median refund)", "CAD_cents", "derived",
            unit_cents, fmt_cad(unit_cents),
            note="(1 - item cost ratio) x median refund - net extra handling. The same formula at the synthetic "
                 "CAD 100 order gives the CAD 54 used in the fit scenarios. Incremental economic value versus a "
                 "refund, not gross margin."),
        "avoided_second_return_cost": const(
            "Avoided second-return cost", "CAD_cents", "assumed", to_cents(AVOIDED_SECOND_RETURN_COST_CAD),
            fmt_cad(to_cents(AVOIDED_SECOND_RETURN_COST_CAD)), note="Reused unchanged from the fit scenarios."),
        "total_requests": const(
            "Agent runs charged (total return lines in window)", "runs", "derived", total_requests,
            fmt_int(total_requests), note="Same basis as the fit scenarios: the agent runs on every return line."),
        "agent_cost_per_run": const(
            "Agent cost per run", "CAD_per_run", cost_tag, jnum(cost["cost_per_run_cad"], 4),
            fmt_rate(cost["cost_per_run_cad"]), note="Same cost per run as the fit scenarios."),
    }
    outputs = {
        "converted_cases": per("Converted cases", "cases", "assumed", "converted", lambda v: fmt_dec(v, 0),
                               as_json=lambda v: jnum(v),
                               note="Preference lines x availability x conversion (expected value)."),
        "exchange_value": per("Exchange value", "CAD_cents", "assumed", "exchange_value", fmt_cad,
                              note="Converted cases x value per converted case."),
        "avoided_second_returns": per("Avoided second returns", "cases", "assumed", "avoided",
                                      lambda v: fmt_dec(v, 0), as_json=lambda v: jnum(v)),
        "avoided_second_return_value": per("Avoided second-return value", "CAD_cents", "assumed", "avoided_value",
                                           fmt_cad),
        "gross_value": per("Gross annual value (before agent cost)", "CAD_cents", "assumed", "gross", fmt_cad),
        "agent_cost_total": per("Agent cost (total requests x cost per run)", "CAD_cents", cost_tag, "agent_total",
                                fmt_cad, note="The same agent cost as the fit scenarios, charged once."),
        "annual_net_value_standalone": per(
            "Annual net value if this were the only lever", "CAD_cents", "assumed", "net", fmt_cad,
            note="Gross value minus the full agent cost, the same treatment as the fit scenarios."),
        "fit_gross_value": per("Fit scenarios: gross annual value (reference)", "CAD_cents", "derived", "fit_gross",
                               fmt_cad, note="From the fit scenarios above, unchanged."),
        "combined_gross_value": per(
            "Fit + preference: gross annual value", "CAD_cents", "assumed", "combined_gross", fmt_cad,
            note="Additive by construction: a line has exactly one reason group, so fit and preference lines never "
                 "overlap."),
        "combined_annual_net_value": per(
            "Fit + preference: annual net value", "CAD_cents", "assumed", "combined_net", fmt_cad,
            note="Combined gross minus the agent cost counted once (not once per lever)."),
        "gross_value_at_flat_fit_unit_value": per(
            "Memo: gross value if the flat CAD 54 of the fit scenarios were used per converted case", "CAD_cents",
            "assumed", "flat_gross", fmt_cad,
            note="For comparison only. The preference lever uses the lower value derived from its measured median."),
        "gross_value_specific_reasons_only": per(
            "Floor: gross value if only the lines with a specific reason count", "CAD_cents", "assumed", "floor_gross",
            fmt_cad, note="Same conversion, availability and unit value, baseline without the combined label."),
    }
    return {
        "tag": "assumed",
        "framing": "What-if on a measured baseline: some returns driven by taste (colour, style, change of mind) "
                   "could end as an in-store or mail exchange for another colour or style in the same price band "
                   "instead of a refund. Baseline lines and the median refund are measured; availability, conversion "
                   "and the unit economics are assumptions to validate in a pilot. Not a forecast.",
        "additivity": "Additive by construction: each return line has exactly one reason group, so fit and "
                      "preference lines never overlap and the two levers can be added. The combined rows add the gross "
                      "values and subtract the agent cost once. The pre-purchase prevention what-if is a different "
                      "lever and is not part of this total.",
        "columns": names,
        "inputs": inputs,
        "outputs": outputs,
        "formula": PREFERENCE_FORMULA,
    }


def build_bundle(returns: dict, total_lines: int, cost: dict) -> dict:
    """Multi-line (bundle) return metrics from the returns file and the agent-run sensitivity."""
    lines_per_return = returns["lines_per_return"]
    n_returns = len(lines_per_return)
    multi = [n for n in lines_per_return.values() if n >= 2]
    n_multi, lines_in_multi = len(multi), sum(multi)
    mean_lines = Decimal(total_lines) / n_returns if n_returns else Decimal(0)
    distinct = returns["multi_line_returns_with_distinct_skus"]
    share_returns, share_lines = pct_of(n_multi, n_returns), pct_of(lines_in_multi, total_lines)
    share_distinct = pct_of(distinct, n_multi)
    metrics = {
        "return_numbers": metric("Distinct return numbers in scope", n_returns, "returns", "measured",
                                 fmt_int(n_returns)),
        "return_lines": metric("Return lines in scope", total_lines, "lines", "measured", fmt_int(total_lines)),
        "lines_per_return_mean": metric(
            "Lines per return number, mean", jnum(mean_lines), "lines_per_return", "measured", fmt_dec(mean_lines),
            note="Return lines divided by distinct return numbers."),
        "lines_per_return_max": metric("Lines per return number, maximum", max(lines_per_return.values(), default=0),
                                       "lines_per_return", "measured", fmt_int(max(lines_per_return.values(), default=0))),
        "returns_with_2plus_lines": metric("Return numbers with two or more lines", n_multi, "returns", "measured",
                                           fmt_int(n_multi)),
        "share_of_returns_with_2plus_lines": metric(
            "Share of return numbers with two or more lines", jnum(share_returns), "percent", "measured",
            f"{fmt_dec(share_returns)}%"),
        "lines_in_multi_line_returns": metric("Lines that sit in a multi-line return", lines_in_multi, "lines",
                                              "measured", fmt_int(lines_in_multi)),
        "share_of_lines_in_multi_line_returns": metric(
            "Share of lines that sit in a multi-line return", jnum(share_lines), "percent", "measured",
            f"{fmt_dec(share_lines)}%"),
        "multi_line_returns_with_distinct_skus_share": metric(
            "Multi-line returns that contain two or more distinct SKUs", jnum(share_distinct), "percent", "measured",
            f"{fmt_dec(share_distinct)}%",
            note="Checks that extra lines are different items, not repeats of the same SKU (the source has "
                 "duplicate order and SKU groups)."),
    }

    names = ["per_return_number", "per_line"]
    per_run = cost["cost_per_run_cad"]
    tag_cost = cost_tag_of(cost)
    runs = {"per_return_number": n_returns, "per_line": total_lines}
    totals = {n: to_cents(Decimal(runs[n]) * per_run) for n in names}
    calc = {n: {"runs": runs[n], "total": totals[n], "saving": totals["per_line"] - totals[n]} for n in names}
    per, const = scenario_builders(calc, names)
    sensitivity = {
        "columns": names,
        "inputs": {
            "agent_runs_per_year": per(
                "Agent runs per year", "runs", "derived", "runs", fmt_int,
                note="One run per return number (bundled) versus one run per return line (the basis of the cost "
                     "model)."),
            "agent_cost_per_run": const(
                "Agent cost per run", "CAD_per_run", tag_cost, jnum(per_run, 4), fmt_rate(per_run),
                note="Assumes a multi-line run costs the same as the measured single-order runs. Not measured."),
        },
        "outputs": {
            "annual_agent_cost": per("Annual agent cost", "CAD_cents", tag_cost, "total", fmt_cad),
            "saving_versus_per_line": per("Annual agent cost saved versus one run per line", "CAD_cents", tag_cost,
                                          "saving", fmt_cad),
        },
    }
    return {
        "tag": "measured",
        "metrics": metrics,
        "agent_runs_sensitivity": sensitivity,
        "why_bundling_matters": "A customer who returns several items is one request, so one conversation, one quote, "
                                "one shipment and one confirmation should cover all lines instead of one flow per line.",
    }


def build_perk(inputs: dict) -> dict:
    """Platinum free-return-shipping what-if: inputs and cost bounds only, no revenue or retention claim."""
    fee_cents = inputs["fee_cents"]
    uses_min, uses_max = PERK_USES_MIN_PER_YEAR, inputs["per_month"] * 12
    retained_cents = to_cents(RETAINED_CASE_VALUE_CAD)
    names = list(PLATINUM_SHARE_PCT)
    member_min, member_max = fee_cents * uses_min, fee_cents * uses_max
    calc = {}
    for name in names:
        pct = PLATINUM_SHARE_PCT[name]
        members = CUSTOMERS_BASIS * pct // 100
        calc[name] = {
            "pct": pct, "members": members,
            "cost_min_10k": members * member_min, "cost_max_10k": members * member_max,
            "be_min_10k": -(-members * member_min // retained_cents), "be_max_10k": -(-members * member_max // retained_cents),
        }
    per, const = scenario_builders(calc, names)
    source = "; ".join(inputs["sources"]) if inputs["sources"] else "documented fallback (fixture not readable)"
    outputs = {
        "perk_cost_per_member_min": const(
            "Perk cost per member per year, minimum uses", "CAD_cents", "assumed", member_min, fmt_cad(member_min),
            note=f"{uses_min} use x fee per use."),
        "perk_cost_per_member_max": const(
            "Maximum perk cost per member per year", "CAD_cents", "assumed", member_max, fmt_cad(member_max),
            note=f"{uses_max} uses x fee per use."),
        "perk_cost_per_10k_customers_min": per(
            f"Perk cost per {fmt_int(CUSTOMERS_BASIS)} customers per year, minimum uses", "CAD_cents", "assumed",
            "cost_min_10k", fmt_cad),
        "perk_cost_per_10k_customers_max": per(
            f"Maximum perk cost per {fmt_int(CUSTOMERS_BASIS)} customers per year", "CAD_cents", "assumed",
            "cost_max_10k", fmt_cad),
        "break_even_orders_per_member_min": const(
            "Break-even extra retained orders per member per year, minimum uses", "orders", "assumed",
            jnum(Decimal(member_min) / retained_cents), fmt_dec(Decimal(member_min) / retained_cents)),
        "break_even_orders_per_member_max": const(
            "Break-even extra retained orders per member per year, maximum uses", "orders", "assumed",
            jnum(Decimal(member_max) / retained_cents), fmt_dec(Decimal(member_max) / retained_cents)),
        "break_even_orders_per_10k_customers_min": per(
            f"Break-even extra retained orders per {fmt_int(CUSTOMERS_BASIS)} customers per year, minimum uses",
            "orders", "assumed", "be_min_10k", fmt_int, note="Rounded up to whole orders."),
        "break_even_orders_per_10k_customers_max": per(
            f"Break-even extra retained orders per {fmt_int(CUSTOMERS_BASIS)} customers per year, maximum uses",
            "orders", "assumed", "be_max_10k", fmt_int, note="Rounded up to whole orders."),
    }
    return {
        "tag": "assumed",
        "framing": "Inputs and cost bounds only. No revenue, retention or loyalty uplift is claimed: the break-even only "
                   "states how many extra retained orders, valued at the model's retained-case value, would pay for "
                   "the perk.",
        "columns": names,
        "inputs": {
            "shipping_fee_per_use": const(
                "Return shipping fee waived per use", "CAD_cents", "assumed", fee_cents, fmt_cad(fee_cents),
                note=f"Source: {source}."),
            "uses_per_member_per_year_min": const("Uses per member per year, minimum", "uses", "assumed", uses_min,
                                                  fmt_int(uses_min)),
            "uses_per_member_per_year_max": const(
                "Uses per member per year, maximum", "uses", "assumed", uses_max, fmt_int(uses_max),
                note=f"{inputs['per_month']} free return shipping per calendar month x 12 months."),
            "platinum_share_pct": per("Platinum members per 100 customers", "percent", "assumed", "pct",
                                      lambda v: f"{v}%", note="Assumption."),
            "platinum_members_per_10k_customers": per(
                f"Platinum members per {fmt_int(CUSTOMERS_BASIS)} customers", "members", "assumed", "members",
                fmt_int),
            "value_per_retained_order": const(
                "Value per retained order (average retained-case value of the model)", "CAD_cents", "assumed",
                retained_cents, fmt_cad(retained_cents),
                note="Reused from the prevention what-if (handoff section 15 synthetic unit economics)."),
        },
        "outputs": outputs,
        "formula": "max_perk_cost_per_member = fee_per_use x max_uses_per_year\n"
                   "perk_cost_per_10k_customers = 10,000 x platinum_share x perk_cost_per_member\n"
                   "break_even_extra_retained_orders = perk_cost / value_per_retained_order",
        "input_sources": inputs["sources"],
    }


def agent_load_items(daily_mean, daily_p95, daily_max, n_returns, total_lines, profile) -> dict:
    """Agent-load rows for the scale block: run volume always, tokens and concurrency when the eval profile exists."""
    by_return = pct_of(total_lines - n_returns, total_lines)
    basis = ("One agent run per return line, the same basis as the agent cost model. Counting runs per return number "
             f"instead would lower the volume by about {fmt_dec(by_return, 0)}%.")
    items = {
        "agent_runs_per_day_mean": metric("Agent runs per day, mean", jnum(daily_mean), "runs_per_day", "derived",
                                          fmt_dec(daily_mean), note=basis),
        "agent_runs_per_day_p95": metric("Agent runs per day, p95", jnum(daily_p95), "runs_per_day", "derived",
                                         fmt_dec(daily_p95)),
        "agent_runs_per_day_max": metric("Agent runs per day, busiest day", daily_max, "runs_per_day", "derived",
                                         fmt_int(daily_max)),
        "agent_active_window_hours": metric(
            "Active window of the busiest day", ACTIVE_WINDOW_HOURS, "hours", "assumed", f"{ACTIVE_WINDOW_HOURS} h",
            note="Assumption: the day's returns arrive over 12 hours."),
        "agent_busiest_hour_share_pct": metric(
            "Share of the busiest day that falls in its busiest hour", BUSIEST_HOUR_SHARE_PCT, "percent", "assumed",
            f"{BUSIEST_HOUR_SHARE_PCT}%", note="Assumption (an even 12-hour spread would be 8.3%)."),
    }
    hour_runs = Decimal(daily_max) * BUSIEST_HOUR_SHARE_PCT / 100
    items["agent_runs_busiest_hour"] = metric(
        "Agent runs in the busiest hour of the busiest day", jnum(hour_runs), "runs", "assumed", fmt_dec(hour_runs),
        note="Busiest-day runs x busiest-hour share.")
    if profile is None:
        return items
    duration = profile["duration_s_mean"]
    tokens_in, tokens_out = profile["input_tokens_mean"], profile["output_tokens_mean"]
    per_run = tokens_in + tokens_out
    quota = "Compare with the account's on-demand quota for the model: check account quotas, request an increase before launch."
    n_scn = len(profile["scenario_ids"])
    items["agent_run_duration_s"] = metric(
        "Agent run duration (wall clock), mean of the scenario p50 latencies", jnum(duration), "seconds", "measured",
        f"{fmt_dec(duration, 1)} s",
        note=f"Baseline eval, {n_scn} scenarios, {profile['runs']} runs; scenario p50 ranges from "
             f"{fmt_dec(profile['duration_s_min'], 1)} s to {fmt_dec(profile['duration_s_max'], 1)} s. Reset and "
             "database reads are excluded.")
    items["agent_input_tokens_per_run"] = metric(
        "Input tokens per run, mean", jint(tokens_in), "tokens", "measured", fmt_int(jint(tokens_in)),
        note="Mean over the baseline eval scenarios; includes the prompt and tool specs resent on every model round.")
    items["agent_output_tokens_per_run"] = metric(
        "Output tokens per run, mean", jint(tokens_out), "tokens", "measured", fmt_int(jint(tokens_out)))
    for key, label, runs in (("mean", "mean day", daily_mean), ("p95", "p95 day", daily_p95),
                             ("max", "busiest day", Decimal(daily_max))):
        tokens = runs * per_run
        items[f"agent_tokens_per_day_{key}"] = metric(
            f"Implied tokens per day, {label}", jint(tokens), "tokens_per_day", "derived", fmt_int(jint(tokens)),
            note="Runs per day x (input + output tokens per run)." if key == "mean" else None)
    day_tokens = Decimal(daily_max) * per_run
    window_minutes = ACTIVE_WINDOW_HOURS * 60
    items["agent_tokens_per_minute_busiest_day"] = metric(
        f"Tokens per minute on the busiest day over a {ACTIVE_WINDOW_HOURS}-hour window", jint(day_tokens / window_minutes),
        "tokens_per_minute", "assumed", fmt_int(jint(day_tokens / window_minutes)),
        note=f"Average over the window; about {fmt_dec(pct_of(tokens_in, per_run), 0)}% of it is input. " + quota)
    hour_tokens_per_minute = hour_runs * per_run / 60
    items["agent_tokens_per_minute_busiest_hour"] = metric(
        "Tokens per minute in the busiest hour of the busiest day", jint(hour_tokens_per_minute), "tokens_per_minute",
        "assumed", fmt_int(jint(hour_tokens_per_minute)), note="Busiest-hour runs x tokens per run / 60. " + quota)
    concurrent = hour_runs * duration / 3600
    items["agent_concurrent_runs_busiest_hour"] = metric(
        "Concurrent agent runs in the busiest hour, average", jnum(concurrent), "concurrent_runs", "assumed",
        fmt_dec(concurrent),
        note="Little's law: runs per second x mean run duration. An average; bursts are higher. Not a load test.")
    return items


def cost_case_table(cases: dict, tag: str, measured, scenarios: dict, total_requests: int) -> dict:
    """Annual agent cost and fit-scenario net value for alternative cost-per-run cases (columns = cases)."""
    names = list(cases)
    gross = scenarios["outputs"]["gross_value"]["values"]
    calc = {}
    for case, per_run in cases.items():
        total = to_cents(Decimal(total_requests) * per_run)
        calc[case] = {"per_run": per_run, "multiple": per_run / measured, "total": total,
                      **{f"net_{s}": gross[s] - total for s in scenarios["columns"]}}
    per, _ = scenario_builders(calc, names)
    outputs = {
        "annual_agent_cost": per("Annual agent cost (total requests x cost per run)", "CAD_cents", tag, "total", fmt_cad),
    }
    for s in scenarios["columns"]:
        outputs[f"annual_net_value_{s}"] = per(
            f"Annual net value, fit scenarios, {s} case", "CAD_cents", tag, f"net_{s}", fmt_cad)
    return {
        "columns": names,
        "inputs": {
            "agent_cost_per_run": per("Agent cost per run", "CAD_per_run", tag, "per_run", fmt_rate,
                                      as_json=lambda v: jnum(v, 4)),
            "multiple_of_measured_mean": per("Cost per run as a multiple of the measured mean", "ratio", "derived",
                                             "multiple", lambda v: f"{fmt_dec(v)}x", as_json=lambda v: jnum(v)),
        },
        "outputs": outputs,
    }


def build_agent_cost_sensitivity(scenarios: dict, cost: dict, profile, total_requests: int) -> dict:
    """How the fit-scenario net value moves with the agent cost per run. Built only for a measured cost.

    Keeps the net values the model showed with the earlier CAD 0.50 placeholder visible (they are the stress case)."""
    measured = cost["cost_per_run_cad"]
    cases = {"measured_mean": measured}
    labels = {"measured_mean": f"Measured mean of {runs_label(cost)}"}
    if profile is not None:
        top = profile["cost_usd_max"]
        cases["highest_measured_scenario"] = (top["cost_usd"] * cost["usd_to_cad"]).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP)
        labels["highest_measured_scenario"] = f"Most expensive measured scenario ({top['id']})"
    block = cost_case_table(cases, "derived", measured, scenarios, total_requests)
    block["tag"] = "derived"
    block["column_labels"] = labels
    block["previous_placeholder"] = cost_case_table(
        {"previous_placeholder": PLACEHOLDER_AGENT_COST_PER_RUN_CAD}, "assumed", measured, scenarios, total_requests)
    block["previous_placeholder"]["tag"] = "assumed"
    block["previous_placeholder"]["note"] = (
        f"Placeholder of CAD {PLACEHOLDER_AGENT_COST_PER_RUN_CAD} per run, used before the live eval and about "
        f"{fmt_dec(PLACEHOLDER_AGENT_COST_PER_RUN_CAD / measured, 1)}x the measured mean. Kept as a stress test, and so "
        "that the earlier base-case annual net value stays visible.")
    block["note"] = ("Cost per run is the input that moves. The measured mean mixes full exchange flows with refusals "
                     "and is not the production mix; the most expensive measured scenario is a conservative case.")
    return block


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
        (f"Agent cost per run {fmt_rate(per_run)}, measured on {runs_label(cost)} at assumed model prices "
         "(prices in returns_domain.config)."
         if cost_measured else
         f"Agent cost per run {fmt_rate(per_run)} is a placeholder (pending_measurement) until the live eval."),
        "Fit lines are return lines, not customers.",
    ]
    return {"currency_note": CURRENCY_NOTE, "headline_numbers": headline, "key_assumptions": assumptions}


def fmt_cad_round(cents: int, step: int = 1000) -> str:
    """Whole thousands for the slide sentences; the tables keep the exact cents."""
    value = int((Decimal(cents) / 100 / step).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * step
    return f"CAD {value:,}"


def or_list(items: list) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def append_v2_summary(summary: dict, reason_groups: dict, preference: dict, bundle: dict, cost: dict, profile) -> None:
    """Append (never remove or reorder) the v2 headline numbers and assumptions to the slide summary."""
    out, comp = preference["outputs"], reason_groups["preference_composition"]
    pref_lines = reason_groups["groups"]["preference"]["lines"]["value"]
    specific = comp["specific_reason_lines"]["value"]
    gross, conv = out["gross_value"], preference["inputs"]["exchange_conversion_pct"]["display"]
    unit = preference["inputs"]["value_per_converted_case"]["display"]["base"]
    bm = bundle["metrics"]
    headline = summary["headline_numbers"]
    headline.append({
        "key": "preference_exchange_value_base",
        "label": "Preference-return exchange what-if, added to the fit scenarios (base case, gross)",
        "value": gross["values"]["base"], "unit": "CAD_cents", "tag": "assumed", "display": gross["display"]["base"],
        "basis": (f"{fmt_int(pref_lines)} preference lines x {ALT_COLOUR_AVAILABILITY_PCT}% availability of the "
                  f"alternative colour x {conv['base']} conversion (both assumed) x {unit} per case, derived from the "
                  "measured median refund, plus the avoided second-return value as in the fit scenarios. "
                  f"Range {gross['display']['low']} ({conv['low']}) to "
                  f"{gross['display']['high']} ({conv['high']}); floor "
                  f"{out['gross_value_specific_reasons_only']['display']['base']} if only the {fmt_int(specific)} lines "
                  "with a specific reason count. Fit and preference lines never overlap: both levers together give "
                  f"{out['combined_annual_net_value']['display']['base']} net in the base case, agent cost counted once.")})
    headline.append({
        "key": "multi_line_return_share", "label": "Return numbers with two or more lines",
        "value": bm["share_of_returns_with_2plus_lines"]["value"], "unit": "percent", "tag": "measured",
        "display": bm["share_of_returns_with_2plus_lines"]["display"],
        "basis": (f"{bm['returns_with_2plus_lines']['display']} of {bm['return_numbers']['display']} return numbers; "
                  f"they hold {bm['lines_in_multi_line_returns']['display']} of {bm['return_lines']['display']} lines "
                  f"({bm['share_of_lines_in_multi_line_returns']['display']}), up to "
                  f"{bm['lines_per_return_max']['display']} lines in one return. "
                  f"{bm['multi_line_returns_with_distinct_skus_share']['display']} of them contain two or more "
                  "distinct SKUs.")})
    if cost["status"] == "measured":
        basis = f"Mean of {runs_label(cost)} of the ALDO returns-agent scenarios at assumed model prices (prices in returns_domain.config)"
        if cost["usd_to_cad_used"]:
            basis += (f": USD {fmt_dec(cost['raw_value'], 4)} per run, converted at {cost['usd_to_cad']} CAD per USD "
                      "(assumed, from returns_domain.config)")
        if profile is not None:
            lo, hi = profile["cost_usd_min"], profile["cost_usd_max"]
            basis += (f". Single scenarios range from {fmt_rate(lo['cost_usd'] * cost['usd_to_cad'])} ({lo['id']}) to "
                      f"{fmt_rate(hi['cost_usd'] * cost['usd_to_cad'])} ({hi['id']})")
        headline.append({
            "key": "agent_cost_per_run_measured", "label": "Agent cost per run (measured)",
            "value": jnum(cost["cost_per_run_cad"], 4), "unit": "CAD_per_run", "tag": "derived",
            "display": fmt_rate(cost["cost_per_run_cad"]), "basis": basis + "."})
    summary["key_assumptions"].append(
        "Preference-return exchange: conversion " + "/".join(str(v) for v in PREFERENCE_CONVERSION_PCT.values())
        + f"% of preference lines where the alternative colour or style is in stock ({ALT_COLOUR_AVAILABILITY_PCT}% "
        "availability), repeat-return reduction as in the fit scenarios, value per case derived from the measured "
        "median refund and the synthetic unit economics (all assumed, to validate in a pilot).")
    summary["key_assumptions"].append(
        "Fit and preference lines never overlap (one reason group per line), so the two levers can be added; the "
        "pre-purchase prevention what-if stays separate.")
    summary["key_assumptions"].append(
        f"Agent load: a {ACTIVE_WINDOW_HOURS}-hour active window and {BUSIEST_HOUR_SHARE_PCT}% of the busiest day in its "
        "busiest hour (assumed).")


def build_slide_sentences(total_lines, fit_lines, fit_share, scenarios, reason_groups, preference, bundle, cost,
                          scale, profile) -> list:
    """Four to six plain sentences, each with its tag, for the Business Impact and Scalability slides."""
    names = scenarios["columns"]
    net = scenarios["outputs"]["annual_net_value"]
    measured = cost["status"] == "measured"
    pref = reason_groups["groups"]["preference"]
    comp = reason_groups["preference_composition"]
    out = preference["outputs"]
    bm = bundle["metrics"]
    sens = bundle["agent_runs_sensitivity"]["outputs"]["annual_agent_cost"]["values"]
    cost_tag = cost_tag_of(cost)
    # Shares are recomputed from the integer counts so a sentence never rounds an already rounded percentage.
    pref_lines = pref["lines"]["value"]
    combined, combined_store = comp["combined_label_lines"]["value"], comp["combined_label_lines_in_store"]["value"]
    store_lines = comp["store_lines"]["value"]
    n_returns, n_multi = bm["return_numbers"]["value"], bm["returns_with_2plus_lines"]["value"]
    share_returns = pct_of(n_multi, n_returns)
    share_lines = pct_of(bm["lines_in_multi_line_returns"]["value"], total_lines)
    cost_phrase = (f"a measured {fmt_rate(cost['cost_per_run_cad'])} per run ({runs_label(cost)} at assumed model prices)"
                   if measured else f"a placeholder {fmt_rate(cost['cost_per_run_cad'])} per run")

    sentences = [
        ("Business impact", "measured",
         f"Of {fmt_int(total_lines)} completed ALDO_CA return lines between {WINDOW_START.isoformat()} and "
         f"{WINDOW_END.isoformat()}, {fmt_int(fit_lines)} ({fmt_dec(fit_share, 1)}%) gave a size reason, Too small or "
         "Too big (measured)."),
        ("Business impact", net["tag"],
         f"If {or_list([str(SCENARIOS[n]['conversion_pct']) + '%' for n in names])} of those size-related lines became "
         f"exchanges, annual net value would be about {or_list([fmt_cad_round(net['values'][n]) for n in names])} after "
         f"agent cost ({net['tag']}: conversion rates assumed, "
         + ("agent cost measured)." if measured else "agent cost a placeholder, pending measurement).")),
        ("Business impact", "measured",
         f"Another {fmt_int(pref_lines)} lines ({fmt_dec(pct_of(pref_lines, total_lines), 1)}%) carry a "
         "change-of-mind or not-as-expected reason, but "
         f"{fmt_dec(pct_of(combined, pref_lines), 0)}% of them use the combined label 'Changed "
         f"my mind / Other', which sits on {fmt_dec(pct_of(combined_store, store_lines), 1)}% of "
         f"in-store lines against {fmt_dec(pct_of(combined - combined_store, total_lines - store_lines), 1)}% of other "
         "lines, so this group is an upper bound (measured)."),
        ("Business impact", "assumed",
         f"Converting {or_list([str(PREFERENCE_CONVERSION_PCT[n]) + '%' for n in names])} of them to a colour or style "
         f"exchange where the alternative is in stock ({ALT_COLOUR_AVAILABILITY_PCT}% assumed) would add about "
         f"{or_list([fmt_cad_round(out['gross_value']['values'][n]) for n in names])} a year on top of the size-related "
         "exchanges (gross; the agent already runs on every line), or about "
         f"{fmt_cad_round(out['gross_value_specific_reasons_only']['values']['base'])} in the base case if only the "
         f"{fmt_int(comp['specific_reason_lines']['value'])} lines with a specific reason count (assumed)."),
        ("Scalability", cost_tag,
         f"Because {fmt_dec(share_returns, 1)}% of return numbers hold two or more "
         f"lines ({fmt_dec(share_lines, 1)}% of lines), one agent run per return "
         f"number instead of per line lowers the yearly runs from {fmt_int(total_lines)} to "
         f"{fmt_int(n_returns)}; at {cost_phrase} the annual agent cost falls from about "
         f"{fmt_cad_round(sens['per_line'])} to about {fmt_cad_round(sens['per_return_number'])} ({cost_tag}: counts "
         "measured)."),
    ]
    if profile is not None:
        tokens_day = scale["agent_tokens_per_day_max"]["value"]
        tokens_min = scale["agent_tokens_per_minute_busiest_day"]["value"]
        sentences.append((
            "Scalability", "assumed",
            f"On the busiest day ({fmt_int(scale['lines_per_day_max']['value'])} lines) the agent would process about "
            f"{fmt_dec(Decimal(tokens_day) / 1_000_000, 1)} million tokens, roughly "
            f"{fmt_int(int(quantized(Decimal(tokens_min) / 1000, 0)) * 1000)} tokens per minute over a "
            f"{ACTIVE_WINDOW_HOURS}-hour window, with about "
            f"{fmt_dec(Decimal(scale['lines_per_day_max']['value']) * BUSIEST_HOUR_SHARE_PCT / 100 * profile['duration_s_mean'] / 3600, 1)}"
            f" runs in flight on average in the busiest hour ({BUSIEST_HOUR_SHARE_PCT}% of the day, assumed); check "
            "account quotas, request an increase before launch (assumed)."))
    else:
        sentences.append((
            "Scalability", "assumed",
            f"On the busiest day the agent would handle {fmt_int(scale['agent_runs_per_day_max']['value'])} runs, about "
            f"{fmt_dec(scale['agent_runs_busiest_hour']['value'], 0)} of them in the busiest hour "
            f"({BUSIEST_HOUR_SHARE_PCT}% of the day, assumed); token volume stays pending until the live eval "
            "(assumed)."))
    return [{"slide": slide, "tag": tag, "text": text} for slide, tag, text in sentences]



# ---- Margin lever (transaction file profile; additive) --------------------------------------------
MARGIN_FORMULA = (
    "margin_based_value = existing_retained_sale_value x gross_margin_rate_footwear_ALDO_CA\n"
    "memo_retained_margin = cases x measured_margin_per_fit_returned_unit\n"
    "break_even_exchanges_per_year = total_requests x agent_cost_per_run / margin_per_retained_exchange\n"
    "perk_break_even_exchanges_per_use = perk_fee_per_use / margin_per_retained_exchange"
)


def dec(value) -> Decimal:
    return Decimal(str(value))


def load_margin_inputs(path: Path):
    """Read the measured margin facts from the transaction profile; (inputs, None) or (None, reason)."""
    if not path.is_file():
        return None, f"{rel(path)} not found; run scripts/profile_transactions.py"
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
        scope = profile["scopes"]["ALDO_CA"]
        groups = scope["footwear_price_group"]["all_footwear"]
        fit = scope["fit_return_margin"]["all_footwear"]
        retail = sum(dec(g["sales_retail"]) for g in groups.values())
        margin = sum(dec(g["sales_gross_margin"]) for g in groups.values())
        regular, markdown = groups["Regular"], groups["Markdown"]
        inputs = {
            "rate_all": margin / retail,
            "rate_regular": dec(regular["sales_gross_margin"]) / dec(regular["sales_retail"]),
            "sales_units": sum(g["sales_units"] for g in groups.values()),
            "sales_units_regular": regular["sales_units"],
            "margin_per_unit_regular": dec(regular["avg_margin_per_unit_sold"]),
            "margin_per_unit_markdown": dec(markdown["avg_margin_per_unit_sold"]),
            "fit_return_lines": fit["fit_return_lines"],
            "fit_return_units": fit["fit_return_units"],
            "fit_margin_per_unit": dec(fit["avg_margin_per_returned_unit"]),
            "fit_retail_per_unit": dec(fit["avg_retail_per_returned_unit"]),
            "fit_margin_per_unit_regular": dec(fit["by_price_group"]["Regular"]["avg_margin_per_returned_unit"]),
            "fit_markdown_lines": fit["by_price_group"]["Markdown"]["lines"],
            "date_min": scope["business_date_min"],
            "date_max": scope["business_date_max"],
            "rows_read": profile["rows_read"],
            "partial_run": bool(profile.get("partial_run")),
            "sha256": sha256_of(path),
            "file": rel(path),
        }
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        return None, f"{rel(path)} could not be used ({type(exc).__name__}); re-run scripts/profile_transactions.py"
    if inputs["partial_run"]:
        return None, f"{rel(path)} is a partial run (--max-rows); re-run scripts/profile_transactions.py on the full file"
    return inputs, None


def build_margin(path: Path, scenarios: dict, preference: dict, prevention: dict, cost: dict, total_requests: int,
                 fit_lines: int, perk_fee_cents: int) -> dict:
    """Margin version of the retained-sale value: revenue-based (existing, untouched) next to margin-based."""
    m, reason = load_margin_inputs(path)
    if m is None:
        return {"status": "not_available", "reason": reason,
                "note": "The margin lever needs docs/evidence/transaction-profile.json (scripts/profile_transactions.py "
                        "on the 3 GB transaction file). All other blocks of this model are unaffected."}
    rate, per_run = m["rate_all"], cost["cost_per_run_cad"]
    margin_unit = m["fit_margin_per_unit"]
    cost_tag = cost_tag_of(cost)
    names = list(scenarios["columns"])
    basis = (f"ALDO_CA footwear (men's and ladies' categories), sales lines, all price groups, fiscal 2025 "
             f"(business dates {m['date_min']} to {m['date_max']}; the file is named 2025), "
             f"{fmt_int(m['sales_units'])} units sold")
    pct = lambda d: f"{fmt_dec(d * 100)}%"  # noqa: E731

    measured = {
        "gross_margin_rate_footwear": metric(
            "ALDO_CA footwear gross margin rate, all price groups", jnum(rate * 100), "percent", "measured", pct(rate),
            note=f"Gross margin / retail on sales lines. Basis: {basis}. Returns lines are excluded from the rate."),
        "gross_margin_rate_footwear_regular_price": metric(
            "ALDO_CA footwear gross margin rate, Regular price group", jnum(m["rate_regular"] * 100), "percent",
            "measured", pct(m["rate_regular"]),
            note=f"Same basis, price_group Regular only ({fmt_int(m['sales_units_regular'])} units)."),
        "avg_margin_per_unit_regular": metric(
            "Average gross margin per unit sold, Regular price", to_cents(m["margin_per_unit_regular"]), "CAD_cents",
            "measured", fmt_cad(to_cents(m["margin_per_unit_regular"])), note="Currency assumed CAD, unconfirmed."),
        "avg_margin_per_unit_markdown": metric(
            "Average gross margin per unit sold, Markdown price group", to_cents(m["margin_per_unit_markdown"]),
            "CAD_cents", "measured", fmt_cad(to_cents(m["margin_per_unit_markdown"]))),
        "avg_margin_per_fit_returned_unit": metric(
            "Average gross margin per returned unit, fit returns (Too small, Too big)", to_cents(margin_unit),
            "CAD_cents", "measured", fmt_cad(to_cents(margin_unit)),
            note=f"{fmt_int(m['fit_return_lines'])} fit return lines ({fmt_int(m['fit_return_units'])} units) with a "
                 "text reason in the transaction file (web channel). Margin the sale carried, lost on a refund and "
                 "retained by an exchange."),
        "avg_margin_per_fit_returned_unit_regular": metric(
            "Same, units returned from Regular price sales", to_cents(m["fit_margin_per_unit_regular"]), "CAD_cents",
            "measured", fmt_cad(to_cents(m["fit_margin_per_unit_regular"]))),
        "markdown_share_of_fit_return_lines": metric(
            "Share of fit return lines that were Markdown price", jnum(Decimal(m["fit_markdown_lines"]) * 100
                                                                        / m["fit_return_lines"]), "percent", "measured",
            f"{fmt_dec(Decimal(m['fit_markdown_lines']) * 100 / m['fit_return_lines'])}%"),
    }

    case_revenue = RETAINED_CASE_VALUE_CAD
    case_margin = case_revenue * rate
    per_case = {
        "revenue_based_value_per_retained_case": metric(
            "Existing value per retained case (unchanged)", to_cents(case_revenue), "CAD_cents", "assumed",
            fmt_cad(to_cents(case_revenue)), note="Handoff section 15 synthetic unit economics, as in the scenarios."),
        "margin_based_value_per_retained_case": metric(
            "Existing value per retained case x measured margin rate", to_cents(case_margin), "CAD_cents", "derived",
            fmt_cad(to_cents(case_margin)),
            note="Literal product of the existing value and the measured rate. The CAD 54 is already net of an assumed "
                 "item cost (40% of price), so applying a margin rate on top counts cost twice: read this as the "
                 "conservative end."),
        "measured_margin_per_fit_returned_unit": measured["avg_margin_per_fit_returned_unit"],
    }

    def rows(label_key, existing_values, cases_values, tag):
        out = {}
        for n in names:
            revenue = existing_values[n]
            out[n] = {"rev": revenue, "mar": to_cents(Decimal(revenue) / 100 * rate),
                      "direct": to_cents(dec(cases_values[n]) * margin_unit)}
        return out

    levers = {
        "fit_exchange": ("Fit exchange lever", scenarios["outputs"]["exchange_value"]["values"],
                         scenarios["outputs"]["converted_cases"]["values"], "derived"),
        "preference_exchange": ("Preference exchange lever", preference["outputs"]["exchange_value"]["values"],
                                preference["outputs"]["converted_cases"]["values"], "assumed"),
        "prevention": ("Pre-purchase prevention", prevention["outputs"]["retained_sale_value"]["values"],
                       prevention["outputs"]["prevented_lines"]["values"], "assumed"),
    }
    calc = {n: {} for n in names}
    outputs = {}
    for key, (label, existing, cases, tag) in levers.items():
        r = rows(key, existing, cases, tag)
        for n in names:
            calc[n].update({f"{key}_rev": r[n]["rev"], f"{key}_mar": r[n]["mar"], f"{key}_direct": r[n]["direct"]})
    per, const = scenario_builders(calc, names)
    for key, (label, existing, cases, tag) in levers.items():
        outputs[f"{key}_revenue_based"] = per(
            f"{label}: retained-sale value, revenue-based (existing, unchanged)", "CAD_cents", tag, f"{key}_rev", fmt_cad,
            note="Copied from the scenario block above; no number changed.")
        outputs[f"{key}_margin_based"] = per(
            f"{label}: retained-sale value x margin rate (margin-based)", "CAD_cents", tag, f"{key}_mar", fmt_cad,
            note="Existing value x ALDO_CA footwear gross margin rate. Conservative reading, see per-case note.")
        outputs[f"{key}_retained_margin_memo"] = per(
            f"{label}: memo, cases x measured margin per fit-returned unit", "CAD_cents", tag, f"{key}_direct", fmt_cad,
            note="Cases of the lever x CAD " + fmt_dec(margin_unit) + " measured average margin of a fit-returned unit. "
                 "Uses the fit-return unit as a proxy for every lever.")

    agent_total_cents = scenarios["outputs"]["agent_cost_total"]["values"][names[0]]
    be_exchanges = Decimal(agent_total_cents) / 100 / margin_unit
    be_exchanges_whole = int(be_exchanges.to_integral_value(rounding="ROUND_CEILING"))
    perk_per_use = Decimal(perk_fee_cents) / 100
    retained = to_cents(margin_unit)
    break_even = {
        "margin_per_retained_exchange": metric(
            "Margin earned by one retained exchange (average fit-returned unit)", retained, "CAD_cents", "measured",
            fmt_cad(retained), note="Measured average margin of a fit-returned footwear unit, all price groups."),
        "agent_cost_per_run": metric(
            "Agent cost per run", jnum(per_run, 4), "CAD_per_run", cost_tag, fmt_rate(per_run),
            note="Same cost per run as the scenarios."),
        "agent_runs_paid_by_one_exchange": metric(
            "Agent runs paid for by the margin of one retained exchange", jnum(margin_unit / per_run, 0), "runs",
            "derived", fmt_dec(margin_unit / per_run, 0), note="Margin per retained exchange / agent cost per run."),
        "break_even_exchanges_per_year": metric(
            "Retained exchanges per year that pay the whole agent bill (every return line costs one run)",
            be_exchanges_whole, "exchanges", "derived", fmt_int(be_exchanges_whole),
            note=f"Annual agent cost {fmt_cad(agent_total_cents)} / margin per retained exchange, rounded up."),
        "break_even_conversion_of_fit_lines_pct": metric(
            "Same, as share of the measured fit lines", jnum(Decimal(be_exchanges_whole) * 100 / fit_lines), "percent",
            "derived", f"{fmt_dec(Decimal(be_exchanges_whole) * 100 / fit_lines)}%",
            note=f"Against the {fmt_int(fit_lines)} fit lines; the scenarios assume 10 to 30 percent."),
        "perk_fee_per_use": metric(
            "Platinum perk cost per use (return shipping fee waived)", perk_fee_cents, "CAD_cents", "assumed",
            fmt_cad(perk_fee_cents), note="Same fee as the tier perk block."),
        "perk_uses_paid_by_one_exchange": metric(
            "Perk uses paid for by the margin of one retained exchange", jnum(margin_unit / perk_per_use, 1), "uses",
            "derived", fmt_dec(margin_unit / perk_per_use, 1)),
        "perk_break_even_exchanges_per_use": metric(
            "Extra retained exchanges one perk use must cause to pay for itself", jnum(perk_per_use / margin_unit, 3),
            "exchanges", "derived", fmt_dec(perk_per_use / margin_unit, 3),
            note="Perk fee per use / margin per retained exchange. Cost bound only; no uplift is claimed."),
    }

    gap = m["margin_per_unit_regular"] - m["margin_per_unit_markdown"]
    markdown = {
        "tag": "derived",
        "text": (f"ALDO_CA footwear units sold at Regular price earned on average {fmt_cad(to_cents(m['margin_per_unit_regular']))} "
                 f"of gross margin against {fmt_cad(to_cents(m['margin_per_unit_markdown']))} at Markdown, "
                 f"a gap of {fmt_cad(to_cents(gap))} per unit. If a returned unit would only be resold at a markdown, "
                 "an exchange at full price protects about that much more margin. "
                 f"{fmt_dec(Decimal(m['fit_markdown_lines']) * 100 / m['fit_return_lines'], 0)}% of the fit returns were "
                 "themselves Markdown-priced sales, where the margin at stake is lower "
                 f"({fmt_cad(to_cents(dec(m['fit_margin_per_unit_regular'])))} per unit for Regular-price fit returns)."),
        "limits": "A comparison of sales lines by price group in fiscal 2025. The file does not show what price a "
                  "returned unit was later resold at, so the gap is a reference size, not an observed resale loss.",
    }
    return {
        "status": "available",
        "tag": "derived",
        "framing": "Margin version of the retained-sale value, next to the existing revenue-based numbers (unchanged). "
                   "The rate and the margin per unit are measured from the 2025 ALDO transaction file; conversion "
                   "rates and the CAD 54 per case remain the model's assumptions. Currency assumed CAD for ALDO_CA, "
                   "unconfirmed.",
        "source": {"file": m["file"], "sha256": m["sha256"], "rows_read": m["rows_read"], "banner": BANNER,
                   "business_date_min": m["date_min"], "business_date_max": m["date_max"],
                   "producer": "scripts/profile_transactions.py"},
        "measured": measured,
        "per_case": per_case,
        "columns": names,
        "scenarios": outputs,
        "break_even": break_even,
        "markdown": markdown,
        "formula": MARGIN_FORMULA,
        "caveats": [
            "The transaction file is store and web point-of-sale data, not the e-commerce OMS files behind the rest of "
            "the model. Fit text reasons exist only on web-channel return lines; in-store return lines carry numeric "
            "POS reason codes with no legend, so they are not counted as fit. The margin rate describes ALDO_CA "
            "footwear sales as a whole, not only the units that get returned.",
            "The existing CAD 54 per case is net of an assumed 40% item cost, so the margin-based rows (value x rate) "
            "are a conservative reading. The memo rows use the measured margin per fit-returned unit directly.",
            "Margin is gross margin as recorded in the file (retail minus cost); handling, freight and restocking are "
            "not in it. Currency assumed CAD, unconfirmed.",
        ],
    }


def margin_slide_sentences(margin: dict, scenarios: dict) -> list:
    """At most two margin sentences, appended to the slide sentences; none when the profile is not available."""
    if margin["status"] != "available":
        return []
    ms, be, so = margin["measured"], margin["break_even"], margin["scenarios"]["fit_exchange_margin_based"]
    names = scenarios["columns"]
    return [
        {"slide": "Business impact", "tag": "derived",
         "text": f"ALDO_CA footwear earned a {ms['gross_margin_rate_footwear']['display']} gross margin in fiscal 2025 "
                 f"({ms['gross_margin_rate_footwear_regular_price']['display']} on regular-price sales) and a fit-returned "
                 f"unit carries {be['margin_per_retained_exchange']['display']} of margin, so one retained exchange pays "
                 f"for about {be['agent_runs_paid_by_one_exchange']['display']} agent runs at "
                 f"{be['agent_cost_per_run']['display']} per run (derived: margin measured, agent cost "
                 f"{'measured' if be['agent_cost_per_run']['tag'] == 'derived' else 'a placeholder'})."},
        {"slide": "Business impact", "tag": so["tag"],
         "text": f"At the measured margin rate the size-related exchanges retain about "
                 f"{or_list([fmt_cad_round(so['values'][n]) for n in names])} of gross margin a year (a conservative "
                 f"reading), and {be['break_even_exchanges_per_year']['display']} retained exchanges a year "
                 f"({be['break_even_conversion_of_fit_lines_pct']['display']} of size-related lines) would already pay "
                 f"the whole agent bill ({so['tag']}: conversion rates assumed)."},
    ]


def build_model(returns: dict, exchanges: dict, cost: dict, returns_path: Path, exchanges_path: Path,
                profile=None, txn_profile_path: Path = TXN_PROFILE) -> dict:
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
            "unparseable_refund_amounts_in_scope": sum(returns["group_refund_unparseable"].values()),
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
            "mode": cost["mode"], "runs_costed": cost["runs_costed"],
        },
        "taxonomy": {"file": TAXONOMY_SOURCE, "used_for": "mapping raw return reasons to categories (return_reason_groups)"},
    }

    cost_measured = cost["status"] == "measured"
    if cost_measured:
        cost_text = (
            f"Agent cost per run is {fmt_rate(cost['cost_per_run_cad'])}, measured on {runs_label(cost)} at assumed "
            f"model prices (prices in returns_domain.config). Source: `{cost['file']}` (field `{cost['field']}`). "
            "Tokens are measured by the live eval; the token prices are an assumption set in the eval config."
            + (f" The eval reports USD {fmt_dec(cost['raw_value'], 4)} per run, converted at {cost['usd_to_cad']} CAD "
               f"per USD (assumed; one source of truth: {cost['usd_to_cad_source']})." if cost["usd_to_cad_used"] else "")
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

    # ---- impact model v2 (additive) ----
    reason_groups = build_reason_groups(returns, total_lines, fit_lines, refund_sum, refund_median)
    preference = build_preference_lever(reason_groups, scenarios, cost, total_lines)
    bundle = build_bundle(returns, total_lines, cost)
    perk = build_perk(read_perk_inputs())
    sensitivity = build_agent_cost_sensitivity(scenarios, cost, profile, total_lines) if cost_measured else None
    margin = build_margin(txn_profile_path, scenarios, preference, prevention, cost, total_lines, fit_lines,
                          perk["inputs"]["shipping_fee_per_use"]["values"]["base"])
    scale.update(agent_load_items(daily_mean, daily_p95, daily_max, returns["return_ids"], total_lines, profile))
    append_v2_summary(slide_summary, reason_groups, preference, bundle, cost, profile)
    slide_summary["slide_sentences"] = build_slide_sentences(
        total_lines, fit_lines, fit_share, scenarios, reason_groups, preference, bundle, cost, scale, profile)

    slide_summary["slide_sentences"] += margin_slide_sentences(margin, scenarios)

    comp = reason_groups["preference_composition"]
    pref_groups = reason_groups["groups"]["preference"]
    conv_values = list(PREFERENCE_CONVERSION_PCT.values())
    conv_text = ", ".join(str(v) for v in conv_values[:-1]) + f" and {conv_values[-1]} percent"
    gap = reason_groups["capture_gap"]
    if gap:
        caveats.append(
            f"In-store lines fall to {fmt_int(gap['in_store_lines_range'][0])} to {fmt_int(gap['in_store_lines_range'][1])} "
            f"a month in {', '.join(gap['months'])} against {fmt_int(gap['in_store_lines_range_other_months'][0])} to "
            f"{fmt_int(gap['in_store_lines_range_other_months'][1])} in the other months (a break of {gap['break_ratio']}x "
            f"between neighbouring months), while the other channels stay between "
            f"{fmt_int(gap['other_channel_lines_range'][0])} and {fmt_int(gap['other_channel_lines_range'][1])} a month. "
            "The data validation report (caveat C2) shows the returns file misses most in-store returns for those months, "
            "so in-store volumes, and the reason mix that depends on them, are understated. This applies to every "
            "measured volume in this model, including the fit lines.")
    caveats += [
        f"Return reason groups follow the product taxonomy ({TAXONOMY_SOURCE}). 'Changed my mind / Other' mixes change "
        f"of mind with unspecified reasons in one option and makes up {comp['combined_label_share_of_preference']['display']} "
        "of the preference group, so the preference group is an upper bound on taste-based returns. The label sits on "
        f"{comp['combined_label_share_of_store_lines']['display']} of in-store lines against "
        f"{comp['combined_label_share_of_other_channel_lines']['display']} of lines from other channels, so in store it "
        "may be a default entry. The preference lever therefore also shows a floor that counts only the "
        f"{comp['specific_reason_lines']['display']} lines with a specific reason.",
        "Comfort complaints, process reasons (return without merchandise, return extension, gift return) and one French "
        "spelling of 'too big' sit in the group other because they are ambiguous or not a product problem. The fit group "
        "is identical to the earlier definition (Too small and Too big only), and the five groups add up to all lines "
        "in scope.",
        "The preference lever takes its value per converted case from the measured median refund of the preference lines "
        "and the synthetic unit economics of handoff section 15 (item cost 40 percent of price, net extra handling of an "
        "exchange CAD 6). At a CAD 100 order the formula returns the CAD 54 of the fit scenarios, which keep the flat "
        "CAD 54. The median refund stands in for the item price; line versus transaction grain is unconfirmed.",
        f"{pref_groups['share_in_store']['display']} of the preference lines were returned in store, where the customer is "
        "already at the counter, so the lever assumes an exchange is offered there (by an associate or by ALDO bot); this "
        "is not tested. Stores already exchange today: the exchange file has "
        f"{exchange_evidence['confirmed_aldo_ca_stores']['display']} CONFIRMED and "
        f"{exchange_evidence['assumed_aldo_ca_stores']['display']} ASSUMED in-store rows attributed to ALDO_CA in the "
        f"window. The preference conversion rates ({conv_text}) and the {ALT_COLOUR_AVAILABILITY_PCT} percent "
        "availability of the alternative colour are assumptions to validate in a pilot: they are meant as additional to "
        "current in-store exchanges and are not calibrated against them, and no stock data supports the availability.",
        "Fit and preference lines are disjoint by construction (one reason group per line), so the two levers are added "
        "in the combined rows with the agent cost counted once. The pre-purchase prevention what-if stays a separate lever "
        "and is not part of that total.",
        f"Bundle metrics count lines per return number in scope. {bundle['metrics']['multi_line_returns_with_distinct_skus_share']['display']} "
        "of the multi-line returns contain two or more distinct SKUs, so extra lines are normally different items. The "
        "baseline eval has no multi-line scenario, so the agent cost of a bundled run is not measured: one run per return "
        "number assumes it costs the same as a single-order run.",
    ]
    if cost_measured:
        caveats.append(
            f"The measured agent cost is the mean of {runs_label(cost)} priced at assumed token prices and is not the "
            "production mix: single scenarios differ widely, and the agent cost sensitivity shows the most expensive "
            f"measured scenario and the CAD {PLACEHOLDER_AGENT_COST_PER_RUN_CAD} placeholder used before the live eval.")
    caveats += [
        "Agent-load numbers use return lines per day as runs per day, the mean of the scenario p50 wall-clock latencies "
        f"as run duration, an assumed {ACTIVE_WINDOW_HOURS}-hour active window and an assumed {BUSIEST_HOUR_SHARE_PCT} "
        "percent busiest-hour share. Concurrency is an average (Little's law) and bursts are higher. This is not a load "
        "test. Compare with the account's on-demand quotas for the model: check account quotas, request an increase "
        "before launch. No quota numbers are stated.",
        "Prompt caching is not modelled: the baseline eval carries no cache-read token counts, so no saving is claimed.",
        "The tier perk block gives inputs and cost bounds only; no revenue, retention or loyalty uplift is claimed. The "
        "return shipping fee and the monthly allowance come from the demo fixtures; the Platinum share and the uses per "
        "member are assumptions.",
    ]

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
        "return_reason_groups": reason_groups,
        "preference_exchange_lever": preference,
        "bundle_returns": bundle,
        **({"agent_cost_sensitivity": sensitivity} if sensitivity is not None else {}),
        "tier_perk_what_if": perk,
        "caveats": caveats,
        "reconciliation": reconciliation,
        "sources": sources,
    }
    if cost["usd_to_cad_used"]:
        rate_block = metric(
            "USD to CAD rate", float(cost["usd_to_cad"]), "CAD_per_USD", "assumed", f"{cost['usd_to_cad']}",
            note="Used only because the eval reports its cost in USD. One source of truth for the eval, the runtime "
                 "metrics and this model: the USD_TO_CAD default in backend/returns_domain/config.py, parsed as text "
                 f"(fallback {USD_TO_CAD_FALLBACK} with a printed warning if it cannot be found).")
        rate_block["source"] = cost["usd_to_cad_source"]
        if cost["eval_usd_to_cad"] is not None:
            rate_block["eval_file_rate"] = float(cost["eval_usd_to_cad"])
            rate_block["matches_eval_file"] = cost["eval_usd_to_cad"] == cost["usd_to_cad"]
        model["usd_to_cad"] = rate_block
    model["margin"] = margin
    return model


# ---- markdown ------------------------------------------------------------------------------
def md_metric_table(items: dict) -> list:
    lines = ["| Metric | Value | Tag | Note |", "| --- | ---: | --- | --- |"]
    for item in items.values():
        lines.append(f"| {item['label']} | {item['display']} | {item['tag']} | {item.get('note', '')} |")
    return lines


def md_scenario_table(items: dict, names: list, labels: dict = None) -> list:
    labels = labels or {}
    lines = ["| Item | " + " | ".join(labels.get(n, n.capitalize()) for n in names) + " | Tag |",
             "| --- | " + " | ".join("---:" for _ in names) + " | --- |"]
    for item in items.values():
        cells = " | ".join(item["display"][n] for n in names)
        lines.append(f"| {item['label']} | {cells} | {item['tag']} |")
    return lines


def md_slide_sentences(ss: dict) -> list:
    sentences = ss.get("slide_sentences")
    if not sentences:
        return []
    out = ["## Slide-ready sentences", ""]
    for slide in dict.fromkeys(s["slide"] for s in sentences):
        out += [slide, "", *[f"- {s['text']}" for s in sentences if s["slide"] == slide], ""]
    out += ["Figures in these sentences are rounded; the tables below keep the exact values and every number keeps "
            "its tag.", ""]
    return out


def md_v2_sections(m: dict) -> list:
    """Markdown for the impact model v2 blocks (reason groups, preference lever, bundle, cost sensitivity, perk)."""
    rg, pl, bd, pk = m["return_reason_groups"], m["preference_exchange_lever"], m["bundle_returns"], m["tier_perk_what_if"]
    out = [
        "## Return reasons by group (all in-window lines)",
        "",
        rg["framing"],
        "",
        "| Group | Lines | Share of lines | Refund sum | Median refund | Returned in store | Tag |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        *[f"| {b['label']} | {b['lines']['display']} | {b['share_of_lines']['display']} | {b['refund_sum']['display']} "
          f"| {b['refund_median']['display']} | {b['share_in_store']['display']} | measured |"
          for b in (rg["groups"][g] for g in GROUPS)],
        "",
        "Composition of the preference group",
        "",
        *md_metric_table(rg["preference_composition"]),
        "",
        "Reason to group mapping (normalized reason text; the normalization is " + rg["normalization"] + ")",
        "",
        "| Raw reason | Lines | In store | Taxonomy category | Group | Note |",
        "| --- | ---: | ---: | --- | --- | --- |",
        *[f"| {r['reason']} | {fmt_int(r['lines'])} | {fmt_int(r['lines_in_store'])} | {r['taxonomy_category']} "
          f"| {r['group']} | {r.get('note', '')} |" for r in rg["mapping"]],
        "",
        rg["other_group_note"],
        "",
        rg["mapping_source"],
        "",
        "Lines by month and channel",
        "",
        "| Month | Returned in store | Other channels |",
        "| --- | ---: | ---: |",
        *[f"| {r['month']} | {fmt_int(r['in_store_lines'])} | {fmt_int(r['other_channel_lines'])} |"
          for r in rg["monthly_channel_mix"]],
        "",
        *[f"- Check: {c['check']}: {fmt_int(c['script'])} versus {fmt_int(c['expected'])}, match {c['match']}"
          for c in rg["checks"]],
        "",
        "## Preference-return exchange lever (what-if, assumed)",
        "",
        pl["framing"],
        "",
        pl["additivity"],
        "",
        "Inputs",
        "",
        *md_scenario_table(pl["inputs"], pl["columns"]),
        "",
        "Results",
        "",
        *md_scenario_table(pl["outputs"], pl["columns"]),
        "",
        "Formula:",
        "",
        "```text",
        pl["formula"],
        "```",
        "",
        "## Multi-line returns (bundle)",
        "",
        *md_metric_table(bd["metrics"]),
        "",
        bd["why_bundling_matters"],
        "",
        "Agent runs: one per return number versus one per line",
        "",
        *md_scenario_table(bd["agent_runs_sensitivity"]["inputs"], bd["agent_runs_sensitivity"]["columns"],
                           {"per_return_number": "One run per return number", "per_line": "One run per line"}),
        *md_scenario_table(bd["agent_runs_sensitivity"]["outputs"], bd["agent_runs_sensitivity"]["columns"],
                           {"per_return_number": "One run per return number", "per_line": "One run per line"})[2:],
        "",
    ]
    sens = m.get("agent_cost_sensitivity")
    if sens:
        out += [
            "## Agent cost sensitivity",
            "",
            sens["note"],
            "",
            *md_scenario_table(sens["inputs"], sens["columns"], sens["column_labels"]),
            *md_scenario_table(sens["outputs"], sens["columns"], sens["column_labels"])[2:],
            "",
            sens["previous_placeholder"]["note"],
            "",
            *md_scenario_table(sens["previous_placeholder"]["inputs"], sens["previous_placeholder"]["columns"],
                               {"previous_placeholder": "Placeholder before the live eval"}),
            *md_scenario_table(sens["previous_placeholder"]["outputs"], sens["previous_placeholder"]["columns"],
                               {"previous_placeholder": "Placeholder before the live eval"})[2:],
            "",
        ]
    out += [
        "## Tier perk what-if (inputs only, assumed)",
        "",
        pk["framing"],
        "",
        "Inputs",
        "",
        *md_scenario_table(pk["inputs"], pk["columns"]),
        "",
        "Results (all tagged assumed)",
        "",
        *md_scenario_table(pk["outputs"], pk["columns"]),
        "",
        "Formula:",
        "",
        "```text",
        pk["formula"],
        "```",
        "",
    ]
    return out


def md_margin_section(m: dict) -> list:
    """Markdown for the margin lever; one status line when the transaction profile is not available."""
    mg = m["margin"]
    out = ["## Gross margin lever (transaction file)", ""]
    if mg["status"] != "available":
        return out + [f"Status: not_available. {mg['reason']}.", "", mg["note"], ""]
    src = mg["source"]
    return out + [
        mg["framing"],
        "",
        f"Source: `{src['file']}` (SHA-256 `{src['sha256']}`), {fmt_int(src['rows_read'])} rows read, banner "
        f"{src['banner']}, business dates {src['business_date_min']} to {src['business_date_max']}.",
        "",
        "Measured margin facts",
        "",
        *md_metric_table(mg["measured"]),
        "",
        "Value per retained case",
        "",
        *md_metric_table(mg["per_case"]),
        "",
        "Retained-sale value: revenue-based (existing) next to margin-based",
        "",
        *md_scenario_table(mg["scenarios"], mg["columns"]),
        "",
        "Break-even",
        "",
        *md_metric_table(mg["break_even"]),
        "",
        "Markdown",
        "",
        mg["markdown"]["text"],
        "",
        mg["markdown"]["limits"],
        "",
        "Formula:",
        "",
        "```text",
        mg["formula"],
        "```",
        "",
        *[f"- {c}" for c in mg["caveats"]],
        "",
    ]


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
        *md_slide_sentences(ss),
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
        *md_metric_table({k: v for k, v in m["scale"].items() if not k.startswith("agent_")}),
        "",
        "Agent load (return lines per day are used as agent runs per day; assumptions are tagged assumed)",
        "",
        *md_metric_table({k: v for k, v in m["scale"].items() if k.startswith("agent_")}),
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
        *md_v2_sections(m),
        *md_margin_section(m),
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
        f"- Reason groups: taxonomy `{src['taxonomy']['file']}`; checks passed: "
        f"{all(c['match'] for c in m['return_reason_groups']['checks'])}.",
    ]
    if "usd_to_cad" in m:
        out.append(f"- USD to CAD {m['usd_to_cad']['display']} (assumed), source: {m['usd_to_cad']['source']}.")
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
    parser.add_argument("--transaction-profile", type=Path, default=TXN_PROFILE,
                        help="aggregates from scripts/profile_transactions.py; margin block says not_available if missing")
    args = parser.parse_args(argv)

    for path in (args.returns_csv, args.exchanges_csv):
        if not path.is_file():
            print(f"error: input file not found: {rel(path)}", file=sys.stderr)
            return 2

    returns = scan_returns(args.returns_csv)
    exchanges = scan_exchanges(args.exchanges_csv, returns["store_banners"])
    cost = load_agent_cost(args.eval_results)
    profile = load_eval_profile(args.eval_results)
    model = build_model(returns, exchanges, cost, args.returns_csv, args.exchanges_csv, profile,
                        txn_profile_path=args.transaction_profile)

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

    # impact model v2
    rg = model["return_reason_groups"]
    print("Reason groups (lines, share of lines, refund sum, median refund, share returned in store):")
    for g in GROUPS:
        b = rg["groups"][g]
        print(f"  {g:<20} {b['lines']['display']:>7}  {b['share_of_lines']['display']:>7}  {b['refund_sum']['display']:>18}  "
              f"{b['refund_median']['display']:>11}  {b['share_in_store']['display']:>7}")
    print("Reasons in group other (ambiguous or not a product problem): " + "; ".join(
        f"{r['reason']} {r['lines']}" for r in rg["mapping"] if r["group"] == "other"))
    print("Reconciliation: " + "; ".join(f"{'OK' if c['match'] else 'MISMATCH'} {c['check']}" for c in rg["checks"]))
    comp = rg["preference_composition"]
    print(f"Preference group: combined label {comp['combined_label_lines']['display']} lines "
          f"({comp['combined_label_share_of_preference']['display']}), specific reasons "
          f"{comp['specific_reason_lines']['display']}; combined label on {comp['combined_label_share_of_store_lines']['display']} "
          f"of in-store lines vs {comp['combined_label_share_of_other_channel_lines']['display']} elsewhere")
    pl = model["preference_exchange_lever"]
    for name in pl["columns"]:
        po = pl["outputs"]
        print(f"  preference {name:>4}: converted {po['converted_cases']['display'][name]}, gross {po['gross_value']['display'][name]}, "
              f"standalone net {po['annual_net_value_standalone']['display'][name]}, fit + preference net "
              f"{po['combined_annual_net_value']['display'][name]}, floor gross {po['gross_value_specific_reasons_only']['display'][name]}")
    bm = model["bundle_returns"]["metrics"]
    print(f"Bundle: {bm['return_numbers']['display']} return numbers, {bm['return_lines']['display']} lines, "
          f"{bm['lines_per_return_mean']['display']} lines per return, max {bm['lines_per_return_max']['display']}; "
          f"{bm['share_of_returns_with_2plus_lines']['display']} of returns and {bm['share_of_lines_in_multi_line_returns']['display']} "
          f"of lines are multi-line; {bm['multi_line_returns_with_distinct_skus_share']['display']} of multi-line returns have distinct SKUs")
    bo = model["bundle_returns"]["agent_runs_sensitivity"]["outputs"]["annual_agent_cost"]
    print(f"Agent runs per return number vs per line: annual agent cost {bo['display']['per_return_number']} vs "
          f"{bo['display']['per_line']} ({bo['tag']})")
    if "agent_cost_sensitivity" in model:
        so = model["agent_cost_sensitivity"]
        print("Agent cost sensitivity, base-case fit net: " + "; ".join(
            f"{c} {so['outputs']['annual_net_value_base']['display'][c]}" for c in so["columns"])
            + f"; previous placeholder {so['previous_placeholder']['outputs']['annual_net_value_base']['display']['previous_placeholder']}")
    for key in ("agent_runs_per_day_max", "agent_run_duration_s", "agent_input_tokens_per_run", "agent_output_tokens_per_run",
                "agent_tokens_per_day_max", "agent_tokens_per_minute_busiest_day", "agent_tokens_per_minute_busiest_hour",
                "agent_concurrent_runs_busiest_hour"):
        if key in sc:
            print(f"  scale {key}: {sc[key]['display']} ({sc[key]['tag']})")
    po = model["tier_perk_what_if"]["outputs"]
    print(f"Tier perk (assumed): max cost per member {po['perk_cost_per_member_max']['display']['base']}; per 10,000 customers "
          f"{po['perk_cost_per_10k_customers_max']['display']['low']} / {po['perk_cost_per_10k_customers_max']['display']['base']} / "
          f"{po['perk_cost_per_10k_customers_max']['display']['high']}; break-even extra retained orders per 10,000 customers (max uses) "
          f"{po['break_even_orders_per_10k_customers_max']['display']['low']} / {po['break_even_orders_per_10k_customers_max']['display']['base']} / "
          f"{po['break_even_orders_per_10k_customers_max']['display']['high']}")
    if "usd_to_cad" in model:
        print(f"USD to CAD {model['usd_to_cad']['display']}: {model['usd_to_cad']['source']}")
    mg = model["margin"]
    if mg["status"] != "available":
        print(f"Margin lever: not_available ({mg['reason']})")
    else:
        mm, be, ms = mg["measured"], mg["break_even"], mg["scenarios"]
        print(f"Margin lever: footwear gross margin rate {mm['gross_margin_rate_footwear']['display']} "
              f"(Regular price {mm['gross_margin_rate_footwear_regular_price']['display']}); margin per fit-returned unit "
              f"{mm['avg_margin_per_fit_returned_unit']['display']}")
        for key in ("fit_exchange", "preference_exchange", "prevention"):
            print(f"  {key}: revenue-based " + " / ".join(ms[f"{key}_revenue_based"]["display"][n] for n in mg["columns"])
                  + "; margin-based " + " / ".join(ms[f"{key}_margin_based"]["display"][n] for n in mg["columns"]))
        print(f"  break-even: {be['break_even_exchanges_per_year']['display']} retained exchanges a year pay the agent bill "
              f"({be['break_even_conversion_of_fit_lines_pct']['display']} of fit lines); one exchange pays "
              f"{be['agent_runs_paid_by_one_exchange']['display']} agent runs or {be['perk_uses_paid_by_one_exchange']['display']} perk uses")
    print(f"impact-model.json {status_json}; impact-model.md {status_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
