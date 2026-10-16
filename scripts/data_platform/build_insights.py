#!/usr/bin/env python3
"""Run the Athena pipeline and write the curated fit_insights document to S3.

Steps (design "Architecture" + "The curated document"):
  1. (re)create the raw external tables         (sql/01_create_raw_tables.sql)
  2. drop + CTAS the Parquet curated tables      (sql/02_ctas_parquet.sql)
  3. run the fit-insights counting query         (sql/03_fit_insights.sql)
  4. shape the result into the SAME schema as fixtures/derived/fit_insights.json, with the
     signals / too_small_share recomputed by returns_domain.fit (the shared functions), plus
     the extra top-level `platform` block.
  5. write curated/fit_insights/v=<utc>/fit_insights.json  and  curated/fit_insights/latest.json

Counting rules are NOT re-implemented here: the SQL reproduces build_fit_insights.py's
normalisation and scoping, and the signal/share come from returns_domain.fit.classify /
small_share, so the Python side cannot drift from the bundled builder's definitions.

All objects are written with SSE-S3. No row or customer value is ever printed. Run through
scripts/with_aws_env.sh:
    scripts/with_aws_env.sh python3 scripts/data_platform/build_insights.py
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SQL_DIR = HERE / "sql"
REGION = "us-east-1"
WORKGROUP = "aldo-demo"
CONFIG_PARAM = "/aldo-demo/data-platform/config"
PIPELINE_VERSION = 1
CURATED_FIT_PREFIX = "curated/fit_insights"
LOCAL_COPY = ROOT / "infra" / "build" / "data-platform" / "last_curated_fit_insights.json"  # gitignored, aggregates only

# Make returns_domain importable (shared signal/share functions) and reuse the demo alias overlay of the bundled file.
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from returns_domain import fit  # noqa: E402
import add_fit_aliases  # noqa: E402

SCOPES = ("ALDO_CA", "ALL")

FILTER_TEXT = (
    "reason (whitespace collapsed, lower-cased, 'return.page.reasons.' prefix dropped) "
    "equals 'too small' or 'too big'; scopes: store_banner == ALDO_CA and ALL banners; "
    "rows with a blank orderitem_product_name are excluded from per-product counts"
)


def ssm_config() -> dict:
    ssm = boto3.client("ssm", region_name=REGION)
    return json.loads(ssm.get_parameter(Name=CONFIG_PARAM)["Parameter"]["Value"])


def load_sql(name: str, db: str, bucket: str) -> str:
    text = (SQL_DIR / name).read_text(encoding="utf-8")
    return text.replace("${DB}", db).replace("${BUCKET}", bucket)


def split_statements(sql: str) -> list[str]:
    """Split a .sql file into individual statements on ';' at end of logical statement.
    Lines that are entirely comments are dropped (Athena's DDL parser is stricter about comments than
    its query parser); empty trailing chunks are dropped too. Our SQL has no ';' inside string literals
    and no comment after code on the same line."""
    out: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            # strip a trailing ';'
            stmt = stmt[:-1].strip() if stmt.endswith(";") else stmt
            # drop pure-comment chunks
            if any(not ln.strip().startswith("--") and ln.strip() for ln in stmt.splitlines()):
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail and any(not ln.strip().startswith("--") and ln.strip() for ln in tail.splitlines()):
        out.append(tail.rstrip(";").strip())
    return out


class Athena:
    def __init__(self, bucket: str):
        self.client = boto3.client("athena", region_name=REGION)
        self.bucket = bucket
        self.exec_ids: list[str] = []
        self.bytes_scanned = 0
        self.per_query: list[dict] = []

    def run(self, sql: str, label: str) -> str:
        resp = self.client.start_query_execution(
            QueryString=sql,
            WorkGroup=WORKGROUP,
            ResultConfiguration={"OutputLocation": f"s3://{self.bucket}/athena-results/"},
        )
        qid = resp["QueryExecutionId"]
        self.exec_ids.append(qid)
        state = "RUNNING"
        info = {}
        while state in ("QUEUED", "RUNNING"):
            time.sleep(1.0)
            info = self.client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = info["Status"]["State"]
        stats = info.get("Statistics", {})
        scanned = int(stats.get("DataScannedInBytes", 0) or 0)
        millis = int(stats.get("TotalExecutionTimeInMillis", 0) or 0)
        self.bytes_scanned += scanned
        self.per_query.append(
            {"label": label, "query_execution_id": qid, "state": state,
             "bytes_scanned": scanned, "seconds": round(millis / 1000.0, 2)}
        )
        if state != "SUCCEEDED":
            reason = info["Status"].get("StateChangeReason", "")
            raise RuntimeError(f"query '{label}' {state}: {reason}")
        return qid

    def rows(self, qid: str):
        """Yield result rows (list[str|None]) EXCLUDING the header row."""
        paginator = self.client.get_paginator("get_query_results")
        first = True
        for page in paginator.paginate(QueryExecutionId=qid):
            for r in page["ResultSet"]["Rows"]:
                cells = [c.get("VarCharValue") for c in r["Data"]]
                if first:
                    first = False
                    continue
                yield cells


def _share(too_small: int, too_big: int) -> float | None:
    """too_small / (too_small + too_big) at full precision, exactly as scripts/build_fit_insights.py stores it.

    (returns_domain.fit.small_share rounds to 4 places for display; the bundled file does not, and the two modes must hand the
    gateway identical numbers.)"""
    n_fit = too_small + too_big
    return (too_small / n_fit) if n_fit else None


def shape_document(rows, source_file_sha: dict, athena: Athena, aliases: dict | None = None) -> dict:
    """Turn fit-query rows into the fit_insights.json schema + the platform block."""
    # columns: row_kind, scope, product_name, total_returns, too_small, too_big,
    #          lines_without_product_name, products_count
    products: dict[str, dict[str, dict]] = {}
    totals: dict[str, dict] = {}
    for row_kind, scope, product_name, total_returns, too_small, too_big, lwpn, pcount in rows:
        ts = int(too_small or 0)
        tb = int(too_big or 0)
        tr = int(total_returns or 0)
        if row_kind == "product":
            entry = products.setdefault(product_name, {})
            entry[scope] = {
                "too_small": ts,
                "too_big": tb,
                "total_returns": tr,
                "too_small_share": _share(ts, tb),
                "n_fit": ts + tb,
                "signal": fit.classify(ts, tb),
            }
        else:  # total
            totals[scope] = {
                "return_lines": tr,
                "too_small": ts,
                "too_big": tb,
                "lines_without_product_name": int(lwpn or 0),
                "products": int(pcount or 0),
            }

    # Fill a zero scope entry for any product missing a scope (matches builder's scope_entry(None)).
    def zero(scope):
        return {"too_small": 0, "too_big": 0, "total_returns": 0,
                "too_small_share": None, "n_fit": 0, "signal": fit.classify(0, 0)}

    shaped_products = {}
    for name in sorted(products):
        shaped_products[name] = {s: products[name].get(s, zero(s)) for s in SCOPES}

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {
        "generated_at": now,
        "provenance": "derived",
        "source": {
            "kind": "aws-athena",
            **source_file_sha,
        },
        "filter": FILTER_TEXT,
        "thresholds": {
            "min_n": fit.MIN_N,
            "runs_small_min_share": fit.RUNS_SMALL_MIN_SHARE,
            "runs_large_max_share": fit.RUNS_LARGE_MAX_SHARE,
            "n_definition": "n_fit = too_small + too_big",
        },
        "totals": {s: totals.get(s, {"return_lines": 0, "too_small": 0, "too_big": 0,
                                     "lines_without_product_name": 0, "products": 0})
                   for s in SCOPES},
        "products": shaped_products,
        "platform": {
            "engine": "athena",
            "workgroup": WORKGROUP,
            "query_execution_ids": list(athena.exec_ids),
            "bytes_scanned": athena.bytes_scanned,
            "pipeline_version": PIPELINE_VERSION,
        },
    }
    if aliases:
        # The bundled file carries the demo names (ALDO Men 001 -> Toney) as extra entries; the curated document must too,
        # otherwise a lookup by the demo name would work in static mode and fail in platform mode.
        doc = add_fit_aliases.add_aliases(doc, aliases)
        doc["platform"]["aliases_applied"] = len(aliases)
    return doc


def put_json(bucket: str, key: str, obj: dict) -> str:
    s3 = boto3.client("s3", region_name=REGION)
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    resp = s3.put_object(
        Bucket=bucket, Key=key, Body=body,
        ContentType="application/json", ServerSideEncryption="AES256",
    )
    return resp["ETag"].strip('"')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bucket", default=None, help="override the bucket (default: SSM config)")
    parser.add_argument("--skip-ddl", action="store_true", help="assume raw tables exist")
    args = parser.parse_args(argv)

    cfg = ssm_config()
    bucket = args.bucket or cfg["bucket"]
    db = cfg["database"]
    print(f"bucket={bucket} database={db} workgroup={WORKGROUP}")

    athena = Athena(bucket)

    if not args.skip_ddl:
        for stmt in split_statements(load_sql("01_create_raw_tables.sql", db, bucket)):
            athena.run(stmt, "ddl_raw")

    # idempotent CTAS: drop targets first
    for tbl in ("curated_returns", "curated_orders"):
        athena.run(f"DROP TABLE IF EXISTS {db}.{tbl}", f"drop_{tbl}")
    for stmt in split_statements(load_sql("02_ctas_parquet.sql", db, bucket)):
        label = "ctas_returns" if "curated_returns" in stmt else "ctas_orders"
        athena.run(stmt, label)

    fit_sql = split_statements(load_sql("03_fit_insights.sql", db, bucket))[0]
    qid = athena.run(fit_sql, "fit_insights")

    # source identity: name + sha of the local returns CSV so the curated doc records provenance
    import hashlib
    returns_csv = ROOT / "dataset" / "OMS Data_Feb24-Jan26_new.csv"
    sha = hashlib.sha256()
    with returns_csv.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
    source_file_sha = {"file": returns_csv.name, "sha256": sha.hexdigest()}

    aliases = add_fit_aliases.load_mapping(add_fit_aliases.DEFAULT_ALIASES)
    doc = shape_document(athena.rows(qid), source_file_sha, athena, aliases)

    version = datetime.now(timezone.utc).strftime("v=%Y-%m-%dT%H-%M-%SZ")
    key = f"{CURATED_FIT_PREFIX}/{version}/fit_insights.json"
    etag = put_json(bucket, key, doc)
    latest = {"key": key, "etag": etag, "generated_at": doc["generated_at"]}
    put_json(bucket, f"{CURATED_FIT_PREFIX}/latest.json", latest)

    # Save a local copy for the parity report (no customer data; aggregates only).
    out_local = LOCAL_COPY
    out_local.parent.mkdir(parents=True, exist_ok=True)
    out_local.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

    print("ATHENA QUERIES:")
    for q in athena.per_query:
        print(f"  {q['label']:<16} {q['state']:<10} scanned={q['bytes_scanned']:>12,} "
              f"bytes  {q['seconds']:>6}s  {q['query_execution_id']}")
    print(f"total bytes scanned: {athena.bytes_scanned:,}")
    print(f"ALDO_CA products: {doc['totals']['ALDO_CA']['products']}  "
          f"ALL products: {doc['totals']['ALL']['products']}")
    print(f"wrote s3://{bucket}/{key} (etag {etag})")
    print(f"wrote s3://{bucket}/{CURATED_FIT_PREFIX}/latest.json")
    print(f"local copy: {out_local}")
    print("RESULT " + json.dumps(latest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
