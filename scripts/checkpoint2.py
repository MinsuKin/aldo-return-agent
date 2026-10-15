#!/usr/bin/env python3
"""Checkpoint 2 (tasks.md task 10): the hero return flow through the cloud API, verified in DynamoDB.

    reset -> sign in -> message -> events -> quote -> approve (twice: the double click) -> events
    -> transaction, then read the table and assert exactly what the checkpoint asks for: ONE
    transaction, stock -1 at the chosen location (exchanges), the order line's returned/reserved
    quantity +1 and the approval consumed by that transaction.

Run against the deployed system (credentials only through the helper; the admin key is read from
the deployed secret and is never printed, neither is a token):

    cd /Users/minsu/Desktop/aldo-return-agent/backend
    ../scripts/with_aws_env.sh env SECRETS_ARN=aldo-demo/secrets DEMO_BASE_URL=https://<cloudfront> \\
        uv run python ../scripts/checkpoint2.py [--variant dc_stockout]

Exit code 0: every check passed. 1: a check failed. 2: the script could not start (no base URL,
no admin key). The base URL defaults to ``$DEMO_BASE_URL``, then to the SSM parameter
``/aldo-demo/gateway-url``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

REGION = "us-east-1"
DEFAULT_TABLE = "aldo-returns-demo"
DEFAULT_SECRET = "aldo-demo/secrets"
GATEWAY_URL_PARAM = "/aldo-demo/gateway-url"
VARIANTS = ("default", "dc_stockout", "all_stockout")
HERO_CUSTOMER = "DEMO-CUST-AVA"
HERO_ORDER = "DEMO-1001"
HERO_LINE = "DEMO-1001-L1"
HERO_MESSAGE = (
    "The Stessy shoes from my order DEMO-1001 are too small. They are unworn and I still have "
    "the box. I need a bigger size before Saturday for a wedding."
)
#: What to approve first: store pickup is the only option that meets a Saturday deadline.
OPTION_PREFERENCE = ("exchange_store_pickup", "exchange_ship_from_store", "exchange_ship_from_dc")
RETRY_STATUSES = (429, 502, 503, 504)


class CheckpointError(Exception):
    """A step could not even be carried out (the run ended without a quote, a route failed)."""


# ------------------------------------------------------------------------------- HTTP


@dataclass
class Http:
    """Minimal JSON client. Tokens and secrets are passed in and never printed."""

    base_url: str
    timeout_s: float = 30.0
    calls: int = 0

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        merged = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            merged["Content-Type"] = "application/json"
        if token:
            merged["Authorization"] = f"Bearer {token}"
        last: tuple[int, Any] = (0, None)
        for attempt in range(4):
            self.calls += 1
            request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=merged)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    status, raw = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read()
                exc.close()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = (0, {"error": {"code": "NETWORK", "message": type(exc).__name__}})
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = {"raw": raw[:200].decode("utf-8", "replace")}
            last = (status, parsed)
            if status in RETRY_STATUSES and attempt < 3:  # throttled or a cold start: try again
                time.sleep(1.5 * (attempt + 1))
                continue
            return last
        return last


def error_code(body: Any) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code", "?") if isinstance(error, dict) else "?"


# --------------------------------------------------------------------------- DynamoDB


class Table:
    """Independent reads of the table (raw keys, consistent), so the check does not reuse the code under test."""

    def __init__(self, table_name: str, resource: Any = None) -> None:
        import boto3

        self.table = (resource or boto3.resource("dynamodb", region_name=REGION)).Table(table_name)

    def item(self, pk: str, sk: str) -> dict[str, Any] | None:
        found = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        return plain(found) if found else None

    def count_prefix(self, prefix: str) -> int:
        from boto3.dynamodb.conditions import Attr

        total, start = 0, None
        while True:
            kwargs: dict[str, Any] = {
                "FilterExpression": Attr("pk").begins_with(prefix),
                "ProjectionExpression": "pk",
                "ConsistentRead": True,
            }
            if start:
                kwargs["ExclusiveStartKey"] = start
            page = self.table.scan(**kwargs)
            total += len(page.get("Items", []))
            start = page.get("LastEvaluatedKey")
            if not start:
                return total

    def snapshot(self, *, sku: str, location_id: str, order_id: str, line_id: str, quote_id: str) -> dict[str, Any]:
        stock = self.item(f"INV#{sku}", f"LOC#{location_id}") or {}
        line = self.item(f"ORDER#{order_id}", f"LINE#{line_id}") or {}
        approval = self.item(f"QUOTE#{quote_id}", "APPROVAL") or {}
        return {
            "transactions": self.count_prefix("TXN#"),
            "atp": stock.get("atp"),
            "reserved": line.get("returned_or_reserved_qty"),
            "consumed_txn_id": approval.get("consumed_txn_id"),
        }


def plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


# ------------------------------------------------------------------------------ report


@dataclass
class Report:
    rows: list[tuple[str, bool, str]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"[{'ok' if ok else 'FAIL':>4}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
        return bool(ok)

    @property
    def passed(self) -> bool:
        return bool(self.rows) and all(ok for _, ok, _ in self.rows)


# ------------------------------------------------------------------------------ the flow


def wait_for_run(http: Http, token: str, run_id: str, *, timeout_s: float, poll_s: float = 1.0) -> list[dict]:
    """Poll the run's events until the ``run_completed`` event arrives, then read them once more from 0.

    The status flips a moment before the last event is written, so the status alone is not the
    signal (the UI waits for the event too). The second read from 0 is the UI's own guard against
    an event that became visible out of order (G-11).
    """
    deadline = time.monotonic() + timeout_s
    after, seen, ended_without_event = 0, [], 0
    while True:
        status, body = http.request("GET", f"/api/runs/{run_id}/events?after={after}", token=token)
        if status != 200:
            raise CheckpointError(f"reading events of {run_id} gave HTTP {status} {error_code(body)}")
        seen.extend(body["events"])
        after = body["next_after"]
        if any(e["type"] == "run_completed" for e in seen):
            status, body = http.request("GET", f"/api/runs/{run_id}/events?after=0", token=token)
            if status != 200:
                raise CheckpointError(f"re-reading events of {run_id} gave HTTP {status}")
            return body["events"]
        if body["status"] in ("completed", "error"):
            ended_without_event += 1
            if ended_without_event >= 20:
                raise CheckpointError(f"run {run_id} is {body['status']} but never wrote run_completed")
        if time.monotonic() > deadline:
            raise CheckpointError(f"run {run_id} did not complete within {timeout_s:.0f} s")
        time.sleep(poll_s)


def first_event(events: list[dict], kind: str) -> dict | None:
    return next((e for e in events if e["type"] == kind), None)


def completed_ok(events: list[dict]) -> bool:
    done = first_event(events, "run_completed")
    return bool(done and done["payload"].get("status") == "completed")


def last_assistant_text(events: list[dict]) -> str:
    texts = [e["payload"].get("text", "") for e in events if e["type"] == "assistant_message"]
    return (texts[-1] if texts else "")[:300]


def pick_option(options: list[dict]) -> dict:
    for wanted in OPTION_PREFERENCE:
        for option in options:
            if option["type"] == wanted:
                return option
    return options[0]


def run_checkpoint(
    http: Http,
    table: Table,
    *,
    admin_key: str | None,
    variant: str,
    customer_id: str = HERO_CUSTOMER,
    order_id: str = HERO_ORDER,
    line_id: str = HERO_LINE,
    message: str = HERO_MESSAGE,
    timeout_s: float = 240.0,
    poll_s: float = 1.0,
    reset: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> Report:
    report = Report()
    started = clock()

    # 1. a known state
    if reset:
        status, body = http.request("POST", "/api/demo/reset", {"variant": variant}, headers={"X-Demo-Admin": admin_key or ""})
        if not report.check("reset", status == 200 and body.get("variant") == variant, f"HTTP {status} {variant}"):
            return report

    # 2. sign in as the hero persona
    status, body = http.request("POST", "/api/demo/sessions", {"customer_id": customer_id})
    if not report.check("sign in", status == 200 and "token" in body, f"HTTP {status}"):
        return report
    token = body["token"]
    status, body = http.request("GET", "/api/orders", token=token)
    mine = [o["order_id"] for o in body["orders"]] if status == 200 else []
    if not report.check("orders list the hero order", order_id in mine, f"{len(mine)} orders"):
        return report

    # 3. message -> run -> quote
    status, body = http.request("POST", "/api/conversations", {"order_id": order_id}, token=token)
    if not report.check("conversation created", status == 201, f"HTTP {status}"):
        return report
    conv_id = body["conv_id"]
    status, body = http.request("POST", f"/api/conversations/{conv_id}/messages", {"text": message, "order_id": order_id}, token=token)
    if not report.check("message accepted (202)", status == 202 and "run_id" in body, f"HTTP {status} {error_code(body)}"):
        return report
    run_id = body["run_id"]
    events = wait_for_run(http, token, run_id, timeout_s=max(30.0, timeout_s - (clock() - started)), poll_s=poll_s)
    quote_event = first_event(events, "quote_created")
    report.check("customer run completed", completed_ok(events), f"{len(events)} events")
    if not report.check("run produced a quote", quote_event is not None, "quote_created" if quote_event else last_assistant_text(events)):
        return report
    quote_id = quote_event["payload"]["quote_id"]
    status, quote = http.request("GET", f"/api/quotes/{quote_id}", token=token)
    if not report.check("quote readable", status == 200 and quote["options"], f"{len(quote.get('options', []))} options"):
        return report
    option = pick_option(quote["options"])
    other = next((o for o in quote["options"] if o["option_id"] != option["option_id"]), None)
    report.facts.update(quote_id=quote_id, option_type=option["type"], option_id=option["option_id"])

    # 4. nothing happens before the click (R7.6)
    where = dict(sku=option["sku"], location_id=option["location_id"], order_id=order_id, line_id=line_id, quote_id=quote_id)
    before = table.snapshot(**where)
    report.check("no transaction before approval", before["transactions"] == 0, f"{before['transactions']} transactions")

    # 5. the click, and the double click
    body_in = {"option_id": option["option_id"], "payload_hash": option["payload_hash"]}
    status, approved = http.request("POST", f"/api/quotes/{quote_id}/approve", body_in, token=token)
    if not report.check("approve accepted (202)", status == 202 and "run_id" in approved, f"HTTP {status} {error_code(approved)}"):
        return report
    approval_run = approved["run_id"]
    status, again = http.request("POST", f"/api/quotes/{quote_id}/approve", body_in, token=token)
    report.check(
        "second approve is a replay (200, same approval and run)",
        status == 200 and again.get("replayed") is True and again.get("run_id") == approval_run
        and again.get("approval_id") == approved["approval_id"],
        f"HTTP {status}",
    )
    if other is not None:
        status, conflict = http.request(
            "POST", f"/api/quotes/{quote_id}/approve",
            {"option_id": other["option_id"], "payload_hash": other["payload_hash"]}, token=token,
        )
        report.check("another option is refused (409 IDEMPOTENCY_CONFLICT)", status == 409 and error_code(conflict) == "IDEMPOTENCY_CONFLICT", f"HTTP {status}")

    # 6. the continuation run executes it
    events = wait_for_run(http, token, approval_run, timeout_s=max(30.0, timeout_s - (clock() - started)), poll_s=poll_s)
    report.check("approval run completed", completed_ok(events), f"{len(events)} events")
    created = first_event(events, "transaction_created")
    if not report.check("run created the transaction", created is not None, "transaction_created" if created else last_assistant_text(events)):
        return report
    transaction_id = created["payload"]["transaction_id"]
    status, txn = http.request("GET", f"/api/transactions/{transaction_id}", token=token)
    report.check(
        "transaction readable and simulated",
        status == 200 and txn.get("state") == "completed" and txn.get("simulated") is True and txn.get("order_id") == order_id,
        f"{transaction_id} {txn.get('option_type') if isinstance(txn, dict) else ''}",
    )
    report.facts.update(transaction_id=transaction_id)

    # 7. the table says the same
    after = table.snapshot(**where)
    stock_delta = -1 if option["type"].startswith("exchange_") else 0
    report.check("exactly one transaction in the table", after["transactions"] == 1, f"{after['transactions']}")
    report.check(
        f"stock {stock_delta:+d} at {option['location_id']}",
        before["atp"] is not None and after["atp"] == before["atp"] + stock_delta,
        f"{before['atp']} -> {after['atp']}",
    )
    report.check("order line reserved +1", after["reserved"] == (before["reserved"] or 0) + 1, f"{before['reserved']} -> {after['reserved']}")
    report.check("approval consumed by that transaction", after["consumed_txn_id"] == transaction_id, str(after["consumed_txn_id"]))
    done = first_event(events, "run_completed")
    report.facts["approval_run_metrics"] = {k: v for k, v in (done or {}).get("payload", {}).items() if k != "error_code"}
    report.facts["seconds"] = round(clock() - started, 1)
    return report


# ---------------------------------------------------------------------------- command line


def resolve_base_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("DEMO_BASE_URL", "").strip()
    if not url:
        import boto3

        try:
            url = boto3.client("ssm", region_name=REGION).get_parameter(Name=GATEWAY_URL_PARAM)["Parameter"]["Value"]
        except Exception as exc:
            raise CheckpointError(
                f"no base URL: pass --base-url, set DEMO_BASE_URL or publish {GATEWAY_URL_PARAM} ({type(exc).__name__})"
            ) from None
    return url.strip().rstrip("/")


def admin_key_from_secret() -> str:
    """The reset key: ``$DEMO_ADMIN_KEY``, else the deployed secret (``$SECRETS_ARN``). Never printed."""
    from returns_domain import config

    config.clear_secret_cache()
    return config.get_admin_key({**os.environ, "SECRETS_ARN": os.environ.get("SECRETS_ARN") or DEFAULT_SECRET})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", help="CloudFront URL (default: $DEMO_BASE_URL, then SSM)")
    parser.add_argument("--variant", choices=VARIANTS, default="default", help="stock variant to reset to")
    parser.add_argument("--table", default=os.environ.get("TABLE_NAME", DEFAULT_TABLE))
    parser.add_argument("--timeout", type=float, default=240.0, help="seconds for the whole flow")
    parser.add_argument("--message", default=HERO_MESSAGE)
    parser.add_argument("--skip-reset", action="store_true", help="use the current state (no admin key needed)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON at the end")
    args = parser.parse_args(argv)

    try:
        base_url = resolve_base_url(args.base_url)
        admin_key = None if args.skip_reset else admin_key_from_secret()
    except Exception as exc:  # no credentials, no secret: say which, never why in detail
        print(f"cannot start: {exc if isinstance(exc, CheckpointError) else type(exc).__name__}", file=sys.stderr)
        return 2

    print(f"Checkpoint 2 against {base_url}  variant={args.variant}  table={args.table}", flush=True)
    try:
        report = run_checkpoint(
            Http(base_url), Table(args.table), admin_key=admin_key, variant=args.variant,
            message=args.message, timeout_s=args.timeout, reset=not args.skip_reset,
        )
    except CheckpointError as exc:
        print(f"[FAIL] {exc}", flush=True)
        return 1
    passed = sum(1 for _, ok, _ in report.rows if ok)
    print(f"{'PASS' if report.passed else 'FAIL'}: {passed}/{len(report.rows)} checks", flush=True)
    if args.json:
        print(json.dumps({"passed": report.passed, "checks": [list(r) for r in report.rows], "facts": report.facts}, default=str))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
