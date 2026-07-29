"""ABCXAUTO Pro desktop shell — native window around live Pro UI + IBKR API.

Launch:
  python -m abcxauto --desktop
  python -m abcxauto.desktop_app

Serves FastAPI (``/api/*`` → IBKR connector) and the built web UI from
``web-pro/dist`` on one local port, then opens a native window via pywebview.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_PRO = REPO_ROOT / "web-pro"
DIST = WEB_PRO / "dist"
DEFAULT_PORT = int(os.environ.get("ABCXAUTO_DESKTOP_PORT", "8765"))
TITLE = "ABCXAUTO Pro"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int = DEFAULT_PORT) -> int:
    if _port_free(preferred):
        return preferred
    for p in range(preferred + 1, preferred + 40):
        if _port_free(p):
            return p
    raise RuntimeError("No free local port for ABCXAUTO desktop UI")


def _ensure_dist() -> bool:
    index = DIST / "index.html"
    if index.is_file():
        return True
    npm = shutil.which("npm")
    if not npm:
        return False
    print("Building web Pro UI (first run)…", flush=True)
    if not (WEB_PRO / "node_modules").is_dir():
        subprocess.check_call([npm, "install"], cwd=str(WEB_PRO))
    subprocess.check_call([npm, "run", "build"], cwd=str(WEB_PRO))
    return index.is_file()


def _start_api_server(port: int) -> Any:
    import uvicorn

    config = uvicorn.Config(
        "abcxauto.pro_api:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server


def _open_webview(url: str) -> int:
    try:
        import webview  # type: ignore
    except ImportError:
        print(
            "pywebview not installed — opening system browser.\n"
            "  pip install pywebview\n"
            "Then re-run for a native desktop window.",
            flush=True,
        )
        webbrowser.open(url)
        print(f"UI at {url} — press Ctrl+C to stop.", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    icon = _icon_path()
    window_kwargs = dict(
        title=TITLE,
        url=url,
        width=1440,
        height=900,
        min_size=(960, 640),
        background_color="#000000",
        text_select=True,
    )
    if icon and icon.is_file():
        try:
            webview.create_window(**window_kwargs)
            webview.start(icon=str(icon) if sys.platform == "win32" else None)
            return 0
        except TypeError:
            pass
    webview.create_window(**window_kwargs)
    webview.start()
    return 0


def _icon_path() -> Path | None:
    assets = REPO_ROOT / "assets"
    for name in (
        "abcxauto-pro.ico",
        "abcxauto-pro.png",
        "abcxauto_logo.png",
        "icon.png",
    ):
        p = assets / name
        if p.is_file():
            return p
    p = WEB_PRO / "public" / "abcxauto_logo.png"
    return p if p.is_file() else None


def run() -> int:
    if not WEB_PRO.is_dir():
        print(f"web-pro missing at {WEB_PRO}", file=sys.stderr)
        return 1
    if not _ensure_dist():
        print(
            "Could not build web-pro/dist. Install Node 20+ and run:\n"
            "  cd web-pro && npm install && npm run build",
            file=sys.stderr,
        )
        return 1

    port = _pick_port()
    _start_api_server(port)
    url = f"http://127.0.0.1:{port}/"
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        print("API server failed to bind", file=sys.stderr)
        return 1

    print(f"{TITLE} → {url}  (live IBKR API on /api/*)", flush=True)
    return _open_webview(url)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
