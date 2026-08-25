"""Relaunch Pro during useful hours unless the operator killed it."""

from __future__ import annotations

import json
import logging
import os
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


def release_desk_lock() -> None:
    p = _lock_path()
    try:
        if p.is_file() and desk_owner_pid() in (0, os.getpid()):
            p.unlink()
    except OSError:
        logger.debug("desk lock release failed", exc_info=True)


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
    killed = orphan_flet_pids(
        [r for r in parsed if isinstance(r, dict)],
        repo=str(_REPO),
    )
    for pid in killed:
        try:
            killer = _REAL_POPEN(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            killer.communicate(timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
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
    while True:
        try:
            sweep_orphan_flet_windows()
        except Exception:
            logger.debug("orphan flet sweep skipped", exc_info=True)
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
        stream = getattr(proc, "stdout", None)
        if stream is not None:
            threading.Thread(
                target=tee_child_output, args=(stream,), daemon=True
            ).start()
        note(f"supervisor: child pid {getattr(proc, 'pid', 0)} up")
        code = proc.wait()
        if int(code or 0) == 0:
            note("supervisor: clean exit — operator closed the window, stay down")
            return 0
        if operator_stopped():
            note("supervisor: operator stop — stay down")
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
