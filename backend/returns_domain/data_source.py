"""Where the fit insights come from: the bundled file, or the document the AWS data pipeline wrote.

The "two-way door" of docs/data-platform/DESIGN.md. Three modes:

* ``static`` (the default): the bundled ``fixtures/derived/fit_insights.json``, exactly as before.
* ``platform``: the curated document in S3 (written by the Athena pipeline, same schema). Any problem falls back to the
  bundled file for that call, so the demo is never worse than ``static``.
* ``shadow``: serve the bundled file, but load the curated document too and log how it differs (once per document).

The mode is read from (first match): env ``ALDO_DATA_MODE``, the SSM parameter ``/aldo-demo/data-platform/mode``, ``static``.
SSM and S3 are only touched when ``AWS_LAMBDA_FUNCTION_NAME`` or ``ALDO_DATA_SSM=1`` is set, so unit tests and local runs never
make a network call unless a test asks for it. No Athena call is ever made here: the request path reads one cached S3 object.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

MODE_STATIC = "static"
MODE_PLATFORM = "platform"
MODE_SHADOW = "shadow"
MODES: tuple[str, ...] = (MODE_STATIC, MODE_PLATFORM, MODE_SHADOW)

SOURCE_BUNDLED = "bundled-json"
SOURCE_PLATFORM = "aws-athena"

ENV_MODE = "ALDO_DATA_MODE"
ENV_ENABLE = "ALDO_DATA_SSM"
PARAM_MODE = "/aldo-demo/data-platform/mode"
PARAM_CONFIG = "/aldo-demo/data-platform/config"
DEFAULT_PREFIX = "curated/"  # what the AldoDataPlatform stack writes into the SSM config as `curated_prefix`
FIT_DIR = "fit_insights/"  # where build_insights.py keeps the fit document and its `latest.json` pointer


def pointer_key(prefix: str | None) -> str:
    """The pointer object, `curated/fit_insights/latest.json`, however the config spells the prefix.

    The stack writes ``curated/``; ``curated`` and ``curated/fit_insights/`` are accepted too, so a hand-edited config
    cannot make the platform quietly unreachable (the door would then fall back to static, which hides the mistake).
    """
    base = (prefix or DEFAULT_PREFIX).strip() or DEFAULT_PREFIX
    if not base.endswith("/"):
        base += "/"
    if not base.endswith(FIT_DIR):
        base += FIT_DIR
    return f"{base}latest.json"

MODE_TTL_S = 15.0
CONFIG_TTL_S = 60.0
POINTER_TTL_S = 60.0

_lock = threading.RLock()
_clock: Callable[[], float] = time.monotonic  # replaced in tests


class PlatformUnavailable(Exception):
    """The curated document cannot be used right now (the caller falls back to the bundled file)."""


@dataclass(frozen=True)
class PlatformDoc:
    doc: dict[str, Any]
    key: str
    etag: str
    generated_at: str
    query_execution_ids: tuple[str, ...]


# ------------------------------------------------------------------------------ state (module level, resettable)

_mode_cache: tuple[float, str] | None = None
_config_cache: tuple[float, dict[str, Any] | None] | None = None
_pointer_cache: tuple[float, PlatformDoc] | None = None
_shadow_seen: dict[str, dict[str, Any]] = {}
_last_error: str | None = None
_clients: dict[str, Any] = {}


def reset_for_tests() -> None:
    global _mode_cache, _config_cache, _pointer_cache, _last_error
    with _lock:
        _mode_cache = _config_cache = _pointer_cache = None
        _last_error = None
        _shadow_seen.clear()
        _clients.clear()


# ------------------------------------------------------------------------------ AWS access (lazy, short timeouts)


def _aws_enabled() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME")) or os.environ.get(ENV_ENABLE, "").strip().lower() in ("1", "true", "yes")


def _client(service: str) -> Any:
    with _lock:
        existing = _clients.get(service)
        if existing is not None:
            return existing
        import boto3
        from botocore.config import Config

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        cfg = Config(connect_timeout=1, read_timeout=2, retries={"max_attempts": 1}, region_name=region)
        client = boto3.client(service, config=cfg)
        _clients[service] = client
        return client


def _ssm_value(name: str) -> str | None:
    try:
        return str(_client("ssm").get_parameter(Name=name)["Parameter"]["Value"])
    except Exception as exc:  # missing parameter, no permission, timeout: all mean "not configured"
        log.debug("data_platform_ssm_unavailable name=%s error=%s", name, type(exc).__name__)
        return None


# ------------------------------------------------------------------------------ mode


def current_mode() -> str:
    """env ALDO_DATA_MODE > SSM mode parameter (15 s cache) > static. Anything unexpected means static."""
    forced = os.environ.get(ENV_MODE, "").strip().lower()
    if forced in MODES:
        return forced
    if not _aws_enabled():
        return MODE_STATIC
    global _mode_cache
    now = _clock()
    with _lock:
        if _mode_cache is not None and now - _mode_cache[0] < MODE_TTL_S:
            return _mode_cache[1]
    value = (_ssm_value(PARAM_MODE) or MODE_STATIC).strip().lower()
    mode = value if value in MODES else MODE_STATIC
    with _lock:
        _mode_cache = (now, mode)
    return mode


def set_mode(mode: str) -> dict[str, Any]:
    """Write the SSM mode parameter (admin route). Raises ValueError for an unknown mode."""
    wanted = (mode or "").strip().lower()
    if wanted not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    _client("ssm").put_parameter(Name=PARAM_MODE, Value=wanted, Type="String", Overwrite=True)
    global _mode_cache
    with _lock:
        _mode_cache = (_clock(), wanted)
    return status()


# ------------------------------------------------------------------------------ the curated document


def _platform_config() -> dict[str, Any]:
    global _config_cache
    now = _clock()
    with _lock:
        if _config_cache is not None and now - _config_cache[0] < CONFIG_TTL_S and _config_cache[1] is not None:
            return _config_cache[1]
    raw = _ssm_value(PARAM_CONFIG)
    if not raw:
        raise PlatformUnavailable("data platform config is not set")
    try:
        cfg = json.loads(raw)
    except ValueError:
        raise PlatformUnavailable("data platform config is not JSON") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("bucket"), str) or not cfg["bucket"]:
        raise PlatformUnavailable("data platform config has no bucket")
    with _lock:
        _config_cache = (now, cfg)
    return cfg


def _valid(doc: object) -> bool:
    if not isinstance(doc, dict):
        return False
    products = doc.get("products")
    if not isinstance(products, dict) or not products:
        return False
    sample = next(iter(products.values()))
    return isinstance(sample, Mapping) and isinstance(doc.get("thresholds"), Mapping) and isinstance(doc.get("totals"), Mapping)


def _platform_doc() -> PlatformDoc:
    """The curated document, cached; the pointer object is re-read at most every 60 s. Raises PlatformUnavailable."""
    global _pointer_cache, _last_error
    now = _clock()
    with _lock:
        if _pointer_cache is not None and now - _pointer_cache[0] < POINTER_TTL_S:
            return _pointer_cache[1]
    try:
        cfg = _platform_config()
        bucket = cfg["bucket"]
        s3 = _client("s3")
        pointer = json.loads(s3.get_object(Bucket=bucket, Key=pointer_key(cfg.get("curated_prefix")))["Body"].read())
        key = pointer.get("key")
        if not isinstance(key, str) or not key:
            raise PlatformUnavailable("latest.json has no key")
        with _lock:
            cached = _pointer_cache[1] if _pointer_cache is not None else None
        if cached is not None and cached.key == key and cached.etag == str(pointer.get("etag", "")):
            current = cached
        else:
            obj = s3.get_object(Bucket=bucket, Key=key)
            doc = json.loads(obj["Body"].read())
            if not _valid(doc):
                raise PlatformUnavailable("curated document is malformed")
            platform = doc.get("platform") if isinstance(doc.get("platform"), Mapping) else {}
            current = PlatformDoc(
                doc=doc,
                key=key,
                etag=str(pointer.get("etag") or obj.get("ETag", "")),
                generated_at=str(doc.get("generated_at") or pointer.get("generated_at") or ""),
                query_execution_ids=tuple(str(q) for q in (platform.get("query_execution_ids") or [])),
            )
        with _lock:
            _pointer_cache = (now, current)
            _last_error = None
        return current
    except PlatformUnavailable as exc:
        _remember_error(str(exc))
        raise
    except Exception as exc:  # S3 error, timeout, bad JSON: never raised to the caller
        _remember_error(type(exc).__name__)
        raise PlatformUnavailable(type(exc).__name__) from exc


def _remember_error(message: str) -> None:
    global _last_error
    with _lock:
        _last_error = message


# ------------------------------------------------------------------------------ the seam used by returns_domain.fit


def resolve_insights(static_loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """The insights document for the current mode. ``static_loader`` loads the bundled file (and raises as it always did)."""
    mode = current_mode()
    if mode == MODE_STATIC:
        return static_loader()
    try:
        platform = _platform_doc()
    except PlatformUnavailable as exc:
        log.warning("data_platform_fallback mode=%s reason=%s", mode, exc)
        return static_loader()
    if mode == MODE_PLATFORM:
        return platform.doc
    bundled = static_loader()  # shadow
    _compare_once(platform, bundled)
    return bundled


def source_info(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Provenance of a loaded document for the trace: ``bundled-json`` or ``aws-athena`` with the Athena query ids."""
    platform = doc.get("platform")
    if isinstance(platform, Mapping) and platform.get("engine") == "athena":
        return {
            "data_source": SOURCE_PLATFORM,
            "query_execution_ids": [str(q) for q in (platform.get("query_execution_ids") or [])][:6],
            "generated_at": str(doc.get("generated_at") or ""),
        }
    return {"data_source": SOURCE_BUNDLED}


