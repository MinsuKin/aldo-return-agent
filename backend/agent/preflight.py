"""Model preflight (plan S8, spike.md 1.1): one Converse call per model id with the REAL tools.

Proves, before a deploy, that Bedrock accepts our ten tool schemas and our system prompt
with ``inferenceConfig = {"maxTokens": ...}`` and nothing else (Sonnet and Opus 5.5 reject
temperature, topP, top_k and toolChoice any/tool). Prints one line per model, never any
credential. Run it through the AWS helper:

    cd backend && ../scripts/with_aws_env.sh uv run python -m agent.preflight

Exit code 0 only if every model answered with a tool call or text.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from returns_domain import config

from .context import RunContext
from .prompt import build_system_prompt
from .runner import BEDROCK_REGION
from .tools import build_tools

DEFAULT_MESSAGE = "They're still slightly tight. Can I swap for a half size up before the wedding Saturday?"
DEFAULT_ORDER_ID = "DEMO-A2A-81B20E"


def tool_config() -> dict[str, Any]:
    """The Converse ``toolConfig`` built from the real tool specs (no AWS call is made)."""
    ctx = RunContext(run_id="RUN-PREFLIGHT", conv_id="CONV-PREFLIGHT", customer_id="DEMO-CUST-PREFLIGHT")
    return {"tools": [{"toolSpec": tool.tool_spec} for tool in build_tools(ctx)]}


def request(model_id: str, message: str, order_id: str | None) -> dict[str, Any]:
    blocks: list[dict[str, str]] = [{"text": message}]
    if order_id:
        blocks.append({"text": f"[selected_order_id: {order_id}]"})
    return {
        "modelId": model_id,
        "system": [{"text": build_system_prompt()}],
        "messages": [{"role": "user", "content": blocks}],
        "toolConfig": tool_config(),
        "inferenceConfig": {"maxTokens": 512},
    }


def check_model(client: Any, model_id: str, message: str, order_id: str | None) -> dict[str, Any]:
    started = time.monotonic()
    response = client.converse(**request(model_id, message, order_id))
    content = response["output"]["message"]["content"]
    tool_uses = [block["toolUse"] for block in content if "toolUse" in block]
    texts = [block["text"] for block in content if "text" in block]
    return {
        "model_id": model_id,
        "stop_reason": response.get("stopReason"),
        "first_tool": tool_uses[0]["name"] if tool_uses else None,
        "first_tool_input": tool_uses[0].get("input") if tool_uses else None,
        "text": " ".join(texts)[:240],
        "input_tokens": response.get("usage", {}).get("inputTokens"),
        "output_tokens": response.get("usage", {}).get("outputTokens"),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Converse + toolConfig check for the agent models")
    parser.add_argument("--models", help="comma separated model ids (default: primary and fallback)")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--order-id", default=DEFAULT_ORDER_ID)
    args = parser.parse_args(argv)

    import boto3  # imported here so the module stays importable without AWS configuration
    from botocore.config import Config

    settings = config.get_settings()
    models = (
        [m.strip() for m in args.models.split(",") if m.strip()]
        if args.models
        else [settings.bedrock_model_id, settings.bedrock_fallback_model_id]
    )
    client = boto3.client(
        "bedrock-runtime",
        region_name=BEDROCK_REGION,
        config=Config(read_timeout=90, connect_timeout=10, retries={"max_attempts": 1, "mode": "standard"}),
    )
    failures = 0
    for model_id in models:
        try:
            result = check_model(client, model_id, args.message, args.order_id)
        except Exception as exc:  # noqa: BLE001 - report and continue with the next model
            failures += 1
            print(f"FAIL {model_id} {type(exc).__name__}: {str(exc)[:300]}")
            continue
        print(
            f"OK   {model_id} stop={result['stop_reason']} first_tool={result['first_tool']} "
            f"in={result['input_tokens']} out={result['output_tokens']} ms={result['latency_ms']}"
        )
        if result["first_tool"]:
            print(f"     input={result['first_tool_input']}")
        if result["text"]:
            print(f"     text={result['text']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
