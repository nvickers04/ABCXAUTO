"""Grokfolio — Autopilot-style portfolio owner on an hourly/daily clock.

Public Autopilot "Grok Portfolio" (Lopez-Lira / AI Finance Labs): a scoring
system feeds Grok, Grok constructs ~15 names, the book rebalances on a
calendar (monthly there), no human override.

This shell runs the same stages — macro → score legal universe → construct
weights → verify vs % of NetLiq → execute diffs — on **hourly and/or daily**
RTH slots. Protect-first still interrupts. Hunt scalping is not the product.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STATE = _REPO_ROOT / "grokfolio_state.json"
# Autopilot Grok book is 15 names. Hourly slots are RTH hours after the open.
TARGET_HOLDINGS = 15
DAILY_HOUR_ET = 10
HOURLY_HOURS_ET = (10, 11, 12, 13, 14, 15)
DRIFT_PCT_OF_NL = 1.0  # rebalance a name if |target-current| exceeds 1% of NL


def _state_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_GROKFOLIO_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_STATE


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.debug("grokfolio state read failed", exc_info=True)
        return {}


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    try:
        path.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("grokfolio state write failed")


def now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=_ET)
    return now.astimezone(_ET)


def cadence(cfg: Any = None) -> str:
    raw = str(getattr(cfg, "grokfolio_cadence", None) or "both").strip().lower()
    return raw if raw in ("hourly", "daily", "both") else "both"


def due_kind(
    *,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
    cfg: Any = None,
    session_status: str = "regular",
) -> str | None:
    """Return 'daily', 'hourly', or None if this cycle should wait."""
    if str(session_status or "").lower() != "regular":
        return None
    et = now_et(now)
    if et.weekday() >= 5:
        return None
    cad = cadence(cfg)
    st = state if state is not None else load_state()
    last_d = str(st.get("last_daily") or "")
    last_h = str(st.get("last_hourly") or "")
    today = et.date().isoformat()
    hour_key = f"{today}T{et.hour:02d}"

    daily_ok = cad in ("daily", "both") and et.hour == DAILY_HOUR_ET and last_d != today
    hourly_ok = (
        cad in ("hourly", "both")
        and et.hour in HOURLY_HOURS_ET
        and last_h != hour_key
    )
    if cad == "both" and daily_ok:
        return "daily"
    if cad == "daily":
        return "daily" if daily_ok else None
    if hourly_ok:
        return "hourly"
    return None


def mark_ran(kind: str, *, now: datetime | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    et = now_et(now)
    st = dict(state or load_state())
    today = et.date().isoformat()
    if kind == "daily":
        st["last_daily"] = today
        st["last_hourly"] = f"{today}T{et.hour:02d}"
    else:
        st["last_hourly"] = f"{today}T{et.hour:02d}"
    st["last_kind"] = kind
    st["last_ran_at"] = et.isoformat()
    save_state(st)
    return st


def sleep_until_next_s(
    *,
    now: datetime | None = None,
    cfg: Any = None,
    session_status: str = "regular",
) -> float:
    """Seconds until the next grokfolio slot (protect path does not use this)."""
    et = now_et(now)
    cad = cadence(cfg)
    hours = list(HOURLY_HOURS_ET)
    if cad == "daily":
        hours = [DAILY_HOUR_ET]
    sess = str(session_status or "").lower()
    for add in range(0, 8):
        day = (et + timedelta(days=add)).date()
        if day.weekday() >= 5:
            continue
        for h in hours:
            slot = datetime(day.year, day.month, day.day, h, 0, tzinfo=_ET)
            if slot <= et:
                continue
            if add == 0 and sess != "regular" and slot.date() == et.date() and et.hour < 9:
                continue
            return max(60.0, (slot - et).total_seconds())
    return 3600.0


def clamp_holdings(
    raw: list[Any],
    *,
    legal: set[str],
    max_n: int,
    max_position_pct: float,
    max_risk_per_trade_pct: float,
) -> list[dict[str, Any]]:
    """Legal symbols, unique, weight-capped, normalized toward 100%."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym or sym in seen or (legal and sym not in legal):
            continue
        try:
            w = float(row.get("weight_pct") or 0)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        try:
            stop_pct = float(row.get("stop_pct") or 8.0)
        except (TypeError, ValueError):
            stop_pct = 8.0
        stop_pct = min(20.0, max(2.0, stop_pct))
        cap = float(max_position_pct or 20.0)
        if stop_pct > 0 and max_risk_per_trade_pct > 0:
            cap = min(cap, (max_risk_per_trade_pct / stop_pct) * 100.0)
        w = min(w, cap)
        seen.add(sym)
        out.append(
            {
                "symbol": sym,
                "weight_pct": w,
                "thesis": str(row.get("thesis") or "")[:400],
                "stop_pct": stop_pct,
            }
        )
        if len(out) >= max(1, int(max_n or TARGET_HOLDINGS)):
            break
    total = sum(h["weight_pct"] for h in out)
    if total > 100.0 and total > 0:
        for h in out:
            h["weight_pct"] = h["weight_pct"] * 100.0 / total
    return out