def _counts(entry: object) -> tuple[Any, ...]:
    out: list[Any] = []
    for scope in ("ALDO_CA", "ALL"):
        view = entry.get(scope) if isinstance(entry, Mapping) else None
        view = view if isinstance(view, Mapping) else {}
        out.append((view.get("too_small"), view.get("too_big"), view.get("total_returns")))
    return tuple(out)


def compare_documents(platform_doc: Mapping[str, Any], bundled_doc: Mapping[str, Any]) -> dict[str, Any]:
    """Counts of products whose per-scope counts differ or that exist on one side only."""
    pp, bp = platform_doc.get("products", {}), bundled_doc.get("products", {})
    mismatched = [name for name in bp if name in pp and _counts(pp[name]) != _counts(bp[name])]
    return {
        "products_bundled": len(bp),
        "products_platform": len(pp),
        "mismatched": len(mismatched),
        "only_bundled": sum(1 for n in bp if n not in pp),
        "only_platform": sum(1 for n in pp if n not in bp),
        "examples": mismatched[:5],
    }


def _compare_once(platform: PlatformDoc, bundled: Mapping[str, Any]) -> None:
    with _lock:
        if platform.key in _shadow_seen:
            return
    result = compare_documents(platform.doc, bundled)
    with _lock:
        _shadow_seen[platform.key] = result
    if result["mismatched"] or result["only_bundled"]:
        log.warning("data_platform_shadow_mismatch key=%s result=%s", platform.key, json.dumps(result))
    else:
        log.info("data_platform_shadow_ok key=%s products=%d", platform.key, result["products_platform"])


# ------------------------------------------------------------------------------ admin view


def status() -> dict[str, Any]:
    """What the admin route returns: the mode, whether the curated document can be loaded, and shadow results so far."""
    mode = current_mode()
    out: dict[str, Any] = {"mode": mode, "modes": list(MODES), "aws_enabled": _aws_enabled(), "available": False}
    if _aws_enabled():
        try:
            platform = _platform_doc()
            out["available"] = True
            out["curated"] = {
                "key": platform.key,
                "generated_at": platform.generated_at,
                "products": len(platform.doc.get("products", {})),
                "query_execution_ids": list(platform.query_execution_ids)[:6],
            }
        except PlatformUnavailable as exc:
            out["error"] = str(exc)
    with _lock:
        shadow = dict(_shadow_seen)
    if shadow:
        out["shadow"] = shadow
    return out
