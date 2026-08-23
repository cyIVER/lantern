#!/usr/bin/env python3
"""Extract genuine CS2 map icons from the installed game's VPK archives.

Source 2 '.vsvg_c' files are a thin binary wrapper around plain SVG text, so the
markup can be recovered with a byte scan -- no ValveResourceFormat or .NET
decompiler needed. Icons are written once at build time and served locally, so
the UI has no external dependencies and works offline.

    uv run extract-map-icons.py [--vpk PATH] [--out DIR]
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

DEFAULT_VPK = (
    r"E:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive"
    r"\game\csgo\pak01_dir.vpk"
)
ICON_RE = re.compile(r"panorama/images/map_icons/map_icon_(?P<map>[a-z0-9_]+)\.vsvg_c$", re.I)


def extract_svg(blob: bytes) -> bytes | None:
    start = blob.find(b"<svg")
    if start < 0:
        return None
    end = blob.find(b"</svg>", start)
    if end < 0:
        return None
    return blob[start:end + len(b"</svg>")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vpk", default=DEFAULT_VPK)
    ap.add_argument("--out", default=str(pathlib.Path(__file__).parent / "static" / "maps"))
    args = ap.parse_args()

    try:
        import vpk  # noqa: PLC0415
    except ImportError:
        sys.exit("missing dependency: uv pip install vpk")

    vpk_path = pathlib.Path(args.vpk)
    if not vpk_path.exists():
        sys.exit(f"VPK not found: {vpk_path}\nPass --vpk if CS2 lives elsewhere.")

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pak = vpk.open(str(vpk_path))
    written, skipped = 0, []
    for entry in pak:
        m = ICON_RE.search(entry)
        if not m:
            continue
        name = m.group("map").lower()
        svg = extract_svg(pak[entry].read())
        if not svg:
            skipped.append(name)
            continue
        (out / f"{name}.svg").write_bytes(svg)
        written += 1

    print(f"wrote {written} icons to {out}")
    for f in sorted(out.glob("*.svg")):
        print(f"  {f.stem:22s} {f.stat().st_size // 1024:>5d} KB")
    if skipped:
        print(f"no SVG payload found for: {', '.join(sorted(skipped))}")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
