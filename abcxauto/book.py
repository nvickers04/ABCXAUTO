"""Product book surface — compact state for prompts and look snapshots."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = (
    "build_book",
    "build_book_from_snap",
    "clerk_halt_facts",
    "portfolio_narrative",
)


def _account_float(account: dict, *keys: str) -> Optional[float]:
    for key in keys:
        if key in account and account[key] is not None:
            try:
                return float(account[key])
            except (TypeError, ValueError):
                continue
        lower = key.lower()
        for ak, av in account.items():
            if str(ak).lower() == lower and av is not None:
                try:
                    return float(av)
                except (TypeError, ValueError):
                    break
    return None


def _position_qty(pos: dict) -> float:
    raw = pos.get("quantity")
    if raw is None:
        raw = pos.get("position")
    if raw is None:
        raw = pos.get("qty")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _is_open_lot(pos: object) -> bool:
    """Broker lot with nonzero qty. A bare ticker is not a lot."""
    if not isinstance(pos, dict):
        return False
    return abs(_position_qty(pos)) > 1e-9


def _slim_positions(positions: list, limit: int = 12, net_liq: float | None = None) -> List[dict]:
    """Compact open lots only. Never pad with SPY/QQQ/IWM/DIA or a tape seed."""
    from abcxauto.world_state import compact_position

    out: List[dict] = []
    cap = max(0, int(limit))
    for p in positions or []:
        if not _is_open_lot(p):
            continue
        out.append(compact_position(p, net_liq=net_liq))
        if len(out) >= cap:
            break
    return out


def _peak_dd_pct(net_liq: Optional[float]) -> Optional[float]:
    try:
        from abcxauto.risk_gates import get_risk_gate

        gate = get_risk_gate()
        peak = gate.peak_equity
        if peak is None or peak <= 0 or net_liq is None:
            return None
        if net_liq > 0:
            try:
                gate.update_equity(net_liq)
                peak = gate.peak_equity or peak
            except Exception:
                pass
        dd = (peak - float(net_liq)) / peak * 100.0
        return round(max(0.0, dd), 4)
    except Exception:
        return None


def clerk_halt_facts(
    net_liq: Optional[float] = None,
    daily_pnl: Optional[float] = None,
) -> Dict[str, Any]:
    """Code halt vs the daily-loss trip. Not a notebook rule."""
    halted = False
    halt_reason = ""
    halt_kind = ""
    try:
        from abcxauto.risk_gates import get_risk_gate

        gate = get_risk_gate()
        halted = bool(gate.is_halted)
        halt_reason = str(gate.halt_reason or "")
        halt_kind = str(getattr(gate, "halt_kind", "") or "")
    except Exception:
        logger.debug("clerk halt gate unavailable", exc_info=True)
    limit_pct = 25.0
    try:
        from abcxauto.config import get_config

        limit_pct = float(getattr(get_config(), "daily_loss_limit_pct", 25.0) or 0.0)
    except Exception:
        pass
    trips_at: Optional[float] = None
    day_vs: Optional[float] = None
    try:
        nl = float(net_liq) if net_liq is not None else None
    except (TypeError, ValueError):
        nl = None
    if nl and nl > 0 and limit_pct > 0:
        trips_at = round(-(limit_pct / 100.0) * nl, 2)
        if daily_pnl is not None:
            try:
                day_vs = round(float(daily_pnl) - trips_at, 2)
            except (TypeError, ValueError):
                day_vs = None
    return {
        "clerk_halted": halted,
        "halt_kind": halt_kind or None,
        "halt_reason": halt_reason or None,
        "daily_loss_limit_pct": limit_pct,
        "halt_trips_at_usd": trips_at,
        "ibkr_day_vs_halt": day_vs,
    }


def _trades_today_and_halt() -> tuple[Optional[int], Optional[bool], Optional[str]]:
    trades: Optional[int] = None
    halted: Optional[bool] = None
    halt_reason: Optional[str] = None
    try:
        from abcxauto.risk_gates import get_risk_gate

        gate = get_risk_gate()
        trades = int(gate.daily_trade_count())
        halted = bool(gate.is_halted)
        halt_reason = gate.halt_reason or None
    except Exception:
        pass
    if trades is None:
        try:
            from abcxauto.memory import get_journal

            summary = get_journal().daily_summary()
            trades = int(summary.get("dispatch_ok") or 0)
            if halted is None:
                halted = int(summary.get("halts") or 0) > 0
        except Exception:
            pass
    return trades, halted, halt_reason


def _journal_memory_bits() -> tuple[List[dict], str]:
    recent: List[dict] = []
    thesis = ""
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        for d in journal.recent_decisions(limit=5):
            recent.append(
                {
                    "ts": d.get("ts"),
                    "action": d.get("action"),
                    "strategy": d.get("strategy"),
                    "rationale": (d.get("rationale") or "")[:160],
                    "outcome": (
                        (d.get("outcome") or {}).get("status")
                        if isinstance(d.get("outcome"), dict)
                        else d.get("outcome")
                    ),
                }
            )
        thesis = (journal.get_working_thesis() or "")[:400]
    except Exception as exc:
        logger.debug("book journal bits unavailable: %s", exc)
    return recent, thesis


def portfolio_narrative(state: dict) -> str:
    """One-liner summary of book/portfolio state."""
    nliq = state.get("net_liq")
    ibkr = state.get("ibkr_daily_pnl")
    if ibkr is None:
        ibkr = state.get("daily_pnl")
    ou = state.get("open_upnl")
    n_pos = len(state.get("positions") or [])
    unprotected = state.get("unprotected_symbols") or []
    halt = state.get("halt")
    bits = [
        f"NL={nliq}" if nliq is not None else "NL=?",
        f"ibkrDay={ibkr}" if ibkr is not None else "ibkrDay=?",
        f"openU={ou}" if ou is not None else "openU=?",
        f"{n_pos} pos",
        f"{state.get('open_orders_count', 0)} orders",
    ]
    if unprotected:
        bits.append(f"UNPROTECTED:{','.join(str(x) for x in unprotected)}")
    else:
        bits.append("protected/flat")
    if halt:
        bits.append(f"HALTED:{state.get('halt_reason') or 'yes'}")
    thesis = (state.get("working_thesis") or "").strip()
    if thesis:
        bits.append(f"thesis={thesis[:80]}")
    return " | ".join(bits)


def build_book(
    account: Optional[dict] = None,
    positions: Optional[list] = None,
    open_orders: Optional[list] = None,
    protection: Optional[dict] = None,
    *,
    include_narrative: bool = True,
) -> Dict[str, Any]:
    """Build compact book dict for prompts and snap attachment."""
    account = account if isinstance(account, dict) else {}
    positions = positions if isinstance(positions, list) else []
    open_orders = open_orders if isinstance(open_orders, list) else []
    protection = protection if isinstance(protection, dict) else {}

    net_liq = _account_float(account, "netliquidation", "NetLiquidation")
    daily_pnl = _account_float(account, "dailypnl", "DailyPnL")
    daily_pnl_pct: Optional[float] = None
    if net_liq is not None and net_liq != 0 and daily_pnl is not None:
        try:
            daily_pnl_pct = round(float(daily_pnl) / float(net_liq) * 100.0, 4)
        except (TypeError, ValueError, ZeroDivisionError):
            daily_pnl_pct = None

    unprotected = list(protection.get("unprotected_symbols") or [])
    trades_today, halt, halt_reason = _trades_today_and_halt()
    recent_decisions, working_thesis = _journal_memory_bits()
    from abcxauto.world_state import _portfolio_risk, open_upnl_of, pct_of_nl

    open_upnl = open_upnl_of(positions)
    halt_facts = clerk_halt_facts(net_liq, daily_pnl)
    total_cash = _account_float(
        account, "totalcashvalue", "TotalCashValue", "total_cash", "TotalCash"
    )
    port = _portfolio_risk(
        positions,
        float(net_liq) if net_liq is not None else 0.0,
        total_cash=total_cash,
    )
    state: Dict[str, Any] = {
        "net_liq": net_liq,
        "daily_pnl": daily_pnl,
        "ibkr_daily_pnl": daily_pnl,
        "open_upnl": open_upnl,
        "daily_pnl_pct": daily_pnl_pct,
        "daily_pnl_pct_of_nl": daily_pnl_pct,
        "open_upnl_pct_of_nl": pct_of_nl(open_upnl, net_liq),
        "halt_trips_at_pct_of_nl": pct_of_nl(halt_facts.get("halt_trips_at_usd"), net_liq),
        "ibkr_day_vs_halt_pct_of_nl": pct_of_nl(halt_facts.get("ibkr_day_vs_halt"), net_liq),
        "peak_dd_pct": _peak_dd_pct(net_liq),
        "positions": _slim_positions(positions, net_liq=net_liq),
        "portfolio_risk": port,
        "exposure": port.get("exposure"),
        "capital_liquidity": port.get("capital_liquidity"),
        "unprotected_symbols": unprotected,
        "open_orders_count": len(open_orders),
        "trades_today": trades_today,
        "halt": halt,
        "halt_reason": halt_reason,
        "recent_decisions": recent_decisions,
        "working_thesis": working_thesis,
    }
    state.update(halt_facts)
    if halt_reason and not state.get("halt_reason"):
        state["halt_reason"] = halt_reason
    if include_narrative:
        state["narrative"] = portfolio_narrative(state)
    return state


def build_book_from_snap(snap: dict) -> dict:
    """Build book state from a look/pro snap dict."""
    if not isinstance(snap, dict):
        return build_book()
    return build_book(
        account=snap.get("account") if isinstance(snap.get("account"), dict) else None,
        positions=snap.get("positions") if isinstance(snap.get("positions"), list) else None,
        open_orders=snap.get("open_orders") if isinstance(snap.get("open_orders"), list) else None,
        protection=snap.get("protection") if isinstance(snap.get("protection"), dict) else None,
    )
