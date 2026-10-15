"""Stub API Lambda, deployed until the real backend/api/handler.py is bundled.

Purpose: prove the base infrastructure (CloudFront /api/* routing, HTTP API,
IAM, env vars, dependency bundle) before the real handlers exist. Routes live
under /api/stub/ and are deliberately unauthenticated and synthetic. They never
return secret values or Authorization header contents.
"""
import base64
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
import time
import uuid

import boto3

STUB_REVISION = 2  # bump when the stub changes; visible in /api/stub/ping

_clients = {}


def _client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


def _error(status, code, message):
    return _resp(status, {"error": {"code": code, "message": message}, "stub": True})


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _import_check(module):
    """Import a module and report success and the time it took (cold-start cost)."""
    start = time.perf_counter()
    try:
        importlib.import_module(module)
        return {"ok": True, "ms": round((time.perf_counter() - start) * 1000)}
    except Exception as exc:  # report the error class only
        return {"ok": False, "error": type(exc).__name__}


def _tz_check():
    try:
        from zoneinfo import ZoneInfo

        return {"ok": True, "offset": str(ZoneInfo("America/Toronto").utcoffset(None))}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def _health():
    table_name = os.environ.get("TABLE_NAME")
    personal_table_name = os.environ.get("PERSONAL_AGENT_TABLE_NAME")
    secret_name = os.environ.get("SECRETS_ARN")
    fixtures_dir = os.environ.get("FIXTURES_DIR", "")
    report = {
        "ok": True,
        "stub": True,
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "deps": {
            name: _version(name)
            for name in ("boto3", "pydantic", "aws-lambda-powertools", "tzdata", "strands-agents")
        },
        "imports": {
            "pydantic": _import_check("pydantic"),
            "aws_lambda_powertools.event_handler": _import_check("aws_lambda_powertools.event_handler"),
        },
        "packages_present": {
            name: importlib.util.find_spec(name) is not None
            for name in ("returns_domain", "personal_agent", "strands")
        },
        "timezone": _tz_check(),
        "env_present": {
            name: bool(os.environ.get(name))
            for name in (
                "TABLE_NAME",
                "PERSONAL_AGENT_TABLE_NAME",
                "DEMO_NOW",
                "SECRETS_ARN",
                "FIXTURES_DIR",
                "WORKER_FUNCTION_NAME",
                "GATEWAY_TRANSPORT",
                "GATEWAY_BASE_URL_PARAM",
                "DELEGATION_MAX_CENTS",
            )
        },
        "fixtures_dir_exists": os.path.isdir(fixtures_dir),
        "fixtures_files": sorted(os.listdir(fixtures_dir)) if os.path.isdir(fixtures_dir) else [],
    }
    for label, name in (("table", table_name), ("personal_table", personal_table_name)):
        try:
            _client("dynamodb").get_item(
                TableName=name,
                Key={"pk": {"S": "STUB#HEALTH"}, "sk": {"S": "PROBE"}},
                ConsistentRead=True,
            )
            report[label] = {"name": name, "readable": True}
        except Exception as exc:
            report["ok"] = False
            report[label] = {"name": name, "readable": False, "error": type(exc).__name__}
    try:
        raw = _client("secretsmanager").get_secret_value(SecretId=secret_name)["SecretString"]
        parsed = json.loads(raw)
        report["secret"] = {
            "keys": sorted(parsed),
            "all_non_empty": all(bool(v) for v in parsed.values()),
        }
    except Exception as exc:
        report["ok"] = False
        report["secret"] = {"error": type(exc).__name__}
    param = os.environ.get("GATEWAY_BASE_URL_PARAM")
    try:
        value = _client("ssm").get_parameter(Name=param)["Parameter"]["Value"]
        report["gateway_url_param"] = {"name": param, "readable": True, "is_https_url": value.startswith("https://")}
    except Exception as exc:
        report["ok"] = False
        report["gateway_url_param"] = {"name": param, "readable": False, "error": type(exc).__name__}
    return _resp(200 if report["ok"] else 500, report)


def _ddb_write_probe():
    """Put then delete one throwaway item in each table to prove the role can write."""
    key = {"pk": {"S": "STUB#PROBE"}, "sk": {"S": uuid.uuid4().hex}}
    ddb = _client("dynamodb")
    written = {}
    for label, env_name in (("table", "TABLE_NAME"), ("personal_table", "PERSONAL_AGENT_TABLE_NAME")):
        table_name = os.environ[env_name]
        ddb.put_item(
            TableName=table_name,
            Item={**key, "note": {"S": "stub write probe"}},
            ConditionExpression="attribute_not_exists(pk)",
        )
        ddb.delete_item(TableName=table_name, Key=key)
        written[label] = True
    return _resp(200, {"ok": True, "stub": True, "wrote_and_deleted": written})


def _invoke_worker():
    function_name = os.environ["WORKER_FUNCTION_NAME"]
    result = _client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"source": "api-stub", "ping": True}).encode(),
    )
    return _resp(202, {"ok": True, "stub": True, "worker": function_name, "status_code": result["StatusCode"]})


def _body_of(event):
    body = event.get("body")
    if not body:
        return None, 0
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8", errors="replace")
    try:
        return json.loads(body), len(body)
    except ValueError:
        return body, len(body)


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "")
    path = event.get("rawPath", "")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    print(json.dumps({"msg": "api stub request", "method": method, "path": path, "request_id": context.aws_request_id}))

    if method == "GET" and path == "/.well-known/agent-card.json":
        return _resp(200, {"stub": True, "name": "ALDO gateway stub", "note": "agent card placeholder; real card comes from backend/api"})
    if method == "GET" and path == "/api/stub/ping":
        return _resp(
            200,
            {"ok": True, "stub": True, "revision": STUB_REVISION, "method": method, "rawPath": path},
        )
    if method == "GET" and path == "/api/stub/unauthenticated":
        return _error(401, "NOT_AUTHENTICATED", "Stub 401 body; CloudFront must pass it through unchanged.")
    if method == "GET" and path == "/api/stub/forbidden":
        return _error(403, "NOT_AUTHORIZED", "Stub 403 body; CloudFront must pass it through unchanged.")
    if method == "GET" and path == "/api/stub/missing":
        return _error(404, "ORDER_NOT_FOUND", "Stub 404 body; CloudFront must pass it through unchanged.")
    if method == "POST" and path == "/api/stub/echo":
        body, size = _body_of(event)
        return _resp(
            200,
            {
                "ok": True,
                "stub": True,
                "method": method,
                "rawPath": path,
                "content_type": headers.get("content-type"),
                "bytes": size,
                "body": body,
                "has_authorization_header": "authorization" in headers,
                "has_admin_header": "x-demo-admin" in headers,
            },
        )
    if method == "GET" and path == "/api/stub/health":
        return _health()
    if method == "POST" and path == "/api/stub/ddb-probe":
        return _ddb_write_probe()
    if method == "POST" and path == "/api/stub/invoke-worker":
        return _invoke_worker()
    return _error(404, "NOT_FOUND", f"Stub API has no route for {method} {path}")
