#!/usr/bin/env python3.12
"""Profile the 2025 ALDO CA/US store+web transaction detail file (aggregates only).

Single streaming pass with the stdlib ``csv`` module, no pandas. The input is about 3 GB, so run it
in the background and poll the log, for example:

    nohup python3.12 scripts/profile_transactions.py \
        --input "new dataset/Transaction_Detail_ALDO_CA_US_2025.csv" \
        --out docs/evidence/transaction-profile.json > /tmp/profile_transactions.log 2>&1 &

Safety: the file holds identifiers (transaction numbers, consumer guids, site ids, postal codes,
city names, image URLs). This script never keeps or writes them. Only these columns feed the
aggregates: banner, business_date, merchandise_category_name, style_name, size_value, channel,
return_reason, in_store_return_indicator, return_indicator, price_group and the four amounts.
Free text in return_reason, size_value and style_name is passed through an allow-list shape check
before it may become an output key (anything else is bucketed and counted).

Conventions (checked on a sample, restated in the output under ``conventions``):
* Money and units are summed as given. A return line has return_indicator 1 and negative units,
  retail, cost and margin. A sales line has return_indicator 0.
* Amounts are stored as integers in 1/10,000 of a currency unit while summing (no float drift).
* Gross margin rate = margin / retail, computed on sales lines only. Margin lost on returns is
  reported separately from the return lines (a positive number, the absolute value).
* ALL is the sum of the banners. Money is NOT summed across banners (ALDO_CA is CAD, ALDO_US is
  assumed USD, unconfirmed); ALL carries counts and units, and rates marked indicative.

Idempotent: no timestamps in the output, atomic replace, unchanged file left alone.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "new dataset" / "Transaction_Detail_ALDO_CA_US_2025.csv"
DEFAULT_OUT = ROOT / "docs" / "evidence" / "transaction-profile.json"
FIT_INSIGHTS = ROOT / "fixtures" / "derived" / "fit_insights.json"

PROGRESS_EVERY = 5_000_000
SCALE = 10_000  # amounts are accumulated as integers in 1/10,000 units
SCHEMA_VERSION = 1

REQUIRED = [
    "banner",
    "business_date",
    "merchandise_category_name",
    "style_name",
    "size_value",
    "channel",
    "return_reason",
    "in_store_return_indicator",
    "return_indicator",
    "price_group",
    "Net_Sales_Retail_Amount",
    "Net_Sales_Units",
    "Net_Sales_Cost_Amount",
    "Gross_Margin_Amount",
]

FOOTWEAR_RE = re.compile(r"^[ML] (Cas|Dress|Perf) (Shoes|Boots|Sandals)$")
FOOTWEAR_EXTRA = {"L Footwear", "M Footwear"}
TRUTHY = {"1", "true", "y", "yes", "t"}
SAFE_REASON_RE = re.compile(r"^[a-z0-9 /:_.,'()&\-]{1,60}$")
CODE_RE = re.compile(r"^[0-9]{1,4}$")
SAFE_SIZE_RE = re.compile(r"^[0-9A-Za-z./ \-]{1,8}$")
SAFE_STYLE_RE = re.compile(r"^[A-Za-z0-9 ._'&+\-]{1,40}$")
DIGITS_ONLY_RE = re.compile(r"^[0-9]{5,}$")

REDACTED_TEXT = "<redacted_free_text>"
REDACTED_DIGITS = "<free_text_with_digits>"
RARE_TEXT = "<rare_free_text>"
RARE_MIN_LINES = 25  # free-text reasons seen on fewer return lines (all banners) are pooled, so no one-off note survives

SEGMENTS = ["M_footwear", "L_footwear", "M_nonfootwear", "L_nonfootwear", "other_footwear", "other_nonfootwear"]

# Reason groups. Numeric POS codes have no legend in the project: they are reported, never mapped.
PREFERENCE = {"not what i expected", "changed my mind / other", "changed idea", "changed my mind"}
COMFORT = {"uncomfortable", "inconfortable"}
QUALITY = {"arrived in poor condition", "manufacturer defect"}
DELIVERY = {"lost in transit", "wrong item delivered", "return to sender"}
FIT_REASONS = {"too small", "too big"}


def classify_category(cat: str) -> tuple[str, bool, str]:
    """Return (gender, is_footwear, segment). Gender from the 'M ' / 'L ' prefix."""
    gender = "M" if cat.startswith("M ") else "L" if cat.startswith("L ") else "other"
    foot = bool(FOOTWEAR_RE.match(cat)) or cat in FOOTWEAR_EXTRA
    return gender, foot, f"{gender}_{'footwear' if foot else 'nonfootwear'}"


def normalize_reason(raw: str) -> str:
    s = " ".join(raw.strip().lower().split())
    if s.startswith("return.page.reasons."):
        s = s[len("return.page.reasons."):]
    if not s:
        return ""
    if CODE_RE.match(s):
        return s
    if any(ch.isdigit() for ch in s):
        # free text with digits can carry ticket, case or order numbers: never keep it
        return REDACTED_DIGITS
    if SAFE_REASON_RE.match(s):
        return s
    return REDACTED_TEXT


def reason_group(norm: str) -> str:
    if norm == "":
        return "blank"
    if norm in (REDACTED_TEXT, REDACTED_DIGITS, RARE_TEXT):
        return "other_text"
    if CODE_RE.match(norm):
        return "pos_code_unmapped"
    if norm in FIT_REASONS:
        return "fit"
    if norm in PREFERENCE:
        return "preference"
    if norm in COMFORT:
        return "comfort"
    if norm in QUALITY:
        return "quality_damage"
    if norm in DELIVERY:
        return "delivery_wrong_item"
    if (
        norm.startswith("return without merch")
        or norm.startswith("return extension")
        or norm.startswith("gift return")
        or "cancel" in norm
        or norm.startswith("riskified")
        or norm.startswith("manually_cancelled")
    ):
        return "process_or_cancel"
    return "other_text"


def to_units(s: str) -> tuple[int, bool]:
    try:
        f = float(s)
    except ValueError:
        return 0, False
    i = int(round(f))
    return i, abs(f - i) < 1e-9


def to_amount(s: str) -> tuple[int, bool]:
    try:
        return int(round(float(s) * SCALE)), True
    except ValueError:
        return 0, False


def bump(d: dict, key, vals: tuple) -> None:
    cur = d.get(key)
    if cur is None:
        d[key] = list(vals)
    else:
        for i, v in enumerate(vals):
            cur[i] += v


def read_thresholds() -> dict:
    fallback = {"min_n": 30, "runs_small_min_share": 0.65, "runs_large_max_share": 0.35}
    try:
        t = json.loads(FIT_INSIGHTS.read_text(encoding="utf-8"))["thresholds"]
        return {k: t[k] for k in ("min_n", "runs_small_min_share", "runs_large_max_share")}
    except (OSError, KeyError, ValueError):
        return fallback


def fit_signal(too_small: int, too_big: int, th: dict) -> str:
    """Same rule as backend/returns_domain/fit.py, in exact fractions."""
    n = too_small + too_big
    if n < th["min_n"]:
        return "insufficient_data"
    share = Fraction(too_small, n)
    if share >= Fraction(str(th["runs_small_min_share"])):
        return "runs_small"
    if share <= Fraction(str(th["runs_large_max_share"])):
        return "runs_large"
    return "mixed"


def money(v: int) -> float:
    """Integer 1/10,000 units to a float with 2 decimals (half away from zero)."""
    sign = -1 if v < 0 else 1
    cents = (abs(v) + SCALE // 200) // (SCALE // 100)
    return sign * cents / 100


def rate(num: int, den: int, digits: int = 6) -> float | None:
    return round(num / den, digits) if den else None


def stat_block(vals: list[int] | None) -> dict:
    """vals = [rows, units, retail, cost, margin]."""
    rows, units, retail, cost, margin = vals or [0, 0, 0, 0, 0]
    return {
        "rows": rows,
        "units": units,
        "retail": money(retail),
        "cost": money(cost),
        "gross_margin": money(margin),
    }


def add5(a: list[int] | None, b: list[int] | None) -> list[int]:
    a = a or [0] * 5
    b = b or [0] * 5
    return [x + y for x, y in zip(a, b)]


class Acc:
    """All accumulators, keyed by banner as the first key element."""

    def __init__(self) -> None:
        self.rows = {}  # banner -> count
        self.date_min = {}
        self.date_max = {}
        self.by_kind = {}  # (banner, kind) -> [rows, units, retail, cost, margin]
        self.by_channel = {}  # (banner, kind, channel, in_store) -> 5
        self.segment = {}  # (banner, kind, segment) -> 5
        self.category = {}  # (banner, kind, category) -> 5
        self.price = {}  # (banner, kind, segment, price_group) -> 5
        self.reasons = {}  # (banner, segment, channel, in_store, reason) -> 5 (return lines only)
        self.fit_price = {}  # (banner, segment, price_group) -> 5 (fit return lines)
        self.sizes = {}  # (banner, segment, kind, size) -> [rows, units]
        self.styles = {}  # (banner, segment, style) -> [s_rows, s_units, s_retail, s_margin, r_rows, r_units, r_margin]
        self.style_fit = {}  # (banner, segment, style, bucket) -> [too_small, too_big, small_units, big_units]
        self.style_sizes = {}  # (banner, segment, style, size) -> [units sold]
        self.zero_sales = {}  # (banner, segment) -> [rows, units] sales lines with units > 0 and retail <= 0
        self.month = {}  # (banner, kind, in_store, month) -> [rows]
        self.sign = {}  # (kind_label, sign pattern) -> [rows]
        self.cat_class = {}  # category -> segment
        self.parse_issues = {"bad_units": 0, "non_integer_units": 0, "bad_amount": 0, "short_rows": 0}
        self.redacted = {"reason_free_text_rows": 0, "size_other_rows": 0, "style_other_rows": 0}
        self.return_indicator_values = {}
        self.price_group_values = {}


def run(path: Path, max_rows: int | None) -> tuple[Acc, dict]:
    t0 = time.time()
    acc = Acc()
    reason_cache: dict[str, str] = {}
    cat_cache: dict[str, tuple[str, bool, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        missing = [c for c in REQUIRED if c not in idx]
        if missing:
            raise SystemExit(f"missing columns: {missing}")
        i_ban, i_dt, i_cat, i_sty = idx["banner"], idx["business_date"], idx["merchandise_category_name"], idx["style_name"]
        i_siz, i_chn, i_rsn = idx["size_value"], idx["channel"], idx["return_reason"]
        i_ins, i_ret, i_prc = idx["in_store_return_indicator"], idx["return_indicator"], idx["price_group"]
        i_rtl, i_unt, i_cst, i_mrg = (
            idx["Net_Sales_Retail_Amount"],
            idx["Net_Sales_Units"],
            idx["Net_Sales_Cost_Amount"],
            idx["Gross_Margin_Amount"],
        )
        need = max(idx[c] for c in REQUIRED) + 1
        n = 0
        for row in reader:
            n += 1
            if len(row) < need:
                acc.parse_issues["short_rows"] += 1
                continue
            b = row[i_ban]
            date = row[i_dt]
            ret_raw = row[i_ret].strip().lower()
            is_ret = ret_raw in TRUTHY
            acc.return_indicator_values[ret_raw] = acc.return_indicator_values.get(ret_raw, 0) + 1
            ins = 1 if row[i_ins].strip().lower() in TRUTHY else 0
            kind = "returns" if is_ret else "sales"
            units, ok_u = to_units(row[i_unt])
            retail, ok1 = to_amount(row[i_rtl])
            cost, ok2 = to_amount(row[i_cst])
            margin, ok3 = to_amount(row[i_mrg])
            if not (ok_u and ok1 and ok2 and ok3):
                acc.parse_issues["bad_amount"] += 1
            elif row[i_unt] and abs(float(row[i_unt]) - units) > 1e-9:
                acc.parse_issues["non_integer_units"] += 1
            vals = (1, units, retail, cost, margin)

            acc.rows[b] = acc.rows.get(b, 0) + 1
            if b not in acc.date_min or date < acc.date_min[b]:
                acc.date_min[b] = date
            if b not in acc.date_max or date > acc.date_max[b]:
                acc.date_max[b] = date

            cat = row[i_cat]
            ci = cat_cache.get(cat)
            if ci is None:
                ci = classify_category(cat)
                cat_cache[cat] = ci
                acc.cat_class[cat] = ci[2]
            _gender, foot, seg = ci
            chn = row[i_chn]
            price = row[i_prc]
            acc.price_group_values[price] = acc.price_group_values.get(price, 0) + 1

            # sign convention check (one counter per pattern)
            sg = (
                "units<0" if units < 0 else "units>0" if units > 0 else "units=0",
                "retail<0" if retail < 0 else "retail>0" if retail > 0 else "retail=0",
            )
            bump(acc.sign, (kind, sg[0], sg[1]), (1,))
            if not is_ret and units > 0 and retail <= 0:
                bump(acc.zero_sales, (b, seg), (1, units))

            bump(acc.by_kind, (b, kind), vals)
            bump(acc.by_channel, (b, kind, chn, ins), vals)
            bump(acc.segment, (b, kind, seg), vals)
            bump(acc.category, (b, kind, cat), vals)
            bump(acc.month, (b, kind, ins, date[:7]), (1,))
            if foot:
                bump(acc.price, (b, kind, seg, price), vals)

            # sizes (footwear only)
            if foot:
                size = row[i_siz].strip()
                if not SAFE_SIZE_RE.match(size):
                    size = "<other>"
                    acc.redacted["size_other_rows"] += 1
                bump(acc.sizes, (b, seg, kind, size), (1, units))

            # style level (footwear only)
            style = ""
            if foot:
                style = row[i_sty].strip()
                if not SAFE_STYLE_RE.match(style) or DIGITS_ONLY_RE.match(style):
                    style = "<other>"
                    acc.redacted["style_other_rows"] += 1
                if is_ret:
                    bump(acc.styles, (b, seg, style), (0, 0, 0, 0, 1, units, margin))
                else:
                    bump(acc.styles, (b, seg, style), (1, units, retail, margin, 0, 0, 0))
                    bump(acc.style_sizes, (b, seg, style, size), (units,))

            if is_ret:
                rr = row[i_rsn]
                norm = reason_cache.get(rr)
                if norm is None:
                    norm = normalize_reason(rr)
                    reason_cache[rr] = norm
                if norm in (REDACTED_TEXT, REDACTED_DIGITS):
                    acc.redacted["reason_free_text_rows"] += 1
                bump(acc.reasons, (b, seg, chn, ins, norm), vals)
                if norm in FIT_REASONS:
                    bump(acc.fit_price, (b, seg, price), vals)
                    if foot:
                        bucket = "in_store_return" if ins else (chn or "blank")
                        small = 1 if norm == "too small" else 0
                        big = 1 - small
                        bump(acc.style_fit, (b, seg, style, bucket), (small, big, small * -units, big * -units))

            if n % PROGRESS_EVERY == 0:
                print(f"rows={n:,} elapsed={time.time() - t0:,.0f}s", flush=True)
            if max_rows and n >= max_rows:
                break
    meta = {"rows_read": n, "header_columns": len(header), "elapsed_s": round(time.time() - t0)}
    return acc, meta


# ----------------------------------------------------------------------------- summaries


def seg_block(acc: Acc, banners: list[str], money_ok: bool) -> dict:
    """Per segment: sales and returns blocks, rates, margin lost on returns."""
    out = {}
    for seg in SEGMENTS:
        s = r = None
        for b in banners:
            s = add5(s, acc.segment.get((b, "sales", seg)))
            r = add5(r, acc.segment.get((b, "returns", seg)))
        blk = {
            "sales_lines": _maybe_money(stat_block(s), money_ok),
            "return_lines": _maybe_money(stat_block(r), money_ok),
            "gross_margin_rate_on_sales": rate(s[4], s[2]),
            "return_units_over_sales_units": rate(-r[1], s[1]),
        }
        zr = sum(acc.zero_sales.get((b, seg), [0, 0])[0] for b in banners)
        zu = sum(acc.zero_sales.get((b, seg), [0, 0])[1] for b in banners)
        blk["sales_lines_with_zero_or_negative_retail"] = {"rows": zr, "units": zu}
        if money_ok:
            blk["margin_lost_on_returned_lines"] = money(-r[4])
        out[seg] = blk
    return out


def _maybe_money(blk: dict, money_ok: bool) -> dict:
    if money_ok:
        return blk
    return {"rows": blk["rows"], "units": blk["units"]}


def price_block(acc: Acc, banners: list[str], money_ok: bool, segs: list[str]) -> dict:
    groups = sorted({k[3] for k in acc.price if k[2] in segs and k[0] in banners})
    out = {}
    for g in groups:
        s = r = None
        for b in banners:
            for seg in segs:
                s = add5(s, acc.price.get((b, "sales", seg, g)))
                r = add5(r, acc.price.get((b, "returns", seg, g)))
        e = {
            "sales_units": s[1],
            "return_units": -r[1],
            "gross_margin_rate_on_sales": rate(s[4], s[2]),
        }
        if money_ok:
            e["sales_retail"] = money(s[2])
            e["sales_gross_margin"] = money(s[4])
        if money_ok and s[1]:
            e["avg_retail_per_unit_sold"] = round(s[2] / SCALE / s[1], 2)
            e["avg_cost_per_unit_sold"] = round(s[3] / SCALE / s[1], 2)
            e["avg_margin_per_unit_sold"] = round(s[4] / SCALE / s[1], 2)
        if money_ok and r[1]:
            e["avg_margin_lost_per_returned_unit"] = round(r[4] / SCALE / r[1], 2)
        out[g] = e
    return out


def reason_tables(acc: Acc, banners: list[str], money_ok: bool) -> dict:
    """Distinct reasons with counts; splits by in-store flag, channel, men's vs ladies footwear."""
    by_reason = {}
    for (b, seg, chn, ins, norm), v in acc.reasons.items():
        if b not in banners:
            continue
        e = by_reason.setdefault(norm, {"group": reason_group(norm), "lines": 0, "units": 0, "in_store_return_lines": 0})
        e["lines"] += v[0]
        e["units"] += -v[1]
        if ins:
            e["in_store_return_lines"] += v[0]
    total_lines = sum(e["lines"] for e in by_reason.values())
    reasons_sorted = dict(sorted(by_reason.items(), key=lambda kv: (-kv[1]["lines"], kv[0])))

    def group_split(filter_fn) -> dict:
        g = {}
        tot = 0
        for (b, seg, chn, ins, norm), v in acc.reasons.items():
            if b not in banners or not filter_fn(seg, chn, ins):
                continue
            grp = reason_group(norm)
            e = g.setdefault(grp, {"lines": 0, "units": 0})
            e["lines"] += v[0]
            e["units"] += -v[1]
            tot += v[0]
        res = {}
        for grp, e in sorted(g.items(), key=lambda kv: (-kv[1]["lines"], kv[0])):
            res[grp] = {"lines": e["lines"], "units": e["units"], "share_of_lines": rate(e["lines"], tot, 4)}
        return {"total_return_lines": tot, "groups": res}

    splits = {
        "all_return_lines": group_split(lambda seg, chn, ins: True),
        "in_store_return_lines": group_split(lambda seg, chn, ins: ins == 1),
        "not_in_store_return_lines": group_split(lambda seg, chn, ins: ins == 0),
        "M_footwear": group_split(lambda seg, chn, ins: seg == "M_footwear"),
        "L_footwear": group_split(lambda seg, chn, ins: seg == "L_footwear"),
        "M_footwear_in_store": group_split(lambda seg, chn, ins: seg == "M_footwear" and ins == 1),
        "L_footwear_in_store": group_split(lambda seg, chn, ins: seg == "L_footwear" and ins == 1),
    }

    # where does the fit text appear? (channel x in-store flag), footwear men's and ladies
    fit_where = {}
    for (b, seg, chn, ins, norm), v in acc.reasons.items():
        if b in banners and norm in FIT_REASONS and seg in ("M_footwear", "L_footwear"):
            key = f"{seg}|{chn or 'blank'}|{'in_store_return' if ins else 'not_in_store_return'}"
            e = fit_where.setdefault(key, {"too_small": 0, "too_big": 0})
            e["too_small" if norm == "too small" else "too_big"] += v[0]
    fit_where = dict(sorted(fit_where.items()))

    # coded (numeric) reasons for footwear, so the codes can be mapped later if a legend turns up
    codes = {}
    for (b, seg, chn, ins, norm), v in acc.reasons.items():
        if b in banners and CODE_RE.match(norm) and seg in ("M_footwear", "L_footwear"):
            e = codes.setdefault(norm, {"M_footwear": 0, "L_footwear": 0})
            e[seg] += v[0]
    codes = dict(sorted(codes.items(), key=lambda kv: -(kv[1]["M_footwear"] + kv[1]["L_footwear"])))

    return {
        "total_return_lines": total_lines,
        "distinct_reasons": len(by_reason),
        "reasons": reasons_sorted,
        "group_splits": splits,
        "fit_text_reason_where": fit_where,
        "numeric_codes_footwear_lines": codes,
    }


