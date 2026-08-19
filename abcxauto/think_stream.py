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
THINK_PREV_PATH = _STATE_DIR / "think_prev.txt"
LAST_TURN_PATH = _STATE_DIR / "last_turn.json"
DESK_BRIEF_PATH = _STATE_DIR / "desk_brief.json"
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


def _write_think_tail(buf: str, *, force: bool = False) -> None:
    global _last_tail_write
    now = time.monotonic()
    if not force and now - _last_tail_write < _TAIL_MIN_INTERVAL:
        return
    _last_tail_write = now
    try:
        THINK_TAIL_PATH.parent.mkdir(parents=True, exist_ok=True)
        THINK_TAIL_PATH.write_text(buf[-8000:], encoding="utf-8")
    except OSError:
        logger.debug("think_tail write failed", exc_info=True)


def flush_think_tail() -> None:
    """Write the live buffer now so a kill does not lose the last chunks."""
    eng = _engine
    buf = ""
    if eng is not None:
        try:
            buf = str(getattr(getattr(eng, "state", None), "think_live", "") or "")
        except Exception:
            buf = ""
    if not buf and THINK_TAIL_PATH.is_file():
        return
    _write_think_tail(buf or (THINK_TAIL_PATH.read_text(encoding="utf-8") if THINK_TAIL_PATH.is_file() else ""), force=True)


def _archive_think_tail() -> None:
    try:
        if not THINK_TAIL_PATH.is_file():
            return
        text = THINK_TAIL_PATH.read_text(encoding="utf-8")
        if not text.strip():
            return
        THINK_PREV_PATH.parent.mkdir(parents=True, exist_ok=True)
        THINK_PREV_PATH.write_text(text, encoding="utf-8")
    except OSError:
        logger.debug("think_prev archive failed", exc_info=True)


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


def mark_review_stale(*, archive_tail: bool = False) -> None:
    """Mark last_turn dead. Keep the think tail so a mid-turn kill is readable."""
    flush_think_tail()
    prev = _read_json(LAST_TURN_PATH)
    run = current_run()
    payload = {
        "stale": True,
        "previous_run_id": prev.get("run_id") or run.get("run_id") or "",
        "previous_cycle": prev.get("cycle"),
        "previous_strat": prev.get("strat") or "",
        "open_lots": list(prev.get("open_lots") or []),
        "net_liquidation": prev.get("net_liquidation"),
        "flat": prev.get("flat"),
        "session": prev.get("session") or {},
        "ibkr_connected": prev.get("ibkr_connected"),
        "mix": prev.get("mix") if isinstance(prev.get("mix"), dict) else {},
        "rationale": (prev.get("rationale") or "")[:400],
        "tool_trace": list(prev.get("tool_trace") or []),
        "sends": prev.get("sends") or 0,
    }
    try:
        LAST_TURN_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_TURN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("last_turn stale mark failed", exc_info=True)
    if archive_tail:
        _archive_think_tail()
        try:
            if THINK_TAIL_PATH.is_file():
                THINK_TAIL_PATH.write_text("", encoding="utf-8")
        except OSError:
            logger.debug("think_tail clear failed", exc_info=True)


def begin_run() -> dict[str, Any]:
    """Stamp a new process identity. Call after killing leftovers."""
    global _run
    mark_review_stale(archive_tail=True)
    try:
        from abcxauto.wake_bus import ensure_next_look

        ensure_next_look(previous_set_at="")
    except Exception:
        logger.debug("grok_wake seed on begin_run failed", exc_info=True)
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


def _desk_brief_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_DESK_BRIEF_PATH") or "").strip()
    return Path(raw) if raw else DESK_BRIEF_PATH


