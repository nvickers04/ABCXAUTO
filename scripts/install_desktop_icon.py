#!/usr/bin/env python3
"""Install an ABCXAUTO Pro icon on the user's Desktop (and app menu where possible).

Usage (from repo root):
  python scripts/install_desktop_icon.py

Creates a launcher that runs:  python -m abcxauto --desktop
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "assets"
WEB_LOGO = REPO / "web-pro" / "public" / "abcxauto_logo.png"


def _python() -> str:
    return sys.executable


def _logo() -> Path:
    for p in (
        ASSETS / "abcxauto-pro.png",
        ASSETS / "abcxauto_logo.png",
        WEB_LOGO,
    ):
        if p.is_file():
            return p
    raise SystemExit("No logo PNG found — expected web-pro/public/abcxauto_logo.png")


def _desktop_dir() -> Path:
    home = Path.home()
    # Linux XDG + common Desktop names
    for name in ("Desktop", "desktop", "Schreibtisch"):
        d = home / name
        if d.is_dir():
            return d
    xdg = os.environ.get("XDG_DESKTOP_DIR")
    if xdg:
        return Path(xdg)
    d = home / "Desktop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_unix_launcher(dest: Path) -> Path:
    logo = _logo()
    # copy icon next to launcher assets
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon_png = ASSETS / "abcxauto-pro.png"
    if not icon_png.is_file() or icon_png.resolve() != logo.resolve():
        shutil.copy2(logo, icon_png)

    sh = dest / "ABCXAUTO Pro.command" if platform.system() == "Darwin" else dest / "ABCXAUTO Pro.desktop"
    py = _python()
    if platform.system() == "Darwin":
        content = f"""#!/bin/bash
cd "{REPO}"
export ABCXAUTO_DESKTOP=1
exec "{py}" -m abcxauto --desktop
"""
        sh.write_text(content)
        sh.chmod(sh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        # macOS: also try to set custom icon via file icon (best-effort)
        try:
            subprocess_set_icon = f'''
use framework "AppKit"
set imagePath to "{icon_png}"
set filePath to "{sh}"
'''
            # simpler: create app-like folder
        except Exception:
            pass
        print(f"Created: {sh}")
        print("On macOS: right-click → Open once if Gatekeeper blocks.")
        return sh

    # Linux .desktop
    desktop_file = dest / "ABCXAUTO Pro.desktop"
    desktop_file.write_text(
        f"""[Desktop Entry]
Type=Application
Name=ABCXAUTO Pro
Comment=Agentic IBKR portfolio cockpit
Exec={py} -m abcxauto --desktop
Path={REPO}
Icon={icon_png}
Terminal=false
Categories=Finance;Office;
StartupWMClass=ABCXAUTO Pro
"""
    )
    desktop_file.chmod(desktop_file.stat().st_mode | stat.S_IEXEC)
    # also install to applications menu
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    menu = apps / "abcxauto-pro.desktop"
    shutil.copy2(desktop_file, menu)
    menu.chmod(menu.stat().st_mode | stat.S_IEXEC)
    print(f"Created: {desktop_file}")
    print(f"App menu: {menu}")
    return desktop_file


def _write_windows_launcher(dest: Path) -> Path:
    logo = _logo()
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon_png = ASSETS / "abcxauto-pro.png"
    shutil.copy2(logo, icon_png)
    bat = dest / "ABCXAUTO Pro.bat"
    py = _python()
    bat.write_text(
        f"""@echo off
cd /d "{REPO}"
"{py}" -m abcxauto --desktop
if errorlevel 1 pause
"""
    )
    # VBS to hide console
    vbs = dest / "ABCXAUTO Pro.vbs"
    vbs.write_text(
        f'''Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "{REPO}"
sh.Run """{py}"" -m abcxauto --desktop", 0, False
'''
    )
    print(f"Created: {bat}")
    print(f"Silent launcher: {vbs}")
    print("Tip: right-click Desktop → New → Shortcut → point at the .vbs file,")
    print(f"     then Change Icon… and pick {icon_png} (or convert to .ico).")
    return bat


def main() -> int:
    dest = _desktop_dir()
    system = platform.system()
    print(f"Repo: {REPO}")
    print(f"Desktop: {dest}")
    print(f"Python: {_python()}")
    if system == "Windows":
        _write_windows_launcher(dest)
    else:
        _write_unix_launcher(dest)
    print()
    print("Done. Double-click “ABCXAUTO Pro” on your Desktop.")
    print("First launch builds the UI if needed (Node required once).")
    print("Optional native window:  pip install pywebview")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
