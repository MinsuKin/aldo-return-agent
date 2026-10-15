"""Demo fixtures: load ``fixtures/demo/*.json``, turn them into table items, seed and reset.

The seven JSON files are synthetic reference data (contracts.md section (d)). The loader
validates them (ids start with ``DEMO-``, every record is ``source: synthetic``, sizes are
strings), derives every ``pk``/``sk``/GSI key through the ``store`` key builders and applies
the reset variant's inventory overrides. ``reset`` deletes every runtime partition
(``store.RUNTIME_PREFIXES``: conversations, runs, policy checks, quotes, transactions, cases,
audit, size requests, the write-back outbox and the orders the agent-to-agent purchase created)
and then writes the fixture items again, which also restores inventory and order lines.

Fixtures v2 (PIVOT_ADDENDUM 11.3) are additive: ``customers_v2.json``, ``orders_v2.json``,
``products_v2.json`` and ``inventory_v2.json`` are merged behind the base records when they exist
(an older bundle without them still loads), and ``reviews.json`` holds the synthetic product
reviews (``load_reviews``). References are checked across the merged records: order to customer,
line to sku, inventory to location and sku, review to style.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
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
#: Fixtures v2 (``customers_v2.json``, ``orders_v2.json``, ``products_v2.json``, ``inventory_v2.json``) are
#: merged behind the base records when they exist; a missing v2 file is not an error. The reviews are separate.
REVIEWS_FILE = "reviews.json"
#: Member tier of a customer record that has none (the three base personas).
DEFAULT_TIER = "none"
#: Review ids start with ``DEMO-`` like every other id; ``REV-INJECT-`` marks the adversarial
#: review that the prompt-injection checks look up by name (PIVOT_ADDENDUM 11.3).
REVIEW_ID_PREFIXES: tuple[str, ...] = (DEMO_ID_PREFIX, "REV-INJECT-")

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
    reviews: list[dict[str, Any]] = field(default_factory=list)


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


def _optional(directory: Path, name: str) -> Any | None:
    """Parsed JSON of an additive file, or ``None`` when it does not exist (a broken file still raises)."""
    return _read_json(directory / name) if (directory / name).is_file() else None


def _merged(directory: Path, base: list[dict[str, Any]], v2_name: str, key: str) -> list[dict[str, Any]]:
    """``base`` followed by the records under ``key`` of the additive file ``v2_name`` (if present)."""
    extra = _optional(directory, v2_name)
    return list(base) if extra is None else [*base, *_collection(extra, key, v2_name)]


def _unique(values: list[Any], what: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise _fixture_error(f"duplicate {what}: {value}")
        seen.add(value)


def _whole(value: Any, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _check_customer(record: Mapping[str, Any], location_ids: set[str], tiers: Any) -> None:
    """Optional v2 attributes: ``tier`` (a key of policy.json ``tiers``), spend, travel minutes, label."""
    where = f"customer {record.get('customer_id')}"
    if "tier" in record:
        tier = record["tier"]
        if not isinstance(tier, str) or not tier or (isinstance(tiers, dict) and tier not in tiers):
            raise _fixture_error(f"{where} tier must be one of the policy.json tiers")
    if "annual_spend_cents" in record and not _whole(record["annual_spend_cents"]):
        raise _fixture_error(f"{where} annual_spend_cents must be whole cents")
    if "travel_minutes" in record:
        travel = record["travel_minutes"]
        if not isinstance(travel, dict) or not all(
            loc in location_ids and _whole(minutes) for loc, minutes in travel.items()
        ):
            raise _fixture_error(f"{where} travel_minutes must map location ids to whole minutes")
    if "location_label" in record:
        label = record["location_label"]
        if not isinstance(label, str) or not label.strip():
            raise _fixture_error(f"{where} location_label must be a non-empty string")


def _check_aliases(products: list[dict[str, Any]]) -> None:
    """Optional product ``aliases``: distinct non-empty strings, and no alias shared by two styles."""
    owner: dict[str, str] = {}
    for record in products:
        aliases = record.get("aliases")
        if aliases is None:
            continue
        where = f"product {record.get('sku')}"
        if not isinstance(aliases, list) or not aliases or not all(isinstance(a, str) and a.strip() for a in aliases):
            raise _fixture_error(f"{where} aliases must be a non-empty list of strings")
        folded = [a.strip().casefold() for a in aliases]
        if len(set(folded)) != len(folded):
            raise _fixture_error(f"{where} aliases must be distinct")
        for alias in folded:
            if owner.setdefault(alias, record["style"]) != record["style"]:
                raise _fixture_error(f"alias '{alias}' belongs to two styles")


def _check_review(review: Mapping[str, Any], style_ids: set[str]) -> None:
    where = f"review {review.get('review_id')}"
    review_id = review.get("review_id")
    if not isinstance(review_id, str) or not review_id.startswith(REVIEW_ID_PREFIXES):
        raise _fixture_error(f"{where} id must start with one of {', '.join(REVIEW_ID_PREFIXES)}")
    if review.get("source") != "synthetic":
        raise _fixture_error(f"{where} must have source 'synthetic'")
    if review.get("style_id") not in style_ids:
        raise _fixture_error(f"{where} refers to an unknown style")
    rating = review.get("rating")
    if not _whole(rating, 1) or rating > 5:
        raise _fixture_error(f"{where} rating must be a whole number from 1 to 5")
    for name in ("title", "text"):
        if not isinstance(review.get(name), str) or not review[name].strip():
            raise _fixture_error(f"{where} needs a non-empty {name}")
    language = review.get("language")
    if not isinstance(language, str) or len(language) != 2 or not language.islower():
        raise _fixture_error(f"{where} language must be a two-letter lower-case code")
    if not isinstance(review.get("verified_purchase"), bool):
        raise _fixture_error(f"{where} verified_purchase must be true or false")
    if not _whole(review.get("helpful_votes")):
        raise _fixture_error(f"{where} helpful_votes must be a whole number")
    try:
        date.fromisoformat(review.get("created_at"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _fixture_error(f"{where} created_at must be a YYYY-MM-DD date") from None


def load_bundle(fixtures_dir: Path | str | None = None) -> FixtureBundle:
    """Read and validate the seven fixture files, merge the v2 files and attach the reviews.

    The base records keep their order and the additive files follow them. The result is a new
    set of lists (the JSON cache is never modified), so callers may keep the bundle.
    """
    directory = _demo_dir(fixtures_dir)
    raw = {name: _read_json(directory / name) for name in FIXTURE_FILES}

    customers = _merged(directory, _collection(raw["customers.json"], "customers", "customers.json"), "customers_v2.json", "customers")
    orders = _merged(directory, _collection(raw["orders.json"], "orders", "orders.json"), "orders_v2.json", "orders")
    products = _merged(directory, _collection(raw["products.json"], "products", "products.json"), "products_v2.json", "products")
    locations = _collection(raw["locations.json"], "locations", "locations.json")
    inventory = raw["inventory.json"]
    base = _merged(directory, _collection(inventory, "base", "inventory.json"), "inventory_v2.json", "base")
    variants = inventory.get("variants")
    if (
        not isinstance(variants, dict)
        or set(variants) != set(VARIANTS)
        or not all(isinstance(v, dict) for v in variants.values())
    ):
        raise _fixture_error(f"inventory.json variants must be exactly {list(VARIANTS)}")
    extra_inventory = _optional(directory, "inventory_v2.json")
    if extra_inventory is not None:
        extra_variants = extra_inventory.get("variants")
        if (
            not isinstance(extra_variants, dict)
            or not set(extra_variants) <= set(VARIANTS)
            or not all(isinstance(v, dict) for v in extra_variants.values())
        ):
            raise _fixture_error(f"inventory_v2.json variants must be among {list(VARIANTS)}")
        variants = {
            name: {
                **variants[name],
                "overrides": [
                    *(variants[name].get("overrides") or []),
                    *((extra_variants.get(name) or {}).get("overrides") or []),
                ],
            }
            for name in VARIANTS
        }
    logistics, policy = raw["logistics.json"], raw["policy.json"]
    if not isinstance(policy, dict) or not isinstance(logistics, dict):
        raise _fixture_error("policy.json and logistics.json must be JSON objects")

    for record in locations:
        _require_demo(record, ("location_id",), f"location {record.get('location_id')}")
    location_ids = {loc["location_id"] for loc in locations}
    tiers = policy.get("tiers")
    for record in customers:
        _require_demo(record, ("customer_id",), f"customer {record.get('customer_id')}")
        _check_customer(record, location_ids, tiers)
    for record in products:
        _require_demo(record, ("sku", "style"), f"product {record.get('sku')}")
        _require_size(record, f"product {record.get('sku')}")
    _check_aliases(products)
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
    skus = {p["sku"] for p in products}
    styles = {p["style"] for p in products}
    _unique([c["customer_id"] for c in customers], "customer")
    _unique([loc["location_id"] for loc in locations], "location")
    _unique([p["sku"] for p in products], "sku")
    _unique([o["order_id"] for o in orders], "order")
    _unique([line["line_id"] for o in orders for line in o["lines"]], "order line")
    _unique([(r["sku"], r["location_id"]) for r in base], "inventory row")
    for order in orders:
        if order["customer_id"] not in customer_ids:
            raise _fixture_error(f"order {order['order_id']} has an unknown customer")
        for line in order["lines"]:
            if line["sku"] not in skus:
                raise _fixture_error(f"line {line['line_id']} has an unknown sku")
    for row in base:
        if row["sku"] not in skus or row["location_id"] not in location_ids:
            raise _fixture_error(f"inventory row {row['sku']}/{row['location_id']} is dangling")

    reviews = _load_reviews(directory)
    for review in reviews:
        _check_review(review, styles)
    _unique([r["review_id"] for r in reviews], "review")

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
        reviews=reviews,
    )


def _load_reviews(directory: Path) -> list[dict[str, Any]]:
    """Records of ``reviews.json`` (``[]`` when the file does not exist); shape checked, not content."""
    data = _optional(directory, REVIEWS_FILE)
    return [] if data is None else _collection(data, "reviews", REVIEWS_FILE)


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
        # tier, annual_spend_cents, travel_minutes and location_label ride along when the record has them
        items.append({"tier": DEFAULT_TIER, **customer, "pk": pk, "sk": sk})

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


def _tier_labels() -> dict[str, str]:
    """``{tier: label}`` from ``policy.json`` ``tiers`` (``{}`` for a policy file without tiers)."""
    tiers = load_policy_json().get("tiers")
    if not isinstance(tiers, dict):
        return {}
    return {
        name: config["label"]
        for name, config in tiers.items()
        if isinstance(config, dict) and isinstance(config.get("label"), str)
    }


def persona_list() -> list[dict[str, str]]:
    """The demo personas, straight from ``customers.json`` and ``customers_v2.json`` (no table read).

    Every row carries the member ``tier`` (``none`` when the record has none), its ``tier_label``
    from ``policy.json`` ``tiers`` and the ``location_label`` ("" when the record has none).
    Base customers come first, then the v2 customers, in file order.
    """
    directory = _demo_dir()
    customers = _merged(
        directory,
        _collection(_read_json(directory / "customers.json"), "customers", "customers.json"),
        "customers_v2.json",
        "customers",
    )
    labels = _tier_labels()
    rows: list[dict[str, str]] = []
    for c in customers:
        tier = c["tier"] if isinstance(c.get("tier"), str) and c["tier"] else DEFAULT_TIER
        default_label = "Member" if tier == DEFAULT_TIER else f"{tier.capitalize()} member"
        rows.append(
            {
                "customer_id": c["customer_id"],
                "display_name": c["display_name"],
                "description": c.get("description", ""),
                "tier": tier,
                "tier_label": labels.get(tier, default_label),
                "location_label": c.get("location_label", ""),
            }
        )
    return rows


def load_reviews(style_id: str | None = None, fixtures_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """The synthetic product reviews of ``reviews.json`` in file order, for one style when given.

    Each call returns fresh copies, so a caller can add fields (for example ``flags``) without
    touching the cache. A bundle without ``reviews.json`` or a style without reviews gives ``[]``.
    The review text is UNTRUSTED data (one fixture review is an indirect prompt injection).
    """
    records = _load_reviews(_demo_dir(fixtures_dir))
    return [dict(r) for r in records if style_id is None or r.get("style_id") == style_id]


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
