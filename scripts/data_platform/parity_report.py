#!/usr/bin/env python3
"""Compare the curated fit_insights document with the bundled fixtures/derived/fit_insights.json.

Parity gate (design "The curated document"): for every product and scope, too_small, too_big
and total_returns must equal the bundled file. Reports:
  * products compared, and the mismatches (first 20, with both values side by side)
  * totals equal? per scope (return_lines, too_small, too_big, lines_without_product_name)
  * bytes scanned and seconds per Athena query, and the cost estimate (USD 5 per TB scanned)

The curated document is read from the local copy build_insights.py saved
(infra/build/data-platform/last_curated_fit_insights.json) by default, or from S3 with --from-s3
(reads curated/fit_insights/latest.json then the pointed object). Aggregates only; no row or
customer value is printed.

Usage:
    python3 scripts/data_platform/parity_report.py
    scripts/with_aws_env.sh python3 scripts/data_platform/parity_report.py --from-s3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BUNDLED = ROOT / "fixtures" / "derived" / "fit_insights.json"
LOCAL_CURATED = ROOT / "infra" / "build" / "data-platform" / "last_curated_fit_insights.json"  # written by build_insights.py (gitignored)
REGION = "us-east-1"
CONFIG_PARAM = "/aldo-demo/data-platform/config"

SCOPES = ("ALDO_CA", "ALL")
COMPARE_KEYS = ("too_small", "too_big", "total_returns")
TOTAL_KEYS = ("return_lines", "too_small", "too_big", "lines_without_product_name", "products")
USD_PER_TB = 5.0


def load_bundled() -> dict:
    return json.loads(BUNDLED.read_text(encoding="utf-8"))


def load_curated(from_s3: bool, bucket: str | None) -> dict:
    if not from_s3:
        return json.loads(LOCAL_CURATED.read_text(encoding="utf-8"))
    import boto3

    ssm = boto3.client("ssm", region_name=REGION)
    if bucket is None:
        bucket = json.loads(ssm.get_parameter(Name=CONFIG_PARAM)["Parameter"]["Value"])["bucket"]
    s3 = boto3.client("s3", region_name=REGION)
    latest = json.loads(s3.get_object(Bucket=bucket, Key="curated/fit_insights/latest.json")["Body"].read())
    body = s3.get_object(Bucket=bucket, Key=latest["key"])["Body"].read()
    return json.loads(body)


def compare(bundled: dict, curated: dict) -> dict:
    bprod = bundled.get("products", {})
    cprod = curated.get("products", {})
    all_names = sorted(set(bprod) | set(cprod))
    mismatches: list[dict] = []
    compared = 0
    for name in all_names:
        b = bprod.get(name)
        c = cprod.get(name)
        for scope in SCOPES:
            bs = (b or {}).get(scope)
            cs = (c or {}).get(scope)
            compared += 1
            if bs is None and cs is None:
                continue
            if bs is None or cs is None:
                mismatches.append({"product": name, "scope": scope,
                                   "bundled": bs, "curated": cs, "why": "missing scope"})
                continue
            for key in COMPARE_KEYS:
                if int(bs.get(key, 0)) != int(cs.get(key, 0)):
                    mismatches.append({"product": name, "scope": scope, "key": key,
                                       "bundled": bs.get(key), "curated": cs.get(key)})
    # totals
    total_mismatches = []
    for scope in SCOPES:
        bt = bundled.get("totals", {}).get(scope, {})
        ct = curated.get("totals", {}).get(scope, {})
        for key in TOTAL_KEYS:
            if int(bt.get(key, 0)) != int(ct.get(key, 0)):
                total_mismatches.append({"scope": scope, "key": key,
                                         "bundled": bt.get(key), "curated": ct.get(key)})
    return {
        "products_in_bundled": len(bprod),
        "products_in_curated": len(cprod),
        "scope_cells_compared": compared,
        "product_mismatches": mismatches,
        "total_mismatches": total_mismatches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--from-s3", action="store_true", help="read curated doc from S3 latest.json")
    parser.add_argument("--bucket", default=None, help="bucket override for --from-s3")
    args = parser.parse_args(argv)

    bundled = load_bundled()
    curated = load_curated(args.from_s3, args.bucket)
    result = compare(bundled, curated)

    print("=== PARITY REPORT: curated (aws-athena) vs bundled (derived) ===")
    print(f"products: bundled={result['products_in_bundled']} curated={result['products_in_curated']}")
    print(f"scope cells compared: {result['scope_cells_compared']}")

    # totals
    print("\n-- totals per scope --")
    for scope in SCOPES:
        bt = bundled.get("totals", {}).get(scope, {})
        ct = curated.get("totals", {}).get(scope, {})
        print(f"  {scope}:")
        for key in TOTAL_KEYS:
            mark = "ok" if int(bt.get(key, 0)) == int(ct.get(key, 0)) else "MISMATCH"
            print(f"    {key:<28} bundled={bt.get(key)!s:<10} curated={ct.get(key)!s:<10} [{mark}]")
    totals_equal = not result["total_mismatches"]
    print(f"  totals equal? {'YES' if totals_equal else 'NO'}")

    # product mismatches
    pm = result["product_mismatches"]
    print(f"\n-- product mismatches: {len(pm)} --")
    for m in pm[:20]:
        if "key" in m:
            print(f"  {m['product']!r} [{m['scope']}] {m['key']}: bundled={m['bundled']} curated={m['curated']}")
        else:
            print(f"  {m['product']!r} [{m['scope']}] {m.get('why')}: bundled={m['bundled']} curated={m['curated']}")
    if len(pm) > 20:
        print(f"  ... and {len(pm) - 20} more")

    # athena cost / scan from the curated platform block
    platform = curated.get("platform", {})
    scanned = int(platform.get("bytes_scanned", 0))
    cost = scanned / (1024 ** 4) * USD_PER_TB
    print("\n-- athena cost --")
    print(f"  query_execution_ids: {len(platform.get('query_execution_ids', []))}")
    print(f"  total bytes scanned: {scanned:,}  ({scanned / (1024**3):.3f} GiB)")
    print(f"  estimated cost: USD {cost:.5f}  (USD {USD_PER_TB:.0f} per TB)")

    full_parity = totals_equal and not pm
    print(f"\nPARITY: {'100% (all products and scopes equal)' if full_parity else 'NOT 100% -- see mismatches above'}")
    print("PARITY_RESULT " + json.dumps(
        {"full_parity": full_parity, "product_mismatches": len(pm),
         "total_mismatches": len(result["total_mismatches"]),
         "bytes_scanned": scanned, "cost_usd": round(cost, 5)}, ensure_ascii=False))
    return 0 if full_parity else 1


if __name__ == "__main__":
    raise SystemExit(main())