def size_dist(acc: Acc, banners: list[str], seg: str) -> dict:
    agg = {}
    for (b, s, kind, size), v in acc.sizes.items():
        if b in banners and s == seg:
            e = agg.setdefault(size, {"sales_units": 0, "sales_lines": 0, "return_units": 0, "return_lines": 0})
            if kind == "sales":
                e["sales_units"] += v[1]
                e["sales_lines"] += v[0]
            else:
                e["return_units"] += -v[1]
                e["return_lines"] += v[0]

    def key(sz: str):
        try:
            return (0, float(sz))
        except ValueError:
            return (1, sz)

    return {k: agg[k] for k in sorted(agg, key=key)}


def style_rows(acc: Acc, banners: list[str], seg: str, th: dict) -> list[dict]:
    agg = {}
    for (b, s, style), v in acc.styles.items():
        if b in banners and s == seg:
            e = agg.setdefault(style, [0] * 7)
            for i, x in enumerate(v):
                e[i] += x
    fit = {}
    for (b, s, style, bucket), v in acc.style_fit.items():
        if b in banners and s == seg:
            e = fit.setdefault(style, {}).setdefault(bucket, [0, 0])
            e[0] += v[0]
            e[1] += v[1]
    sizes = {}
    for (b, s, style, size), v in acc.style_sizes.items():
        if b in banners and s == seg:
            sizes.setdefault(style, {})
            sizes[style][size] = sizes[style].get(size, 0) + v[0]
    rows = []
    for style, v in agg.items():
        buckets = fit.get(style, {})
        sz = sizes.get(style, {})
        total_sz = sum(sz.values())
        size_9_plus = 0
        for k, u in sz.items():
            try:
                if float(k) >= 9:
                    size_9_plus += u
            except ValueError:
                pass
        small = sum(x[0] for x in buckets.values())
        big = sum(x[1] for x in buckets.values())
        row = {
            "style": style,
            "units_sold": v[1],
            "sales_lines": v[0],
            "return_lines": v[4],
            "return_units": -v[5],
            "share_units_size_9_plus": rate(size_9_plus, total_sz, 4),
            "fit_too_small": small,
            "fit_too_big": big,
            "n_fit": small + big,
            "too_small_share": rate(small, small + big, 4),
            "signal": fit_signal(small, big, th),
            "fit_by_channel": {k: {"too_small": x[0], "too_big": x[1]} for k, x in sorted(buckets.items())},
        }
        rows.append(row)
    return rows


