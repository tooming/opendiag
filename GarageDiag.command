#!/bin/bash
# Double-clickable launcher for the desktop app (macOS Finder).
# On first run, creates the .venv and installs pywebview; afterwards it
# just launches the native window.
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "First run: setting up the desktop environment…"
  python3 -m venv .venv || exit 1
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements-desktop.txt || exit 1
fi

exec .venv/bin/python desktop_app.py
