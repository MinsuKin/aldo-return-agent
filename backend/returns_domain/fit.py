"""Fit insight lookup over ``fixtures/derived/fit_insights.json`` (requirement 5, design 5.2).

The derived file holds per-product counts of returns whose reason is exactly "Too small" or
"Too big", for ALDO_CA and for all banners. This module turns counts into a signal with the
thresholds of requirement 5.3, using exact integer arithmetic (no float comparisons):

* ``n_fit = too_small + too_big``; fewer than 30 -> ``insufficient_data``
* ``too_small / n_fit >= 0.65`` -> ``runs_small``; ``<= 0.35`` -> ``runs_large``; else ``mixed``

The signal is always recomputed from the counts; the file's own ``signal`` is not trusted.
Lookup is exact on the product name. The headline scope is ALDO_CA; ALL banners are used
only when ALDO_CA has too little data, and the result says which scope it used (G-22).

``search_product_names`` / ``resolve_product_name`` turn a free-text product query from the
personal agent (untrusted input) into an exact product name by case-insensitive substring
match, or an UNKNOWN_STYLE error that lists the candidates (PIVOT_ADDENDUM 5.2, C7). The
gateway passes the names of the products ALDO actually sells; without ``names`` they search
the product names of the derived file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import config, data_source
from .errors import DomainError, ErrorCode

MIN_N = 30
RUNS_SMALL_MIN_SHARE = 0.65  # documented value; comparisons use the exact fractions below
RUNS_LARGE_MAX_SHARE = 0.35
_SMALL_NUM, _LARGE_NUM, _DEN = 13, 7, 20  # 13/20 = 0.65, 7/20 = 0.35

RUNS_SMALL = "runs_small"
RUNS_LARGE = "runs_large"
MIXED = "mixed"
INSUFFICIENT_DATA = "insufficient_data"
SIGNALS: tuple[str, ...] = (RUNS_SMALL, RUNS_LARGE, MIXED, INSUFFICIENT_DATA)

SCOPE_ALDO_CA = "ALDO_CA"
SCOPE_ALL = "ALL"
PROVENANCE = "derived"

_SOURCE_KEYS = ("kind", "file", "sha256", "rows_read")


def classify(too_small: int, too_big: int) -> str:
    """Signal for a pair of counts (requirement 5.3)."""
    n_fit = too_small + too_big
    if n_fit < MIN_N:
        return INSUFFICIENT_DATA
    if too_small * _DEN >= _SMALL_NUM * n_fit:
        return RUNS_SMALL
    if too_small * _DEN <= _LARGE_NUM * n_fit:
        return RUNS_LARGE
    return MIXED


def small_share(too_small: int, too_big: int) -> float | None:
    """too_small / (too_small + too_big), rounded to 4 places; None when there are no fit returns."""
    n_fit = too_small + too_big
    return None if n_fit == 0 else round(too_small / n_fit, 4)


def get_fit_insight(
    product_name: str, insights: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Fit insight for ``product_name`` (exact match). Unknown products give insufficient_data.

    ``insights`` is the parsed derived file; it defaults to the file under the configured
    fixtures directory. The result is JSON-safe (ints, strings, floats, dicts).
    """
    if not isinstance(product_name, str) or not product_name.strip():
        raise DomainError(ErrorCode.VALIDATION_ERROR, "product_name is required")
    name = product_name.strip()
    data = load_insights() if insights is None else insights

    products = data.get("products")
    entry = products.get(name) if isinstance(products, Mapping) else None
    scopes: dict[str, dict[str, Any]] = {}
    if isinstance(entry, Mapping):
        for scope in (SCOPE_ALDO_CA, SCOPE_ALL):
            if scope in entry:
                scopes[scope] = _scope_view(scope, entry[scope])

    primary = scopes.get(SCOPE_ALDO_CA)
    wider = scopes.get(SCOPE_ALL)
    if primary is not None and primary["signal"] != INSUFFICIENT_DATA:
        headline = primary
    elif wider is not None and wider["signal"] != INSUFFICIENT_DATA:
        headline = wider
    else:
        headline = primary if primary is not None else _scope_view(SCOPE_ALDO_CA, None)

    source = data.get("source")
    origin = data_source.source_info(data)
    return {
        "product_name": name,
        "found": isinstance(entry, Mapping),
        "scope": headline["scope"],
        "signal": headline["signal"],
        "too_small": headline["too_small"],
        "too_big": headline["too_big"],
        "n_fit": headline["n_fit"],
        "total_returns": headline["total_returns"],
        "too_small_share": headline["too_small_share"],
        "scopes": scopes,
        "min_n": MIN_N,
        "provenance": PROVENANCE,
        "source": (
            {key: source[key] for key in _SOURCE_KEYS if key in source}
            if isinstance(source, Mapping)
            else {}
        ),
        # Only a curated (platform) document says where it came from; the bundled file keeps today's exact output.
        **({} if origin["data_source"] == data_source.SOURCE_BUNDLED else {"data_source": origin["data_source"], "query_execution_ids": origin["query_execution_ids"]}),
    }