def load_oms_fit_products() -> dict:
    """Lower-cased product name -> fit counts from the OMS-derived fixture (read-only reference)."""
    try:
        data = json.loads(FIT_INSIGHTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name.lower(): scopes for name, scopes in data.get("products", {}).items() if isinstance(scopes, dict)}


def men_candidates_oms_join(m_rows: list[dict], l_rows: list[dict], th: dict, min_units: int = 500, top: int = 20) -> dict:
    """Men's footwear styles of this file joined by name with the OMS-derived fit counts (24 months, all channels).

    The OMS side holds far more fit-reason lines per style than this file's 12-month web text reasons, so it is
    the volume that can satisfy the n_fit >= 100 test; this file settles that the style is men's footwear.
    """
    oms = load_oms_fit_products()
    ladies = {r["style"].lower() for r in l_rows}
    rows = []
    for r in m_rows:
        if r["units_sold"] < min_units:
            continue
        ref = oms.get(r["style"].lower())
        if not ref:
            continue
        ca, al = ref.get("ALDO_CA", {}), ref.get("ALL", {})
        ca_s, ca_b = ca.get("too_small", 0), ca.get("too_big", 0)
        al_s, al_b = al.get("too_small", 0), al.get("too_big", 0)
        rows.append(
            {
                "style": r["style"],
                "also_sold_as_ladies_footwear_in_this_file": r["style"].lower() in ladies,
                "units_sold_in_this_file": r["units_sold"],
                "share_units_size_9_plus_in_this_file": r["share_units_size_9_plus"],
                "txn_file_fit_too_small": r["fit_too_small"],
                "txn_file_fit_too_big": r["fit_too_big"],
                "oms_aldo_ca": {"too_small": ca_s, "too_big": ca_b, "n_fit": ca_s + ca_b,
                                "too_big_share": rate(ca_b, ca_s + ca_b, 4), "signal": fit_signal(ca_s, ca_b, th)},
                "oms_all_banners": {"too_small": al_s, "too_big": al_b, "n_fit": al_s + al_b,
                                    "too_big_share": rate(al_b, al_s + al_b, 4), "signal": fit_signal(al_s, al_b, th)},
            }
        )
    rows = [x for x in rows if x["oms_aldo_ca"]["n_fit"] >= 50 or x["oms_all_banners"]["n_fit"] >= 100]
    rows.sort(key=lambda x: (-x["oms_aldo_ca"]["n_fit"], x["style"]))
    return {
        "rule": f"men's footwear styles of this file with at least {min_units} units sold, matched by lower-cased name to fixtures/derived/fit_insights.json (OMS file, Feb 2024 to Jan 2026, all return channels); kept when OMS ALDO_CA n_fit >= 50 or OMS ALL n_fit >= 100",
        "caveat": "A name match is not a key match: the OMS file has no category column for men's, so the match relies on the style name only (checked against ladies styles of this file in also_sold_as_ladies_footwear_in_this_file).",
        "candidates": rows[:top],
    }


