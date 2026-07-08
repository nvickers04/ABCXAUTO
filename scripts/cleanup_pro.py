"""Kill stale ABCXAUTO / Flet desktop processes and clear project Python caches.

Usage:
  python scripts/cleanup_pro.py
  python scripts/cleanup_pro.py --aggressive      # also empty-cmdline python orphans
  python scripts/cleanup_pro.py --flet-cache      # wipe ~/.flet/client
  python -m abcxauto --cleanup
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _ps(cmd: str) -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    return ((r.stdout or "") + (r.stderr or "")).strip()


def kill_stale(*, aggressive: bool = False) -> None:
    flag = "true" if aggressive else "false"
    script = f"""
$aggressive = '{flag}'
$killed = [System.Collections.Generic.List[string]]::new()
function Kill-Pid([int]$Id, [string]$Label) {{
  try {{
    Stop-Process -Id $Id -Force -ErrorAction Stop
    $killed.Add("killed $Id $Label") | Out-Null
    return
  }} catch {{}}
  & taskkill.exe /F /T /PID $Id 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {{ $killed.Add("taskkill $Id $Label") | Out-Null }}
  else {{ $killed.Add("skip $Id $Label") | Out-Null }}
}}
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -match '^(python|pythonw|flet)\\.exe$'
}} | ForEach-Object {{
  $cmd = $_.CommandLine
  $isFlet = $_.Name -eq 'flet.exe'
  $isPro = $cmd -and ($cmd -match 'abcxauto|pro_desktop|pro_launch|_pro_|-m abcxauto')
  $isOrphanPy = ($aggressive -eq 'true') -and ($_.Name -match '^pythonw?\\.exe$') -and (-not $cmd)
  if ($isFlet -or $isPro -or $isOrphanPy) {{ Kill-Pid $_.ProcessId "$($_.Name)" }}
}}
Get-Process -ErrorAction SilentlyContinue | Where-Object {{
  $_.MainWindowTitle -match 'ABCXAUTO Pro|Working\\.\\.\\.'
}} | ForEach-Object {{
  Kill-Pid $_.Id "$($_.ProcessName) title=$($_.MainWindowTitle)"
}}
if ($killed.Count -eq 0) {{ 'killed none' }} else {{ $killed }}
"""
    print(_ps(script) or "killed none")


def _is_project_path(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return not parts.intersection({".venv", "venv", "site-packages", "node_modules", ".git"})


def clear_pycache() -> None:
    n = 0
    for d in REPO.rglob("__pycache__"):
        if d.is_dir() and _is_project_path(d):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
            print(f"removed {d}")
    for p in REPO.rglob("*.pyc"):
        if not _is_project_path(p):
            continue
        try:
            p.unlink()
            n += 1
            print(f"removed {p}")
        except OSError:
            pass
    if not n:
        print("pycache already clean")


def clear_flet_cache() -> None:
    root = Path.home() / ".flet" / "client"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
        print(f"removed {root}")
    else:
        print("flet client cache already clean")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--flet-cache", action="store_true", help="wipe ~/.flet/client")
    ap.add_argument(
        "--aggressive",
        action="store_true",
        help="also kill python processes with empty CommandLine",
    )
    args = ap.parse_args()
    os.chdir(REPO)
    print(f"repo={REPO}")
    kill_stale(aggressive=args.aggressive)
    clear_pycache()
    if args.flet_cache:
        clear_flet_cache()
    sys.path.insert(0, str(REPO))
    import abcxauto.pro_desktop as pro

    print(f"pro_desktop={pro.__file__}")
    print(f"title={pro.TITLE}")
    print(f"has_reveal={hasattr(pro.ProTerminal, '_reveal_window')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
