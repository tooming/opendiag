#!/usr/bin/env python3
"""Generate app.icns (macOS) and app.ico (Windows) for the frozen build.

Renders the gauge-dial icon (shared with build_app.py) and writes both
formats. The committed app.icns / app.ico are what garagediag.spec and CI
consume, so the generation-time deps (Pillow, macOS sips/iconutil) are only
needed HERE, not in CI. Regenerate and commit after changing the icon:

    pip install pillow          # macOS only (icns needs sips/iconutil)
    python3 make_icons.py
"""
import os

from PIL import Image

from build_app import build_icns, render_icon

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    # macOS .icns via the shared renderer + sips/iconutil
    build_icns(os.path.join(HERE, "app.icns"))

    # Windows .ico via Pillow (proper multi-size ICO PyInstaller accepts)
    png = os.path.join(HERE, "_appico_1024.png")
    render_icon(png, 1024)
    Image.open(png).convert("RGBA").save(
        os.path.join(HERE, "app.ico"), format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64),
               (128, 128), (256, 256)])
    os.remove(png)
    print("wrote app.icns + app.ico")


if __name__ == "__main__":
    main()
