#!/usr/bin/env python3
"""Add demo aliases to fixtures/derived/fit_insights.json.

Reads fixtures/demo/fit_aliases.json (alias -> real ALDO_CA style) and gives every alias its own
entry in fit_insights.json["products"]: the ALDO_CA and ALL scope counts and signals are copied from
the real style, plus "alias_of" (the real style) and a one-line "note". returns_domain.fit matches
product names exactly, so the demo name of a product (ALDO Men 001) then reaches the real return
counts of the style behind it (Toney, the real men's style TONEY) without any change to fit.py.

Only the alias entries change. generated_at, provenance, source (file, sha256, rows_read), filter,
thresholds and totals (they describe the real data; aliases are not counted) and every real product
stay as they are, and the file keeps the one-product-per-line layout of build_fit_insights.py.

Idempotent: entries that carry "alias_of" are rebuilt from the mapping on every run, so a second run
writes nothing and an alias removed from the mapping disappears. scripts/build_fit_insights.py
rewrites the file without aliases, so run this script again after every rebuild.

Usage:
    python3.12 scripts/add_fit_aliases.py [--insights JSON] [--aliases JSON] [--check]

Exit codes: 0 done (or up to date with --check), 1 --check found the file out of date, 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:  # also importable from tests that load this file by path
    sys.path.insert(0, str(SCRIPTS))

from build_fit_insights import render  # noqa: E402  same one-product-per-line layout as the build script

DEFAULT_INSIGHTS = ROOT / "fixtures" / "derived" / "fit_insights.json"
DEFAULT_ALIASES = ROOT / "fixtures" / "demo" / "fit_aliases.json"
SCOPES = ("ALDO_CA", "ALL")


class AliasError(ValueError):
    """The mapping or the fit file cannot be used."""


def load_mapping(path: Path) -> dict[str, str]:
    """``{alias: real style}`` from ``fit_aliases.json`` (the object under ``aliases``)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AliasError(f"cannot read {path.name}: {exc}") from None
    aliases = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(aliases, dict) or not aliases:
        raise AliasError(f"{path.name} needs a non-empty object under 'aliases'")
    for alias, real in aliases.items():
        if not isinstance(real, str) or not real.strip() or not alias.strip() or alias != alias.strip():
            raise AliasError(f"{path.name}: {alias!r} -> {real!r} must be two non-blank, trimmed names")
        if alias == real:
            raise AliasError(f"{path.name}: {alias!r} cannot be an alias of itself")
    return dict(aliases)


def is_alias(entry: Any) -> bool:
    return isinstance(entry, Mapping) and "alias_of" in entry


def add_aliases(doc: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    """A copy of ``doc`` whose products are the real entries plus exactly one entry per alias."""
    real_products = {name: entry for name, entry in doc["products"].items() if not is_alias(entry)}
    added: dict[str, Any] = {}
    for alias, real in aliases.items():
        if alias in real_products:
            raise AliasError(f"{alias!r} is the name of a real product; an alias must not replace it")
        entry = real_products.get(real)
        if not isinstance(entry, Mapping) or not all(isinstance(entry.get(s), Mapping) for s in SCOPES):
            raise AliasError(f"{real!r} is not a product of the fit file with both scopes")
        added[alias] = {
            **{scope: dict(entry[scope]) for scope in SCOPES},
            "alias_of": real,
            "note": f"Demo alias of the real style {real}: counts and signals are copied from its OMS fit returns (24 months).",
        }
    return {**doc, "products": _insert_sorted(real_products, added)}


def _insert_sorted(real: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """``real`` in its own order with each ``extra`` entry placed where a sorted file would hold it."""
    pending = sorted(extra)
    out: dict[str, Any] = {}
    for name, entry in real.items():
        while pending and pending[0] < name:
            key = pending.pop(0)
            out[key] = extra[key]
        out[name] = entry
    for key in pending:
        out[key] = extra[key]
    return out


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--insights", type=Path, default=DEFAULT_INSIGHTS, help="fit_insights.json to update")
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES, help="fit_aliases.json (alias -> real style)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 when the file is out of date")
    args = parser.parse_args(argv)

    try:
        aliases = load_mapping(args.aliases)
        text = args.insights.read_text(encoding="utf-8")
        doc = json.loads(text)
        if not isinstance(doc, dict) or not isinstance(doc.get("products"), dict):
            raise AliasError(f"{args.insights.name} has no 'products' object")
        updated_doc = add_aliases(doc, aliases)
    except (AliasError, OSError, ValueError) as exc:
        print(f"add_fit_aliases: {exc}", file=sys.stderr)
        return 2

    updated = render(updated_doc)
    for alias, real in aliases.items():
        scope = updated_doc["products"][alias]["ALDO_CA"]
        print(f"{alias} -> {real} (ALDO_CA n_fit {scope['n_fit']}, {scope['signal']})")
    if updated == text:
        print(f"{args.insights.name} is up to date")
        return 0
    if args.check:
        print(f"{args.insights.name} is out of date; run scripts/add_fit_aliases.py", file=sys.stderr)
        return 1
    _write(args.insights, updated)
    print(f"wrote {args.insights} ({args.insights.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