def top_styles(rows: list[dict], key: str, n: int = 15) -> list[dict]:
    return sorted(rows, key=lambda r: (-r[key], r["style"]))[:n]


def monthly(acc: Acc, b: str) -> dict:
    out = {}
    for (bb, kind, ins, month), v in acc.month.items():
        if bb != b:
            continue
        label = f"{kind}{'_in_store_return_flag' if ins else ''}"
        out.setdefault(month, {})[label] = out.setdefault(month, {}).get(label, 0) + v[0]
    return {m: dict(sorted(out[m].items())) for m in sorted(out)}


def scope_summary(acc: Acc, banners: list[str], money_ok: bool, th: dict, scope_label: str) -> dict:
    tot = {k: add5(None, None) for k in ("sales", "returns")}
    for b in banners:
        for kind in ("sales", "returns"):
            tot[kind] = add5(tot[kind], acc.by_kind.get((b, kind)))
    rows = sum(acc.rows.get(b, 0) for b in banners)
    ret_rows = tot["returns"][0]
    channels = {}
    for (b, kind, chn, ins), v in acc.by_channel.items():
        if b in banners:
            e = channels.setdefault(chn or "blank", {"sales_lines": 0, "return_lines": 0, "in_store_return_flag_return_lines": 0})
            if kind == "sales":
                e["sales_lines"] += v[0]
            else:
                e["return_lines"] += v[0]
                if ins:
                    e["in_store_return_flag_return_lines"] += v[0]
    segs = seg_block(acc, banners, money_ok)
    m_rows = style_rows(acc, banners, "M_footwear", th)
    l_rows = style_rows(acc, banners, "L_footwear", th)
    m_sig_counts = {}
    for r in m_rows:
        m_sig_counts[r["signal"]] = m_sig_counts.get(r["signal"], 0) + 1
    mf_fit = {"too_small": sum(r["fit_too_small"] for r in m_rows), "too_big": sum(r["fit_too_big"] for r in m_rows)}
    lf_fit = {"too_small": sum(r["fit_too_small"] for r in l_rows), "too_big": sum(r["fit_too_big"] for r in l_rows)}
    summary = {
        "scope": scope_label,
        "rows": rows,
        "business_date_min": min((acc.date_min[b] for b in banners if b in acc.date_min), default=None),
        "business_date_max": max((acc.date_max[b] for b in banners if b in acc.date_max), default=None),
        "sales_lines": _maybe_money(stat_block(tot["sales"]), money_ok),
        "return_lines": _maybe_money(stat_block(tot["returns"]), money_ok),
        "return_line_share_of_rows": rate(ret_rows, rows, 4),
        "gross_margin_rate_on_sales_lines": rate(tot["sales"][4], tot["sales"][2]),
        "channels": dict(sorted(channels.items())),
        "segments": segs,
        "categories": category_table(acc, banners, money_ok),
        "reasons": reason_tables(acc, banners, money_ok),
        "footwear_fit_returns_text_reasons": {
            "M_footwear": mf_fit,
            "L_footwear": lf_fit,
            "M_footwear_signal_counts_over_styles": dict(sorted(m_sig_counts.items())),
        },
        "M_footwear_sizes": size_dist(acc, banners, "M_footwear"),
        "L_footwear_sizes": size_dist(acc, banners, "L_footwear"),
        "M_footwear_top15_by_units_sold": top_styles(m_rows, "units_sold"),
        "M_footwear_top15_by_fit_returns": top_styles(m_rows, "n_fit"),
        "M_footwear_styles_with_n_fit_ge_100": sorted(
            [r for r in m_rows if r["n_fit"] >= 100], key=lambda r: (-r["n_fit"], r["style"])
        ),
        "L_footwear_top5_by_fit_returns": top_styles(l_rows, "n_fit", 5),
        "M_footwear_styles_matched_to_oms_fit_insights": men_candidates_oms_join(m_rows, l_rows, th),
        "biglect_reference": [r for r in l_rows if r["style"].lower() == "biglect"],
        "footwear_price_group": {
            "M_footwear": price_block(acc, banners, money_ok, ["M_footwear"]),
            "L_footwear": price_block(acc, banners, money_ok, ["L_footwear"]),
            "all_footwear": price_block(acc, banners, money_ok, ["M_footwear", "L_footwear"]),
        },
        "fit_return_margin": fit_return_margin(acc, banners, money_ok),
        "monthly_lines": {b: monthly(acc, b) for b in banners} if len(banners) == 1 else None,
    }
    if not money_ok:
        summary["money_note"] = (
            "Money is not summed across banners (ALDO_CA assumed CAD, ALDO_US assumed USD, unconfirmed). "
            "Rates are unitless and indicative; counts and units are exact."
        )
    else:
        summary["money_currency"] = "CAD assumed, unconfirmed" if banners == ["ALDO_CA"] else "USD assumed, unconfirmed"
    return summary


