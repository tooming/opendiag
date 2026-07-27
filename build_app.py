#!/usr/bin/env python3
"""Build a native macOS ``.app`` bundle for the desktop dashboard.

This does NOT freeze the code (no py2app). It produces a real, double-
clickable ``GarageDiag.app`` — icon in Finder/Applications, launches
with no Terminal window — whose executable simply runs ``desktop_app.py``
through the project's ``.venv``. Because the real source files run in
place, every ``__file__``-relative path (ui.html, the *.json tables) just
works, and no core module is modified.

    python3 build_app.py            # builds ./GarageDiag.app
    open "GarageDiag.app"      # or double-click it in Finder
    # to put it in the Dock/Launchpad: drag it into /Applications

The project path is baked into the launcher at build time, so the .app
keeps working after you move it to /Applications. Rebuild if you move the
project itself.

Stdlib only (uses the system `sips`/`iconutil` for the icon).
"""
import math
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import zlib

PROJECT = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "GarageDiag"
APP = os.path.join(PROJECT, f"{APP_NAME}.app")
BUNDLE_ID = "com.tooming.garagediag"


# ---------------------------------------------------------------------------
# Icon: a simple gauge dial, rendered pixel-by-pixel to a PNG (pure stdlib),
# then handed to `sips`/`iconutil` to assemble the .icns.
# ---------------------------------------------------------------------------
def _write_png(path, w, h, rgba):
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)  # 8-bit RGBA
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # filter type 0
        raw.extend(rgba[y * stride:(y + 1) * stride])
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _cov(sd):
    """Anti-aliased coverage for a signed distance (inside = sd<0)."""
    return min(1.0, max(0.0, 0.5 - sd))


def _over(dst, rgb, a):
    """Alpha-composite rgb (coverage a) over dst (opaque)."""
    if a <= 0:
        return dst
    return tuple(round(rgb[i] * a + dst[i] * (1 - a)) for i in range(3))


def render_icon(path, S=1024):
    top, bot = (17, 38, 61), (9, 20, 34)          # bg gradient
    ring_col = (214, 228, 242)
    accent = (74, 163, 255)
    cx, cy = S / 2, S * 0.53
    # rounded-square background
    half = S * 0.46
    corner = S * 0.22
    Rmid, thick = S * 0.30, S * 0.045             # gauge ring
    gap = 44                                       # bottom opening, degrees
    # needle tip (screen angle: 0=right, +down); -58° = upper-right
    nang = math.radians(-58)
    tipx = cx + math.cos(nang) * (Rmid - S * 0.02)
    tipy = cy + math.sin(nang) * (Rmid - S * 0.02)
    hub = S * 0.035
    # tick dots along the drawn arc
    ticks = []
    for k in range(9):
        deg = 130 + k * (280 / 8)                  # 130°..410° clockwise
        r = math.radians(deg)
        ticks.append((cx + math.cos(r) * (Rmid - thick - S * 0.03),
                      cy + math.sin(r) * (Rmid - thick - S * 0.03)))

    buf = bytearray(S * S * 4)
    for y in range(S):
        for x in range(S):
            px, py = x + 0.5, y + 0.5
            # background rounded rect (SDF)
            dx, dy = abs(px - S / 2) - (half - corner), abs(py - S / 2) - (half - corner)
            sd_bg = (math.hypot(max(dx, 0), max(dy, 0))
                     + min(max(dx, dy), 0) - corner)
            bg_a = _cov(sd_bg)
            if bg_a <= 0:
                continue
            col = _lerp(top, bot, py / S)
            # gauge ring (annulus) masked to an arc
            d = math.hypot(px - cx, py - cy)
            sd_ring = abs(d - Rmid) - thick
            ang = math.degrees(math.atan2(py - cy, px - cx)) % 360
            in_gap = abs(((ang - 90 + 180) % 360) - 180) < gap  # gap at bottom (90°)
            if not in_gap:
                col = _over(col, ring_col, _cov(sd_ring))
            # tick dots
            for tx, ty in ticks:
                col = _over(col, ring_col, _cov(math.hypot(px - tx, py - ty) - S * 0.012))
            # needle (segment center->tip) + hub
            vx, vy = tipx - cx, tipy - cy
            t = max(0, min(1, ((px - cx) * vx + (py - cy) * vy) / (vx * vx + vy * vy)))
            seg = math.hypot(px - (cx + t * vx), py - (cy + t * vy)) - S * 0.012
            col = _over(col, accent, _cov(seg))
            col = _over(col, accent, _cov(math.hypot(px - cx, py - cy) - hub))
            i = (y * S + x) * 4
            buf[i:i + 4] = bytes((col[0], col[1], col[2], round(bg_a * 255)))
    _write_png(path, S, S, buf)


def build_icns(dest):
    base = os.path.join(PROJECT, "_iconbase.png")
    iconset = os.path.join(PROJECT, "_app.iconset")
    print("  rendering icon…")
    render_icon(base, 1024)
    if os.path.isdir(iconset):
        shutil.rmtree(iconset)
    os.makedirs(iconset)
    sizes = [(16, ""), (16, "@2x"), (32, ""), (32, "@2x"), (128, ""),
             (128, "@2x"), (256, ""), (256, "@2x"), (512, ""), (512, "@2x")]
    for base_sz, suffix in sizes:
        px = base_sz * (2 if suffix else 1)
        out = os.path.join(iconset, f"icon_{base_sz}x{base_sz}{suffix}.png")
        subprocess.run(["sips", "-z", str(px), str(px), base, "--out", out],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", dest], check=True)
    shutil.rmtree(iconset)
    os.remove(base)


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------
LAUNCHER = """#!/bin/bash
# Auto-generated by build_app.py — launches the dashboard via the project venv.
set -e
PROJECT={project}
cd "$PROJECT"
if [ ! -x ".venv/bin/python" ]; then
  /usr/bin/env python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements-desktop.txt
fi
exec .venv/bin/python desktop_app.py
"""


def build():
    if os.path.isdir(APP):
        shutil.rmtree(APP)
    macos = os.path.join(APP, "Contents", "MacOS")
    resources = os.path.join(APP, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(resources)

    build_icns(os.path.join(resources, "app.icns"))

    launcher = os.path.join(macos, "run")
    with open(launcher, "w") as f:
        # shell-quote the project path for safety
        f.write(LAUNCHER.format(project="'" + PROJECT.replace("'", "'\\''") + "'"))
    os.chmod(launcher, 0o755)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "run",
        "CFBundleIconFile": "app",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "10.13",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    }
    with open(os.path.join(APP, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f)

    print(f"\nBuilt: {APP}")
    print("Launch:  open \"GarageDiag.app\"   (or double-click in Finder)")
    print("Install: drag it into /Applications")


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("This builder targets macOS (uses sips/iconutil).")
    build()
