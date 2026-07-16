"""WorldState — deterministic perceive layer for Judge/Act (no LLM)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from abcxauto.config import get_config, resolve_effective_posture, risk_envelope_snapshot
from abcxauto.opportunity_scan import format_opportunities
from abcxauto.session_cadence import load_prep, load_review, maybe_auto_prep_from_world
from abcxauto.structure_grade import (
    format_structure_lessons_for_prompt,
    load_structure_vocab,
    recent_structure_lessons,
    structure_cooldown_symbols,
)
from abcxauto.trade_plan import load_trade_plan

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_IDLE_STATE_PATH = _REPO_ROOT / "idle_streak_state.json"

STANCES = frozenset({"protect", "manage", "hunt", "idle"})


def _idle_path() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_IDLE_STREAK_PATH", "").strip()
    return Path(raw) if raw else _IDLE_STATE_PATH


def load_idle_streak() -> dict[str, Any]:
    p = _idle_path()
    if not p.is_file():
        return {"count": 0, "top_symbol": "", "last_dismiss": ""}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {"count": 0, "top_symbol": "", "last_dismiss": ""}
    except Exception:
        return {"count": 0, "top_symbol": "", "last_dismiss": ""}


def save_idle_streak(state: dict[str, Any]) -> None:
    p = _idle_path()
    try:
        p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("save_idle_streak failed")


def reset_idle_streak() -> None:
    save_idle_streak({"count": 0, "top_symbol": "", "last_dismiss": ""})


def _session_phase(session_status: str, current_et: str | None = None) -> str:
    s = (session_status or "").lower()
    if s != "regular":
        return s or "closed"
    # Heuristic from HH:MM if present
    try:
        hhmm = (current_et or "")[:5]
        if len(hhmm) >= 4 and ":" in hhmm:
            h, m = hhmm.split(":")[:2]
            minutes = int(h) * 60 + int(m)
            if minutes < 10 * 60 + 30:
                return "open"
            if minutes >= 15 * 60:
                return "close"
            return "mid"
    except Exception:
        pass
    return "mid"


def _regime_from_opps(opportunities: list[dict], pulse: dict) -> dict[str, Any]:
    """Cheap regime strip from ranked ideas + session."""
    session = (pulse.get("session") or {}) if isinstance(pulse, dict) else {}
    status = str(session.get("status") or "").lower()
    phase = _session_phase(status, session.get("current_time_et"))
    longs = sum(1 for o in opportunities[:5] if str(o.get("bias") or "").upper() == "LONG")
    shorts = sum(1 for o in opportunities[:5] if str(o.get("bias") or "").upper() == "SHORT")
    if longs >= 3 and longs > shorts:
        trend = "bullish"
    elif shorts >= 3 and shorts > longs:
        trend = "bearish"
    else:
        trend = "mixed"
    avg_score = 0.0
    if opportunities:
        avg_score = sum(float(o.get("score") or 0) for o in opportunities[:5]) / min(5, len(opportunities))
    vol = "elevated" if avg_score > 0.7 else ("normal" if avg_score > 0.4 else "quiet")
    return {
        "session_status": status or "unknown",
        "session_phase": phase,
        "trend_bias": trend,
        "vol_proxy": vol,
        "top_longs": longs,
        "top_shorts": shorts,
        "avg_opp_score": round(avg_score, 3),
    }


def _portfolio_risk(positions: list[dict], net_liq: float) -> dict[str, Any]:
    n = len(positions or [])
    top_pct = 0.0
    top_sym = ""
    if net_liq and net_liq > 0:
        best = 0.0
        for p in positions or []:
            try:
                mv = abs(float(p.get("marketValue") or p.get("market_value") or 0))
            except (TypeError, ValueError):
                mv = 0.0
            if mv > best:
                best = mv
                top_sym = str(p.get("symbol") or "")
        top_pct = round(100.0 * best / float(net_liq), 2)
    return {
        "n_positions": n,
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
    }


def hunt_cooldown_remaining(recent_decisions: list[dict], symbol: str) -> int:
    """Cycles of hunt cooldown left for symbol (0 = clear)."""
    if not symbol:
        return 0
    sym = symbol.upper()
    for i, d in enumerate(recent_decisions or []):
        strat = str(d.get("strategy") or d.get("action") or "").lower()
        if strat in ("bracket", "market_bracket"):
            rat = str(d.get("rationale") or "")
            blob = rat.upper() + json.dumps(d.get("outcome") or {}, default=str).upper()
            if sym in blob:
                return max(0, 2 - i)
    return 0


@dataclass
class WorldState:
    cycle: int
    session_status: str
    flat: bool
    needs_protection: bool
    unprotected: list[str]
    net_liquidation: float
    daily_pnl: float
    positions: list[dict]
    open_orders: list[dict]
    opportunities: list[dict]
    news_items: list[dict]
    risk_posture: str
    effective_posture: str
    gates: dict[str, Any]
    envelope: dict[str, Any]
    regime: dict[str, Any]
    portfolio_risk: dict[str, Any]
    working_thesis: str
    recent_decisions: list[dict]
    trade_plan: dict[str, Any] | None
    idle_streak: int
    idle_top_symbol: str
    prep: dict[str, Any]
    review: dict[str, Any]
    structure_lessons: list[dict] = field(default_factory=list)
    structure_vocab: dict[str, Any] = field(default_factory=dict)
    structure_cooldown: dict[str, str] = field(default_factory=dict)
    book: dict[str, Any] = field(default_factory=dict)
    pulse: dict[str, Any] = field(default_factory=dict)
    taken_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "session_status": self.session_status,
            "flat": self.flat,
            "needs_protection": self.needs_protection,
            "unprotected": list(self.unprotected),
            "net_liquidation": self.net_liquidation,
            "daily_pnl": self.daily_pnl,
            "n_positions": len(self.positions),
            "n_orders": len(self.open_orders),
            "opportunities": self.opportunities[:5],
            "news_items": [
                {"symbol": n.get("symbol"), "headline": str(n.get("headline") or "")[:160]}
                for n in self.news_items[:12]
                if n.get("headline")
            ],
            "risk_posture": self.risk_posture,
            "effective_posture": self.effective_posture,
            "gates": self.gates,
            "envelope": self.envelope,
            "regime": self.regime,
            "portfolio_risk": self.portfolio_risk,
            "working_thesis": self.working_thesis[:400],
            "recent_decisions": self.recent_decisions[:3],
            "trade_plan": self.trade_plan,
            "idle_streak": self.idle_streak,
            "idle_top_symbol": self.idle_top_symbol,
            "structure_lessons": self.structure_lessons[:5],
            "structure_vocab": {
                k: self.structure_vocab.get(k)
                for k in ("pass_rate", "failed", "passed", "ts", "source")
                if k in self.structure_vocab
            },
            "structure_cooldown": dict(self.structure_cooldown),
            "prep": {
                k: self.prep.get(k)
                for k in ("bias", "levels", "do_not_trade_if", "watchlist", "ts")
                if k in self.prep
            },
            "review": {
                k: self.review.get(k)
                for k in ("what_worked", "mistake", "next_change", "ts")
                if k in self.review
            },
            "taken_at": self.taken_at,
        }

    def prompt_block(self, *, limit: int = 4500) -> str:
        """Compact WORLD block for Judge/Act prompts."""
        body = {
            "cycle": self.cycle,
            "session": self.session_status,
            "regime": self.regime,
            "flat": self.flat,
            "needs_protection": self.needs_protection,
            "unprotected": self.unprotected,
            "net_liquidation": self.net_liquidation,
            "daily_pnl": self.daily_pnl,
            "portfolio_risk": self.portfolio_risk,
            "posture": self.effective_posture or self.risk_posture,
            "gates": self.gates,
            "envelope": self.envelope,
            "working_thesis": self.working_thesis[:300],
            "trade_plan": self.trade_plan,
            "idle_streak": self.idle_streak,
            "prep": self.prep.get("bias") or self.prep.get("notes"),
            "review_lesson": self.review.get("next_change") or self.review.get("mistake"),
            "structure_cooldown": self.structure_cooldown,
            "suite_failed": (self.structure_vocab or {}).get("failed"),
            "opportunities": self.opportunities[:5],
            "news": [
                f"[{n.get('symbol')}] {n.get('headline')}"
                for n in self.news_items[:8]
                if n.get("headline")
            ],
            "positions": [
                {
                    "conId": p.get("conId") or p.get("con_id"),
                    "symbol": p.get("symbol"),
                    "sec": p.get("secType") or p.get("sec_type"),
                    "qty": p.get("quantity") or p.get("position"),
                }
                for p in self.positions[:8]
            ],
        }
        text = "WORLDSTATE (code truth — cite these facts):\n" + json.dumps(body, default=str)
        opp = format_opportunities(self.opportunities)
        lessons = format_structure_lessons_for_prompt(self.structure_lessons)
        out = text[:limit] + "\n\n" + opp + "\n\n" + lessons
        return out[: limit + 1200]


def build_world_state(
    *,
    cycle: int,
    snap: dict[str, Any],
    opportunities: list[dict],
    news_items: list[dict],
) -> WorldState:
    """Assemble WorldState from snap + scan + journal + plan."""
    from abcxauto.book import build_book_from_snap
    from abcxauto.memory import get_journal

    positions = list(snap.get("positions") or [])
    orders = list(snap.get("open_orders") or [])
    acct = snap.get("account") or {}
    pulse = snap.get("reality_pulse") or {}
    protection = snap.get("protection") or {}
    unprotected = list(protection.get("unprotected_symbols") or [])
    session = str((pulse.get("session") or {}).get("status") or "").lower()
    try:
        net = float(acct.get("netliquidation") or acct.get("NetLiquidation") or 0)
    except (TypeError, ValueError):
        net = 0.0
    try:
        pnl = float(acct.get("dailypnl") or acct.get("DailyPnL") or acct.get("unrealizedpnl") or 0)
    except (TypeError, ValueError):
        pnl = 0.0

    cfg = get_config()
    posture = str(getattr(cfg, "risk_posture", "") or "")
    eff = resolve_effective_posture(posture, getattr(cfg, "trading_mode", "paper") or "paper")
    env_snap = risk_envelope_snapshot()
    gates = env_snap.get("current") or {}
    envelope = env_snap.get("envelope") or {}

    thesis = ""
    recent: list[dict] = []
    try:
        j = get_journal()
        thesis = j.get_working_thesis() or ""
        recent = j.recent_decisions(limit=5)
    except Exception:
        pass

    plan = load_trade_plan()
    plan_dict = plan.to_dict() if plan else None
    idle = load_idle_streak()
    regime = _regime_from_opps(opportunities, pulse)
    port_risk = _portfolio_risk(positions, net)
    lessons = recent_structure_lessons(5)
    vocab = load_structure_vocab()
    cool = structure_cooldown_symbols(lessons)

    book = snap.get("portfolio_state") or build_book_from_snap(snap)
    ws = WorldState(
        cycle=cycle,
        session_status=session or "unknown",
        flat=not positions,
        needs_protection=bool(unprotected),
        unprotected=unprotected,
        net_liquidation=net,
        daily_pnl=pnl,
        positions=positions,
        open_orders=orders,
        opportunities=list(opportunities or []),
        news_items=list(news_items or []),
        risk_posture=posture,
        effective_posture=eff,
        gates=gates,
        envelope=envelope,
        regime=regime,
        portfolio_risk=port_risk,
        working_thesis=thesis,
        recent_decisions=[
            {
                "strategy": d.get("strategy"),
                "action": d.get("action"),
                "rationale": (d.get("rationale") or "")[:100],
            }
            for d in recent[:3]
        ],
        trade_plan=plan_dict,
        idle_streak=int(idle.get("count") or 0),
        idle_top_symbol=str(idle.get("top_symbol") or ""),
        prep=load_prep(),
        review=load_review(),
        structure_lessons=lessons,
        structure_vocab=vocab,
        structure_cooldown=cool,
        book=book if isinstance(book, dict) else {},
        pulse=pulse if isinstance(pulse, dict) else {},
        taken_at=str(snap.get("taken_at") or ""),
    )
    # Auto prep once/day
    try:
        ws.prep = maybe_auto_prep_from_world(ws.to_dict())
    except Exception:
        pass
    return ws


def idle_streak_threshold(posture: str) -> int:
    p = (posture or "").lower()
    if p == "aggressive":
        return 2
    if p == "defensive":
        return 5
    return 3


def update_idle_streak_after_judgment(
    judgment: dict[str, Any],
    world: WorldState,
) -> dict[str, Any]:
    """Update persisted idle streak from judgment stance."""
    stance = str(judgment.get("stance") or "").lower()
    top = ""
    if world.opportunities:
        top = str(world.opportunities[0].get("symbol") or "").upper()
    state = load_idle_streak()
    if stance == "idle" and world.flat:
        if top and top == str(state.get("top_symbol") or "").upper():
            state["count"] = int(state.get("count") or 0) + 1
        else:
            state["count"] = 1
            state["top_symbol"] = top
        state["last_dismiss"] = str(judgment.get("dismissed") or "")[:300]
    else:
        state = {"count": 0, "top_symbol": top, "last_dismiss": ""}
    save_idle_streak(state)
    return state