def category_table(acc: Acc, banners: list[str], money_ok: bool) -> dict:
    cats = sorted({k[2] for k in acc.category if k[0] in banners})
    out = {}
    for c in cats:
        s = r = None
        for b in banners:
            s = add5(s, acc.category.get((b, "sales", c)))
            r = add5(r, acc.category.get((b, "returns", c)))
        e = {
            "segment": acc.cat_class[c],
            "sales_units": s[1],
            "return_units": -r[1],
            "gross_margin_rate_on_sales": rate(s[4], s[2]),
        }
        if money_ok:
            e["sales_retail"] = money(s[2])
            e["sales_gross_margin"] = money(s[4])
        out[c] = e
    return out


def fit_return_margin(acc: Acc, banners: list[str], money_ok: bool) -> dict:
    """Average margin lost per returned unit for the fit text reasons, footwear segments."""
    out = {}
    for label, segs in (("M_footwear", ("M_footwear",)), ("L_footwear", ("L_footwear",)), ("all_footwear", ("M_footwear", "L_footwear"))):
        t = None
        for (b, s, price), v in acc.fit_price.items():
            if b in banners and s in segs:
                t = add5(t, v)
        t = t or [0] * 5
        e = {"fit_return_lines": t[0], "fit_return_units": -t[1]}
        if money_ok and t[1]:
            e["avg_retail_per_returned_unit"] = round(t[2] / SCALE / t[1], 2)
            e["avg_cost_per_returned_unit"] = round(t[3] / SCALE / t[1], 2)
            e["avg_margin_per_returned_unit"] = round(t[4] / SCALE / t[1], 2)
        groups = {}
        for (b, s, price), v in acc.fit_price.items():
            if b in banners and s in segs:
                g = groups.setdefault(price, {"lines": 0, "units": 0, "margin": 0})
                g["lines"] += v[0]
                g["units"] += -v[1]
                g["margin"] += -v[4]
        e["by_price_group"] = {
            p: ({"lines": g["lines"], "units": g["units"], "avg_margin_per_returned_unit": round(g["margin"] / SCALE / g["units"], 2)} if money_ok and g["units"] else {"lines": g["lines"], "units": g["units"]})
            for p, g in sorted(groups.items())
        }
        out[label] = e
    return out


