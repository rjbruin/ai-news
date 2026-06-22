#!/usr/bin/env python3
"""Render PNG app icons from the source SVG.

Tries `cairosvg` first (pip install cairosvg), then the `rsvg-convert` /
`inkscape` CLIs. Run:  python scripts/gen_icons.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ICON_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
SVG = ICON_DIR / "icon.svg"
TARGETS = {
    "icon-192.png": 192,
    "icon-512.png": 512,
    "icon-maskable-512.png": 512,
}


def via_cairosvg(src: Path, dst: Path, size: int) -> bool:
    try:
        import cairosvg  # type: ignore
    except (ImportError, OSError):
        # Missing package, or missing native libcairo — fall through.
        return False
    cairosvg.svg2png(url=str(src), write_to=str(dst), output_width=size, output_height=size)
    return True


def via_cli(src: Path, dst: Path, size: int) -> bool:
    if shutil.which("rsvg-convert"):
        subprocess.run(
            ["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(dst), str(src)],
            check=True,
        )
        return True
    if shutil.which("inkscape"):
        subprocess.run(
            ["inkscape", str(src), "--export-type=png", f"--export-filename={dst}",
             f"--export-width={size}", f"--export-height={size}"],
            check=True,
        )
        return True
    return False


def via_pillow(dst: Path, size: int, maskable: bool = False) -> bool:
    """Dependency-light fallback: draw the newsletter icon with Pillow."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False

    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded square background (brand gradient approximated by a solid colour).
    radius = int(s * (0.12 if not maskable else 0.0))
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=(31, 58, 95, 255))

    # Newsletter sheet (leave generous safe-zone padding for maskable).
    pad = int(s * (0.20 if maskable else 0.16))
    x0, y0, x1, y1 = pad, pad, s - pad, s - pad
    paper = (246, 241, 231, 255)
    d.rounded_rectangle([x0, y0, x1, y1], radius=int(s * 0.03), fill=paper)

    w = x1 - x0
    h = y1 - y0
    # Masthead bar
    d.rounded_rectangle(
        [x0 + w * 0.08, y0 + h * 0.08, x1 - w * 0.08, y0 + h * 0.20],
        radius=int(s * 0.012), fill=(31, 58, 95, 255),
    )
    # Headline image block
    d.rounded_rectangle(
        [x0 + w * 0.08, y0 + h * 0.26, x0 + w * 0.46, y0 + h * 0.52],
        radius=int(s * 0.012), fill=(200, 132, 58, 255),
    )
    # Text lines
    gray = (154, 166, 178, 255)
    for i, frac in enumerate([0.27, 0.35, 0.43]):
        d.rounded_rectangle(
            [x0 + w * 0.52, y0 + h * frac, x1 - w * 0.08, y0 + h * (frac + 0.04)],
            radius=int(s * 0.008), fill=gray,
        )
    for i, frac in enumerate([0.60, 0.68, 0.76]):
        right = 0.92 if i < 2 else 0.66
        d.rounded_rectangle(
            [x0 + w * 0.08, y0 + h * frac, x0 + w * right, y0 + h * (frac + 0.04)],
            radius=int(s * 0.008), fill=gray,
        )

    img.save(dst, "PNG")
    return True


def main() -> int:
    if not SVG.exists():
        print(f"Source SVG not found: {SVG}", file=sys.stderr)
        return 1
    for name, size in TARGETS.items():
        dst = ICON_DIR / name
        maskable = "maskable" in name
        if via_cairosvg(SVG, dst, size) or via_cli(SVG, dst, size) or via_pillow(
            dst, size, maskable=maskable
        ):
            print(f"wrote {dst} ({size}px)")
        else:
            print(
                "No renderer available. Install Pillow (`pip install pillow`), "
                "cairosvg, or rsvg-convert/inkscape.",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
