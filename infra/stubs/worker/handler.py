"""Stub worker Lambda, deployed until the real backend/worker/handler.py is bundled."""
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
import time

import boto3


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


def _client_known(service_name):
    try:
        boto3.client(service_name, region_name=os.environ.get("AWS_REGION", "us-east-1"))
        return True
    except Exception:
        return False


def _ssm_gateway_url():
    name = os.environ.get("GATEWAY_BASE_URL_PARAM")
    try:
        value = boto3.client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
        return {"name": name, "readable": True, "is_https_url": value.startswith("https://")}
    except Exception as exc:
        return {"name": name, "readable": False, "error": type(exc).__name__}


def _secret_keys():
    name = os.environ.get("SECRETS_ARN")
    try:
        parsed = json.loads(boto3.client("secretsmanager").get_secret_value(SecretId=name)["SecretString"])
        return {"keys": sorted(parsed), "all_non_empty": all(bool(v) for v in parsed.values())}
    except Exception as exc:
        return {"name": name, "error": type(exc).__name__}


def _bedrock_invoke_denied_check():
    """Tiny Converse call (5 output tokens), only when the event asks for it: proves the worker role may invoke the model."""
    try:
        boto3.client("bedrock-runtime").converse(
            modelId="us.anthropic.claude-sonnet-5-5",
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 5},
        )
        return {"ok": True}
    except Exception as exc:
        return {"ok": type(exc).__name__ != "AccessDeniedException", "error": type(exc).__name__}


def handler(event, context):
    result = {
        "ok": True,
        "stub": True,
        "received_keys": sorted(event) if isinstance(event, dict) else None,
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "boto3": boto3.__version__,
        "pydantic": _version("pydantic"),
        "strands_agents": _version("strands-agents"),
        "imports": {
            "pydantic": _import_check("pydantic"),
            "strands": _import_check("strands"),
        },
        "packages_present": {
            name: importlib.util.find_spec(name) is not None for name in ("returns_domain", "personal_agent")
        },
        "bedrock_agentcore_client_available": _client_known("bedrock-agentcore"),
        "agent_runtime_arn_set": bool(os.environ.get("AGENT_RUNTIME_ARN")),
        "agent_runtime_arn_is_placeholder": "aldo_placeholder_not_deployed" in os.environ.get("AGENT_RUNTIME_ARN", ""),
        "table_name": os.environ.get("TABLE_NAME"),
        "personal_agent_table_name": os.environ.get("PERSONAL_AGENT_TABLE_NAME"),
        "gateway_transport": os.environ.get("GATEWAY_TRANSPORT"),
        "delegation_max_cents": os.environ.get("DELEGATION_MAX_CENTS"),
        "gateway_url_param": _ssm_gateway_url(),
        "secret": _secret_keys(),
        "request_id": context.aws_request_id,
    }
    if isinstance(event, dict) and event.get("check_bedrock"):
        result["bedrock_converse"] = _bedrock_invoke_denied_check()
    print(json.dumps({"msg": "worker stub invoked", **{k: v for k, v in result.items() if k != "secret"}}))
    return result