def current_stk_weights(positions: list[Any], net_liq: float) -> dict[str, float]:
    """Symbol → current weight % of NL for long stock."""
    out: dict[str, float] = {}
    nl = float(net_liq or 0) or 1.0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
        if sec not in ("STK", "STOCK", ""):
            continue
        try:
            qty = float(p.get("quantity") or p.get("position") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        px = p.get("market_price") or p.get("marketPrice") or p.get("avg_cost") or p.get("avgCost")
        try:
            px_f = float(px or 0)
        except (TypeError, ValueError):
            px_f = 0.0
        notional = qty * px_f if px_f > 0 else 0.0
        out[sym] = out.get(sym, 0.0) + (notional / nl) * 100.0
    return out


def diff_book(
    target: list[dict[str, Any]],
    *,
    current_w: dict[str, float],
    net_liq: float,
    drift_pct_of_nl: float = DRIFT_PCT_OF_NL,
) -> list[dict[str, Any]]:
    """Closes, entries, and size-adjusts to move toward target weights."""
    wanted = {h["symbol"]: h for h in target}
    actions: list[dict[str, Any]] = []
    for sym, w in (current_w or {}).items():
        if sym not in wanted:
            actions.append({"op": "close", "symbol": sym, "why": "not in grokfolio"})
    for h in target:
        sym = h["symbol"]
        tw = float(h["weight_pct"])
        cw = float((current_w or {}).get(sym) or 0.0)
        if abs(tw - cw) < float(drift_pct_of_nl):
            continue
        if cw <= 0:
            actions.append({"op": "buy", "symbol": sym, "weight_pct": tw, "stop_pct": h["stop_pct"], "thesis": h.get("thesis") or ""})
        else:
            actions.append(
                {
                    "op": "resize",
                    "symbol": sym,
                    "weight_pct": tw,
                    "current_pct": cw,
                    "stop_pct": h["stop_pct"],
                    "thesis": h.get("thesis") or "",
                }
            )
    return actions


def candidate_rows(world: Any) -> list[dict[str, Any]]:
    """Unranked legal-set facts for Grok to score (shell never ranks)."""
    rows: list[dict[str, Any]] = []
    legal = []
    tape = []
    if hasattr(world, "legal_symbols"):
        legal = list(getattr(world, "legal_symbols") or [])
    elif isinstance(world, dict):
        legal = list(world.get("legal_symbols") or [])
        tape = list(world.get("opportunities") or world.get("scan_tape") or [])
    if hasattr(world, "opportunities"):
        tape = list(getattr(world, "opportunities") or [])
    by_sym: dict[str, dict[str, Any]] = {}
    for it in tape:
        if not isinstance(it, dict):
            continue
        sym = str(it.get("symbol") or "").upper()
        if not sym:
            continue
        by_sym[sym] = {
            "symbol": sym,
            "last": it.get("last") or it.get("price"),
            "ret5": it.get("ret5") or it.get("ret_5"),
            "sma20": it.get("sma20"),
            "note": str(it.get("note") or it.get("headline") or "")[:160],
        }
    for sym in legal:
        s = str(sym).upper()
        by_sym.setdefault(s, {"symbol": s})
    rows = list(by_sym.values())
    return rows[:80]


def _legal_set(world: Any) -> set[str]:
    if hasattr(world, "legal_symbols"):
        return {str(s).upper() for s in (getattr(world, "legal_symbols") or []) if s}
    if isinstance(world, dict):
        return {str(s).upper() for s in (world.get("legal_symbols") or []) if s}
    return set()


def _prompt(kind: str, world: Any, state: dict[str, Any], cfg: Any) -> str:
    n = int(getattr(cfg, "grokfolio_holdings", TARGET_HOLDINGS) or TARGET_HOLDINGS)
    try:
        mop = int(getattr(cfg, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        mop = 0
    if mop > 0:
        n = min(n, mop)
    cands = candidate_rows(world)
    book = []
    if hasattr(world, "positions"):
        book = list(getattr(world, "positions") or [])
    elif isinstance(world, dict):
        book = list(world.get("positions") or [])
    prev = state.get("holdings") or []
    macro_prev = state.get("macro") or ""
    stage = "DAILY full construct" if kind == "daily" else "HOURLY trim/swap (at most 3 name changes)"
    return (
        f"GROKFOLIO {stage}. You own the whole paper book as % of NetLiq.\n"
        "Pipeline (Autopilot Grok Portfolio, faster clock): "
        "1) macro (Fed, inflation, geopolitics, sector rotation) "
        "2) score the legal candidates 3) construct up to "
        f"{n} long holdings with weight_pct 4) do not exceed max_position_pct "
        "or max_risk_per_trade_pct.\n"
        "JSON only:\n"
        '{"macro":"...", "holdings":[{"symbol":"MSFT","weight_pct":8,'
        '"stop_pct":8,"thesis":"..."}], "dismissed":"..."}\n'
        f"Prior macro: {str(macro_prev)[:800]}\n"
        f"Prior holdings: {json.dumps(prev)[:1200]}\n"
        f"Open STK: {json.dumps([p.get('symbol') if isinstance(p, dict) else p for p in book])[:800]}\n"
        f"Candidates (unranked legal facts): {json.dumps(cands)[:4000]}\n"
        "Long-only cash. No shorts. Weights are % of NetLiq and should sum near 100 "
        "if you want to be fully invested, or less if you want cash. "
        "Hourly: keep the book unless thesis broke; daily: rebuild allowed."
    )


async def _construct(g: Any, kind: str, world: Any, state: dict[str, Any], cfg: Any) -> dict[str, Any]:
    from abcxauto.agent_loop import grok, parse_json

    raw = await grok(g, _prompt(kind, world, state, cfg), stage="judge")
    parsed = parse_json(raw) or {}
    legal = _legal_set(world)
    max_n = int(getattr(cfg, "grokfolio_holdings", TARGET_HOLDINGS) or TARGET_HOLDINGS)
    try:
        mop = int(getattr(cfg, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        mop = 0
    if mop > 0:
        max_n = min(max_n, mop)
    holdings = clamp_holdings(
        parsed.get("holdings") or [],
        legal=legal,
        max_n=max_n,
        max_position_pct=float(getattr(cfg, "max_position_pct", 20) or 20),
        max_risk_per_trade_pct=float(getattr(cfg, "max_risk_per_trade_pct", 1) or 1),
    )
    return {
        "macro": str(parsed.get("macro") or "")[:2000],
        "holdings": holdings,
        "dismissed": str(parsed.get("dismissed") or "")[:800],
        "raw_ok": bool(holdings or parsed.get("macro")),
    }


def _qty_for_weight(weight_pct: float, net_liq: float, price: float) -> int:
    if price <= 0 or net_liq <= 0 or weight_pct <= 0:
        return 0
    return max(0, int((weight_pct / 100.0) * net_liq / price))


def _bracket_act(sym: str, qty: int, last: float, stop_pct: float, thesis: str) -> dict[str, Any]:
    stop = round(last * (1.0 - stop_pct / 100.0), 2)
    target = round(last * (1.0 + 2.0 * stop_pct / 100.0), 2)
    return {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "rationale": thesis or f"grokfolio buy {sym}",
        "params": {
            "symbol": sym,
            "quantity": qty,
            "direction": "LONG",
            "stop_price": stop,
            "target_price": target,
            "price_hint": last,
        },
    }


def _close_act(sym: str, qty: int) -> dict[str, Any]:
    return {
        "action": "market_order",
        "strategy": "market_order",
        "rationale": f"grokfolio exit {sym}",
        "params": {
            "symbol": sym,
            "quantity": max(1, int(qty)),
            "direction": "SHORT",
            "closing_position": True,
        },
    }


def _stk_qty(positions: list[Any], sym: str) -> int:
    total = 0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("symbol") or "").upper() != sym:
            continue
        sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
        if sec not in ("STK", "STOCK", ""):
            continue
        try:
            total += int(float(p.get("quantity") or p.get("position") or 0))
        except (TypeError, ValueError):
            pass
    return total


async def handle_cycle(
    *,
    n: int,
    connector: Any,
    g: Any,
    hist: list,
    prev: float,
    snap: dict[str, Any],
    world: Any,
    world_dict: dict[str, Any],
    pnl: float,
    eq: float,
    inventory: str,
    needs_prot: bool,
) -> dict[str, Any] | None:
    """If grokfolio owns this cycle, return a run_cycle out dict. Else None."""
    from abcxauto.agent_loop import _HIST_KEYS, _journal_stages, _result_dict, extract_kahneman
    from abcxauto.config import get_config
    from abcxauto.send import send_action

    cfg = get_config()
    if not bool(getattr(cfg, "grokfolio_enabled", True)):
        return None
    if needs_prot:
        return None

    session = ""
    pulse = snap.get("reality_pulse") or {}
    session = str((pulse.get("session") or {}).get("status") or "").lower()
    st = load_state()
    kind = due_kind(state=st, cfg=cfg, session_status=session or "regular")
    if kind is None:
        wait = sleep_until_next_s(cfg=cfg, session_status=session or "regular")
        act = {
            "action": "hold",
            "strategy": "hold",
            "rationale": f"grokfolio_wait: next slot in {wait:.0f}s",
        }
        out = _result_dict(
            n=n, s=snap, act=act, strat="hold",
            result={"status": "held", "note": act["rationale"]},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation=act["rationale"], kahneman=extract_kahneman(act),
            judgment={"stance": "idle", "thesis": st.get("macro") or "grokfolio waiting", "focus": "schedule"},
            world=world_dict,
        )
        out["pace"] = {"tier": "grokfolio", "sleep_s": wait, "reason": "grokfolio_schedule"}
        _journal_stages(out, act, snap, out.get("judgment"))
        hist.append({"snapshot": snap, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return out

    constructed = await _construct(g, kind, world, st, cfg)
    holdings = constructed["holdings"]
    nl = float(eq or 0)
    cur_w = current_stk_weights(snap.get("positions") or [], nl)
    diffs = diff_book(holdings, current_w=cur_w, net_liq=nl)
    results: list[dict[str, Any]] = []
    last_act: dict[str, Any] = {
        "action": "hold",
        "strategy": "hold",
        "rationale": f"grokfolio {kind}: no diffs",
    }
    last_result: dict[str, Any] = {"status": "held", "note": last_act["rationale"]}

    from abcxauto.agent_loop import _extract_last, _tool

    for step in diffs[:20]:
        sym = step["symbol"]
        if step["op"] == "close":
            qty = _stk_qty(snap.get("positions") or [], sym)
            if qty <= 0:
                continue
            last_act = _close_act(sym, qty)
            last_result = await send_action(last_act, connector)
            results.append({"symbol": sym, "op": "close", "result": last_result})
            continue
        last = None
        try:
            q = await _tool(connector, "quote", {"symbol": sym})
            last = _extract_last(q if isinstance(q, dict) else None)
        except Exception:
            last = None
        if last is None:
            results.append({"symbol": sym, "op": step["op"], "result": {"status": "blocked", "note": "no IBKR live"}})
            continue
        qty = _qty_for_weight(float(step["weight_pct"]), nl, last)
        have = _stk_qty(snap.get("positions") or [], sym)
        if step["op"] == "resize" and qty < have:
            trim = have - qty
            if trim <= 0:
                continue
            last_act = _close_act(sym, trim)
            last_result = await send_action(last_act, connector)
            results.append({"symbol": sym, "op": "trim", "result": last_result})
            continue
        buy_qty = qty if step["op"] == "buy" else max(0, qty - have)
        if buy_qty <= 0:
            continue
        last_act = _bracket_act(sym, buy_qty, last, float(step.get("stop_pct") or 8), str(step.get("thesis") or ""))
        last_result = await send_action(last_act, connector)
        results.append({"symbol": sym, "op": "buy", "result": last_result})

    st["holdings"] = holdings
    st["macro"] = constructed["macro"]
    st["last_diffs"] = diffs
    st["last_results"] = results
    mark_ran(kind, state=st)

    thesis = constructed["macro"] or f"grokfolio {kind} {len(holdings)} names"
    judgment = {
        "stance": "hunt" if diffs else "idle",
        "thesis": thesis,
        "focus": f"grokfolio {kind}",
        "dismissed": constructed.get("dismissed") or "",
        "intent": {"kind": "grokfolio", "holdings": len(holdings)},
    }
    strat = str(last_act.get("strategy") or "hold")
    out = _result_dict(
        n=n, s=snap, act=last_act, strat=strat,
        result={**last_result, "grokfolio": results, "kind": kind, "holdings": holdings},
        pnl=pnl, eq=eq, prev=prev, inventory=inventory,
        validation=f"grokfolio_{kind}",
        kahneman=extract_kahneman(last_act),
        judgment=judgment, world=world_dict,
    )
    wait = sleep_until_next_s(cfg=cfg, session_status=session or "regular")
    out["pace"] = {"tier": "grokfolio", "sleep_s": wait, "reason": f"grokfolio_{kind}_done"}
    _journal_stages(out, last_act, snap, judgment)
    hist.append({"snapshot": snap, "action": last_act, **{k: out[k] for k in _HIST_KEYS}})
    return out
