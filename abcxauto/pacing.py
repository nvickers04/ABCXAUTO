"""Pulse sleep helper. Live Grok cadence is wake_bus, not a nap ladder."""

from __future__ import annotations

import asyncio
import time

URGENT_WAKES = frozenset({"unprotected", "halt"})
WAKE_WHITELIST = frozenset({"unprotected", "fill", "halt", "flat_confirmed"})
_WAKE_DEBOUNCE_S = 15.0


class WakeGate:
    """Debounce non-urgent wakes; unprotected/halt always interrupt."""

    def __init__(self, debounce_s: float = _WAKE_DEBOUNCE_S) -> None:
        self.debounce_s = max(0.0, float(debounce_s))
        self._last: dict[str, float] = {}

    def try_wake(self, reason: str, *, now_mono: float | None = None) -> bool:
        r = str(reason or "").strip().lower()
        if r not in WAKE_WHITELIST:
            return False
        now = time.monotonic() if now_mono is None else now_mono
        if r in URGENT_WAKES:
            self._last[r] = now
            return True
        prev = self._last.get(r, 0.0)
        if prev and (now - prev) < self.debounce_s:
            return False
        self._last[r] = now
        return True


async def wait_for_pace(
    sleep_s: float,
    wake_event: asyncio.Event,
    *,
    chunk_s: float = 0.5,
) -> str:
    """Sleep up to sleep_s, returning early if wake_event is set.

    Caller should stash the wake reason before set() and clear the event after.
    Returns \"woken\" if interrupted, else \"\" .
    """
    deadline = time.monotonic() + max(0.0, float(sleep_s))
    chunk = max(0.05, float(chunk_s))
    while True:
        if wake_event.is_set():
            return "woken"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=min(chunk, remaining))
            return "woken"
        except asyncio.TimeoutError:
            continue
