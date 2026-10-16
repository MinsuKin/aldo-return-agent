#!/usr/bin/env python3
"""Upload the ALDO dataset to the data-platform bucket under raw/<table>/, scrubbed.

Streaming only: every file is read line by line with the csv module and re-serialised
to a temporary file, then uploaded with multipart put. No file is ever loaded whole, and
NO row or customer value is ever printed -- only headers (column names) and counts.

Scrubbing (design "Data handling"): the returns file drops the customer identifier
columns ``cnsmr_prof_sid`` and ``customer_uid`` while copying. The other four files are
copied as-is (they carry no customer identifier).

Tables and sources (dataset/ is read-only, never modified):
  orders       All OMS orders_Feb 2024 to Feb 2026.csv      (UTF-8 BOM on the header)
  returns      OMS Data_Feb24-Jan26_new.csv                 (DROP cnsmr_prof_sid, customer_uid)
  returnable   McGill - Returns - Returnable Flag 2024-2025.csv
  restocking   McGill - Returns - Restocking Fees 2024 - 2025.csv
  exchanges    McGill_Instore_return_exchanges.csv

All objects are written with SSE-S3 (ServerSideEncryption=AES256) to a private,
TLS-only bucket. The bucket name comes from the SSM config parameter (or --bucket).

Usage (through scripts/with_aws_env.sh):
    scripts/with_aws_env.sh python3 scripts/data_platform/upload_dataset.py [--bucket NAME] [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"
REGION = "us-east-1"
CONFIG_PARAM = "/aldo-demo/data-platform/config"
RAW_PREFIX = "raw"

# table -> (source filename, encoding, columns to drop)
TABLES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "orders": ("All OMS orders_Feb 2024 to Feb 2026.csv", "utf-8-sig", ()),
    "returns": (
        "OMS Data_Feb24-Jan26_new.csv",
        "utf-8-sig",
        ("cnsmr_prof_sid", "customer_uid"),
    ),
    "returnable": ("McGill - Returns - Returnable Flag 2024-2025.csv", "utf-8-sig", ()),
    "restocking": ("McGill - Returns - Restocking Fees 2024 - 2025.csv", "utf-8-sig", ()),
    "exchanges": ("McGill_Instore_return_exchanges.csv", "utf-8-sig", ()),
}

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def resolve_bucket(explicit: str | None) -> str:
    if explicit:
        return explicit
    import boto3

    ssm = boto3.client("ssm", region_name=REGION)
    raw = ssm.get_parameter(Name=CONFIG_PARAM)["Parameter"]["Value"]
    return json.loads(raw)["bucket"]


def _flat(value: str) -> str:
    """Athena's OpenCSVSerDe reads one record per line, so a line break inside a quoted field would split the record."""
    return value.replace("\r", " ").replace("\n", " ")


def scrub_to_temp(src: Path, encoding: str, drop: tuple[str, ...]) -> tuple[Path, list[str], list[str], int, int]:
    """Stream src -> a UTF-8 (no BOM) temp CSV, dropping `drop` columns and flattening line breaks inside fields.
    Returns (temp_path, out_header, dropped_present, data_rows, flattened_fields). Never holds the file in memory."""
    tmp = Path(tempfile.mkstemp(prefix="aldo_upload_", suffix=".csv")[1])
    dropped_present: list[str] = []
    out_header: list[str] = []
    rows = 0
    flattened = 0
    with src.open("r", newline="", encoding=encoding) as fin, tmp.open(
        "w", newline="", encoding="utf-8"
    ) as fout:
        reader = csv.reader(fin)
        writer = csv.writer(fout)
        header = next(reader)
        drop_idx = {i for i, col in enumerate(header) if col in drop}
        dropped_present = [col for col in header if col in drop]
        out_header = [col for i, col in enumerate(header) if i not in drop_idx]
        writer.writerow(out_header)
        for row in reader:
            kept = [v for i, v in enumerate(row) if i not in drop_idx] if drop_idx else row
            if any(("\n" in v or "\r" in v) for v in kept):
                flattened += sum(1 for v in kept if ("\n" in v or "\r" in v))
                kept = [_flat(v) for v in kept]
            writer.writerow(kept)
            rows += 1
    return tmp, out_header, dropped_present, rows, flattened


def upload(bucket: str, key: str, path: Path) -> None:
    import boto3
    from boto3.s3.transfer import TransferConfig

    s3 = boto3.client("s3", region_name=REGION)
    cfg = TransferConfig(multipart_threshold=8 * 1024 * 1024, multipart_chunksize=16 * 1024 * 1024)
    s3.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"ServerSideEncryption": "AES256"},
        Config=cfg,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket", default=None, help="override the bucket (default: SSM config)")
    parser.add_argument("--dry-run", action="store_true", help="scrub + count, do not upload")
    args = parser.parse_args(argv)

    bucket = resolve_bucket(args.bucket) if not args.dry_run else (args.bucket or "<dry-run>")
    print(f"bucket: {bucket}")
    print(f"{'table':<12} {'rows':>12} {'bytes':>14}  dropped_columns")
    grand_rows = 0
    grand_bytes = 0
    manifest: list[dict] = []
    for table, (fname, encoding, drop) in TABLES.items():
        src = DATASET / fname
        if not src.is_file():
            print(f"{table:<12} MISSING SOURCE: {fname}", file=sys.stderr)
            return 2
        tmp, out_header, dropped, rows, flattened = scrub_to_temp(src, encoding, drop)
        try:
            size = tmp.stat().st_size
            key = f"{RAW_PREFIX}/{table}/{table}.csv"
            if not args.dry_run:
                upload(bucket, key, tmp)
            grand_rows += rows
            grand_bytes += size
            dropped_text = ("drop " + ",".join(dropped)) if dropped else "(none)"
            print(f"{table:<12} {rows:>12,} {size:>14,}  {dropped_text}")
            print(f"             cols: {out_header}")
            if flattened:
                print(f"             line breaks inside fields flattened to spaces: {flattened:,}")
            manifest.append(
                {"table": table, "key": key, "rows": rows, "bytes": size,
                 "dropped": dropped, "columns": out_header, "flattened_fields": flattened}
            )
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    print(f"{'TOTAL':<12} {grand_rows:>12,} {grand_bytes:>14,}")
    if not args.dry_run:
        print(f"uploaded {len(manifest)} objects under s3://{bucket}/{RAW_PREFIX}/ (SSE-S3)")
    print("MANIFEST " + json.dumps({"bucket": bucket, "tables": manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
