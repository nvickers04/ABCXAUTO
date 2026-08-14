"""Live Grok thinking stream — tool-loop tokens as they arrive.

Headless prints to stdout (ASCII). ProEngine binds so the UI can show the same buffer.
A short tail file lets Cursor review the stream without the window.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

Listener = Callable[[str, str], None]

_lock = threading.Lock()
_listeners: list[Listener] = []
_engine: Any = None
_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
THINK_TAIL_PATH = _STATE_DIR / "think_tail.txt"
LAST_TURN_PATH = _STATE_DIR / "last_turn.json"
RUN_PATH = _STATE_DIR / "run.json"
_TAIL_MIN_INTERVAL = 2.0
_last_tail_write = 0.0
_run: dict[str, Any] = {}

logger = logging.getLogger(__name__)


def ascii_text(text: str) -> str:
    """Windows consoles are often cp1252 — never emit non-ASCII to stdout."""
    return (text or "").encode("ascii", "replace").decode("ascii")


def subscribe(fn: Listener) -> None:
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def unsubscribe(fn: Listener) -> None:
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def bind_engine(engine: Any | None) -> None:
    """One ProEngine at a time; think_live buffer updates without flooding the UI queue."""
    global _engine
    _engine = engine


def emit(kind: str, text: str) -> None:
    if not text:
        return
    with _lock:
        fns = list(_listeners)
        eng = _engine
    if eng is not None:
        try:
            _append_engine(eng, kind, text)
        except Exception:
            logger.debug("think_stream engine append failed", exc_info=True)
    for fn in fns:
        try:
            fn(kind, text)
        except Exception:
            logger.debug("think_stream listener failed", exc_info=True)


def _append_engine(eng: Any, kind: str, text: str) -> None:
    s = getattr(eng, "state", None)
    if s is None:
        return
    if kind == "stage":
        label = ascii_text(text).strip().upper()
        if label in ("", "GROK", "JUDGE", "ACT"):
            piece = "\n--- GROK ---\n"
        else:
            piece = f"\n--- GROK {label} ---\n"
    elif kind == "stage_end":
        piece = "\n"
    else:
        piece = ascii_text(text)
    cur = getattr(s, "think_live", "") or ""
    s.think_live = (cur + piece)[-24000:]
    _write_think_tail(s.think_live)


def _write_think_tail(buf: str) -> None:
    global _last_tail_write
    now = time.monotonic()
    if now - _last_tail_write < _TAIL_MIN_INTERVAL:
        return
    _last_tail_write = now
    try:
        THINK_TAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
        THINK_TAIL_PATH.write_text(buf[-8000:], encoding="utf-8")
    except OSError:
        logger.debug("think_tail write failed", exc_info=True)


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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def current_run() -> dict[str, Any]:
    if _run:
        return dict(_run)
    return _read_json(RUN_PATH)


def mark_review_stale() -> None:
    """Prior last_turn / think_tail belong to a dead or stopped process."""
    prev = _read_json(LAST_TURN_PATH)
    run = current_run()
    payload = {
        "stale": True,
        "previous_run_id": prev.get("run_id") or run.get("run_id") or "",
        "previous_cycle": prev.get("cycle"),
        "previous_strat": prev.get("strat") or "",
    }
    try:
        LAST_TURN_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_TURN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("last_turn stale mark failed", exc_info=True)
    try:
        if THINK_TAIL_PATH.is_file():
            THINK_TAIL_PATH.write_text("", encoding="utf-8")
    except OSError:
        logger.debug("think_tail clear failed", exc_info=True)


def begin_run() -> dict[str, Any]:
    """Stamp a new process identity. Call after killing leftovers."""
    global _run
    mark_review_stale()
    _run = {
        "run_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUN_PATH.write_text(json.dumps(_run, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("run.json write failed", exc_info=True)
    return dict(_run)


def last_turn_is_live(payload: dict[str, Any] | None = None) -> bool:
    """True only if last_turn belongs to this live process."""
    data = payload if isinstance(payload, dict) else _read_json(LAST_TURN_PATH)
    if not data or data.get("stale"):
        return False
    run = current_run()
    if not run or data.get("run_id") != run.get("run_id"):
        return False
    try:
        pid = int(run.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid and not _pid_alive(pid):
        return False
    return True


def write_last_turn(out: dict[str, Any]) -> None:
    """Clerk snapshot of the last cycle for the Cursor review loop."""
    try:
        LAST_TURN_PATH.parent.mkdir(parents=True, exist_ok=True)
        pulse = out.get("reality_pulse") or {}
        world = out.get("world_state") or {}
        run = current_run()
        payload = {
            "cycle": out.get("cycle"),
            "strat": out.get("strat"),
            "rationale": (out.get("rationale") or "")[:400],
            "validation": (out.get("validation") or "")[:400],
            "tool_trace": list(out.get("tool_trace") or []),
            "stage_error": out.get("stage_error") or "",
            "session": pulse.get("session") or world.get("session") or {},
            "scan_fetched": list(out.get("scan_fetched") or []),
            "run_id": run.get("run_id") or "",
            "pid": run.get("pid"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "stale": False,
        }
        LAST_TURN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("last_turn write failed", exc_info=True)


def stdout_printer(kind: str, text: str) -> None:
    t = ascii_text(text)
    if kind == "stage":
        label = t.strip().upper()
        banner = "GROK" if label in ("", "GROK", "JUDGE", "ACT") else f"GROK {label}"
        print(f"\n--- {banner} ---", flush=True)
        return
    if kind == "stage_end":
        print("", flush=True)
        return
    sys.stdout.write(t)
    sys.stdout.flush()
