"""Stay-up must not grow a sit clock or a background look scheduler."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "abcxauto"

_WATCHED = (
    _ROOT / "pro_engine.py",
    _ROOT / "desk_mode.py",
    _ROOT / "park_clock.py",
    _ROOT / "pacing.py",
    _ROOT / "brain.py",
    _ROOT / "monitor.py",
    _ROOT / "world_state.py",
)

_BANNED = (
    "threading.Timer",
    "AsyncIOScheduler",
    "BackgroundScheduler",
    "schedule.every",
    "loop.call_later",
)


def test_no_sit_clock_or_background_look_scheduler():
    for path in _WATCHED:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        text = path.read_text(encoding="utf-8")
        for needle in _BANNED:
            assert needle not in text, (path.name, needle)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"Timer", "call_later"}:
                raise AssertionError(f"timer in {path.name}")