def exchange_crosscheck(acc: Acc) -> dict:
    """Context for the in-store comparison with the OMS-side exchange evidence."""
    model = ROOT / "docs" / "impact" / "impact-model.json"
    ev = None
    if model.is_file():
        try:
            ev = json.loads(model.read_text(encoding="utf-8")).get("exchange_evidence")
        except ValueError:
            ev = None
    out = {"impact_model_exchange_evidence": None}
    if ev:
        out["impact_model_exchange_evidence"] = {
            k: ev[k]["value"] for k in ("confirmed_all_stores", "assumed_all_stores", "confirmed_aldo_ca_stores", "assumed_aldo_ca_stores") if k in ev
        }
        out["impact_model_exchange_window"] = "2025-02-01..2026-01-31 (McGill_Instore_return_exchanges.csv)"
    ca_instore = acc.by_channel
    ca = {"in_store_return_flag_return_lines": 0, "return_lines": 0, "return_lines_channel_STR": 0, "return_lines_channel_WEB": 0}
    for (b, kind, chn, ins), v in ca_instore.items():
        if b == "ALDO_CA" and kind == "returns":
            ca["return_lines"] += v[0]
            if ins:
                ca["in_store_return_flag_return_lines"] += v[0]
            if chn in ("STR", "WEB"):
                ca[f"return_lines_channel_{chn}"] += v[0]
    out["aldo_ca_transaction_file_2025"] = ca
    return out


