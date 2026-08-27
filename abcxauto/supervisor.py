"""Relaunch Pro during useful hours unless the operator killed it."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_REAL_POPEN = subprocess.Popen

_REPO = Path(__file__).resolve().parents[1]
STOP_PATH = _REPO / "data" / "state" / "operator_stop.json"
DESK_OUT_PATH = _REPO / "logs" / "desk.out"


def _desk_out_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_DESK_OUT_PATH") or "").strip()
    return Path(raw) if raw else DESK_OUT_PATH


_DESK_OUT_LOCK = threading.Lock()


def _desk_out_logger() -> logging.Logger:
    """Own file for the child's console. Kept off app.log so the structured
    record stays readable, and off ``propagate`` so it is not double-written.

    The lock matters: the tee thread and the lifecycle notes both land here, and
    two handlers on one file writes every line twice.
    """
    lg = logging.getLogger("abcxauto.desk_out")
    lg.propagate = False
    target = str(_desk_out_path().resolve())
    with _DESK_OUT_LOCK:
        for h in list(lg.handlers):
            if not isinstance(h, RotatingFileHandler):
                continue
            try:
                same = Path(getattr(h, "baseFilename", "")).resolve() == Path(target)
            except OSError:
                return lg
            if same:
                return lg
            lg.removeHandler(h)
            h.close()
        _desk_out_path().parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
    return lg


def tee_child_output(stream: Any) -> None:
    """Persist the child's stdout/stderr and still echo it to our console.

    Tracebacks that kill a flet loop, and the ib_insync warnings, only ever reach
    the child's console. Losing them means the operator has no evidence of a
    crash the desk survived.
    """
    log = _desk_out_logger()
    for raw in iter(stream.readline, ""):
        line = str(raw).rstrip()
        if not line:
            continue
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        try:
            log.info(line)
        except Exception:
            pass


def _stop_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_OPERATOR_STOP_PATH") or "").strip()
    return Path(raw) if raw else STOP_PATH


def mark_operator_stop() -> None:
    p = _stop_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"ts": datetime.now().isoformat(), "stop": True}),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("operator_stop write failed", exc_info=True)


def clear_operator_stop() -> None:
    p = _stop_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        logger.debug("operator_stop clear failed", exc_info=True)


def operator_stopped() -> bool:
    p = _stop_path()
    if not p.is_file():
        return False
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return True
    return bool(raw.get("stop", True))


def useful_hours(*, now: datetime | None = None) -> bool:
    """RTH plus last hour of premarket (ET). Closed / weekend stay down."""
    try:
        from zoneinfo import ZoneInfo

        clock = now or datetime.now(ZoneInfo("America/New_York"))
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
        else:
            clock = clock.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        clock = now or datetime.now()
    if clock.weekday() >= 5:
        return False
    minutes = clock.hour * 60 + clock.minute
    return (8 * 60 + 30) <= minutes < (16 * 60)


def tws_listening(host: str = "127.0.0.1", port: int = 7497, timeout: float = 2.0) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


DESK_LOCK_PATH = _REPO / "data" / "state" / "desk.lock"


def _lock_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_DESK_LOCK_PATH") or "").strip()
    return Path(raw) if raw else DESK_LOCK_PATH


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, 0, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def desk_owner_pid() -> int:
    """PID of a live desk, or 0. One book = one process, one client id."""
    p = _lock_path()
    if not p.is_file():
        return 0
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        pid = int(raw.get("pid") or 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0
    return pid if _pid_alive(pid) else 0


def claim_desk_lock() -> bool:
    """Claim the desk for this process. False when another desk is already up."""
    owner = desk_owner_pid()
    if owner and owner != os.getpid():
        return False
    p = _lock_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"pid": os.getpid(), "ts": datetime.now().isoformat()}),
            encoding="utf-8",
        )
        return True
    except OSError:
        logger.debug("desk lock write failed", exc_info=True)
        return True


def release_desk_lock(*, force: bool = False) -> None:
    p = _lock_path()
    try:
        if not p.is_file():
            return
        if force or desk_owner_pid() in (0, os.getpid()):
            p.unlink()
    except OSError:
        logger.debug("desk lock release failed", exc_info=True)


def clear_stale_desk_lock() -> bool:
    """Drop desk.lock when the owner pid is already dead. Live owners stay."""
    p = _lock_path()
    if not p.is_file():
        return False
    if desk_owner_pid() != 0:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        logger.debug("stale desk lock clear failed", exc_info=True)
        return False


# Same launchers cleanup_pro matches. Not cleanup itself — Start is not flatten.
_PRO_CMDLINE_MARKERS = (
    "-m abcxauto",
    "-mabcxauto",
    "abcxauto.pro_desktop",
    "pro_desktop.py",
    "_start_pro.py",
    "pro_launch",
)
_PRO_CMDLINE_SKIP = ("cleanup_pro", "--cleanup", "pytest")


def _cmdline_is_pro(cmd: Any) -> bool:
    """True for a Pro / supervisor launcher, never for cleanup or pytest."""
    if isinstance(cmd, (list, tuple)):
        blob = " ".join(str(part) for part in cmd)
    else:
        blob = str(cmd or "")
    low = blob.lower().replace("\\", "/")
    if any(skip in low for skip in _PRO_CMDLINE_SKIP):
        return False
    return any(mark in low for mark in _PRO_CMDLINE_MARKERS)


def live_pro_pids(*, exclude: set[int] | None = None) -> list[int]:
    """PIDs of already-running Pro desks. Read-only — never signals them."""
    skip = {os.getpid()}
    try:
        parent = int(os.getppid() or 0)
        if parent > 0:
            skip.add(parent)
    except Exception:
        pass
    if exclude:
        skip.update(int(pid) for pid in exclude if int(pid) > 0)
    found: list[int] = []
    try:
        import psutil

        for proc in psutil.process_iter(["pid", "cmdline"]):
            info = proc.info or {}
            pid = int(info.get("pid") or 0)
            if pid <= 0 or pid in skip:
                continue
            if _cmdline_is_pro(info.get("cmdline") or []):
                found.append(pid)
    except Exception:
        logger.debug("live Pro scan failed", exc_info=True)
    return found


def foreign_desk_pid(*, exclude: set[int] | None = None) -> int:
    """A live paper Pro that is not this process, or 0.

    The desk lock is the supervisor wrapper's pid. ``_start_pro.py`` never
    writes it, and a dead wrapper heals the lock while its child still holds
    client id 42 on 7497. Spawning then is Error 326 — or a stolen session
    that looks like Start flattened the book.
    """
    skip = {os.getpid()}
    if exclude:
        skip.update(int(pid) for pid in exclude if int(pid) > 0)
    owner = desk_owner_pid()
    if owner and owner not in skip:
        return owner
    for pid in live_pro_pids(exclude=skip):
        return pid
    return 0


_TWS_NAME_MARKERS = ("tws.exe", "ibgateway", "twslaunch")


def ancestor_pids() -> set[int]:
    """Parent chain of this process. Flet re-entry must not kill the desk."""
    found: set[int] = set()
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        for _ in range(12):
            parent = proc.parent()
            if parent is None:
                break
            pid = int(parent.pid or 0)
            if pid <= 0 or pid in found:
                break
            found.add(pid)
            proc = parent
    except Exception:
        pass
    try:
        ppid = int(os.getppid() or 0)
        if ppid > 0:
            found.add(ppid)
    except Exception:
        pass
    return found


def protected_pids(*, extra: set[int] | None = None) -> set[int]:
    """PIDs start/stop must never signal: this process, parents, extras."""
    skip = {os.getpid()}
    skip.update(ancestor_pids())
    if extra:
        skip.update(int(pid) for pid in extra if int(pid) > 0)
    return {pid for pid in skip if pid > 1}


def ancestor_holds_desk() -> bool:
    """True when a live lock owner is in our parent chain (Flet re-entry)."""
    owner = desk_owner_pid()
    if not owner or owner == os.getpid():
        return False
    return owner in ancestor_pids()


def _is_in_tree(root: int, target: int) -> bool:
    if root <= 0 or target <= 0:
        return False
    if root == target:
        return True
    try:
        import psutil

        return any(int(c.pid) == target for c in psutil.Process(int(root)).children(recursive=True))
    except Exception:
        return False


def _pid_is_tws(pid: int) -> bool:
    """TWS / Gateway stay up. Never part of the Pro tree."""
    try:
        import psutil

        proc = psutil.Process(int(pid))
        blob = f"{proc.name() or ''} {' '.join(str(p) for p in (proc.cmdline() or []))}"
    except Exception:
        return False
    low = blob.lower().replace("\\", "/")
    return any(mark in low for mark in _TWS_NAME_MARKERS)


def process_tree_pids(root: int, *, exclude: set[int] | None = None) -> list[int]:
    """Descendants first, then root. Protected pids are omitted."""
    skip = protected_pids(extra=exclude)
    root = int(root or 0)
    if root <= 1 or root in skip or _pid_is_tws(root):
        return []
    kids: list[int] = []
    try:
        import psutil

        kids = [int(c.pid) for c in psutil.Process(root).children(recursive=True)]
    except Exception:
        kids = []
    ordered: list[int] = []
    seen: set[int] = set()
    for pid in [*kids, root]:
        if pid <= 1 or pid in skip or pid in seen or _pid_is_tws(pid):
            continue
        seen.add(pid)
        ordered.append(pid)
    return ordered


def kill_pid(pid: int, *, tree: bool = False) -> bool:
    """Kill one ABCXAUTO pid. Never TWS. Never this process / parents."""
    pid = int(pid or 0)
    if pid <= 1 or pid in protected_pids() or _pid_is_tws(pid):
        return False
    if os.name == "nt":
        cmd = ["taskkill", "/F", "/PID", str(pid)]
        if tree:
            cmd.insert(2, "/T")
        try:
            killer = _REAL_POPEN(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            killer.communicate(timeout=5)
            return int(killer.returncode or 0) == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False


def kill_pid_tree(root: int, *, exclude: set[int] | None = None) -> list[int]:
    """Kill a leftover Pro python and its flet children. No orphan window."""
    skip = protected_pids(extra=exclude)
    root = int(root or 0)
    if root <= 1 or root in skip:
        return []
    tree = process_tree_pids(root, exclude=skip)
    if len(tree) <= 1 and os.name == "nt":
        if kill_pid(root, tree=True):
            return [root]
        return []
    killed: list[int] = []
    for pid in tree:
        if kill_pid(pid):
            killed.append(pid)
    return killed


def leftover_pro_pids(*, exclude: set[int] | None = None) -> list[int]:
    """Other ABCXAUTO python launchers. Never self, parents, or our own tree."""
    skip = protected_pids(extra=exclude)
    me = os.getpid()
    found: list[int] = []
    for pid in live_pro_pids(exclude=skip):
        if pid in skip or _pid_is_tws(pid):
            continue
        if _is_in_tree(pid, me):
            continue
        found.append(pid)
    return found


def flet_descendant_pids(root: int | None = None) -> list[int]:
    """flet.exe / flet children of a python so Stop can close the window."""
    if root is None:
        root = os.getpid()
    root = int(root or 0)
    if root <= 1:
        return []
    out: list[int] = []
    try:
        import psutil

        for child in psutil.Process(root).children(recursive=True):
            name = str(child.name() or "").lower()
            if name in ("flet.exe", "flet"):
                out.append(int(child.pid))
    except Exception:
        return []
    return out


def kill_descendant_flet(*, root: int | None = None) -> list[int]:
    """Window close / child exit: drop this python's flet, leave TWS alone."""
    killed: list[int] = []
    for pid in flet_descendant_pids(root):
        if kill_pid(pid):
            killed.append(pid)
    return killed


