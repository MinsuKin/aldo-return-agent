#!/usr/bin/env python3
"""Build fixtures/derived/fit_insights.json from the provided returns CSV.

Streams dataset/OMS Data_Feb24-Jan26_new.csv with csv.DictReader (stdlib only, no
pandas). For every orderitem_product_name it counts return lines whose reason is
"Too small" or "Too big" (reason normalized: whitespace collapsed, lower-cased,
the "return.page.reasons." prefix dropped) in two scopes: store_banner ALDO_CA
and ALL banners. Requirements 5.1 to 5.3; design section 5.2.

The output holds aggregates only. It never contains customer, order or return
identifiers. dataset/ is read-only and is never copied.

Usage:
    python3.12 scripts/build_fit_insights.py [--input CSV] [--output JSON] [--top N]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "dataset" / "OMS Data_Feb24-Jan26_new.csv"
DEFAULT_OUTPUT = ROOT / "fixtures" / "derived" / "fit_insights.json"

CA_BANNER = "ALDO_CA"
SCOPES = (CA_BANNER, "ALL")
REASON_PREFIX = "return.page.reasons."
REASON_TOO_SMALL = "too small"
REASON_TOO_BIG = "too big"

# Requirement 5.3 thresholds. The signal uses exact integer arithmetic
# (share >= 0.65  <=>  too_small * 100 >= 65 * n) so float rounding never decides it.
MIN_N = 30
RUNS_SMALL_PCT = 65
RUNS_LARGE_PCT = 35

FILTER_TEXT = (
    "reason (whitespace collapsed, lower-cased, 'return.page.reasons.' prefix dropped) "
    "equals 'too small' or 'too big'; scopes: store_banner == ALDO_CA and ALL banners; "
    "rows with a blank orderitem_product_name are excluded from per-product counts"
)


def normalize_reason(raw: str | None) -> str:
    """strip, lowercase, drop the return.page.reasons. prefix, collapse whitespace."""
    text = (raw or "").strip().lower()
    if text.startswith(REASON_PREFIX):
        text = text[len(REASON_PREFIX):]
    return " ".join(text.split())


def signal_for(too_small: int, too_big: int) -> str:
    """Requirement 5.3. n is the number of fit lines (too small + too big)."""
    n = too_small + too_big
    if n < MIN_N:
        return "insufficient_data"
    if too_small * 100 >= RUNS_SMALL_PCT * n:
        return "runs_small"
    if too_small * 100 <= RUNS_LARGE_PCT * n:
        return "runs_large"
    return "mixed"


def scope_entry(counts: list[int] | None) -> dict:
    too_small, too_big, total = counts if counts else (0, 0, 0)
    n_fit = too_small + too_big
    return {
        "too_small": too_small,
        "too_big": too_big,
        "total_returns": total,
        "too_small_share": (too_small / n_fit) if n_fit else None,
        "n_fit": n_fit,
        "signal": signal_for(too_small, too_big),
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(input_path: Path) -> dict:
    # counts[scope][product] = [too_small, too_big, total_return_lines]
    counts: dict[str, dict[str, list[int]]] = {s: defaultdict(lambda: [0, 0, 0]) for s in SCOPES}
    totals = {s: {"return_lines": 0, "too_small": 0, "too_big": 0, "lines_without_product_name": 0}
              for s in SCOPES}
    rows_read = 0

    # newline="" is required so quoted fields containing line breaks parse correctly.
    with input_path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows_read += 1
            banner = (row.get("store_banner") or "").strip()
            reason = normalize_reason(row.get("reason"))
            name = (row.get("orderitem_product_name") or "").strip()
            scopes = ("ALL", CA_BANNER) if banner == CA_BANNER else ("ALL",)
            for scope in scopes:
                totals[scope]["return_lines"] += 1
                if reason == REASON_TOO_SMALL:
                    totals[scope]["too_small"] += 1
                elif reason == REASON_TOO_BIG:
                    totals[scope]["too_big"] += 1
                if not name:
                    totals[scope]["lines_without_product_name"] += 1
                    continue
                bucket = counts[scope][name]
                bucket[2] += 1
                if reason == REASON_TOO_SMALL:
                    bucket[0] += 1
                elif reason == REASON_TOO_BIG:
                    bucket[1] += 1

    names = sorted(counts["ALL"])  # every ALDO_CA product also appears in ALL
    products = {
        name: {scope: scope_entry(counts[scope].get(name)) for scope in SCOPES}
        for name in names
    }
    for scope in SCOPES:
        totals[scope]["products"] = len(counts[scope])

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": "derived",
        "source": {
            "kind": "provided_csv",
            "file": input_path.name,
            "sha256": sha256_of(input_path),
            "rows_read": rows_read,
        },
        "filter": FILTER_TEXT,
        "thresholds": {
            "min_n": MIN_N,
            "runs_small_min_share": RUNS_SMALL_PCT / 100,
            "runs_large_max_share": RUNS_LARGE_PCT / 100,
            "n_definition": "n_fit = too_small + too_big",
        },
        "totals": totals,
        "products": products,
    }


def render(doc: dict) -> str:
    """Pretty header, one product per line (keeps the committed file small and diffs readable)."""
    head = {k: v for k, v in doc.items() if k != "products"}
    head_text = json.dumps(head, indent=2, ensure_ascii=False)  # ends with "\n}"
    rows = ",\n".join(
        f"    {json.dumps(name, ensure_ascii=False)}: "
        f"{json.dumps(entry, ensure_ascii=False, separators=(',', ':'))}"
        for name, entry in doc["products"].items()
    )
    text = head_text[:-2] + ',\n  "products": {\n' + rows + "\n  }\n}\n"
    if json.loads(text) != json.loads(json.dumps(doc)):  # guard against a rendering bug
        raise RuntimeError("rendered JSON does not round-trip")
    return text


def print_summary(doc: dict, output_path: Path, top: int) -> None:
    src = doc["source"]
    print(f"input file      : {src['file']}")
    print(f"sha256          : {src['sha256']}")
    print(f"rows read       : {src['rows_read']}")
    for scope in SCOPES:
        t = doc["totals"][scope]
        print(
            f"{scope:<8} lines={t['return_lines']} too_small={t['too_small']} too_big={t['too_big']} "
            f"fit_lines={t['too_small'] + t['too_big']} products={t['products']} "
            f"lines_without_product_name={t['lines_without_product_name']}"
        )
    signals: dict[str, int] = defaultdict(int)
    for entry in doc["products"].values():
        if entry[CA_BANNER]["total_returns"] > 0:  # only products that have ALDO_CA return lines
            signals[entry[CA_BANNER]["signal"]] += 1
    print("ALDO_CA products (with return lines) by signal:", dict(sorted(signals.items())))
    ranked = sorted(
        ((name, e[CA_BANNER]) for name, e in doc["products"].items()),
        key=lambda item: (-item[1]["n_fit"], item[0]),
    )[:top]
    print(f"top {top} ALDO_CA products by fit lines (n_fit):")
    for name, e in ranked:
        share = "n/a" if e["too_small_share"] is None else f"{e['too_small_share']:.3f}"
        print(
            f"  {name:<28} n_fit={e['n_fit']:<5} too_small={e['too_small']:<5} "
            f"too_big={e['too_big']:<5} share={share:<6} total={e['total_returns']:<5} {e['signal']}"
        )
    print(f"wrote {output_path} ({output_path.stat().st_size} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="returns CSV (read-only)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="fit_insights.json path")
    parser.add_argument("--top", type=int, default=15, help="rows in the printed top-products table")
    args = parser.parse_args(argv)

    if not args.input.is_file():
        print(f"input CSV not found: {args.input}", file=sys.stderr)
        return 2
    doc = build(args.input)
    text = render(doc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print_summary(doc, args.output, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
