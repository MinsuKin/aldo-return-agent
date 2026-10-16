"""The real ALDO photos shipped with the app match the demo fixtures (names and colours), nothing more, nothing less.

The browser looks a photo up by `<style>-<colour>` (frontend/src/lib/productImages.ts); a product without a photo silently
falls back to a colour swatch. These tests make that fall-back a decision instead of an accident: when a fixture product is
renamed or recoloured, or a photo file is added or removed, this file says so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PHOTO_DIR = ROOT / "frontend" / "public" / "products"
PHOTO_INDEX = ROOT / "frontend" / "src" / "lib" / "productImages.ts"
SOURCES = ROOT / "docs" / "design" / "product-photo-sources.md"
PRODUCTS = ROOT / "fixtures" / "demo" / "products_v2.json"
ORDERS = ROOT / "fixtures" / "demo" / "orders_v2.json"

MAX_PHOTO_BYTES = 40_000  # the phone shows them at most 88 px wide; keep the app light


def _slug(name: str) -> str:
    """Same rule as `slug` in productImages.ts."""
    text = re.sub(r"^aldo\s+", "", name.lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def _key(name: str, color: str) -> str:
    return f"{_slug(name)}-{re.sub(r'[^a-z]', '', color.lower())}"


def _indexed_photos() -> set[str]:
    source = PHOTO_INDEX.read_text(encoding="utf-8")
    block = re.search(r"new Set<string>\(\[(.*?)\]\)", source, re.S)
    assert block, "could not find the PHOTOS set in productImages.ts"
    return set(re.findall(r'"([a-z0-9]+(?:-[a-z0-9]+)+)"', block.group(1)))


def _fixture_pairs() -> set[str]:
    products = json.loads(PRODUCTS.read_text(encoding="utf-8"))["products"]
    return {_key(p["product_name"], p["color"]) for p in products}


def test_the_photo_index_covers_every_product_and_colour_of_the_fixtures() -> None:
    assert _indexed_photos() == _fixture_pairs()


def test_every_line_of_davids_order_has_a_photo() -> None:
    order = json.loads(ORDERS.read_text(encoding="utf-8"))["orders"][0]
    missing = [line["line_id"] for line in order["lines"] if _key(line["product_name"], line["color"]) not in _indexed_photos()]
    assert len(order["lines"]) == 10
    assert missing == []


def test_every_indexed_photo_is_a_small_jpeg_and_every_jpeg_is_indexed() -> None:
    indexed = _indexed_photos()
    files = {p.stem: p for p in PHOTO_DIR.glob("*.jpg")}
    assert set(files) == indexed
    for key, path in files.items():
        data = path.read_bytes()
        assert data[:3] == b"\xff\xd8\xff", f"{key}.jpg is not a JPEG (a failed download is often an HTML page)"
        assert len(data) <= MAX_PHOTO_BYTES, f"{key}.jpg is {len(data)} bytes"


def test_every_photo_has_a_source_note() -> None:
    notes = SOURCES.read_text(encoding="utf-8").lower()
    unlisted = sorted(key for key in _indexed_photos() if key not in notes and f"{key}.jpg" not in notes)
    assert unlisted == []
