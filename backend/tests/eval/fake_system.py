"""In-memory simulator of the deployed ALDO Fit Gateway for scripts/eval_scenarios.py (--dry-run and tests).

It speaks the HTTP contract of contracts.md (c), PIVOT_ADDENDUM 5 and 10.1 and writes DynamoDB items (moto
tables) in the shapes of contracts.md A.6-A.13 and F.3-F.9, so the eval's assertions run against real item
layouts. No model, no network: the "agents" are scripted. `faults` switch on deliberate defects, which the
tests use to prove each assertion can fail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from boto3.dynamodb.conditions import Key

from returns_domain import auth, policy
from returns_domain.errors import DomainError, ErrorCode

ALDO_TABLE = "aldo-returns-demo"
PA_TABLE = "aldo-personal-agent-demo"
REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_TODAY = date(2026, 10, 16)
DEADLINE = "2026-10-17"
AGENT_TOKENS = {"input_tokens": 4800, "output_tokens": 520}
BOT_TOKENS = {"input_tokens": 3100, "output_tokens": 260}
MODEL_ID = "us.anthropic.claude-sonnet-5-5"
ALL_FAULTS = frozenset({
    "double_txn", "no_atp_decrement", "early_side_effect", "approve_replay_new_run", "no_case", "no_audit_on_denied",
    "bot_places_order", "accept_invalid_choice", "leak_retailer", "dup_writeback", "skip_reset_cleanup",
})


def create_tables(resource: Any) -> None:
    schema = [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]
    attrs = [{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk", "gsi1pk", "gsi1sk")]
    resource.create_table(
        TableName=ALDO_TABLE, KeySchema=schema, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[{"IndexName": "GSI1", "Projection": {"ProjectionType": "ALL"}, "KeySchema": [
            {"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}]}])
    resource.create_table(TableName=PA_TABLE, KeySchema=schema, AttributeDefinitions=attrs[:2], BillingMode="PAY_PER_REQUEST")


def _ddb(obj: Any) -> Any:
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, Mapping):
        return {k: _ddb(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_ddb(v) for v in obj]
    return obj


def _py(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, Mapping):
        return {k: _py(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_py(v) for v in obj]
    return obj


def new_id(prefix: str, n: int = 6) -> str:
    return f"{prefix}{uuid.uuid4().hex[:n].upper()}"


def now_iso(delta_seconds: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta_seconds)).replace(microsecond=0).isoformat()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Req:
    def __init__(self, method: str, path: str, query: dict, headers: Mapping[str, str], raw: bytes, params: dict) -> None:
        self.method, self.path, self.query, self.raw, self.params = method, path, query, raw, params
        self.headers = {k.lower(): v for k, v in headers.items()}  # HTTP header names are case-insensitive

    @property
    def json(self) -> dict:
        try:
            data = json.loads(self.raw.decode("utf-8")) if self.raw else {}
        except ValueError:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "body is not JSON") from None
        if not isinstance(data, dict):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "body must be an object")
        return data

    @property
    def authorization(self) -> str | None:
        return self.headers.get("authorization")


class FakeSystem:
    """HTTP-shaped fake: `transport` plugs into scripts/eval_scenarios.ApiClient."""

    ROUTES = [
        ("POST", r"/demo/sessions", "h_session"), ("POST", r"/demo/reset", "h_reset"),
        ("POST", r"/demo/simulate-delivery", "h_simulate_delivery"),
        ("POST", r"/conversations", "h_conv_create"), ("POST", r"/conversations/(?P<conv>[^/]+)/messages", "h_conv_message"),
        ("GET", r"/runs/(?P<run>[^/]+)/events", "h_events"), ("GET", r"/quotes/(?P<qid>[^/]+)", "h_quote"),
        ("POST", r"/quotes/(?P<qid>[^/]+)/approve", "h_approve"),
        ("POST", r"/gateway/size-assist", "h_size_assist"), ("POST", r"/gateway/size-decision", "h_size_decision"),
        ("GET", r"/purchases/(?P<rid>[^/]+)", "h_purchase_view"), ("POST", r"/purchases/(?P<rid>[^/]+)/confirm", "h_purchase_confirm"),
        ("POST", r"/personal-agent/conversations", "h_pa_conv"), ("POST", r"/personal-agent/conversations/(?P<conv>[^/]+)/messages", "h_pa_message"),
        ("GET", r"/personal-agent/memory", "h_pa_memory"), ("POST", r"/personal-agent/memory-updates", "h_memory_updates"),
        ("GET", r"/orders", "h_orders"),
    ]

    def __init__(self, tables: Any, *, facts: Any, faults: frozenset[str] = frozenset()) -> None:
        unknown = set(faults) - ALL_FAULTS
        if unknown:
            raise ValueError(f"unknown faults: {sorted(unknown)}")
        self.t, self.facts, self.faults = tables, facts, set(faults)
        self.token_key = b"fake-token-key-0123456789abcdef"
        self.admin_key = "fake-admin-key-0123456789"
        self.writeback_key = b"fake-writeback-key-0123456789ab"
        self.latency_polls = 2  # event polls that still show "running" per run (exercises the eval's polling)
        self._seq: dict[tuple[str, str], int] = {}
        self._hidden: dict[str, int] = {}
        self._routes = [(m, re.compile(p + r"$"), h) for m, p, h in self.ROUTES]

    # ---------------------------------------------------------------- HTTP plumbing

    def transport(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float):
        parts = urlsplit(url)
        path = parts.path[len("/api"):] if parts.path.startswith("/api/") else parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        for route_method, pattern, handler in self._routes:
            match = pattern.match(path)
            if route_method == method and match:
                req = Req(method, path, query, dict(headers), body or b"", match.groupdict())
                try:
                    status, payload = getattr(self, handler)(req)
                except DomainError as exc:
                    err = {"code": exc.code, "message": exc.message, "retryable": exc.retryable, "request_id": "fake"}
                    err.update(exc.details)
                    status, payload = exc.http_status, {"error": err}
                return status, json.dumps(payload, default=str).encode("utf-8"), {"content-type": "application/json"}
        return 404, json.dumps({"error": {"code": "NOT_FOUND", "message": f"no route {method} {path}"}}).encode(), {}

    def session_customer(self, req: Req) -> str:
        return auth.customer_id_from_authorization(req.authorization, "session", key=self.token_key)

    def delegated_claims(self, req: Req) -> Any:
        return auth.claims_from_authorization(req.authorization, "delegated_gateway", key=self.token_key)

    # ---------------------------------------------------------------- table helpers

    def get(self, pk: str, sk: str, table: str = "aldo") -> dict | None:
        tbl = self.t.aldo if table == "aldo" else self.t.pa
        item = tbl.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        return _py(item) if item else None

    def put(self, item: dict, table: str = "aldo", *, only_new: bool = False) -> bool:
        tbl = self.t.aldo if table == "aldo" else self.t.pa
        try:
            if only_new:
                tbl.put_item(Item=_ddb(item), ConditionExpression="attribute_not_exists(pk)")
            else:
                tbl.put_item(Item=_ddb(item))
            return True
        except tbl.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    def update(self, pk: str, sk: str, values: dict, table: str = "aldo") -> None:
        tbl = self.t.aldo if table == "aldo" else self.t.pa
        names = {f"#a{i}": k for i, k in enumerate(values)}
        vals = {f":v{i}": _ddb(v) for i, v in enumerate(values.values())}
        expr = "SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(values)))
        tbl.update_item(Key={"pk": pk, "sk": sk}, UpdateExpression=expr, ExpressionAttributeNames=names,
                        ExpressionAttributeValues=vals, ConditionExpression="attribute_exists(pk)")

    def add(self, pk: str, sk: str, attr: str, delta: int, extra: dict | None = None) -> None:
        names, vals, sets = {"#n": attr}, {":d": delta}, ""
        if extra:
            names.update({f"#x{i}": k for i, k in enumerate(extra)})
            vals.update({f":x{i}": _ddb(v) for i, v in enumerate(extra.values())})
            sets = " SET " + ", ".join(f"#x{i} = :x{i}" for i in range(len(extra)))
        self.t.aldo.update_item(Key={"pk": pk, "sk": sk}, UpdateExpression="ADD #n :d" + sets, ExpressionAttributeNames=names,
                                ExpressionAttributeValues=vals, ConditionExpression="attribute_exists(pk)")

    def query(self, pk: str, sk_prefix: str | None = None, table: str = "aldo") -> list[dict]:
        tbl = self.t.aldo if table == "aldo" else self.t.pa
        cond = Key("pk").eq(pk) & Key("sk").begins_with(sk_prefix) if sk_prefix else Key("pk").eq(pk)
        return [_py(i) for i in tbl.query(KeyConditionExpression=cond, ConsistentRead=True)["Items"]]

    def scan(self, table: str = "aldo") -> list[dict]:
        tbl = self.t.aldo if table == "aldo" else self.t.pa
        items, kwargs = [], {"ConsistentRead": True}
        while True:
            page = tbl.scan(**kwargs)
            items += [_py(i) for i in page["Items"]]
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def audit(self, action: str, refs: dict, actor: str = "api") -> None:
        if "no_audit_on_denied" in self.faults:
            return
        self.put({"pk": f"AUDIT#{date.today().isoformat()}", "sk": f"{now_iso()}#{uuid.uuid4().hex}", "actor": actor,
                  "action": action, "outcome": "denied", "refs": refs, "ts": now_iso()})

    # ---------------------------------------------------------------- sessions and reset

    def h_session(self, req: Req):
        cid = req.json.get("customer_id")
        profile = self.get(f"CUST#{cid}", "PROFILE")
        if not profile:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "unknown persona")
        token = auth.sign_token(cid, key=self.token_key)
        return 200, {"token": token.token, "expires_at": token.to_dict()["expires_at"],
                     "customer": {"customer_id": cid, "display_name": profile["display_name"], "home_region": profile["home_region"]}}

    def h_reset(self, req: Req):
        auth.verify_admin_secret(req.headers.get("x-demo-admin"), expected=self.admin_key)
        variant = req.json.get("variant", "default")
        if variant not in ("default", "dc_stockout", "all_stockout"):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "unknown variant")
        deleted, written = self.reset(variant)
        return 200, {"ok": True, "variant": variant, "deleted": deleted, "written": written}

    def reset(self, variant: str) -> tuple[int, int]:
        deleted = 0
        for table, tbl in (("aldo", self.t.aldo), ("pa", self.t.pa)):
            keep_runtime = "skip_reset_cleanup" in self.faults and table == "aldo"
            with tbl.batch_writer() as batch:
                for item in self.scan(table):
                    if keep_runtime and not item["pk"].startswith(("CUST#", "PRODUCT#", "LOC#", "INV#")) and not (
                            item["pk"].startswith("ORDER#DEMO-1")):
                        continue
                    batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                    deleted += 1
        self._seq.clear()
        self._hidden.clear()
        root = Path(os.environ.get("FIXTURES_DIR") or REPO_ROOT / "fixtures")
        load = lambda name: json.loads((root / name).read_text(encoding="utf-8"))  # noqa: E731
        items: list[dict] = []
        for c in load("demo/customers.json")["customers"]:
            items.append({"pk": f"CUST#{c['customer_id']}", "sk": "PROFILE", **c})
        for o in load("demo/orders.json")["orders"]:
            meta = {k: v for k, v in o.items() if k != "lines"}
            items.append({**meta, "pk": f"ORDER#{o['order_id']}", "sk": "META", "gsi1pk": f"CUST#{o['customer_id']}",
                          "gsi1sk": f"ORDER#{o['ordered_at']}#{o['order_id']}"})
            items += [{**line, "pk": f"ORDER#{o['order_id']}", "sk": f"LINE#{line['line_id']}", "order_id": o["order_id"]} for line in o["lines"]]
        for p in load("demo/products.json")["products"]:
            items.append({**p, "pk": f"PRODUCT#{p['sku']}", "sk": "META", "gsi1pk": f"STYLE#{p['style']}#{p['color']}",
                          "gsi1sk": f"SIZE#{float(p['size']):05.1f}"})
        items += [{**loc, "pk": f"LOC#{loc['location_id']}", "sk": "META"} for loc in load("demo/locations.json")["locations"]]
        inv_doc = load("demo/inventory.json")
        rows = {(r["sku"], r["location_id"]): dict(r, version=1) for r in inv_doc["base"]}
        for over in inv_doc["variants"][variant]["overrides"]:
            rows[(over["sku"], over["location_id"])]["atp"] = over["atp"]
        items += [{**r, "pk": f"INV#{r['sku']}", "sk": f"LOC#{r['location_id']}"} for r in rows.values()]
        with self.t.aldo.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=_ddb(item))
        pa_items = []
        for owner in load("personal_agent/memory.json")["owners"]:
            for m in owner["memory"]:
                pa_items.append({"pk": f"OWNER#{owner['owner_id']}", "sk": f"MEM#{m['memory_id']}", **m})
        with self.t.pa.batch_writer() as batch:
            for item in pa_items:
                batch.put_item(Item=_ddb(item))
        self.audit("reset", {"variant": variant}, actor="admin")
        return deleted, len(items) + len(pa_items)

    # ---------------------------------------------------------------- events and runs

    def next_seq(self, pk: str, sk: str = "META") -> int:
        self._seq[(pk, sk)] = self._seq.get((pk, sk), 0) + 1
        return self._seq[(pk, sk)]

    def emit(self, run_id: str, type_: str, payload: dict, *, actor: str = "aldo_agent", owner: str | None = None) -> int:
        if owner:  # personal-agent run: events live in the personal-agent table
            seq = self.next_seq(f"OWNER#{owner}", f"PRUN#{run_id}")
            self.put({"pk": f"OWNER#{owner}", "sk": f"PRUN#{run_id}#EVT#{seq:06d}", "type": type_, "ts": now_iso(),
                      "actor": actor, "payload": payload}, table="pa")
            return seq
        seq = self.next_seq(f"RUN#{run_id}")
        self.put({"pk": f"RUN#{run_id}", "sk": f"EVT#{seq:06d}", "type": type_, "ts": now_iso(), "actor": actor, "payload": payload})
        return seq

    def emit_a2a(self, customer_id: str, type_: str, payload: dict, *, actor: str) -> None:
        pk = f"RUN#A2A-{customer_id}"
        if self.get(pk, "META") is None:
            self.put({"pk": pk, "sk": "META", "status": "open", "seq": 0})
        seq = self.next_seq(pk)
        self.put({"pk": pk, "sk": f"EVT#{seq:06d}", "type": type_, "ts": now_iso(), "actor": actor, "payload": payload})

    def start_run(self, run_id: str, customer_id: str, conv_id: str, kind: str, *, owner: str | None = None) -> None:
        meta = {"customer_id": customer_id, "conv_id": conv_id, "kind": kind, "status": "running", "seq": 0, "created_at": now_iso()}
        if owner:
            self.put({"pk": f"OWNER#{owner}", "sk": f"PRUN#{run_id}#META", **meta}, table="pa")
        else:
            self.put({"pk": f"RUN#{run_id}", "sk": "META", **meta})
        self._hidden[run_id] = self.latency_polls

    def finish_run(self, run_id: str, tokens: Mapping[str, int], *, owner: str | None = None, status: str = "completed") -> None:
        self.emit(run_id, "run_completed", {"status": status, "rounds": 5, "tool_count": 6, **tokens, "total_latency_ms": 9100,
                                            "model_id_used": MODEL_ID, "fallback_used": False, "error_code": None}, owner=owner,
                  actor="personal_agent" if owner else "aldo_agent")
        key = ({"pk": f"OWNER#{owner}", "sk": f"PRUN#{run_id}#META"}, "pa") if owner else ({"pk": f"RUN#{run_id}", "sk": "META"}, "aldo")
        self.update(key[0]["pk"], key[0]["sk"], {"status": status}, table=key[1])

    def h_events(self, req: Req):
        run_id, after = req.params["run"], int(req.query.get("after", 0))
        if run_id.startswith("PRUN-"):
            owner = self.session_customer(req)
            meta = self.get(f"OWNER#{owner}", f"PRUN#{run_id}#META", "pa")
            rows = [(int(i["sk"].rsplit("#", 1)[1]), i) for i in self.query(f"OWNER#{owner}", f"PRUN#{run_id}#EVT#", "pa")]
        else:
            customer = self.session_customer(req)
            meta = self.get(f"RUN#{run_id}", "META")
            if meta and meta["customer_id"] != customer:
                raise DomainError(ErrorCode.NOT_AUTHORIZED, "not your run")
            rows = [(int(i["sk"][4:]), i) for i in self.query(f"RUN#{run_id}", "EVT#")]
        if meta is None:
            raise DomainError(ErrorCode.NOT_FOUND, "run not found")
        status = meta["status"]
        if self._hidden.get(run_id, 0) > 0:
            self._hidden[run_id] -= 1
            rows, status = rows[:1], "running"
        events = [{"seq": s, "type": i["type"], "ts": i["ts"], "actor": i.get("actor"), "payload": i["payload"]} for s, i in rows if s > after]
        return 200, {"run_id": run_id, "status": status, "events": events, "next_after": events[-1]["seq"] if events else after}

    # ---------------------------------------------------------------- ALDO returns agent (scripted)

    def h_orders(self, req: Req):
        self.session_customer(req)  # session tokens only; delegated tokens get 403 here
        return 200, {"orders": []}

    def owned_order(self, cid: str, order_id: str) -> dict:
        order = self.get(f"ORDER#{order_id}", "META")
        if order is None:
            raise DomainError(ErrorCode.ORDER_NOT_FOUND, "order not found")
        if order["customer_id"] != cid:
            self.audit("access_denied", {"resource_type": "order", "resource_id": order_id}, actor=cid)
            raise DomainError(ErrorCode.NOT_AUTHORIZED, "Order not found for this customer.")
        return order

    def h_conv_create(self, req: Req):
        cid, order_id = self.session_customer(req), req.json.get("order_id")
        if order_id:
            self.owned_order(cid, order_id)
        conv = new_id("CONV-", 10)
        self.put({"pk": f"CONV#{conv}", "sk": "META", "customer_id": cid, "selected_order_id": order_id, "messages": "[]"})
        return 201, {"conv_id": conv}

    def h_conv_message(self, req: Req):
        cid, body = self.session_customer(req), req.json
        conv = self.get(f"CONV#{req.params['conv']}", "META")
        if not conv or conv["customer_id"] != cid:
            raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
        run_id = new_id("RUN-", 10)
        self.start_run(run_id, cid, req.params["conv"], "customer")
        self.aldo_turn(cid, run_id, body.get("order_id"), str(body.get("text", "")))
        return 202, {"run_id": run_id, "conv_id": req.params["conv"]}

    def tool(self, run_id: str, name: str, ok: bool = True, code: str | None = None, evidence: dict | None = None) -> None:
        self.emit(run_id, "tool_call", {"tool": name, "args": {}, "round": 1})
        self.emit(run_id, "tool_result", {"tool": name, "ok": ok, "code": code, "latency_ms": 40, "evidence": evidence or {}, "source_refs": [], "provenance": "synthetic", "round": 1})

    def say(self, run_id: str, text: str) -> None:
        self.emit(run_id, "assistant_message", {"text": text})
        self.finish_run(run_id, AGENT_TOKENS)

    def aldo_turn(self, cid: str, run_id: str, selected: str | None, text: str) -> None:
        self.emit(run_id, "run_started", {"kind": "customer", "model_id": MODEL_ID, "max_rounds": 8})
        found = re.findall(r"DEMO-(?:A2A-[0-9A-F]{6}|\d{4})", text)
        target = selected or (found[0] if found else None)
        if target is None:
            return self.say(run_id, "Which order is this about?")
        self.tool(run_id, "get_order_context")
        try:
            order = self.owned_order(cid, target)
        except DomainError as exc:
            self.tool(run_id, "get_order_context", ok=False, code=exc.code)
            return self.say(run_id, "I can only see the signed-in customer's orders.")
        line = next(i for i in self.query(f"ORDER#{target}", "LINE#"))
        reason = ("QUALITY_DAMAGE" if re.search(r"snapped|broke|defect", text, re.I) else
                  "CHANGE_OF_MIND" if "changed my mind" in text else "FIT_SIZE")
        condition = "damaged" if "damaged" in text else "unworn" if "unworn" in text else "worn" if "worn" in text else "unknown"
        direction = "too_small" if re.search(r"too small|tight", text, re.I) else "too_big" if re.search(r"too big|loose", text, re.I) else "unknown"
        result = policy.evaluate(order, line, 1, reason, condition).to_dict()
        pchk = new_id("PCHK-", 8)
        self.put({"pk": f"PCHK#{pchk}", "sk": "META", "customer_id": cid, "order_id": target, "line_id": line["line_id"], "qty": 1,
                  "reason_category": reason, "condition": condition, "size_direction": direction, "refund_cents": line["unit_paid_cents"],
                  "result": result, "policy_version": result["policy_version"], "created_at": now_iso()})
        self.tool(run_id, "evaluate_return_policy", evidence={"decision": result["decision"], "policy_check_id": pchk})
        if result["decision"] == "needs_review":
            if "no_case" not in self.faults:
                case = new_id("DEMO-CASE-")
                self.put({"pk": f"CASE#{case}", "sk": "META", "customer_id": cid, "order_id": target, "line_id": line["line_id"],
                          "reason_code": "POLICY_REVIEW", "summary": "Defect on a final-sale item", "policy_result": result,
                          "rule_ids": result["rule_ids"], "status": "open", "created_at": now_iso(), "run_id": run_id, "gsi1pk": "CASES", "gsi1sk": now_iso()})
                self.emit(run_id, "case_created", {"case_id": case, "reason_code": "POLICY_REVIEW"})
            return self.say(run_id, "A person will review this.")
        if result["decision"] != "eligible" or reason != "FIT_SIZE":
            return self.say(run_id, "This item cannot be returned under the demo policy.")
        self.make_quote(cid, run_id, order, line, pchk, text, direction)


    def locations(self) -> dict[str, dict]:
        return {i["location_id"]: i for i in self.scan() if i["pk"].startswith("LOC#")}

    def make_quote(self, cid: str, run_id: str, order: dict, line: dict, pchk: str, text: str, direction: str) -> None:
        profile = self.get(f"CUST#{cid}", "PROFILE")
        sizes = re.findall(r"size (\d+(?:\.\d)?)", text)
        wanted = f"{float(sizes[-1]):.1f}" if sizes else f"{float(line['size']) + (0.5 if direction != 'too_big' else -0.5):.1f}"
        sku = re.sub(r"-\d{3}$", f"-{round(float(wanted) * 10):03d}", line["sku"])
        deadline = DEADLINE if re.search(r"saturday", text, re.I) else None
        locs, stock = self.locations(), {i["sk"][4:]: i for i in self.query(f"INV#{sku}")}
        raw: list[dict] = []

        def option(type_: str, loc_id: str, eta: date, *, exchange: bool, fee: int = 0) -> dict:
            refund = 0 if exchange else line["unit_paid_cents"] - fee
            return {"type": type_, "sku": sku if exchange else line["sku"], "replacement_size": wanted if exchange else None,
                    "location_id": loc_id, "location_name": locs[loc_id]["name"], "qty": 1, "refund_cents": refund, "fee_cents": fee,
                    "currency": "CAD", "eta_date": eta.isoformat(),
                    "meets_deadline": (eta.isoformat() <= deadline) if (exchange and deadline) else None}
        dc = next(i for i, loc in locs.items() if loc["type"] == "DC")
        stores = sorted((i for i, loc in locs.items() if loc["type"] == "STORE"))
        home = [s for s in stores if locs[s]["region"] == profile["home_region"]]
        if int(stock.get(dc, {}).get("atp", 0)) >= 1:
            raw.append(option("exchange_ship_from_dc", dc, DEMO_TODAY + timedelta(days=locs[dc]["ship_days"]), exchange=True))
        ship = sorted((s for s in home if int(stock.get(s, {}).get("atp", 0)) >= 1), key=lambda s: (locs[s]["ship_days"], s))[:1]
        raw += [option("exchange_ship_from_store", s, DEMO_TODAY + timedelta(days=locs[s]["ship_days"]), exchange=True) for s in ship]
        raw += [option("exchange_store_pickup", s, DEMO_TODAY + timedelta(days=locs[s]["pickup_ready_days"]), exchange=True)
                for s in home if int(stock.get(s, {}).get("atp", 0)) >= 1]
        raw.append(option("return_in_store", (home or stores)[0], DEMO_TODAY, exchange=False))
        raw.append(option("return_mail", dc, DEMO_TODAY + timedelta(days=10), exchange=False, fee=595))
        quote_id = new_id("QUO-", 10)
        for n, opt in enumerate(raw, 1):
            opt["option_id"] = f"OPT-{n}"
        rank = {"exchange_store_pickup": 0, "exchange_ship_from_store": 1, "exchange_ship_from_dc": 2, "return_in_store": 3, "return_mail": 4}
        ranked = sorted(raw, key=lambda o: rank[o["type"]])
        for opt in ranked:
            opt["payload_hash"] = hashlib.sha256(canonical({"quote_id": quote_id, "option_id": opt["option_id"], "type": opt["type"],
                                                            "sku": opt["sku"], "location": opt["location_id"], "qty": 1,
                                                            "refund_cents": opt["refund_cents"], "fee_cents": opt["fee_cents"],
                                                            "currency": "CAD", "policy_version": policy.POLICY_VERSION}).encode()).hexdigest()
        expires = now_iso(900)
        self.put({"pk": f"QUOTE#{quote_id}", "sk": "META", "customer_id": cid, "order_id": order["order_id"], "line_id": line["line_id"],
                  "conv_id": "fake", "run_id": run_id, "policy_check_id": pchk, "status": "open", "created_at": now_iso(),
                  "expires_at": expires, "options": ranked})
        if "early_side_effect" in self.faults and raw and raw[0]["type"].startswith("exchange"):
            self.add(f"INV#{sku}", f"LOC#{raw[0]['location_id']}", "atp", -1)
        self.tool(run_id, "find_resolution_options")
        self.tool(run_id, "create_quote")
        self.emit(run_id, "quote_created", {"quote_id": quote_id, "option_ids": [o["option_id"] for o in ranked], "expires_at": expires})
        self.say(run_id, "Please review the options and approve one.")

    def h_quote(self, req: Req):
        cid, quote = self.session_customer(req), self.get(f"QUOTE#{req.params['qid']}", "META")
        if not quote:
            raise DomainError(ErrorCode.NOT_FOUND, "quote not found")
        if quote["customer_id"] != cid:
            self.audit("access_denied", {"resource_type": "quote", "resource_id": req.params["qid"]}, actor=cid)
            raise DomainError(ErrorCode.NOT_AUTHORIZED, "not your quote")
        approval = self.get(f"QUOTE#{req.params['qid']}", "APPROVAL")
        return 200, {**{k: v for k, v in quote.items() if k not in ("pk", "sk")}, "quote_id": req.params["qid"], "approval": approval}

    # ---------------------------------------------------------------- approve and execute

    def h_approve(self, req: Req):
        cid, qid, body = self.session_customer(req), req.params["qid"], req.json
        quote = self.get(f"QUOTE#{qid}", "META")
        if not quote:
            raise DomainError(ErrorCode.NOT_FOUND, "quote not found")
        if quote["customer_id"] != cid:
            self.audit("access_denied", {"resource_type": "quote", "resource_id": qid}, actor=cid)
            raise DomainError(ErrorCode.NOT_AUTHORIZED, "not your quote")
        option = next((o for o in quote["options"] if o["option_id"] == body.get("option_id")), None)
        if option is None or option["payload_hash"] != body.get("payload_hash"):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "unknown option or payload hash mismatch")
        existing = self.get(f"QUOTE#{qid}", "APPROVAL")
        if existing:
            if existing["option_id"] != option["option_id"]:
                raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "another option was already approved")
            if "approve_replay_new_run" in self.faults:
                run_id = new_id("RUN-", 10)
                self.start_run(run_id, cid, quote["conv_id"], "approval")
                self.finish_run(run_id, AGENT_TOKENS)
                return 202, {"approval_id": existing["approval_id"], "run_id": run_id, "quote_id": qid, "option_id": option["option_id"], "replayed": False}
            return 200, {"approval_id": existing["approval_id"], "run_id": existing["run_id"], "quote_id": qid, "option_id": option["option_id"], "replayed": True}
        if quote["status"] != "open" or quote["expires_at"] <= now_iso():
            raise DomainError(ErrorCode.QUOTE_EXPIRED, "quote expired or superseded")
        approval = {"pk": f"QUOTE#{qid}", "sk": "APPROVAL", "approval_id": new_id("APR-"), "option_id": option["option_id"],
                    "payload_hash": option["payload_hash"], "run_id": new_id("RUN-", 10), "approved_at": now_iso()}
        self.put(approval, only_new=True)
        self.start_run(approval["run_id"], cid, quote["conv_id"], "approval")
        self.emit(approval["run_id"], "approval_received", {"approval_id": approval["approval_id"], "quote_id": qid, "option_id": option["option_id"]})
        self.emit(approval["run_id"], "run_started", {"kind": "approval", "model_id": MODEL_ID, "max_rounds": 8})
        self.execute(cid, quote, option, approval)
        self.tool(approval["run_id"], "get_resolution_status")
        self.say(approval["run_id"], "Your simulated exchange was created.")
        return 202, {"approval_id": approval["approval_id"], "run_id": approval["run_id"], "quote_id": qid, "option_id": option["option_id"], "replayed": False}

    def execute(self, cid: str, quote: dict, option: dict, approval: dict) -> str:
        run_id, qid = approval["run_id"], quote["pk"][len("QUOTE#"):]
        idem = self.get(f"IDEM#{approval['approval_id']}", "META")
        if idem:
            return idem["transaction_id"]
        exchange = option["type"].startswith("exchange_")
        tid = new_id("DEMO-RMA-") if exchange else new_id("DEMO-RET-")
        txn = {"pk": f"TXN#{tid}", "sk": "META", "customer_id": cid, "quote_id": qid, "order_id": quote["order_id"], "line_id": quote["line_id"],
               "state": "completed", "option": {k: option[k] for k in ("type", "sku", "location_id", "replacement_size", "qty", "refund_cents", "fee_cents", "eta_date")},
               "eta_date": option["eta_date"], "next_step": "Simulated; follow the pickup instructions.", "created_at": now_iso()}
        self.tool(run_id, "execute_approved_resolution", evidence={"transaction_id": tid, "replayed": False})
        self.put(txn)
        if "double_txn" in self.faults:
            self.put({**txn, "pk": f"TXN#{new_id('DEMO-RMA-')}"})
        self.put({"pk": f"IDEM#{approval['approval_id']}", "sk": "META", "transaction_id": tid})
        self.update(f"QUOTE#{qid}", "APPROVAL", {"consumed_txn_id": tid})
        self.add(f"ORDER#{quote['order_id']}", f"LINE#{quote['line_id']}", "returned_or_reserved_qty", 1)
        if exchange and "no_atp_decrement" not in self.faults and "early_side_effect" not in self.faults:
            self.add(f"INV#{option['sku']}", f"LOC#{option['location_id']}", "atp", -1)
            self.add(f"INV#{option['sku']}", f"LOC#{option['location_id']}", "version", 1)
        self.update(f"QUOTE#{qid}", "META", {"status": "executed"})
        if exchange:
            self.enqueue_update(cid, tid, quote, option)
        self.emit(run_id, "transaction_created", {"transaction_id": tid, "state": "completed", "option_type": option["type"], "quote_id": qid, "replayed": False})
        self.emit_a2a(cid, "transaction_created", {"transaction_id": tid, "state": "completed", "option_type": option["type"], "order_id": quote["order_id"], "replayed": False}, actor="aldo_agent")
        return tid

    def domain_execute(self, customer_id: str, quote_id: str, option_id: str | None) -> Mapping[str, Any]:
        approval = self.get(f"QUOTE#{quote_id}", "APPROVAL")
        if not approval or (option_id and option_id != approval["option_id"]):
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "no matching approval")
        idem = self.get(f"IDEM#{approval['approval_id']}", "META")
        if idem is None:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "not executed yet")
        return {"transaction_id": idem["transaction_id"], "replayed": True}

    # ---------------------------------------------------------------- write-back (outbox -> personal-agent table)

    def enqueue_update(self, cid: str, tid: str, quote: dict, option: dict) -> None:
        line = self.get(f"ORDER#{quote['order_id']}", f"LINE#{quote['line_id']}")
        pchk = next((p for p in self.scan() if p["pk"].startswith("PCHK#") and p.get("order_id") == quote["order_id"]), {})
        direction = pchk.get("size_direction")
        update = {"update_id": "MU-" + hashlib.sha1(f"{tid}|exchange_created".encode()).hexdigest()[:6].upper(), "owner_ref": cid,
                  "order_id": quote["order_id"], "style_id": line["style"], "product_name": line["product_name"], "color": line["color"],
                  "original_size": line["size"], "new_size": option["replacement_size"],
                  "reason": {"too_small": "TOO_SMALL", "too_big": "TOO_BIG"}.get(direction, "OTHER"),
                  "outcome": "exchange_created", "provisional": True, "source": "ALDO_CA", "ts": now_iso()}
        self.put({"pk": f"A2AOUT#{update['update_id']}", "sk": "META", "payload": update, "customer_id": cid, "status": "pending",
                  "attempts": 0, "created_at": now_iso(), "gsi1pk": "A2AOUT#pending", "gsi1sk": now_iso()}, only_new=True)

    def flush_outbox(self, cid: str) -> None:
        for item in self.scan():
            if not item["pk"].startswith("A2AOUT#") or item.get("customer_id") != cid or item["status"] != "pending":
                continue
            raw = json.dumps(item["payload"], sort_keys=True, separators=(",", ":")).encode()
            sig = hmac.new(self.writeback_key, raw, hashlib.sha256).hexdigest()
            status, _body, _h = self.transport("POST", "https://fake/api/personal-agent/memory-updates", {"X-ALDO-Signature": sig}, raw, 5)
            if status == 200:
                try:  # only the winner of pending -> delivered emits memory_updated
                    self.t.aldo.update_item(Key={"pk": item["pk"], "sk": "META"}, UpdateExpression="SET #s = :d REMOVE gsi1pk, gsi1sk",
                                            ConditionExpression="#s = :p", ExpressionAttributeNames={"#s": "status"},
                                            ExpressionAttributeValues={":d": "delivered", ":p": "pending"})
                except self.t.aldo.meta.client.exceptions.ConditionalCheckFailedException:
                    continue
                u = item["payload"]
                public = {k: u[k] for k in ("update_id", "style_id", "product_name", "original_size", "new_size", "reason", "outcome", "provisional")}
                self.emit_a2a(cid, "memory_updated", public, actor="personal_agent")

    def h_memory_updates(self, req: Req):
        expected = hmac.new(self.writeback_key, req.raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, str(req.headers.get("x-aldo-signature", ""))):
            raise DomainError(ErrorCode.NOT_AUTHENTICATED, "invalid signature")
        update = req.json
        row = {"pk": f"OWNER#{update['owner_ref']}", "sk": f"UPD#{update['update_id']}",
               **{k: v for k, v in update.items() if k not in ("owner_ref", "ts")},
               "status": "active", "applied_at": now_iso()}
        fresh = self.put(row, table="pa", only_new=True)
        if not fresh and "dup_writeback" in self.faults:
            self.put({**row, "sk": f"UPD#{update['update_id']}-again"}, table="pa")
            fresh = True
        return 200, {"update_id": update["update_id"], "applied": fresh, "duplicate": not fresh}

    def h_pa_memory(self, req: Req):
        cid = self.session_customer(req)
        self.flush_outbox(cid)
        rows = self.query(f"OWNER#{cid}", None, "pa")
        owner = next(o for o in self.facts_owner_memory() if o["owner_id"] == cid)
        updates = [{**{k: v for k, v in r.items() if k not in ("pk", "sk")}, "confidence": "provisional" if r.get("provisional") else "confirmed"}
                   for r in rows if r["sk"].startswith("UPD#")]
        return 200, {"owner_id": cid, "display_name": owner["display_name"], "size_system": owner["size_system"],
                     "typical_size": owner["typical_size"], "source": "synthetic", "label": "Synthetic demo memory",
                     "memory": [{k: v for k, v in r.items() if k not in ("pk", "sk")} for r in rows if r["sk"].startswith("MEM#")], "updates": updates}

    def facts_owner_memory(self) -> list[dict]:
        root = Path(os.environ.get("FIXTURES_DIR") or REPO_ROOT / "fixtures")
        return json.loads((root / "personal_agent" / "memory.json").read_text(encoding="utf-8"))["owners"]

    def h_simulate_delivery(self, req: Req):
        cid, order_id = self.session_customer(req), req.json.get("order_id", "")
        order = self.owned_order(cid, order_id)
        if order.get("channel") != "agent":
            raise DomainError(ErrorCode.VALIDATION_ERROR, "only agent-placed orders can be delivered by the demo control")
        already = order.get("delivered_at") is not None
        if not already:
            self.update(f"ORDER#{order_id}", "META", {"delivered_at": DEMO_TODAY.isoformat()})
        return 200, {"order_id": order_id, "delivered_at": DEMO_TODAY.isoformat(), "already_delivered": already}

    # ---------------------------------------------------------------- gateway: assist, proposal, owner confirmation

    def h_size_assist(self, req: Req):
        return 200, self.size_assist(self.delegated_claims(req), req.json)

    def size_assist(self, claims: Any, body: Mapping[str, Any]) -> dict:
        extra = set(body) - {"intent", "product_query", "color", "size_system", "deadline_date", "channel_pref"}
        if extra or body.get("intent") != "size_assist" or len(str(body.get("product_query", ""))) < 2:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"invalid size-assist request ({sorted(extra)})")
        products = [p for p in self.scan() if p["pk"].startswith("PRODUCT#")]
        styles = {p["style"]: p for p in products if str(body["product_query"]).lower() in p["product_name"].lower()}
        color = str(body.get("color") or "BLACK").upper()
        run = sorted((p for p in products if p["style"] in styles and p["color"] == color), key=lambda p: float(p["size"]))
        if len(styles) != 1 or not run:
            raise DomainError(ErrorCode.UNKNOWN_STYLE, "no single style matches", details={"candidates": [
                {"style_id": s, "product_name": p["product_name"], "colors": [color]} for s, p in styles.items()]})
        cid, deadline = claims.customer_id, body.get("deadline_date")
        home, locs = self.get(f"CUST#{cid}", "PROFILE")["home_region"], self.locations()
        sizes, offered, n = [], {}, 0
        for prod in run:
            stock = {i["sk"][4:]: i for i in self.query(f"INV#{prod['sku']}")}
            cands: list[tuple[str, str, date]] = []
            for loc_id, loc in locs.items():
                have = int(stock.get(loc_id, {}).get("atp", 0)) >= 1
                if loc["type"] == "DC" and have:
                    cands.append(("ship_from_dc", loc_id, DEMO_TODAY + timedelta(days=loc["ship_days"])))
            stores = sorted(s for s, loc in locs.items() if loc["type"] == "STORE" and loc["region"] == home and int(stock.get(s, {}).get("atp", 0)) >= 1)
            if stores:
                best = min(stores, key=lambda s: (locs[s]["ship_days"], s))
                cands.append(("ship_from_store", best, DEMO_TODAY + timedelta(days=locs[best]["ship_days"])))
            cands += [("store_pickup", s, DEMO_TODAY + timedelta(days=locs[s]["pickup_ready_days"])) for s in stores]
            opts = []
            for type_, loc_id, eta in cands:
                n += 1
                opt = {"option_id": f"SO-{n}", "type": type_, "location_id": loc_id, "location_name": locs[loc_id]["name"], "eta_date": eta.isoformat(),
                       "eta_text": eta.strftime("%a %b ") + str(eta.day), "meets_deadline": (eta.isoformat() <= deadline) if deadline else None,
                       "atp": int(stock[loc_id]["atp"])}
                opts.append(opt)
                offered[opt["option_id"]] = {"size": prod["size"], "type": type_, "sku": prod["sku"], "location_id": loc_id, "location_name": locs[loc_id]["name"],
                                             "eta_date": opt["eta_date"], "atp": opt["atp"], "inventory_version": int(stock[loc_id]["version"])}
            if opts:
                sizes.append({"size": prod["size"], "options": opts})
        hero = run[0]
        try:
            from returns_domain import fit

            insight = fit.get_fit_insight(hero["product_name"])
            signal = {"signal": insight["signal"], "too_small_share": insight["too_small_share"], "n_fit": insight["n_fit"], "scope": insight["scope"], "provenance": "derived"}
        except Exception:  # noqa: BLE001 - the simulator only needs a plausible signal
            signal = {"signal": "runs_small", "too_small_share": 0.8, "n_fit": 582, "scope": "ALDO_CA", "provenance": "derived"}
        rid, expires = new_id("SZR-"), now_iso(900)
        self.put({"pk": f"SIZEREQ#{rid}", "sk": "META", "customer_id": cid, "style_id": hero["style"], "product_name": hero["product_name"],
                  "color": color, "price_cents": hero["price_cents"], "deadline_date": deadline, "offered": offered, "status": "open",
                  "created_at": now_iso(), "expires_at": expires}, only_new=True)
        public = {"size_request_id": rid}
        self.emit_a2a(cid, "size_assist_requested", {**public, "product_query": body["product_query"], "color": color, "deadline_date": deadline}, actor="personal_agent")
        self.emit_a2a(cid, "fit_signal_shared", {**public, "style_id": hero["style"], "product_name": hero["product_name"], **signal}, actor="aldo_gateway")
        self.emit_a2a(cid, "size_requested", {**public, "sizes": [s["size"] for s in sizes], "option_count": n, "expires_at": expires}, actor="aldo_gateway")
        return {"size_request_id": rid, "style_id": hero["style"], "product_name": hero["product_name"], "color": color,
                "price_cents": hero["price_cents"], "currency": "CAD", "fit_signal": signal, "sizes": sizes,
                "question": "Which size and option should I reserve for your owner?", "expires_at": expires}


    def sizereq_for(self, cid: str, rid: str) -> dict:
        sr = self.get(f"SIZEREQ#{rid}", "META")
        if sr is None:
            raise DomainError(ErrorCode.NOT_FOUND, "size request not found")
        if sr["customer_id"] != cid:
            self.audit("access_denied", {"resource_type": "size_request", "resource_id": rid}, actor=cid)
            raise DomainError(ErrorCode.NOT_AUTHORIZED, "not your size request")
        return sr

    def h_size_decision(self, req: Req):
        return 200, self.propose(self.delegated_claims(req), req.json)

    def propose(self, claims: Any, body: Mapping[str, Any]) -> dict:
        """Delegated token: records a PROPOSAL only (no order, no ATP change)."""
        extra = set(body) - {"size_request_id", "option_id", "chosen_size", "rationale_public", "confidence"}
        if extra or not re.fullmatch(r"SZR-[0-9A-F]{6}", str(body.get("size_request_id", ""))) or len(str(body.get("rationale_public", ""))) > 120:
            raise DomainError(ErrorCode.VALIDATION_ERROR, "invalid size-decision request")
        cid, rid = claims.customer_id, body["size_request_id"]
        sr = self.sizereq_for(cid, rid)
        if sr["status"] == "decided":
            raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "already decided")
        if sr["expires_at"] <= now_iso():
            raise DomainError(ErrorCode.REQUEST_EXPIRED, "size request expired")
        offered = sr["offered"].get(body.get("option_id"))
        if "accept_invalid_choice" not in self.faults and (offered is None or offered["size"] != body.get("chosen_size")):
            self.audit("decision_denied", {"resource_type": "size_request", "resource_id": rid}, actor=cid)
            raise DomainError(ErrorCode.INVALID_SIZE_CHOICE, "the choice was not offered")
        if offered is not None and sr["price_cents"] > int(claims.max_amount_cents or 0):
            self.audit("decision_denied", {"resource_type": "size_request", "resource_id": rid}, actor=cid)
            raise DomainError(ErrorCode.DELEGATION_LIMIT_EXCEEDED, "above the delegated limit")
        self.update(f"SIZEREQ#{rid}", "META", {"status": "proposed", "proposed_option_id": body["option_id"], "proposed_size": body["chosen_size"],
                                              "rationale_public": body.get("rationale_public", ""), "confidence": body.get("confidence", "medium"),
                                              "proposed_at": now_iso()})
        rationale = body.get("rationale_public", "")
        if "leak_retailer" in self.faults:
            rationale += " matches Retailer A (synthetic) fit"
        self.emit_a2a(cid, "size_recommended", {"size_request_id": rid, "option_id": body["option_id"], "chosen_size": body["chosen_size"],
                                                "option_type": (offered or {}).get("type"), "location_name": (offered or {}).get("location_name"),
                                                "eta_date": (offered or {}).get("eta_date"), "rationale_public": rationale,
                                                "confidence": body.get("confidence")}, actor="personal_agent")
        return {"size_request_id": rid, "status": "proposed", "awaiting": "owner_confirmation", "expires_at": sr["expires_at"],
                "summary": {"size": body["chosen_size"], "option_id": body["option_id"]}}

    def h_purchase_view(self, req: Req):
        cid, sr = self.session_customer(req), None
        sr = self.sizereq_for(cid, req.params["rid"])
        chosen = sr["offered"].get(sr.get("proposed_option_id")) or {}
        return 200, {"size_request_id": req.params["rid"], "status": sr["status"], "product_name": sr["product_name"], "color": sr["color"],
                     "size": sr.get("proposed_size"), "option_type": chosen.get("type"), "location_name": chosen.get("location_name"),
                     "price_cents": sr["price_cents"], "currency": "CAD", "rationale_public": sr.get("rationale_public"), "expires_at": sr["expires_at"]}

    def h_purchase_confirm(self, req: Req):
        cid, rid, body = self.session_customer(req), req.params["rid"], req.json
        return 200, self.confirm_purchase(cid, rid, body.get("option_id"))

    def confirm_purchase(self, cid: str, rid: str, option_id: str | None) -> dict:
        sr = self.sizereq_for(cid, rid)
        if sr["status"] == "decided":
            if option_id and option_id != sr.get("proposed_option_id"):
                raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "another option was confirmed")
            return {**self.order_response(sr["order_id"]), "replayed": True}
        if sr["status"] != "proposed":
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "no proposal to confirm yet")
        if option_id and option_id != sr["proposed_option_id"]:
            raise DomainError(ErrorCode.IDEMPOTENCY_CONFLICT, "option differs from the proposal")
        if sr["expires_at"] <= now_iso():
            raise DomainError(ErrorCode.REQUEST_EXPIRED, "size request expired")
        chosen = sr["offered"][sr["proposed_option_id"]]
        inv = self.get(f"INV#{chosen['sku']}", f"LOC#{chosen['location_id']}")
        if int(inv["atp"]) < 1:
            raise DomainError(ErrorCode.STOCK_CHANGED, "out of stock")
        oid = "DEMO-A2A-" + hashlib.sha1(rid.encode()).hexdigest()[:6].upper()
        prod = self.get(f"PRODUCT#{chosen['sku']}", "META")
        fulfil = {"type": chosen["type"], "location_id": chosen["location_id"], "location_name": chosen["location_name"], "eta_date": chosen["eta_date"]}
        self.put({"pk": f"ORDER#{oid}", "sk": "META", "order_id": oid, "customer_id": cid, "banner": "ALDO_CA", "currency": "CAD", "channel": "agent",
                  "ordered_at": DEMO_TODAY.isoformat(), "delivered_at": None, "size_request_id": rid, "fulfillment": fulfil, "source": "synthetic",
                  "gsi1pk": f"CUST#{cid}", "gsi1sk": f"ORDER#{DEMO_TODAY.isoformat()}#{oid}"}, only_new=True)
        self.put({"pk": f"ORDER#{oid}", "sk": f"LINE#{oid}-L1", "order_id": oid, "line_id": f"{oid}-L1", "sku": chosen["sku"],
                  "product_name": prod["product_name"], "style": prod["style"], "size": prod["size"], "color": prod["color"], "purchased_qty": 1,
                  "returned_or_reserved_qty": 0, "unit_paid_cents": sr["price_cents"], "final_sale": False, "source": "synthetic"})
        self.add(f"INV#{chosen['sku']}", f"LOC#{chosen['location_id']}", "atp", -1)
        self.add(f"INV#{chosen['sku']}", f"LOC#{chosen['location_id']}", "version", 1)
        self.update(f"SIZEREQ#{rid}", "META", {"status": "decided", "order_id": oid, "chosen_option_id": sr["proposed_option_id"],
                                              "chosen_size": sr["proposed_size"], "decided_at": now_iso(), "approved_by": "owner_click"})
        self.emit_a2a(cid, "size_confirmed", {"size_request_id": rid, "order_id": oid, "size": prod["size"], "location_name": chosen["location_name"],
                                              "eta_date": chosen["eta_date"], "price_cents": sr["price_cents"], "approved_by": "owner_click"}, actor="aldo_gateway")
        return {**self.order_response(oid), "replayed": False}

    def order_response(self, oid: str) -> dict:
        order, line = self.get(f"ORDER#{oid}", "META"), self.get(f"ORDER#{oid}", f"LINE#{oid}-L1")
        return {"order_id": oid, "status": "placed_simulated", "line": {"sku": line["sku"], "size": line["size"], "unit_paid_cents": line["unit_paid_cents"]},
                "fulfillment": order["fulfillment"], "next_step": "Simulated order, no payment taken."}

    # ---------------------------------------------------------------- ALDO bot (personal agent, scripted)

    def h_pa_conv(self, req: Req):
        cid = self.session_customer(req)
        conv = new_id("PCONV-", 10)
        self.put({"pk": f"OWNER#{cid}", "sk": f"PCONV#{conv}", "messages": "[]"}, table="pa")
        return 201, {"conv_id": conv}

    def h_pa_message(self, req: Req):
        cid, text = self.session_customer(req), str(req.json.get("text", ""))
        if not text.strip() or text.lower().startswith("[server]"):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "invalid message")
        run_id = new_id("PRUN-", 10)
        self.start_run(run_id, cid, req.params["conv"], "customer", owner=cid)
        self.bot_turn(cid, run_id, text)
        return 202, {"run_id": run_id, "conv_id": req.params["conv"]}

    def bot_turn(self, cid: str, run_id: str, text: str) -> None:
        ev = lambda t, p: self.emit(run_id, t, p, actor="personal_agent", owner=cid)  # noqa: E731
        ev("run_started", {"kind": "customer", "model_id": MODEL_ID, "max_rounds": 6})
        memory = next(o for o in self.facts_owner_memory() if o["owner_id"] == cid)
        ev("tool_call", {"tool": "recall_size_memory", "args": {}, "round": 1})
        name = next((p["product_name"] for p in self.scan() if p["pk"].startswith("PRODUCT#") and p["product_name"].lower() in text.lower()), None)
        if name is None:
            ev("assistant_message", {"text": "Which shoe should I look for?"})
            return self.finish_run(run_id, BOT_TOKENS, owner=cid)
        token = auth.sign_token(cid, scope=auth.SCOPE_DELEGATED_GATEWAY, actor="personal_agent", key=self.token_key).token
        claims = auth.verify_claims(token, "delegated_gateway", key=self.token_key)
        ev("tool_call", {"tool": "ask_aldo_size_assist", "args": {"product_query": name}, "round": 2})
        assist = self.size_assist(claims, {"intent": "size_assist", "product_query": re.match(r"[A-Za-z]+", name).group(0), "color": "BLACK",
                                           "size_system": "US_W", "deadline_date": DEADLINE if re.search("saturday", text, re.I) else None, "channel_pref": None})
        want = f"{float(memory['typical_size']) + 0.5:.1f}"  # runs_small -> one half size above the owner's typical size
        picks = [(e["size"], o) for e in assist["sizes"] for o in e["options"] if o["type"] == "store_pickup" and o["meets_deadline"]]
        pick = next((p for p in picks if p[0] == want), None) or (picks[0] if picks else None)
        if pick is None:
            ev("assistant_message", {"text": "Nothing meets the deadline."})
            return self.finish_run(run_id, BOT_TOKENS, owner=cid)
        ev("tool_call", {"tool": "send_size_decision", "args": {"option_id": pick[1]["option_id"]}, "round": 3})
        self.propose(claims, {"size_request_id": assist["size_request_id"], "option_id": pick[1]["option_id"], "chosen_size": pick[0],
                              "rationale_public": "runs small per ALDO signal; fits the deadline", "confidence": "high"})
        if "bot_places_order" in self.faults:
            self.confirm_purchase(cid, assist["size_request_id"], None)
        ev("assistant_message", {"text": "Your proposal is ready. Please confirm it in the app."})
        self.finish_run(run_id, BOT_TOKENS, owner=cid)
