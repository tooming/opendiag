# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds the desktop app frozen and self-contained.

  pyinstaller --noconfirm garagediag.spec

Produces `dist/GarageDiag.exe` on Windows and `dist/GarageDiag.app` on macOS.
The read-only tables + ui.html are bundled as data (found via
paths.resource_dir() -> sys._MEIPASS at runtime); writable data (logs,
backups) goes to a per-user folder via paths.data_dir().
"""
import os
import subprocess
import sys
import tempfile

import certifi

# Read-only resources the running app loads by name from resource_dir().
datas = [(f, '.') for f in (
    'ui.html',
    'e39_body_dtc_en.json',
    'e87_dtc.json',
    'ms41_dtc.json',
    'ms41_ram_params.json',
    'ms43_ram_params.json',
    'operations.json',
    'reference_ms43_ds2_commands.ts',
)]

# Frozen builds have no .git inside the bundle for paths.app_version() to
# read at runtime -- bake the git identity in now, at build time, instead.
try:
    _rev = subprocess.run(
        ['git', 'rev-parse', '--short', 'HEAD'],
        capture_output=True, text=True, timeout=5).stdout.strip()
    _dirty = subprocess.run(
        ['git', 'status', '--porcelain'],
        capture_output=True, text=True, timeout=5).stdout.strip()
    _version = (_rev or 'unknown') + ('+dirty' if _dirty else '')
except (OSError, subprocess.SubprocessError):
    _version = 'unknown'
# Human-friendly counterpart of _version for the macOS bundle's
# CFBundleShortVersionString (the native "About GarageDiag" panel reads that
# plist field directly, not version.txt) -- resolves to the release tag
# (e.g. "v0.8.24") on a tagged build, or "<tag>-<n>-g<sha>" between tags.
try:
    _tag_version = subprocess.run(
        ['git', 'describe', '--tags', '--always', '--dirty'],
        capture_output=True, text=True, timeout=5).stdout.strip() or 'unknown'
except (OSError, subprocess.SubprocessError):
    _tag_version = 'unknown'
_version_dir = tempfile.mkdtemp()
_version_file = os.path.join(_version_dir, 'version.txt')
with open(_version_file, 'w') as _vf:
    _vf.write(_version)
datas += [(_version_file, '.')]
# A frozen build has no OS certificate store to fall back on, so every
# HTTPS call in ovpf_cloud.py would fail with CERTIFICATE_VERIFY_FAILED
# without one. Bundling certifi's cacert.pem as a plain resource file
# (read back via paths.resource_dir(), same mechanism as ui.html) rather
# than importing certifi at runtime -- a pure-Python package's own
# __file__-relative path resolution (which is how certifi.where() finds
# its own data file) isn't reliable once PyInstaller has packed it into
# its zipped archive.
datas += [(certifi.where(), '.')]

# diag_ui pulls most of these in via endpoint-level imports; list them so
# PyInstaller's static analysis can't miss any.
hiddenimports = [
    'diag_ui', 'power_diag', 'ds2_diag', 'transaction', 'coding',
    'operations', 'dme_registry', 'ecu_registry', 'snapshot', 'adaptations',
    'actuators', 'correlate', 'plugins', 'vehicle_profiles',
    'trace', 'dev_console', 'paths',
]
hiddenimports += ['ovpf_core', 'ovpf_producer', 'ovpf_cloud', 'segno',
                  'certifi']
if sys.platform.startswith('win'):
    hiddenimports += ['serial', 'serial.tools.list_ports']

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == 'darwin':
    # onedir + BUNDLE -> a proper .app (onefile .app clashes with Gatekeeper)
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True,
        name='GarageDiag', debug=False, strip=False, upx=False,
        console=False, disable_windowed_traceback=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
                   name='GarageDiag')
    app = BUNDLE(
        coll,
        name='GarageDiag.app',
        icon='app.icns',
        bundle_identifier='com.tooming.garagediag',
        info_plist={
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.13',
            'CFBundleShortVersionString': _tag_version,
            'CFBundleVersion': _rev or 'unknown',
        },
    )
else:
    # Windows/Linux: single-file executable, easiest to hand over
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name='GarageDiag', debug=False, bootloader_ignore_signals=False,
        strip=False, upx=False, runtime_tmpdir=None,
        console=False, disable_windowed_traceback=False,
        icon='app.ico',
    )
