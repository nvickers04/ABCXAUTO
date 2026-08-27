"""Kill leftover Flet / Pro python and clear project Python caches.

Operator stop (``python -m abcxauto --cleanup``) kills the paper Pro tree
(python + flet). Start reaps leftovers in ``supervisor.prepare_desk_start``
and does not call this script — Start is not flatten. Never TWS. Never flatten.

Usage:
  python scripts/cleanup_pro.py
  python scripts/cleanup_pro.py --aggressive      # also empty-cmdline python orphans
  python scripts/cleanup_pro.py --flet-cache      # wipe ~/.flet/client
  python -m abcxauto --cleanup
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# Same launchers the supervisor uses to see a lockless _start_pro. Not cleanup
# itself — Start is not flatten.
_PRO_CMDLINE_MARKERS = (
    "-m abcxauto",
    "-mabcxauto",
    "abcxauto.pro_desktop",
    "pro_desktop.py",
    "_start_pro.py",
    "pro_launch",
)
_PRO_CMDLINE_SKIP = ("cleanup_pro", "--cleanup", "pytest")


def _ps(cmd: str) -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    return ((r.stdout or "") + (r.stderr or "")).strip()


def cmdline_is_pro(cmd: Any) -> bool:
    """True for a Pro / supervisor launcher, never for cleanup or pytest."""
    if isinstance(cmd, (list, tuple)):
        blob = " ".join(str(part) for part in cmd)
    else:
        blob = str(cmd or "")
    low = blob.lower().replace("\\", "/")
    if any(skip in low for skip in _PRO_CMDLINE_SKIP):
        return False
    return any(mark in low for mark in _PRO_CMDLINE_MARKERS)


def _desk_lock_pid() -> int:
    raw = (os.environ.get("ABCXAUTO_DESK_LOCK_PATH") or "").strip()
    path = Path(raw) if raw else REPO / "data" / "state" / "desk.lock"
    if not path.is_file():
        return 0
    try:
        pid = int((json.loads(path.read_text(encoding="utf-8")) or {}).get("pid") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def spare_live_paper_pids() -> tuple[set[int], bool]:
    """PIDs of the live paper Pro on 7497 (and its tree).

    ``scan_ok`` is False when we cannot see processes — fail closed: do not
    kill python launchers, flet.exe, or ABCXAUTO Pro titles. A desk that is
    still connecting has no 7497 socket yet; spare every Pro launcher.
    """
    spare: set[int] = set()
    owner = _desk_lock_pid()
    if owner:
        spare.add(owner)
    skip = {os.getpid()}
    try:
        parent = int(os.getppid() or 0)
        if parent > 0:
            skip.add(parent)
    except Exception:
        pass
    try:
        import psutil
    except Exception:
        return {p for p in spare if p > 0}, False
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
            if pid <= 0 or pid in skip or not cmdline_is_pro(info.get("cmdline") or []):
                continue
            spare.add(pid)
        extra: set[int] = set()
        for pid in list(spare):
            try:
                extra.update(int(c.pid) for c in psutil.Process(pid).children(recursive=True))
            except Exception:
                pass
        spare.update(extra)
        return {p for p in spare if p > 0}, True
    except Exception:
        return {p for p in spare if p > 0}, False


def kill_policy(
    *,
    aggressive: bool = False,
    exclude_pids: set[int] | None = None,
    python_targets: bool = True,
) -> tuple[set[int], bool, bool, bool]:
    """Return (exclude, kill_python, kill_flet, kill_pro_title). Never flatten."""
    exclude: set[int] = {os.getpid(), os.getppid()}
    if exclude_pids:
        exclude.update(int(p) for p in exclude_pids if int(p) > 0)
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        for _ in range(4):
            parent = proc.parent()
            if parent is None:
                break
            exclude.add(parent.pid)
            proc = parent
    except Exception:
        pass
    _, scan_ok = spare_live_paper_pids()
    # Stop must kill that python and its flet. Self / parents stay excluded.
    # Fail closed when we cannot see processes — do not spray SIGKILL.
    kill_python = bool(python_targets) and scan_ok
    kill_flet = scan_ok
    kill_pro_title = scan_ok
    return {p for p in exclude if p > 0}, kill_python, kill_flet, kill_pro_title


def kill_stale(
    *,
    aggressive: bool = False,
    exclude_pids: set[int] | None = None,
    python_targets: bool = True,
) -> None:
    """Kill leftover Pro python + flet. Never flatten. Never TWS.

    ``python_targets=False`` skips python launchers. Self / parent / exclude_pids
    stay spared. If the process scan fails, python / flet / titles are left alone.
    """
    exclude, kill_python, kill_flet, kill_pro_title = kill_policy(
        aggressive=aggressive,
        exclude_pids=exclude_pids,
        python_targets=python_targets,
    )
    flag = "true" if aggressive else "false"
    kill_py = "true" if kill_python else "false"
    kill_flet_s = "true" if kill_flet else "false"
    kill_title_s = "true" if kill_pro_title else "false"
    exclude_csv = ",".join(str(p) for p in sorted(exclude) if p > 0) or "0"
    # Precise Pro markers — not ``_pro_``, which also matches cleanup_pro.py.
    script = f"""
$aggressive = '{flag}'
$killPy = '{kill_py}'
$killFlet = '{kill_flet_s}'
$killProTitle = '{kill_title_s}'
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
  $isFlet = ($killFlet -eq 'true') -and ($_.Name -eq 'flet.exe')
  $isCleanup = $cmd -and ($cmd -match 'cleanup_pro|--cleanup|pytest')
  $isPro = ($killPy -eq 'true') -and $cmd -and (-not $isCleanup) -and (
    $cmd -match '(-m\\s+abcxauto(\\s|$))|-mabcxauto|abcxauto\\.pro_desktop|pro_desktop\\.py|_start_pro\\.py|pro_launch'
  )
  $isOrphanPy = ($aggressive -eq 'true') -and ($_.Name -match '^pythonw?\\.exe$') -and (-not $cmd)
  if ($isFlet -or $isPro -or $isOrphanPy) {{ Kill-Pid $procId "$($_.Name)" }}
}}
Get-Process -ErrorAction SilentlyContinue | Where-Object {{
  if ($killProTitle -eq 'true') {{
    $_.MainWindowTitle -match 'ABCXAUTO Pro|Working\\.\\.\\.'
  }} else {{
    $_.MainWindowTitle -match 'Working\\.\\.\\.'
  }}
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
        help="only kill stale leftovers (never the live paper Pro; skip pycache)",
    )
    ap.add_argument(
        "--ui-only",
        action="store_true",
        help="only kill orphan flet.exe / leftover titles (never python -m abcxauto)",
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