def list_flet_rows() -> list[dict[str, Any]]:
    """Windows flet.exe snapshot. Empty on other hosts."""
    if os.name != "nt":
        return []
    try:
        proc = _REAL_POPEN(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='flet.exe'\" | "
                "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
                "ConvertTo-Json -Compress",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        raw, _ = proc.communicate(timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        parsed = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    return [row for row in parsed if isinstance(row, dict)]


def reap_leftover_desk(*, exclude: set[int] | None = None) -> list[int]:
    """Kill leftover _start_pro / Pro python and ABCXAUTO flet from a dead parent.

    Start is not flatten: positions stay at IBKR. TWS is not in this set.
    """
    skip = protected_pids(extra=exclude)
    killed: list[int] = []
    seen: set[int] = set()
    for pid in leftover_pro_pids(exclude=skip):
        for dead in kill_pid_tree(pid, exclude=skip):
            if dead not in seen:
                seen.add(dead)
                killed.append(dead)
    for pid in orphan_flet_pids(list_flet_rows(), repo=str(_REPO)):
        if pid in skip or pid in seen:
            continue
        if kill_pid(pid):
            seen.add(pid)
            killed.append(pid)
    if killed:
        note(f"supervisor: reaped leftover Pro/flet {killed}")
    # Lock owner may have been the leftover python we just killed.
    clear_stale_desk_lock()
    return killed


def prepare_desk_start(*, exclude: set[int] | None = None) -> list[int]:
    """One tree on Start: drop stale lock/stop, reap leftovers, then launch."""
    clear_stale_desk_lock()
    clear_operator_stop()
    return reap_leftover_desk(exclude=exclude)


def stop_desk(*, exclude: set[int] | None = None) -> list[int]:
    """One tree on Stop: latch stop, kill Pro python + flet, drop the lock."""
    mark_operator_stop()
    killed = reap_leftover_desk(exclude=exclude)
    killed.extend(kill_descendant_flet())
    release_desk_lock(force=True)
    return killed


def note(msg: str, *, warn: bool = False) -> None:
    """Record a lifecycle decision durably.

    "why is the desk down" is answered by these lines, and they used to go only to
    the console the supervisor was started from — gone the moment it closed.
    """
    (logger.warning if warn else logger.info)(msg)
    try:
        _desk_out_logger().info(msg)
    except Exception:
        pass


def orphan_flet_pids(
    rows: list[dict[str, Any]],
    *,
    repo: str = "",
    pid_alive: Any = None,
) -> list[int]:
    """Flet windows for this repo whose python parent is already dead."""
    root = str(repo or _REPO)
    alive = _pid_alive if pid_alive is None else pid_alive
    out: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("Name") or "").lower()
        if name != "flet.exe":
            continue
        cmd = str(row.get("cmd") or row.get("CommandLine") or "")
        if root.lower() not in cmd.lower():
            continue
        try:
            parent = int(row.get("parent") or row.get("ParentProcessId") or 0)
            pid = int(row.get("pid") or row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        if parent > 0 and alive(parent):
            continue
        out.append(pid)
    return out


def sweep_orphan_flet_windows() -> list[int]:
    """Stop leftover ABCXAUTO windows after a python-only kill/reload."""
    killed: list[int] = []
    for pid in orphan_flet_pids(list_flet_rows(), repo=str(_REPO)):
        if kill_pid(pid):
            killed.append(pid)
    if killed:
        note(f"supervisor: swept orphan flet {killed}")
    return killed


def supervise(child_env: dict[str, str] | None = None) -> int:
    """Run Pro as a child. Relaunch on crash during useful hours if TWS is up."""
    try:
        from abcxauto.config import setup_file_logging

        setup_file_logging()
    except Exception:
        logger.debug("supervisor file logging failed", exc_info=True)
    env = dict(os.environ)
    if child_env:
        env.update(child_env)
    env["ABCXAUTO_SUPERVISED"] = "1"
    backoff = 15.0
    child_pid = 0
    while True:
        try:
            # Reap leftover _start_pro / Pro python and orphan flet before spawn.
            # Do not clear operator_stop here — a crash loop must still honor Stop.
            reap_leftover_desk(exclude={child_pid} if child_pid else None)
        except Exception:
            logger.debug("leftover Pro reap skipped", exc_info=True)
            try:
                sweep_orphan_flet_windows()
            except Exception:
                logger.debug("orphan flet sweep skipped", exc_info=True)
        held = foreign_desk_pid(exclude={child_pid})
        if held:
            note(f"supervisor: Pro already up (pid {held}) — stay down")
            return 0
        proc = subprocess.Popen(
            [sys.executable, "-m", "abcxauto"],
            env=env,
            cwd=str(_REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            errors="replace",
        )
        child_pid = int(getattr(proc, "pid", 0) or 0)
        stream = getattr(proc, "stdout", None)
        if stream is not None:
            threading.Thread(
                target=tee_child_output, args=(stream,), daemon=True
            ).start()
        note(f"supervisor: child pid {child_pid} up")
        code = proc.wait()
        try:
            kill_descendant_flet(root=child_pid)
            reap_leftover_desk(exclude={child_pid})
        except Exception:
            logger.debug("post-exit Pro reap skipped", exc_info=True)
        if int(code or 0) == 0:
            note("supervisor: clean exit — operator closed the window, stay down")
            return 0
        if operator_stopped():
            note("supervisor: operator stop — stay down")
            return int(code or 0)
        held = foreign_desk_pid(exclude={child_pid})
        if held:
            note(
                f"supervisor: Pro still up (pid {held}) after child exit {code} — stay down"
            )
            return int(code or 0)
        if not useful_hours():
            note("supervisor: outside useful hours — stay down")
            return int(code or 0)
        if not tws_listening():
            note("supervisor: TWS 7497 down — stay down")
            return int(code or 0)
        note(f"supervisor: child exited {code} — relaunch in {backoff:.0f}s", warn=True)
        time.sleep(backoff)
        backoff = min(60.0, backoff * 2)
