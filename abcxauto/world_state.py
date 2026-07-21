"""WorldState — deterministic perceive layer for Judge/Act (no LLM)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from abcxauto.config import get_config, resolve_effective_posture, risk_envelope_snapshot
from abcxauto.opportunity_scan import QUOTE_SOURCES_BLOCK, format_scan_tape
from abcxauto.session_cadence import load_prep, load_review, maybe_auto_prep_from_world
from abcxauto.structure_grade import (
    format_structure_lessons_for_prompt,
    load_structure_vocab,
    recent_structure_lessons,
    structure_cooldown_symbols,
)
from abcxauto.trade_plan import capacity_fact, load_trade_plan, load_trade_plans

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
    """Feature-mix strip from tape metrics + session (not regime truth / not ranked)."""
    session = (pulse.get("session") or {}) if isinstance(pulse, dict) else {}
    status = str(session.get("status") or "").lower()
    phase = _session_phase(status, session.get("current_time_et"))
    rows = list(opportunities or [])[:12]
    above = 0
    below = 0
    pos_ret = 0
    dists: list[float] = []
    for o in rows:
        try:
            d = float(o.get("dist20"))
            dists.append(d)
            if d >= 0:
                above += 1
            else:
                below += 1
        except (TypeError, ValueError):
            if o.get("above_sma20") is True:
                above += 1
            elif o.get("above_sma20") is False:
                below += 1
        try:
            if float(o.get("ret5") or 0) > 0:
                pos_ret += 1
        except (TypeError, ValueError):
            pass
    if above >= 3 and above > below:
        trend = "bullish"
    elif below >= 3 and below > above:
        trend = "bearish"
    else:
        trend = "mixed"
    med = 0.0
    if dists:
        sd = sorted(dists)
        med = sd[len(sd) // 2]
    vol = (
        "elevated"
        if abs(med) > 0.03 or pos_ret >= max(3, len(rows) // 2 + 1)
        else ("normal" if rows else "quiet")
    )
    return {
        "session_status": status or "unknown",
        "session_phase": phase,
        "trend_bias": trend,
        "feature_mix_bias": trend,
        "vol_proxy": vol,
        "top_longs": above,
        "top_shorts": below,
        "median_dist20": round(med, 5),
        "pos_ret5_count": pos_ret,
        "avg_heuristic_rank": None,
        "avg_opp_score": None,
        "source": "tape_feature_mix",
    }


def _portfolio_risk(
    positions: list[dict],
    net_liq: float,
    *,
    total_cash: float | None = None,
) -> dict[str, Any]:
    n = len(positions or [])
    top_pct = 0.0
    top_sym = ""
    by_sym: dict[str, float] = {}
    long_mv = 0.0
    if net_liq and net_liq > 0:
        best = 0.0
        for p in positions or []:
            try:
                mv = abs(float(p.get("marketValue") or p.get("market_value") or 0))
            except (TypeError, ValueError):
                mv = 0.0
            try:
                qty = float(p.get("quantity") or p.get("position") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0:
                long_mv += mv
            sym = str(p.get("symbol") or "").upper()
            if sym:
                by_sym[sym] = by_sym.get(sym, 0.0) + mv
            if mv > best:
                best = mv
                top_sym = str(p.get("symbol") or "")
        top_pct = round(100.0 * best / float(net_liq), 2)
    # Soft exposure Fact (not a hold gate): top names + share of NL.
    exposure = {
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
        "symbols": sorted(
            (
                {
                    "symbol": s,
                    "pct_nl": round(100.0 * mv / float(net_liq), 2)
                    if net_liq and net_liq > 0
                    else 0.0,
                }
                for s, mv in by_sym.items()
            ),
            key=lambda r: -float(r.get("pct_nl") or 0),
        )[:8],
        "note": "Fact — soft concentration; not a narrative hold gate",
    }
    try:
        cash = float(total_cash) if total_cash is not None else 0.0
    except (TypeError, ValueError):
        cash = 0.0
    cash_pct = round(100.0 * cash / float(net_liq), 2) if net_liq and net_liq > 0 else 0.0
    deployed_pct = (
        round(100.0 * long_mv / float(net_liq), 2) if net_liq and net_liq > 0 else 0.0
    )
    from abcxauto.config import ROTATION_THIN_CASH_PCT

    capital_liquidity = {
        "total_cash": round(cash, 2),
        "cash_pct_nl": cash_pct,
        "deployed_long_pct_nl": deployed_pct,
        "cash_thin": bool(cash_pct < float(ROTATION_THIN_CASH_PCT)),
        "note": "Fact — liquidity vs NL; not a hold/sell gate",
    }
    return {
        "n_positions": n,
        "top_symbol": top_sym,
        "top_concentration_pct": top_pct,
        "exposure": exposure,
        "capital_liquidity": capital_liquidity,
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
    trade_plans: list[dict[str, Any]] = field(default_factory=list)
    capacity: dict[str, Any] = field(default_factory=dict)
    idle_streak: int = 0
    idle_top_symbol: str = ""
    prep: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    structure_lessons: list[dict] = field(default_factory=list)
    structure_vocab: dict[str, Any] = field(default_factory=dict)
    structure_cooldown: dict[str, str] = field(default_factory=dict)
    book: dict[str, Any] = field(default_factory=dict)
    pulse: dict[str, Any] = field(default_factory=dict)
    taken_at: str = ""
    ibkr_live_last: float | None = None
    ibkr_live_symbol: str = ""
    scan_fetched: list[str] = field(default_factory=list)
    option_facts: list[dict] = field(default_factory=list)
    stop_qty_fact: dict[str, Any] | None = None

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
            "opportunities": self.opportunities[:12],
            "scan_fetched": list(self.scan_fetched),
            "option_facts": list(self.option_facts[:8]),
            "stop_qty_fact": self.stop_qty_fact,
            "ibkr_live_last": self.ibkr_live_last,
            "ibkr_live_symbol": self.ibkr_live_symbol,
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
            "trade_plans": list(self.trade_plans[:12]),
            "capacity": dict(self.capacity or {}),
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
        reg = dict(self.regime or {})
        # Prompt-facing: heuristic mix, not market-regime truth.
        regime_prompt = {
            "session_status": reg.get("session_status"),
            "session_phase": reg.get("session_phase"),
            "feature_mix_bias": reg.get("feature_mix_bias") or reg.get("trend_bias"),
            "vol_proxy": reg.get("vol_proxy"),
            "top_longs": reg.get("top_longs"),
            "top_shorts": reg.get("top_shorts"),
            "avg_heuristic_rank": reg.get("avg_heuristic_rank") or reg.get("avg_opp_score"),
            "note": "heuristic from feature biases — not regime truth",
        }
        body = {
            "cycle": self.cycle,
            "session": self.session_status,
            "regime": regime_prompt,
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
            "trade_plans": list(self.trade_plans[:8]),
            "capacity": dict(self.capacity or {}),
            "exposure": (self.portfolio_risk or {}).get("exposure"),
            "capital_liquidity": (self.portfolio_risk or {}).get("capital_liquidity"),
            "idle_streak": self.idle_streak,
            "prep": self.prep.get("bias") or self.prep.get("notes"),
            "review_lesson": self.review.get("next_change") or self.review.get("mistake"),
            "structure_cooldown": self.structure_cooldown,
            "suite_failed": (self.structure_vocab or {}).get("failed"),
            "scan_tape": [
                {
                    "symbol": o.get("symbol"),
                    "source": o.get("source") or "mda",
                    "freshness": o.get("freshness") or "delayed",
                    "mda_last": o.get("mda_last") or o.get("last"),
                    "dist20": o.get("dist20"),
                    "ret5": o.get("ret5"),
                }
                for o in self.opportunities[:12]
            ],
            "ibkr_live_last": getattr(self, "ibkr_live_last", None),
            "ibkr_live_symbol": getattr(self, "ibkr_live_symbol", None),
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
                    "expiration": p.get("expiration")
                    or p.get("lastTradeDateOrContractMonth"),
                    "strike": p.get("strike"),
                    "right": p.get("right"),
                }
                for p in self.positions[:12]
            ],
            "stop_qty_fact": self.stop_qty_fact,
            "option_facts": self.option_facts[:8],
        }
        text = "WORLDSTATE (code truth — cite these facts):\n" + json.dumps(body, default=str)
        feats = format_scan_tape(self.opportunities)
        lessons = format_structure_lessons_for_prompt(self.structure_lessons)
        try:
            from abcxauto.option_facts import format_option_facts_for_prompt

            opt_block = format_option_facts_for_prompt(self.option_facts)
        except Exception:
            opt_block = ""
        out = (
            text[:limit]
            + "\n\n"
            + QUOTE_SOURCES_BLOCK
            + "\n\n"
            + feats
            + "\n\n"
            + opt_block
            + "\n\n"
            + lessons
        )
        return out[: limit + 2200]


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
    try:
        total_cash = float(
            acct.get("totalcashvalue")
            or acct.get("TotalCashValue")
            or acct.get("total_cash")
            or acct.get("TotalCash")
            or 0
        )
    except (TypeError, ValueError):
        total_cash = 0.0

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

    plans = load_trade_plans()
    plan = plans[0] if plans else load_trade_plan()
    plan_dict = plan.to_dict() if plan else None
    plans_dicts = [p.to_dict() for p in plans]
    idle = load_idle_streak()
    regime = _regime_from_opps(opportunities, pulse)
    port_risk = _portfolio_risk(positions, net, total_cash=total_cash)
    try:
        max_open = int(getattr(cfg, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        max_open = 0
    cap = capacity_fact(positions, max_open_positions=max_open)
    lessons = recent_structure_lessons(5)
    vocab = load_structure_vocab()
    cool = structure_cooldown_symbols(lessons)
    option_facts = list(snap.get("option_facts") or [])
    stop_fact = None
    try:
        from abcxauto.trade_plan import stop_qty_mismatch_fact

        stop_fact = stop_qty_mismatch_fact(positions, orders, None)
    except Exception:
        stop_fact = None

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
        trade_plans=plans_dicts,
        capacity=cap,
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
        option_facts=option_facts,
        stop_qty_fact=stop_fact,
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
    from abcxauto.opportunity_scan import tape_symbols

    stance = str(judgment.get("stance") or "").lower()
    dismissed = str(judgment.get("dismissed") or "").upper()
    top = ""
    for sym in tape_symbols(world.opportunities):
        if sym and sym in dismissed:
            top = sym
            break
    if not top and world.opportunities:
        # Stable A–Z tape: first symbol is alphabetical, not a rank tip
        sorted_syms = sorted(tape_symbols(world.opportunities))
        top = sorted_syms[0] if sorted_syms else ""
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
