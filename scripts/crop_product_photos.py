#!/usr/bin/env python3
"""Crop the product photos in frontend/public/products to the shoe, 4:3, 360x270 JPEG.

The ALDO photos are 560x560 squares with the shoe small in the middle. In a 44 px tile that is a smudge, so each photo is
cropped to the shoe's bounding box (plus a margin) at a 4:3 aspect ratio and saved at 360x270 (about 2x the largest tile).
Uses only macOS `sips` and the standard library (Pillow is not installed): sips converts to PNG, a small decoder finds the
pixels that differ from the background, sips crops and resizes.

    python3 scripts/crop_product_photos.py            # all *.jpg that are still square
    python3 scripts/crop_product_photos.py --dry-run  # print the boxes only
"""

from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHOTOS = ROOT / "frontend" / "public" / "products"
TMP = ROOT / "infra" / "build" / "cdk-tmp"  # inside the project: sips cannot read the scratch folder
OUT_W, OUT_H = 360, 270
MARGIN = 0.10  # of the shoe's width, on every side
DIFF = 14  # per-channel difference from the background that counts as "shoe"


def decode_png(path: Path) -> tuple[int, int, int, bytes]:
    """(width, height, channels, raw pixels) of an 8-bit, non-interlaced RGB/RGBA PNG."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, idat, width, height, depth, ctype, interlace = 8, [], 0, 0, 0, 0, 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        pos += 12 + length
    assert depth == 8 and interlace == 0 and ctype in (2, 6), f"unsupported PNG (depth {depth}, type {ctype}, interlace {interlace})"
    ch = 3 if ctype == 2 else 4
    raw = zlib.decompress(b"".join(idat))
    stride = width * ch
    out = bytearray(height * stride)
    prev = bytearray(stride)
    for y in range(height):
        ft = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        if ft == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
        out[y * stride : (y + 1) * stride] = line
        prev = line
    return width, height, ch, bytes(out)


def shoe_box(width: int, height: int, ch: int, px: bytes) -> tuple[int, int, int, int]:
    """Bounding box (x0, y0, x1, y1) of the pixels that differ from the background (median of the four corners)."""
    def pixel(x: int, y: int) -> tuple[int, int, int]:
        i = (y * width + x) * ch
        return px[i], px[i + 1], px[i + 2]

    corners = [pixel(2, 2), pixel(width - 3, 2), pixel(2, height - 3), pixel(width - 3, height - 3)]
    bg = tuple(sorted(c[k] for c in corners)[1] for k in range(3))
    x0, y0, x1, y1 = width, height, -1, -1
    for y in range(height):
        base = y * width * ch
        for x in range(width):
            i = base + x * ch
            if abs(px[i] - bg[0]) > DIFF or abs(px[i + 1] - bg[1]) > DIFF or abs(px[i + 2] - bg[2]) > DIFF:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    return x0, y0, x1, y1


def crop_box(box: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of a 4:3 window around the box with a margin, kept inside the image."""
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0 + 1, y1 - y0 + 1
    w = bw * (1 + 2 * MARGIN)
    h = w * OUT_H / OUT_W
    if h < bh * (1 + 2 * MARGIN):
        h = bh * (1 + 2 * MARGIN)
        w = h * OUT_W / OUT_H
    w, h = min(w, width), min(h, height)
    w = min(w, h * OUT_W / OUT_H)
    h = w * OUT_H / OUT_W
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    x = max(0, min(width - w, cx - w / 2))
    y = max(0, min(height - h, cy - h / 2))
    return int(x), int(y), int(w), int(h)


def sips(*args: str) -> None:
    subprocess.run(["sips", *args], check=True, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)
    done = 0
    for jpg in sorted(PHOTOS.glob("*.jpg")):
        png = TMP / (jpg.stem + ".png")
        sips("-s", "format", "png", str(jpg), "--out", str(png))
        width, height, ch, px = decode_png(png)
        if (width, height) == (OUT_W, OUT_H):
            print(f"{jpg.name}: already {OUT_W}x{OUT_H}, skipped")
            png.unlink(missing_ok=True)
            continue
        box = shoe_box(width, height, ch, px)
        x, y, w, h = crop_box(box, width, height)
        print(f"{jpg.name}: {width}x{height} shoe {box} -> crop x={x} y={y} {w}x{h}")
        if not args.dry_run:
            cropped = TMP / (jpg.stem + "-crop.jpg")
            sips("-s", "format", "jpeg", "-s", "formatOptions", "82", "-c", str(h), str(w), "--cropOffset", str(y), str(x), str(jpg), "--out", str(cropped))
            sips("-s", "format", "jpeg", "-s", "formatOptions", "82", "-z", str(OUT_H), str(OUT_W), str(cropped), "--out", str(jpg))
            cropped.unlink(missing_ok=True)
            done += 1
        png.unlink(missing_ok=True)
    print(f"cropped {done} photo(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
