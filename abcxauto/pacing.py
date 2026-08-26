"""Pulse sleep helper. Overnight park is park_clock, not a nap ladder.

Stay-up / retry may wait tens of seconds. A 30-minute window or a
remaining-to-bell clock (9:03 → 9:33) is a park — pacing will not sit it out.
"""

from __future__ import annotations

import asyncio
import math
import time

URGENT_WAKES = frozenset({"unprotected", "halt"})
WAKE_WHITELIST = frozenset({"unprotected", "fill", "halt", "flat_confirmed"})
_WAKE_DEBOUNCE_S = 15.0
# Stay-up / retry class: tens of seconds. Not a half-hour nap.
STAY_UP_RETRY_CAP_S = 45.0


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


def stay_up_retry_s(sleep_s: float) -> float:
    """Clamp a stay-up / retry wait so pacing cannot park.

    Short asks stay short. A remaining-to-bell or any >= 30 minute ask
    is cut to STAY_UP_RETRY_CAP_S. Invalid values become 0.
    """
    try:
        sec = float(sleep_s)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(sec) or sec <= 0.0:
        return 0.0
    cap = max(0.0, float(STAY_UP_RETRY_CAP_S))
    return min(cap, sec)


async def wait_for_pace(
    sleep_s: float,
    wake_event: asyncio.Event,
    *,
    chunk_s: float = 0.5,
) -> str:
    """Sleep up to sleep_s, returning early if wake_event is set.

    Stay-up / retry is capped at STAY_UP_RETRY_CAP_S — a 30-minute
    window or remaining-to-bell clock cannot park this helper.

    Caller should stash the wake reason before set() and clear the event after.
    Returns \"woken\" if interrupted, else \"\" .
    """
    sleep_s = stay_up_retry_s(sleep_s)
    deadline = time.monotonic() + sleep_s
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