def collapse_rare_reasons(acc: Acc) -> None:
    """Pool free-text reasons that are rare (all banners) into one bucket; canonical labels and codes stay."""
    totals = {}
    for (_b, _seg, _chn, _ins, norm), v in acc.reasons.items():
        totals[norm] = totals.get(norm, 0) + v[0]
    rare = {n for n, t in totals.items() if reason_group(n) == "other_text" and n not in (REDACTED_TEXT, REDACTED_DIGITS) and t < RARE_MIN_LINES}
    if not rare:
        return
    merged = {}
    for (b, seg, chn, ins, norm), v in acc.reasons.items():
        bump(merged, (b, seg, chn, ins, RARE_TEXT if norm in rare else norm), tuple(v))
    acc.reasons = merged
    acc.redacted["rare_free_text_distinct_reasons_pooled"] = len(rare)


def build(acc: Acc, meta: dict, th: dict, input_name: str) -> dict:
    collapse_rare_reasons(acc)
    banners = sorted(acc.rows)
    scopes = {}
    for b in banners:
        scopes[b] = scope_summary(acc, [b], True, th, b)
    scopes["ALL"] = scope_summary(acc, banners, False, th, "ALL banners (" + ", ".join(banners) + ")")
    return {
        "schema_version": SCHEMA_VERSION,
        "title": "Transaction detail ALDO CA/US 2025: aggregate profile",
        "script": "scripts/profile_transactions.py",
        "source_file": input_name,
        "rows_read": meta["rows_read"],
        "partial_run": meta.get("partial", False),
        "privacy": "Aggregates only. Transaction numbers, consumer guids, site ids, postal codes, city names and image URLs are never read into any output.",
        "conventions": {
            "sales_line": "return_indicator falsy ('0'); units, retail, cost and margin are summed as given",
            "return_line": "return_indicator '1'; units, retail, cost and margin are negative in the file (see sign_patterns); the output reports return units and margin lost as positive numbers",
            "gross_margin_rate": "gross margin / retail on sales lines only (return lines excluded)",
            "gross_margin_unit": "dollars as given, banner currency assumed (ALDO_CA = CAD, ALDO_US = USD, unconfirmed)",
            "gender": "category prefix 'M ' = men's, 'L ' = ladies; anything else = other",
            "footwear_rule": "category matches '^[ML] (Cas|Dress|Perf) (Shoes|Boots|Sandals)$' or equals 'L Footwear'/'M Footwear'; everything else (handbags, jewelry, socks, sunglasses, belts, care products, insoles) is non-footwear. The full category-to-segment map is in each scope's categories block",
            "fit_reasons": "return_reason (strip, lower-case, whitespace collapse) equals 'too small' or 'too big', the same rule as fixtures/derived/fit_insights.json; 'trop grand' and comfort are not fit",
            "numeric_reason_codes": "Store (STR) return lines carry numeric POS reason codes. No legend exists in the project, so they are counted and never mapped to fit or preference",
            "signal_thresholds": th,
            "signal_rule": "n_fit = too_small + too_big; below min_n insufficient_data; too_small/n_fit >= runs_small_min_share runs_small; <= runs_large_max_share runs_large; else mixed (exact fractions, as backend/returns_domain/fit.py)",
            "n_fit_unit": "return lines",
        },
        "sign_patterns": {
            f"{k[0]}|{k[1]}|{k[2]}": v[0] for k, v in sorted(acc.sign.items())
        },
        "return_indicator_values": dict(sorted(acc.return_indicator_values.items())),
        "price_group_values": dict(sorted(acc.price_group_values.items())),
        "parse_issues": acc.parse_issues,
        "redaction_counts": acc.redacted,
        "category_segments": dict(sorted(acc.cat_class.items())),
        "exchange_crosscheck": exchange_crosscheck(acc),
        "scopes": scopes,
    }


def write_if_changed(path: Path, text: str) -> str:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return "written"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--max-rows", type=int, default=None, help="stop after N data rows (verification slices)")
    args = ap.parse_args(argv)
    if not args.input.is_file():
        print(f"input not found: {args.input.name}", file=sys.stderr)
        return 2
    th = read_thresholds()
    acc, meta = run(args.input, args.max_rows)
    meta["partial"] = bool(args.max_rows)
    profile = build(acc, meta, th, args.input.name)
    text = json.dumps(profile, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    status = write_if_changed(args.out, text)
    print(f"rows={meta['rows_read']:,} elapsed={meta['elapsed_s']}s {args.out.name} {status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
