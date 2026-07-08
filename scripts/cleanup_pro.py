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


def kill_stale(
    *,
    aggressive: bool = False,
    exclude_pids: set[int] | None = None,
    python_targets: bool = True,
) -> None:
    """Kill stale Pro/Flet processes, never the caller or its parent chain.

    ``python_targets=False`` only clears flet.exe and windows titled
    ABCXAUTO Pro / Working… — safe for pre-launch cleanup so the brand-new
    ``python -m abcxauto`` process is never matched and killed.
    """
    exclude: set[int] = {os.getpid(), os.getppid()}
    if exclude_pids:
        exclude.update(int(p) for p in exclude_pids if int(p) > 0)
    # Walk a few parent levels so nested python -m abcxauto -> cleanup -> powershell
    # never suicides the launcher.
    try:
        import psutil  # optional

        proc = psutil.Process(os.getpid())
        for _ in range(4):
            parent = proc.parent()
            if parent is None:
                break
            exclude.add(parent.pid)
            proc = parent
    except Exception:
        pass

    flag = "true" if aggressive else "false"
    kill_py = "true" if python_targets else "false"
    exclude_csv = ",".join(str(p) for p in sorted(exclude) if p > 0) or "0"
    script = f"""
$aggressive = '{flag}'
$killPy = '{kill_py}'
$exclude = @({exclude_csv}) | ForEach-Object {{ [int]$_ }}
$excludeSet = @{{}}
foreach ($p in $exclude) {{ $excludeSet[$p] = $true }}
$killed = [System.Collections.Generic.List[string]]::new()
function Kill-Pid([int]$Id, [string]$Label) {{
  if ($excludeSet.ContainsKey($Id)) {{
    $killed.Add("skip $Id self $Label") | Out-Null
    return
  }}
  try {{
    Stop-Process -Id $Id -Force -ErrorAction Stop
    $killed.Add("killed $Id $Label") | Out-Null
    return
  }} catch {{}}
  & taskkill.exe /F /PID $Id 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {{ $killed.Add("taskkill $Id $Label") | Out-Null }}
  else {{ $killed.Add("skip $Id $Label") | Out-Null }}
}}
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -match '^(python|pythonw|flet)\\.exe$'
}} | ForEach-Object {{
  $procId = [int]$_.ProcessId
  if ($excludeSet.ContainsKey($procId)) {{ return }}
  $cmd = $_.CommandLine
  $isFlet = $_.Name -eq 'flet.exe'
  # Match Pro app launches, not every path that merely contains the folder name
  # (e.g. cleanup_pro.py lives under ABCXAUTO\\scripts).
  $isPro = ($killPy -eq 'true') -and $cmd -and (
    $cmd -match '(-m\\s+abcxauto(\\s|$))|(abcxauto\\.pro_desktop)|(pro_desktop\\.py)|(pro_launch)|(_pro_)'
  )
  $isOrphanPy = ($aggressive -eq 'true') -and ($_.Name -match '^pythonw?\\.exe$') -and (-not $cmd)
  if ($isFlet -or $isPro -or $isOrphanPy) {{ Kill-Pid $procId "$($_.Name)" }}
}}
Get-Process -ErrorAction SilentlyContinue | Where-Object {{
  $_.MainWindowTitle -match 'ABCXAUTO Pro|Working\\.\\.\\.'
}} | ForEach-Object {{
  Kill-Pid ([int]$_.Id) "$($_.ProcessName) title=$($_.MainWindowTitle)"
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
    ap.add_argument(
        "--exclude-pid",
        action="append",
        type=int,
        default=[],
        help="PID(s) that must not be killed (launcher / parent)",
    )
    ap.add_argument(
        "--kill-only",
        action="store_true",
        help="only kill stale processes (skip pycache / probe prints)",
    )
    ap.add_argument(
        "--ui-only",
        action="store_true",
        help="only kill flet.exe / titled Pro windows (never python -m abcxauto)",
    )
    args = ap.parse_args()
    os.chdir(REPO)
    print(f"repo={REPO}")
    kill_stale(
        aggressive=args.aggressive,
        exclude_pids=set(args.exclude_pid or []),
        python_targets=not args.ui_only,
    )
    if args.kill_only or args.ui_only:
        return 0
    clear_pycache()
    if args.flet_cache:
        clear_flet_cache()
    sys.path.insert(0, str(REPO))
    import abcxauto.pro_desktop as pro

    print(f"pro_desktop={pro.__file__}")
    title = getattr(pro, "TITLE", None) or getattr(pro, "PRO_TITLE", "?")
    print(f"title={title}")
    print(f"has_reveal={hasattr(pro.ProTerminal, '_reveal_window')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
