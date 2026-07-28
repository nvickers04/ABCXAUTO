"""Fact-based adaptive pacing for the Pro autonomous cycle.

Process only: tiers and wake codes are market-rhythm gates, not trading taste
and not model-cost thrift. Idle sleep is book state (flat + idle), not API budget.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

URGENT_WAKES = frozenset({"unprotected", "halt"})
WAKE_WHITELIST = frozenset({"unprotected", "fill", "halt", "flat_confirmed"})
_WAKE_DEBOUNCE_S = 15.0


@dataclass(frozen=True)
class PaceFacts:
    needs_protection: bool = False
    flat: bool = True
    has_open_risk: bool = False
    session_status: str = "regular"
    posture: str = ""
    features_present: bool = False
    last_stance: str = ""
    wake_reason: str = ""


@dataclass(frozen=True)
class PaceDecision:
    tier: str
    sleep_s: float
    bypass_grok_min: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "sleep_s": self.sleep_s,
            "bypass_grok_min": self.bypass_grok_min,
            "reason": self.reason,
        }


def _f(cfg: Any, name: str, default: float) -> float:
    try:
        v = float(getattr(cfg, name, default) or default)
    except (TypeError, ValueError):
        v = default
    return max(1.0, v)


def compute_pace(facts: PaceFacts, cfg: Any) -> PaceDecision:
    """Pick sleep tier from book/session facts (hunt floor = cycle_sleep_s)."""
    from abcxauto.config import effective_grok_min_interval_s

    cycle = _f(cfg, "cycle_sleep_s", 120.0)
    grok_min = effective_grok_min_interval_s(cfg)
    protect_s = _f(cfg, "pace_protect_s", 20.0)
    manage_s = _f(cfg, "pace_manage_s", 60.0)
    idle_floor = _f(cfg, "pace_idle_s", 240.0)

    sess = str(facts.session_status or "").lower()
    if sess and sess != "regular" and not facts.needs_protection:
        sleep = max(cycle, 900.0)
        return PaceDecision("closed", sleep, False, "session_closed")

    if facts.needs_protection:
        return PaceDecision("protect", protect_s, True, "unprotected_stk")

    if facts.has_open_risk and not facts.flat:
        return PaceDecision("manage", manage_s, False, "open_risk")

    posture = str(facts.posture or "").lower()
    if facts.flat and facts.last_stance == "idle":
        sleep = max(cycle, grok_min, idle_floor)
        return PaceDecision("idle", sleep, False, "idle_hold")

    hunt_ok = (
        facts.flat
        and not facts.has_open_risk
        and posture in ("balanced", "aggressive")
        and facts.features_present
        and sess in ("", "regular")
    )
    if hunt_ok:
        return PaceDecision("hunt", cycle, False, "hunt_window")

    if facts.has_open_risk:
        return PaceDecision("manage", manage_s, False, "open_risk")

    sleep = max(cycle, grok_min, idle_floor)
    return PaceDecision("idle", sleep, False, "protected_or_flat")


def facts_from_cycle(
    out: dict[str, Any] | None,
    *,
    wake_reason: str = "",
    cfg: Any = None,
) -> PaceFacts:
    """Build PaceFacts from a run_cycle result dict."""
    out = out or {}
    world = out.get("world_state") or {}
    judgment = out.get("judgment") or {}
    pulse = out.get("reality_pulse") or {}
    sess = str(
        (pulse.get("session") or {}).get("status")
        or world.get("session_status")
        or ""
    ).lower()
    unprotected = list(
        out.get("unprotected")
        or world.get("unprotected")
        or (out.get("protection") or {}).get("unprotected_symbols")
        or []
    )
    needs = bool(
        world.get("needs_protection")
        or unprotected
        or out.get("needs_protection")
    )
    flat = world.get("flat")
    if flat is None:
        flat = not bool(out.get("positions") or world.get("positions"))
    plan = out.get("trade_plan") or world.get("trade_plan")
    has_plan = bool(plan)
    try:
        from abcxauto.trade_plan import book_has_risk

        has_book = book_has_risk(out.get("positions") or [])
    except Exception:
        has_book = bool(out.get("positions"))
    posture = str(
        world.get("effective_posture")
        or world.get("risk_posture")
        or out.get("risk_posture")
        or (getattr(cfg, "risk_posture", "") if cfg is not None else "")
        or ""
    ).lower()
    ideas = out.get("opportunities") or world.get("opportunities") or []
    stance = str(
        out.get("stance") or judgment.get("stance") or world.get("last_stance") or ""
    ).lower()
    return PaceFacts(
        needs_protection=needs,
        flat=bool(flat),
        has_open_risk=bool(has_plan or has_book),
        session_status=sess or "regular",
        posture=posture,
        features_present=bool(ideas),
        last_stance=stance,
        wake_reason=str(wake_reason or ""),
    )


def allow_grok_call(
    *,
    tier: str,
    wake_reason: str,
    last_grok_mono: float,
    now_mono: float | None = None,
    grok_min_interval_s: float = 120.0,
) -> tuple[bool, str]:
    """Return (allowed, reason). Protect / urgent wakes bypass the budget."""
    now = time.monotonic() if now_mono is None else now_mono
    wake = str(wake_reason or "").lower()
    if str(tier or "") == "protect" or wake in URGENT_WAKES:
        return True, "urgent"
    if last_grok_mono <= 0:
        return True, "first"
    elapsed = now - last_grok_mono
    min_s = max(0.0, float(grok_min_interval_s or 0))
    if min_s <= 0 or elapsed >= min_s:
        return True, "budget_ok"
    return False, "pace_budget"


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
