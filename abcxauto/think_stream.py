"""Live Grok thinking stream — Judge/Act tokens as they arrive.

Headless prints to stdout (ASCII). ProEngine binds so the UI can show the same buffer.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable

Listener = Callable[[str, str], None]

_lock = threading.Lock()
_listeners: list[Listener] = []
_engine: Any = None

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
        if str(text).lower() == "judge":
            s.think_live = ""
        piece = f"\n--- GROK {ascii_text(text).upper()} ---\n"
    elif kind == "stage_end":
        piece = "\n"
    else:
        piece = ascii_text(text)
    cur = getattr(s, "think_live", "") or ""
    s.think_live = (cur + piece)[-12000:]


def stdout_printer(kind: str, text: str) -> None:
    t = ascii_text(text)
    if kind == "stage":
        print(f"\n--- GROK {t.upper()} ---", flush=True)
        return
    if kind == "stage_end":
        print("", flush=True)
        return
    sys.stdout.write(t)
    sys.stdout.flush()
