#!/usr/bin/env python3
"""Run the API Lambda handler as a local HTTP server (design-gap G-29).

The Vite dev server proxies /api to http://localhost:3001. Every request is turned into an HTTP API
v2 event and handed to ``api.handler`` exactly as API Gateway would hand it over, so what runs here
is the deployed code. The worker runs inline in a thread (``AGENT_MODE=local``) and talks to the
returns agent at ``AGENT_LOCAL_URL`` (default http://localhost:8080/invocations, started with
``python agent_app.py``); the personal agent runs in process.

Offline (no AWS at all; an in-memory DynamoDB with the demo fixtures and the personal agent's memory):

    cd /Users/minsu/Desktop/aldo-return-agent/backend
    uv run python ../scripts/local_api.py --moto            # admin key: local-dev-admin-key-0123456789

Against the real tables (credentials only through the helper; the keys come from the deployed secret):

    cd /Users/minsu/Desktop/aldo-return-agent/backend
    ../scripts/with_aws_env.sh env SECRETS_ARN=aldo-demo/secrets uv run python ../scripts/local_api.py

Requests are served one at a time: the Powertools resolver keeps per-request state on the class.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_PORT = 3001
LOCAL_TOKEN_KEY = "local-dev-token-key-0123456789"
LOCAL_ADMIN_KEY = "local-dev-admin-key-0123456789"
VARIANTS = ("default", "dc_stockout", "all_stockout")


class LocalContext:
    """Just enough of the Lambda context for Powertools and the handlers."""

    function_name = "aldo-returns-demo-api"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:aldo-returns-demo-api"

    def __init__(self) -> None:
        self.aws_request_id = str(uuid.uuid4())

    def get_remaining_time_in_millis(self) -> int:
        return 15_000


def v2_event(method: str, target: str, headers: Mapping[str, str], body: bytes) -> dict[str, Any]:
    """The payload-format-2.0 event API Gateway sends for ``method target``."""
    parts = urlsplit(target)
    lowered = {k.lower(): v for k, v in headers.items()}
    query: dict[str, str] = {}
    for pair in filter(None, parts.query.split("&")):
        name, _, value = pair.partition("=")
        query[name] = f"{query[name]},{value}" if name in query else value
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": parts.path,
        "rawQueryString": parts.query,
        "headers": lowered,
        "queryStringParameters": query or None,
        "requestContext": {
            "accountId": "000000000000",
            "apiId": "local",
            "domainName": lowered.get("host", "localhost"),
            "domainPrefix": "local",
            "http": {
                "method": method,
                "path": parts.path,
                "protocol": "HTTP/1.1",
                "sourceIp": "127.0.0.1",
                "userAgent": lowered.get("user-agent", ""),
            },
            "requestId": str(uuid.uuid4()),
            "routeKey": "$default",
            "stage": "$default",
            "time": "",
            "timeEpoch": 0,
        },
        **_body_fields(body),
    }


def _body_fields(body: bytes) -> dict[str, Any]:
    """Text bodies travel as text; only non-UTF-8 bytes are base64, as API Gateway does."""
    if not body:
        return {"body": None, "isBase64Encoded": False}
    try:
        return {"body": body.decode("utf-8"), "isBase64Encoded": False}
    except UnicodeDecodeError:
        return {"body": base64.b64encode(body).decode(), "isBase64Encoded": True}


def make_server(
    handler: Callable[[dict, Any], dict], host: str = "127.0.0.1", port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    """An HTTP server whose requests go through ``handler(event, context)``, one at a time."""
    gate = threading.Lock()

    class Bridge(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            event = v2_event(self.command, self.path, dict(self.headers.items()), body)
            with gate:
                result = handler(event, LocalContext())
            payload = result.get("body") or ""
            raw = base64.b64decode(payload) if result.get("isBase64Encoded") else payload.encode("utf-8")
            self.send_response(int(result["statusCode"]))
            for name, value in (result.get("headers") or {}).items():
                self.send_header(name, str(value))
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
            # method, path and status only: never headers (they carry the bearer token)
            sys.stderr.write(f"{self.command} {urlsplit(self.path).path} -> {args[1] if len(args) > 1 else ''}\n")

    return ThreadingHTTPServer((host, port), Bridge)


def start_moto(variant: str) -> Any:
    """An in-memory AWS: both tables created and seeded, throwaway signing keys. Returns the mock."""
    from moto import mock_aws

    for name, value in (
        ("AWS_ACCESS_KEY_ID", "local"),
        ("AWS_SECRET_ACCESS_KEY", "local"),
        ("AWS_DEFAULT_REGION", "us-east-1"),
        ("AWS_REGION", "us-east-1"),
        ("DEMO_TOKEN_KEY", LOCAL_TOKEN_KEY),
        ("DEMO_ADMIN_KEY", LOCAL_ADMIN_KEY),
    ):
        os.environ[name] = value
    mock = mock_aws()
    mock.start()
    from personal_agent import memory
    from returns_domain import fixtures, store

    store.reset_clients()
    store.create_table()
    fixtures.reset(variant)
    memory.set_repo(None)
    memory.get_repo().create_table()
    memory.reset_memory()
    return mock


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="0 picks a free port")
    parser.add_argument("--moto", action="store_true", help="offline mode: in-memory DynamoDB with the demo data")
    parser.add_argument("--variant", choices=VARIANTS, default="default", help="stock variant to seed (--moto only)")
    return parser.parse_args(argv)


def build(args: argparse.Namespace) -> tuple[ThreadingHTTPServer, Any]:
    """The configured server (not yet serving) and the moto mock, or None for real AWS."""
    os.environ.setdefault("AGENT_MODE", "local")
    os.environ.setdefault("GATEWAY_TRANSPORT", "local")
    os.environ.setdefault("GATEWAY_BASE_URL", f"http://{args.host}:{args.port or DEFAULT_PORT}")
    mock = start_moto(args.variant) if args.moto else None
    from api import handler as entry  # imported after the environment is set

    return make_server(entry.handler, args.host, args.port), mock


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    server, mock = build(args)
    print(
        f"API on http://{args.host}:{server.server_port}/api  "
        f"(AGENT_MODE={os.environ['AGENT_MODE']}, {'moto' if mock else 'real AWS'})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if mock is not None:
            mock.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
