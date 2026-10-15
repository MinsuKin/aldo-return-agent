"""HTTP API v2 event builder, a tiny client over the resolver and small fakes for the API tests."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

from api.http import Api

DOMAIN_NAME = "abc123.execute-api.us-east-1.amazonaws.com"


class FakeContext:
    """Just enough of the Lambda context for Powertools and the handlers."""

    function_name = "aldo-returns-demo-api"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:aldo-returns-demo-api"

    def __init__(self, request_id: str = "req-test-0001", remaining_ms: int = 15_000) -> None:
        self.aws_request_id = request_id
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def v2_event(
    method: str,
    path: str,
    *,
    body: Any = None,
    raw_body: str | None = None,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
    request_id: str = "req-test-0001",
    base64_body: bool = False,
) -> dict[str, Any]:
    """A payload-format-2.0 event as API Gateway sends it for the ``$default`` stage."""
    text = raw_body if raw_body is not None else (json.dumps(body) if body is not None else None)
    if text is not None and base64_body:
        text = base64.b64encode(text.encode()).decode()
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    if text is not None:
        lowered.setdefault("content-type", "application/json")
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": urlencode(query or {}),
        "headers": lowered,
        "queryStringParameters": dict(query) if query else None,
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "abc123",
            "domainName": DOMAIN_NAME,
            "domainPrefix": "abc123",
            "http": {
                "method": method,
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.9",
                "userAgent": "pytest",
            },
            "requestId": request_id,
            "routeKey": "$default",
            "stage": "$default",
            "time": "16/Oct/2026:16:00:00 +0000",
            "timeEpoch": 1_792_166_400_000,
        },
        "body": text,
        "isBase64Encoded": bool(text is not None and base64_body),
    }


@dataclass
class Reply:
    """What the client got back."""

    status: int
    headers: dict[str, str]
    text: str

    @property
    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def error(self) -> dict[str, Any]:
        return self.json["error"]

    @property
    def code(self) -> str:
        return self.error["code"]


class ApiClient:
    """Calls ``api.resolver.resolve`` exactly like the Lambda handler does."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self._calls = 0

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw_body: str | None = None,
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        base64_body: bool = False,
    ) -> Reply:
        self._calls += 1
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        event = v2_event(
            method,
            path,
            body=body,
            raw_body=raw_body,
            headers=merged,
            query=query,
            request_id=f"req-test-{self._calls:04d}",
            base64_body=base64_body,
        )
        result = self.api.resolver.resolve(event, FakeContext(f"req-test-{self._calls:04d}"))
        headers_out = {k.lower(): v for k, v in (result.get("headers") or {}).items()}
        return Reply(result["statusCode"], headers_out, result.get("body") or "")

    def get(self, path: str, **kwargs: Any) -> Reply:
        return self.call("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Reply:
        return self.call("POST", path, body=body, **kwargs)
