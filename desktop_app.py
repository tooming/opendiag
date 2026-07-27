#!/usr/bin/env python3
"""Native desktop-app wrapper for the diagnostic UI.

Wraps ``diag_ui.py``'s localhost web server in a native OS webview window
(pywebview) so the tool launches as a normal Mac/Windows/Linux app instead
of "run a server, open a browser". No Chromium is bundled — this uses the
platform webview (WKWebView on macOS).

This is an OPTIONAL wrapper. The core scripts (``power_diag.py``,
``ds2_diag.py``, ``diag_ui.py``) stay stdlib-only and still run with bare
python3; only this launcher needs the ``pywebview`` dependency:

    pip install -r requirements-desktop.txt
    python3 desktop_app.py

The diag server is started as a child process — it owns the serial port
exclusively (matching its design), so keeping it in its own process keeps
that isolation clean. The window teardown terminates the child.
"""
import atexit
import os
import subprocess
import sys
import time
import urllib.request

import webview

import paths

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("GARAGEDIAG_PORT", "8039"))
BASE = f"http://127.0.0.1:{PORT}"


def _server_up():
    """True if something answers /api/state on PORT.

    Every /api/ route (see diag_ui.py's _csrf_ok) requires the
    X-GarageDiag-Client header to force a CORS preflight against
    cross-origin pages -- without it this always got 403, so this check
    never once saw the server as up, and start_server() always gave up
    after ~10s and raised SystemExit before webview.create_window() ever
    ran. Same header ui.html's own api() helper sends on every call.
    """
    req = urllib.request.Request(f"{BASE}/api/state",
                                  headers={"X-GarageDiag-Client": "1"})
    try:
        with urllib.request.urlopen(req, timeout=0.5) as r:
            return r.status == 200
    except Exception:
        return False


def start_server():
    """Start the diag server and wait for it to come up.

    Frozen build: run it in-process (a frozen exe can't re-spawn itself as a
    plain interpreter). From source: spawn diag_ui.py as a child so it owns
    the serial port in its own process. Returns the Popen (source) or None
    (frozen / an already-running server we attach to).
    """
    if _server_up():
        return None
    if paths.is_frozen():
        import diag_ui
        diag_ui.serve(port=PORT, connect_car=False, block=False)
        for _ in range(100):
            if _server_up():
                return None
            time.sleep(0.1)
        raise SystemExit("diag server did not start within 10 s")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "diag_ui.py"),
         "--no-connect", "--port-http", str(PORT)],
        cwd=HERE)
    for _ in range(100):            # up to ~10 s
        if _server_up():
            return proc
        if proc.poll() is not None:
            raise SystemExit(
                f"diag server exited early (code {proc.returncode})")
        time.sleep(0.1)
    proc.terminate()
    raise SystemExit("diag server did not start within 10 s")


def main():
    proc = start_server()

    def cleanup():
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    atexit.register(cleanup)
    webview.create_window(
        "GarageDiag", BASE,
        width=1280, height=860, min_size=(900, 600))
    try:
        webview.start()
    finally:
        cleanup()


if __name__ == "__main__":
    main()
