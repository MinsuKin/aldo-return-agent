"""Demo fixtures: load ``fixtures/demo/*.json``, turn them into table items, seed and reset.

The seven JSON files are synthetic reference data (contracts.md section (d)). The loader
validates them (ids start with ``DEMO-``, every record is ``source: synthetic``, sizes are
strings), derives every ``pk``/``sk``/GSI key through the ``store`` key builders and applies
the reset variant's inventory overrides. ``reset`` deletes every runtime partition
(``store.RUNTIME_PREFIXES``: conversations, runs, policy checks, quotes, transactions, cases,
audit, size requests, the write-back outbox and the orders the agent-to-agent purchase created)
and then writes the fixture items again, which also restores inventory and order lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel

from . import config, store
from .errors import DomainError, ErrorCode

VARIANTS: tuple[str, ...] = ("default", "dc_stockout", "all_stockout")
DEMO_ID_PREFIX = "DEMO-"
FIXTURE_FILES: tuple[str, ...] = (
    "customers.json",
    "orders.json",
    "products.json",
    "locations.json",
    "inventory.json",
    "logistics.json",
    "policy.json",
)

#: Same tuple as ``store.RUNTIME_PREFIXES`` (the addendum calls it ``fixtures.RUNTIME_PREFIXES``).
RUNTIME_PREFIXES = store.RUNTIME_PREFIXES
FIXTURE_PREFIXES = store.FIXTURE_PREFIXES


class ResetSummary(BaseModel):
    variant: str
    deleted: int
    written: int
    counts: dict[str, int]


@dataclass(frozen=True)
class FixtureBundle:
    customers: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    products: list[dict[str, Any]]
    locations: list[dict[str, Any]]
    inventory_base: list[dict[str, Any]]
    inventory_variants: dict[str, dict[str, Any]]
    logistics: dict[str, Any]
    policy: dict[str, Any]
    directory: Path = field(default_factory=Path)


# ------------------------------------------------------------------------------ loading

_JSON_CACHE: dict[str, tuple[tuple[int, int], Any]] = {}


def _demo_dir(fixtures_dir: Path | str | None = None) -> Path:
    root = Path(fixtures_dir) if fixtures_dir is not None else config.get_settings().fixtures_dir
    return root / "demo" if (root / "demo").is_dir() else root


def _read_json(path: Path) -> Any:
    try:
        stat = path.stat()
    except OSError:
        raise _fixture_error(f"{path.name} is missing") from None
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _JSON_CACHE.get(str(path))
    if cached is not None and cached[0] == signature:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _fixture_error(f"{path.name} is not valid JSON") from None
    _JSON_CACHE[str(path)] = (signature, data)
    return data


def _fixture_error(message: str) -> DomainError:
    return DomainError(ErrorCode.VALIDATION_ERROR, f"fixture error: {message}")


def _collection(data: Any, name: str, file: str) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise _fixture_error(f"{file} must be an object with schema_version 1")
    records = data.get(name)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise _fixture_error(f"{file} must hold a list under '{name}'")
    return records


def _require_demo(record: Mapping[str, Any], fields: tuple[str, ...], where: str) -> None:
    if record.get("source") != "synthetic":
        raise _fixture_error(f"{where} must have source 'synthetic'")
    for name in fields:
        value = record.get(name)
        if not isinstance(value, str) or not value.startswith(DEMO_ID_PREFIX):
            raise _fixture_error(f"{where} field {name} must start with {DEMO_ID_PREFIX}")


def _require_size(record: Mapping[str, Any], where: str) -> None:
    size = record.get("size")
    if not isinstance(size, str) or store.size_str(size) != size:
        raise _fixture_error(f"{where} size must be a string with one decimal")


def load_bundle(fixtures_dir: Path | str | None = None) -> FixtureBundle:
    """Read and validate the seven fixture files."""
    directory = _demo_dir(fixtures_dir)
    raw = {name: _read_json(directory / name) for name in FIXTURE_FILES}

    customers = _collection(raw["customers.json"], "customers", "customers.json")
    orders = _collection(raw["orders.json"], "orders", "orders.json")
    products = _collection(raw["products.json"], "products", "products.json")
    locations = _collection(raw["locations.json"], "locations", "locations.json")
    inventory = raw["inventory.json"]
    base = _collection(inventory, "base", "inventory.json")
    variants = inventory.get("variants")
    if not isinstance(variants, dict) or set(variants) != set(VARIANTS):
        raise _fixture_error(f"inventory.json variants must be exactly {list(VARIANTS)}")
    logistics, policy = raw["logistics.json"], raw["policy.json"]

    for record in customers:
        _require_demo(record, ("customer_id",), f"customer {record.get('customer_id')}")
    for record in locations:
        _require_demo(record, ("location_id",), f"location {record.get('location_id')}")
    for record in products:
        _require_demo(record, ("sku", "style"), f"product {record.get('sku')}")
        _require_size(record, f"product {record.get('sku')}")
    for order in orders:
        _require_demo(order, ("order_id", "customer_id"), f"order {order.get('order_id')}")
        lines = order.get("lines")
        if not isinstance(lines, list) or not lines:
            raise _fixture_error(f"order {order.get('order_id')} needs at least one line")
        for line in lines:
            _require_demo(line, ("line_id", "sku", "style"), f"line {line.get('line_id')}")
            _require_size(line, f"line {line.get('line_id')}")
    for row in base:
        _require_demo(row, ("sku", "location_id"), f"inventory row {row.get('sku')}")
    if policy.get("source") != "synthetic" or logistics.get("source") != "synthetic":
        raise _fixture_error("policy.json and logistics.json must have source 'synthetic'")

    customer_ids = {c["customer_id"] for c in customers}
    location_ids = {loc["location_id"] for loc in locations}
    skus = {p["sku"] for p in products}
    for order in orders:
        if order["customer_id"] not in customer_ids:
            raise _fixture_error(f"order {order['order_id']} has an unknown customer")
        for line in order["lines"]:
            if line["sku"] not in skus:
                raise _fixture_error(f"line {line['line_id']} has an unknown sku")
    for row in base:
        if row["sku"] not in skus or row["location_id"] not in location_ids:
            raise _fixture_error(f"inventory row {row['sku']}/{row['location_id']} is dangling")

    return FixtureBundle(
        customers=customers,
        orders=orders,
        products=products,
        locations=locations,
        inventory_base=base,
        inventory_variants=variants,
        logistics=logistics,
        policy=policy,
        directory=directory,
    )


# -------------------------------------------------------------------------- item building


def _check_variant(variant: str) -> None:
    if variant not in VARIANTS:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"variant must be one of: {', '.join(VARIANTS)}"
        )


def _inventory_rows(bundle: FixtureBundle, variant: str) -> list[dict[str, Any]]:
    rows = {(r["sku"], r["location_id"]): dict(r) for r in bundle.inventory_base}
    overrides = bundle.inventory_variants.get(variant, {}).get("overrides") or []
    for override in overrides:
        key = (override.get("sku"), override.get("location_id"))
        if key not in rows:
            raise _fixture_error(f"variant {variant} overrides {key[0]}/{key[1]} that has no base row")
        atp = override.get("atp")
        if isinstance(atp, bool) or not isinstance(atp, int) or atp < 0:
            raise _fixture_error(f"variant {variant} override {key[0]}/{key[1]} needs a whole atp")
        rows[key]["atp"] = atp
    for row in rows.values():
        row["version"] = 1  # a reset always starts every counter at 1
    return list(rows.values())


def build_items(bundle: FixtureBundle, variant: str = "default") -> list[dict[str, Any]]:
    """Every table item for ``variant``, keys included. Raises VALIDATION_ERROR for bad variants."""
    _check_variant(variant)
    items: list[dict[str, Any]] = []

    for customer in bundle.customers:
        pk, sk = store.customer_key(customer["customer_id"])
        items.append({**customer, "pk": pk, "sk": sk})

    for location in bundle.locations:
        pk, sk = store.location_key(location["location_id"])
        items.append({**location, "pk": pk, "sk": sk})

    for product in bundle.products:
        pk, sk = store.product_key(product["sku"])
        g_pk, g_sk = store.gsi1_product(product["style"], product["color"], product["size"])
        items.append({**product, "pk": pk, "sk": sk, "gsi1pk": g_pk, "gsi1sk": g_sk})

    for row in _inventory_rows(bundle, variant):
        pk, sk = store.inventory_key(row["sku"], row["location_id"])
        items.append({**row, "pk": pk, "sk": sk})

    for order in bundle.orders:
        lines = [dict(line) for line in order["lines"]]
        items.extend(store.order_items(order, lines))

    keys = [(item["pk"], item["sk"]) for item in items]
    if len(keys) != len(set(keys)):
        raise _fixture_error("two fixture records produce the same key")
    return items


def _counts(items: list[dict[str, Any]]) -> dict[str, int]:
    def count(prefix: str, sk_prefix: str | None = None) -> int:
        return sum(
            1
            for item in items
            if item["pk"].startswith(prefix) and (sk_prefix is None or item["sk"].startswith(sk_prefix))
        )

    return {
        "customers": count("CUST#"),
        "orders": count("ORDER#", "META"),
        "order_lines": count("ORDER#", "LINE#"),
        "products": count("PRODUCT#"),
        "locations": count("LOC#"),
        "inventory": count("INV#"),
    }


# ----------------------------------------------------------------------- seed and reset


def seed(variant: str = "default", fixtures_dir: Path | str | None = None) -> ResetSummary:
    """Write the fixture items (overwriting). Deletes nothing."""
    _check_variant(variant)
    items = build_items(load_bundle(fixtures_dir), variant)
    written = store.batch_put(items)
    return ResetSummary(variant=variant, deleted=0, written=written, counts=_counts(items))


def reset(variant: str = "default", fixtures_dir: Path | str | None = None) -> ResetSummary:
    """Delete every runtime partition, then seed ``variant`` (restores inventory and order lines).

    The variant and the fixture files are validated first, so a bad request deletes nothing.
    """
    _check_variant(variant)
    items = build_items(load_bundle(fixtures_dir), variant)
    deleted = store.batch_delete(list(store.scan_keys(store.RUNTIME_PREFIXES)))
    written = store.batch_put(items)
    return ResetSummary(variant=variant, deleted=deleted, written=written, counts=_counts(items))


# --------------------------------------------------------------------- reference lookups


def persona_list() -> list[dict[str, str]]:
    """The demo personas, straight from ``customers.json`` (no table read)."""
    directory = _demo_dir()
    customers = _collection(_read_json(directory / "customers.json"), "customers", "customers.json")
    return [
        {
            "customer_id": c["customer_id"],
            "display_name": c["display_name"],
            "description": c.get("description", ""),
        }
        for c in customers
    ]


def load_policy_json() -> dict[str, Any]:
    """Parsed ``policy.json`` (cached until the file changes)."""
    return _read_json(_demo_dir() / "policy.json")


def load_logistics() -> dict[str, Any]:
    """Parsed ``logistics.json`` (cached until the file changes)."""
    return _read_json(_demo_dir() / "logistics.json")


def location_ids() -> list[str]:
    """Location ids from ``locations.json`` in file order (the table holds the authoritative rows)."""
    directory = _demo_dir()
    records = _collection(_read_json(directory / "locations.json"), "locations", "locations.json")
    return [r["location_id"] for r in records]


def clear_cache() -> None:
    _JSON_CACHE.clear()