def load_desk_brief() -> dict[str, Any]:
    """Last completed look. in_progress last_turn is not memory."""
    p = _desk_brief_path()
    if not p.is_file():
        data = _read_json(LAST_TURN_PATH)
        if data.get("stale") or str(data.get("strat") or "") == "in_progress":
            return {}
        return data
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def write_desk_brief(payload: dict[str, Any]) -> None:
    if str(payload.get("strat") or "") == "in_progress":
        return
    row = {
        "cycle": payload.get("cycle"),
        "strat": payload.get("strat"),
        "sends": payload.get("sends") or 0,
        "open_lots": list(payload.get("open_lots") or [])[:16],
        "net_liquidation": payload.get("net_liquidation"),
        "mix": payload.get("mix") if isinstance(payload.get("mix"), dict) else {},
        "rationale": (payload.get("rationale") or "")[:240],
        "ts": payload.get("ts"),
    }
    p = _desk_brief_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(row, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("desk_brief write failed", exc_info=True)


def _mix_of(out: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    for src in (out.get("mix"), world.get("mix")):
        if isinstance(src, dict) and src:
            return src
    try:
        from abcxauto.world_state import structure_mix

        return structure_mix(out.get("positions") or world.get("positions"))
    except Exception:
        return {}


def write_last_turn(out: dict[str, Any]) -> None:
    """Clerk snapshot of the last cycle for the Cursor review loop."""
    try:
        LAST_TURN_PATH.parent.mkdir(parents=True, exist_ok=True)
        pulse = out.get("reality_pulse") or {}
        world = out.get("world_state") or {}
        run = current_run()
        gates = world.get("gates") if isinstance(world.get("gates"), dict) else {}
        fresh = pulse.get("data_freshness") if isinstance(pulse.get("data_freshness"), dict) else {}
        ibkr = pulse.get("ibkr_connected")
        if ibkr is None:
            ibkr = fresh.get("ibkr_connected")
        from abcxauto.world_state import lot_labels

        open_lots = list(out.get("open_lots") or world.get("open_lots") or [])
        if not open_lots:
            open_lots = lot_labels(out.get("positions") or world.get("positions"))
        unreliable = bool(
            out.get("book_unreliable") or gates.get("book_unreliable")
        )
        ibkr_down = ibkr is False or "ibkr_down" in str(out.get("validation") or "")
        prior = _read_json(LAST_TURN_PATH) if (unreliable or ibkr_down) else {}
        if (unreliable or ibkr_down) and not open_lots:
            open_lots = list(prior.get("open_lots") or [])
        nl = world.get("net_liquidation") or out.get("equity")
        try:
            nl_f = float(nl) if nl is not None else 0.0
        except (TypeError, ValueError):
            nl_f = 0.0
        if (unreliable or ibkr_down) and nl_f <= 0 and prior.get("net_liquidation"):
            nl = prior.get("net_liquidation")
            world = dict(world)
            world["net_liquidation"] = nl
        skip = str(out.get("validation") or out.get("skip_reason") or "")
        if skip.startswith("skipped_grok:"):
            skip = skip.split(":", 1)[-1].strip()
        elif out.get("strat") in ("skipped", "blocked"):
            skip = skip or str(out.get("strat") or "")
        else:
            skip = ""
        payload = {
            "cycle": out.get("cycle"),
            "strat": out.get("strat"),
            "rationale": (out.get("rationale") or "")[:400],
            "validation": (out.get("validation") or "")[:400],
            "tool_trace": list(out.get("tool_trace") or []),
            "stage_error": out.get("stage_error") or "",
            "session": pulse.get("session") or world.get("session") or {},
            "scan_fetched": list(out.get("scan_fetched") or []),
            "ibkr_connected": ibkr,
            "open_lots": open_lots,
            "book_unreliable": bool(
                out.get("book_unreliable") or gates.get("book_unreliable")
            ),
            "skip_reason": skip[:120],
            "flat": world.get("flat"),
            "net_liquidation": world.get("net_liquidation") or out.get("equity") or nl,
            "mix": _mix_of(out, world),
            "sends": (
                int(out["sends"])
                if isinstance(out.get("sends"), int)
                else len([t for t in (out.get("tool_trace") or []) if str(t) == "send"])
            ),
            "run_id": run.get("run_id") or "",
            "pid": run.get("pid"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "ibkr_live_last": world.get("ibkr_live_last") or out.get("ibkr_live_last"),
            "ibkr_live_quotes": dict(
                world.get("ibkr_live_quotes") or out.get("ibkr_live_quotes") or {}
            ),
            "candle_source": (
                world.get("candle_source") or out.get("candle_source") or "none"
            ),
        }
        if str(payload.get("strat") or "") == "in_progress":
            brief = load_desk_brief()
            if brief.get("strat"):
                payload["previous_strat"] = brief.get("strat")
                payload["previous_sends"] = brief.get("sends") or 0
        LAST_TURN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        write_desk_brief(payload)
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
