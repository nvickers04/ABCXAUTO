#!/usr/bin/env python3
"""Install an ABCXAUTO Pro icon on the user's Desktop (and app menu where possible).

Usage (from repo root):
  python scripts/install_desktop_icon.py

Creates a launcher that runs:  python -m abcxauto
(Flet Pro cockpit + think stream.)
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
WEB_LOGO = ASSETS / "abcxauto_logo.png"


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
    raise SystemExit("No logo PNG found — expected assets/abcxauto_logo.png")


def _desktop_dir() -> Path:
    # Windows: use the shell Known Folder (handles OneDrive Desktop redirect).
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            CSIDL_DESKTOP = 0x0000
            SHGFP_TYPE_CURRENT = 0
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            if ctypes.windll.shell32.SHGetFolderPathW(
                None, CSIDL_DESKTOP, None, SHGFP_TYPE_CURRENT, buf
            ) == 0 and buf.value:
                d = Path(buf.value)
                if d.is_dir():
                    return d
        except Exception:
            pass
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
                d = Path(os.path.expandvars(value))
                if d.is_dir():
                    return d
        except Exception:
            pass

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
exec "{py}" -m abcxauto
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
Exec={py} -m abcxauto
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


def _ensure_windows_ico(logo: Path) -> Path:
    """Build assets/abcxauto-pro.ico from the logo PNG (Windows shortcuts need .ico)."""
    ico = ASSETS / "abcxauto-pro.ico"
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required to build the Windows icon. Install with: pip install pillow"
        ) from exc
    ASSETS.mkdir(parents=True, exist_ok=True)
    img = Image.open(logo).convert("RGBA")
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ico, format="ICO", sizes=sizes)
    return ico


def _write_windows_launcher(dest: Path) -> Path:
    logo = _logo()
    ASSETS.mkdir(parents=True, exist_ok=True)
    icon_png = ASSETS / "abcxauto-pro.png"
    if not icon_png.is_file() or icon_png.resolve() != logo.resolve():
        shutil.copy2(logo, icon_png)
    ico = _ensure_windows_ico(logo)
    # Single Desktop icon only — no companion .bat/.vbs clutter.
    lnk = _write_windows_shortcut(dest, Path(_python()), ico, args="-m abcxauto")
    print(f"Desktop icon: {lnk}")
    print(f"Icon file: {ico}")
    return lnk


def _write_windows_shortcut(
    dest: Path, target: Path, ico: Path, *, args: str = ""
) -> Path:
    """Create a Desktop .lnk with a custom icon (visible as a real desktop icon)."""
    lnk = dest / "ABCXAUTO Pro.lnk"
    # Prefer pywin32; fall back to WScript.Shell via PowerShell if missing.
    try:
        import win32com.client  # type: ignore

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(lnk))
        shortcut.TargetPath = str(target)
        shortcut.Arguments = args
        shortcut.WorkingDirectory = str(REPO)
        shortcut.Description = "ABCXAUTO Pro"
        shortcut.IconLocation = str(ico)
        shortcut.save()
    except ImportError:
        import subprocess

        ps = f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}')
$s.TargetPath = '{target}'
$s.Arguments = '{args}'
$s.WorkingDirectory = '{REPO}'
$s.Description = 'ABCXAUTO Pro'
$s.IconLocation = '{ico}'
$s.Save()
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=True,
            capture_output=True,
            text=True,
        )
    return lnk


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
    print("Launches Flet Pro (python -m abcxauto).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