# ------------------------------------------------------------------- query -> product name

MAX_QUERY_CHARS = 64
DEFAULT_CANDIDATE_LIMIT = 10


def search_product_names(
    query: str,
    names: Iterable[str] | None = None,
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[str]:
    """Product names containing ``query`` (case-insensitive), best matches first.

    Order: exact match, then names starting with the query, then the rest, each group
    alphabetical. ``names`` defaults to the product names of the derived file. At most
    ``limit`` names are returned.
    """
    _check_limit(limit)
    return _ranked_matches(_clean_query(query), names)[:limit]


def resolve_product_name(
    query: str,
    names: Iterable[str] | None = None,
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> str:
    """The single product name ``query`` stands for, or UNKNOWN_STYLE with ``candidates``.

    An exact (case-insensitive) name wins even when longer names also contain the query;
    otherwise exactly one substring match is required. With no match or several matches the
    error's ``details["candidates"]`` lists up to ``limit`` names (empty when nothing matched).
    """
    _check_limit(limit)
    needle = _clean_query(query)
    matches = _ranked_matches(needle, names)
    exact = [name for name in matches if name.casefold() == needle.casefold()]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    message = (
        "No product matches the query."
        if not matches
        else "Several products match the query; use one of the candidates."
    )
    raise DomainError(
        ErrorCode.UNKNOWN_STYLE, message, details={"candidates": matches[:limit]}
    )


def _check_limit(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "limit must be a positive integer")


def _clean_query(query: object) -> str:
    if not isinstance(query, str) or not query.strip():
        raise DomainError(ErrorCode.VALIDATION_ERROR, "product_query is required")
    text = " ".join(query.split())
    if len(text) > MAX_QUERY_CHARS:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            f"product_query must be at most {MAX_QUERY_CHARS} characters",
        )
    return text


def _ranked_matches(needle: str, names: Iterable[str] | None) -> list[str]:
    pool: Iterable[str] = load_insights()["products"].keys() if names is None else names
    folded_needle = needle.casefold()
    exact: list[str] = []
    prefix: list[str] = []
    inner: list[str] = []
    seen: set[str] = set()
    for name in pool:
        if not isinstance(name, str) or name in seen:
            continue
        folded = name.casefold()
        if folded_needle not in folded:
            continue
        seen.add(name)
        if folded == folded_needle:
            exact.append(name)
        elif folded.startswith(folded_needle):
            prefix.append(name)
        else:
            inner.append(name)
    return [
        name
        for group in (exact, prefix, inner)
        for name in sorted(group, key=lambda value: (value.casefold(), value))
    ]


# ------------------------------------------------------------------------------ loading

_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def clear_cache() -> None:
    _CACHE.clear()


def load_insights(path: Path | str | None = None) -> dict[str, Any]:
    """Parsed derived file, cached until the file changes. Raises INTERNAL_ERROR if unusable.

    Without a path the data-platform door decides (``data_source``): the bundled file (the default), the curated document
    from S3, or the bundled file while the curated one is compared in the background (shadow). Any platform problem
    falls back to the bundled file, so this is never worse than before.
    """
    if path is None:
        return data_source.resolve_insights(_load_bundled)
    return _load_bundled(path)


def _load_bundled(path: Path | str | None = None) -> dict[str, Any]:
    target = config.get_settings().fit_insights_path if path is None else Path(path)
    try:
        stat = target.stat()
    except OSError:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "fit insights file is not available") from None
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(str(target))
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "fit insights file is unreadable") from None
    if not isinstance(data, dict) or not isinstance(data.get("products"), dict):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "fit insights file is malformed")
    _CACHE[str(target)] = (signature, data)
    return data


# ------------------------------------------------------------------------------ helpers


def _scope_view(scope: str, entry: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = entry if isinstance(entry, Mapping) else {}
    too_small = _count(raw, "too_small")
    too_big = _count(raw, "too_big")
    n_fit = too_small + too_big
    return {
        "scope": scope,
        "too_small": too_small,
        "too_big": too_big,
        "n_fit": n_fit,
        "total_returns": _count(raw, "total_returns", default=n_fit),
        "too_small_share": small_share(too_small, too_big),
        "signal": classify(too_small, too_big),
    }


def _count(raw: Mapping[str, Any], key: str, *, default: int = 0) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainError(ErrorCode.INTERNAL_ERROR, f"fit insights file has an invalid {key}")
    return value
