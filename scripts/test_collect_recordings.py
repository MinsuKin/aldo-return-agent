#!/usr/bin/env python3
"""Collect Playwright demo recordings into named files.

Playwright (with video:'on') drops one ``video.webm`` into a per-test output folder under
``frontend/test-results-rec``; the folder name is derived from the test title, so it contains the
persona word 'John' or 'David'. This copies each ``video.webm`` into the out dir, renamed
``<john|david>-run<N>.webm`` where N is the 1-based order by modification time WITHIN each persona,
and prints a table (file, size, and seconds when ffprobe is available).

Usage:
    python3 scripts/test_collect_recordings.py [--src frontend/test-results-rec] [--out docs/eval/recordings]

Copies only (never deletes the source). Nothing is run against any deployment.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def persona_of(folder_name: str) -> str | None:
    low = folder_name.lower()
    has_john = "john" in low
    has_david = "david" in low
    if has_john and not has_david:
        return "john"
    if has_david and not has_john:
        return "david"
    # Ambiguous (both or neither) -> not a per-persona stage recording; skip it.
    return None


def probe_seconds(path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
        dur = json.loads(out).get("format", {}).get("duration")
        return float(dur) if dur is not None else None
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError):
        return None


def human_size(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024
    return f"{val:.1f} GB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="frontend/test-results-rec",
                    help="dir Playwright wrote per-test video folders into (default frontend/test-results-rec)")
    ap.add_argument("--out", default="docs/eval/recordings",
                    help="dir to copy renamed recordings into (default docs/eval/recordings)")
    args = ap.parse_args()

    src = (REPO / args.src) if not Path(args.src).is_absolute() else Path(args.src)
    out = (REPO / args.out) if not Path(args.out).is_absolute() else Path(args.out)

    if not src.is_dir():
        print(f"source dir not found: {src}")
        return 1

    # Gather (persona, mtime, source_path) for every video.webm under a persona-named folder.
    found: list[tuple[str, float, Path]] = []
    for video in sorted(src.rglob("video.webm")):
        # The persona-named folder is the test-output dir that directly (or indirectly) contains the video.
        persona = None
        for parent in video.relative_to(src).parts[:-1]:
            persona = persona or persona_of(parent)
        if persona is None:
            # also check the whole relative path as a fallback
            persona = persona_of(str(video.relative_to(src)))
        if persona is None:
            print(f"skip (no unambiguous persona in path): {video.relative_to(src)}")
            continue
        found.append((persona, video.stat().st_mtime, video))

    out.mkdir(parents=True, exist_ok=True)

    # Assign run numbers per persona, ordered by modification time.
    counters: dict[str, int] = {}
    rows: list[tuple[str, int, float | None]] = []
    for persona, _mtime, video in sorted(found, key=lambda t: (t[0], t[1])):
        counters[persona] = counters.get(persona, 0) + 1
        name = f"{persona}-run{counters[persona]}.webm"
        dest = out / name
        shutil.copy2(video, dest)
        rows.append((name, dest.stat().st_size, probe_seconds(dest)))

    # Print the table.
    have_ffprobe = shutil.which("ffprobe") is not None
    print(f"\nCollected {len(rows)} recording(s) into {out}")
    if have_ffprobe:
        print(f"{'file':<22} {'size':>12} {'seconds':>9}")
        print("-" * 45)
        for name, size, secs in rows:
            print(f"{name:<22} {human_size(size):>12} {('%.1f' % secs) if secs is not None else 'n/a':>9}")
    else:
        print("(ffprobe not found; showing size only)")
        print(f"{'file':<22} {'size':>12}")
        print("-" * 35)
        for name, size, _secs in rows:
            print(f"{name:<22} {human_size(size):>12}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
