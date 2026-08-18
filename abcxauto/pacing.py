"""Fact-based adaptive pacing for the Pro autonomous cycle.

Process only: tiers and wake codes are market-rhythm gates, not trading taste
and not model-cost thrift. Idle sleep is book state (flat + idle), not API budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

URGENT_WAKES = frozenset({"unprotected", "halt"})
WAKE_WHITELIST = frozenset({"unprotected", "fill", "halt", "flat_confirmed"})
_WAKE_DEBOUNCE_S = 15.0


# Paper lab: keep the research loop fast until the book is actually full-ish.
# One open lot used to drop us onto a 60s manage nap and kill volume.
SPINUP_MAX_OPEN = 8
SPINUP_SLEEP_S = 15.0
# Last hour of premarket only. 4–8 AM ET is not research; it is a token bill.
PREMARKET_RESEARCH_S = 3600.0
# Second Grok wake: look for a ticket in the last minutes before the bell.
PREMARKET_OPEN_HUNT_S = 300.0
_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
PREMARKET_WAKE_PATH = _STATE_DIR / "premarket_wake.json"


def _premarket_wake_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_PREMARKET_WAKE_PATH") or "").strip()
    return Path(raw) if raw else PREMARKET_WAKE_PATH


def _et_date() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.now().date().isoformat()


def _read_premarket_stamp() -> dict[str, Any]:
    p = _premarket_wake_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("done") and "research" not in raw:
        raw["research"] = True
    day = str(raw.get("date") or "")
    if day and day != _et_date():
        return {}
    return raw


def _write_premarket_stamp(payload: dict[str, Any]) -> None:
    try:
        p = _premarket_wake_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        row = dict(payload)
        row["date"] = _et_date()
        p.write_text(json.dumps(row), encoding="utf-8")
    except OSError:
        logger.debug("premarket_wake stamp failed", exc_info=True)


def clear_premarket_wake() -> None:
    try:
        p = _premarket_wake_path()
        if p.is_file():
            p.unlink()
    except OSError:
        logger.debug("premarket_wake clear failed", exc_info=True)


def expire_premarket_wake_if_stale() -> None:
    """Drop yesterday's stamp. Keep today's research so a restart does not re-tour."""
    p = _premarket_wake_path()
    if not p.is_file():
        return
    raw = _read_premarket_stamp()
    if not raw:
        clear_premarket_wake()


def mark_premarket_wake_done() -> None:
    """One Grok research wake per premarket session. Unprotected still interrupts."""
    raw = _read_premarket_stamp()
    raw["research"] = True
    _write_premarket_stamp(raw)


def mark_premarket_open_hunt_done() -> None:
    raw = _read_premarket_stamp()
    raw["research"] = True
    raw["open_hunt"] = True
    _write_premarket_stamp(raw)


def premarket_research_spent() -> bool:
    return bool(_read_premarket_stamp().get("research"))


def premarket_open_hunt_spent() -> bool:
    return bool(_read_premarket_stamp().get("open_hunt"))


@dataclass(frozen=True)
class PaceFacts:
    needs_protection: bool = False
    flat: bool = True
    has_open_risk: bool = False
    open_count: int = 0
    session_status: str = "regular"
    posture: str = ""
    features_present: bool = False
    last_stance: str = ""
    wake_reason: str = ""
    countdown_s: float | None = None
    countdown_to: str = ""


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


def premarket_research_open(
    session: str,
    *,
    countdown_s: float | None,
    countdown_to: str = "open",
) -> bool:
    """True in the last hour before the RTH bell."""
    if str(session or "").lower() != "premarket":
        return False
    if str(countdown_to or "open").lower() not in ("", "open"):
        return False
    if countdown_s is None:
        return False
    try:
        left = float(countdown_s)
    except (TypeError, ValueError):
        return False
    return 0 <= left <= PREMARKET_RESEARCH_S


def premarket_open_hunt_open(
    session: str,
    *,
    countdown_s: float | None,
    countdown_to: str = "open",
) -> bool:
    """True in the last minutes before the RTH bell."""
    if str(session or "").lower() != "premarket":
        return False
    if str(countdown_to or "open").lower() not in ("", "open"):
        return False
    if countdown_s is None:
        return False
    try:
        left = float(countdown_s)
    except (TypeError, ValueError):
        return False
    return 0 <= left <= PREMARKET_OPEN_HUNT_S


def _is_paper(cfg: Any) -> bool:
    if cfg is None:
        return True
    if hasattr(cfg, "is_paper"):
        try:
            return bool(cfg.is_paper)
        except Exception:
            pass
    return str(getattr(cfg, "trading_mode", "paper") or "paper").lower() != "live"


def compute_pace(facts: PaceFacts, cfg: Any) -> PaceDecision:
    """Pick sleep tier from book/session facts (hunt floor = cycle_sleep_s)."""
    from abcxauto.config import effective_grok_min_interval_s

    cycle = _f(cfg, "cycle_sleep_s", 120.0)
    grok_min = effective_grok_min_interval_s(cfg)
    protect_s = _f(cfg, "pace_protect_s", 20.0)
    manage_s = _f(cfg, "pace_manage_s", 60.0)
    idle_floor = _f(cfg, "pace_idle_s", 240.0)

    sess = str(facts.session_status or "").lower()

    if facts.needs_protection:
        return PaceDecision("protect", protect_s, True, "unprotected_stk")

    if sess == "closed":
        sleep = max(cycle, 900.0)
        return PaceDecision("closed", sleep, False, "session_closed")

    if sess == "premarket" and premarket_research_open(
        sess, countdown_s=facts.countdown_s, countdown_to=facts.countdown_to
    ):
        hunt = premarket_open_hunt_open(
            sess, countdown_s=facts.countdown_s, countdown_to=facts.countdown_to
        )
        if not premarket_research_spent():
            if _is_paper(cfg):
                spin = _f(cfg, "pace_spinup_s", SPINUP_SLEEP_S)
                return PaceDecision("spinup", spin, True, "premarket_research")
            return PaceDecision("manage", max(cycle, manage_s), False, "premarket_research")
        if hunt and not premarket_open_hunt_spent():
            spin = _f(cfg, "pace_spinup_s", SPINUP_SLEEP_S)
            return PaceDecision("spinup", spin, True, "premarket_open_hunt")
        left = facts.countdown_s
        try:
            left_f = float(left) if left is not None else 300.0
        except (TypeError, ValueError):
            left_f = 300.0
        if hunt:
            return PaceDecision(
                "extended", max(5.0, min(left_f, 15.0)), False, "premarket_open_hunt_done"
            )
        until_hunt = max(15.0, left_f - PREMARKET_OPEN_HUNT_S)
        return PaceDecision(
            "extended", min(until_hunt, 300.0), False, "premarket_research_done"
        )

    if sess in ("premarket", "postmarket"):
        sleep = max(cycle, 300.0)
        return PaceDecision("extended", sleep, False, "extended_wait")

    rth = sess in ("", "regular")
    open_n = max(0, int(facts.open_count or 0))
    # Paper under 8 opens: pull tape, try structures, keep volume.
    # Bypasses grok_min so a long floor cannot stall the lab.
    if (
        _is_paper(cfg)
        and rth
        and not facts.needs_protection
        and open_n <= SPINUP_MAX_OPEN
    ):
        spin = _f(cfg, "pace_spinup_s", SPINUP_SLEEP_S)
        return PaceDecision("spinup", spin, True, "spinup_research")

    if facts.has_open_risk and not facts.flat:
        return PaceDecision("manage", manage_s, False, "open_risk")

    posture = str(facts.posture or "").lower()
    # Floor locks risk_posture=defensive, which would never hit hunt_ok
    # (balanced/aggressive only). Paper lab while flat in RTH must hunt, not idle.
    paper_hunt = (
        _is_paper(cfg)
        and facts.flat
        and not facts.has_open_risk
        and not facts.needs_protection
        and rth
    )
    if paper_hunt:
        return PaceDecision("hunt", cycle, False, "hunt_window")

    if facts.flat and facts.last_stance == "idle":
        sleep = max(cycle, grok_min, idle_floor)
        return PaceDecision("idle", sleep, False, "idle_hold")

    hunt_ok = (
        facts.flat
        and not facts.has_open_risk
        and posture in ("balanced", "aggressive")
        and facts.features_present
        and rth
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
    sess_block = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
    sess = str(
        sess_block.get("status")
        or world.get("session_status")
        or ""
    ).lower()
    try:
        countdown_s = float(sess_block["countdown_s"]) if sess_block.get("countdown_s") is not None else None
    except (TypeError, ValueError):
        countdown_s = None
    countdown_to = str(sess_block.get("countdown_to") or "")
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
    positions = out.get("positions") or world.get("positions") or []
    try:
        from abcxauto.trade_plan import book_has_risk, open_position_count

        has_book = book_has_risk(positions)
        open_n = open_position_count(positions)
    except Exception:
        has_book = bool(positions)
        open_n = sum(
            1
            for p in positions
            if abs(float((p or {}).get("quantity") or (p or {}).get("position") or 0)) > 0
        )
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
    if not stance:
        strat = str(out.get("strat") or "").lower()
        if strat in ("hold", "skipped"):
            stance = "idle"
    return PaceFacts(
        needs_protection=needs,
        flat=bool(flat),
        has_open_risk=bool(has_plan or has_book),
        open_count=int(open_n),
        session_status=sess or "regular",
        posture=posture,
        features_present=bool(ideas),
        last_stance=stance,
        wake_reason=str(wake_reason or ""),
        countdown_s=countdown_s,
        countdown_to=countdown_to,
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
    tier_s = str(tier or "")
    if tier_s == "protect" or wake in URGENT_WAKES:
        return True, "urgent"
    if tier_s == "spinup":
        return True, "spinup"
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
