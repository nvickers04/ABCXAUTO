"""Product book surface — compact state for prompts and cycle snapshots."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = (
    "build_book",
    "build_book_from_snap",
    "build_portfolio_state",
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


def _slim_positions(positions: list, limit: int = 12) -> List[dict]:
    from abcxauto.world_state import compact_position

    out: List[dict] = []
    for p in positions[:limit]:
        if not isinstance(p, dict):
            continue
        row = compact_position(p)
        row["uPnL"] = p.get("unrealizedPNL") or p.get("unrealized_pnl")
        out.append(row)
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
                    "cycle": d.get("cycle"),
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
    pnl = state.get("daily_pnl")
    n_pos = len(state.get("positions") or [])
    unprotected = state.get("unprotected_symbols") or []
    halt = state.get("halt")
    bits = [
        f"NL={nliq}" if nliq is not None else "NL=?",
        f"dayPnL={pnl}" if pnl is not None else "dayPnL=?",
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

    mandate_summary = ""
    try:
        from abcxauto.config import get_config

        mandate_summary = (get_config().trading_mandate or "")[:240]
    except Exception:
        mandate_summary = ""

    net_liq = _account_float(account, "netliquidation", "NetLiquidation")
    daily_pnl = _account_float(account, "dailypnl", "DailyPnL", "unrealizedpnl")
    daily_pnl_pct: Optional[float] = None
    if net_liq is not None and net_liq != 0 and daily_pnl is not None:
        try:
            daily_pnl_pct = round(float(daily_pnl) / float(net_liq) * 100.0, 4)
        except (TypeError, ValueError, ZeroDivisionError):
            daily_pnl_pct = None

    unprotected = list(protection.get("unprotected_symbols") or [])
    trades_today, halt, halt_reason = _trades_today_and_halt()
    recent_decisions, working_thesis = _journal_memory_bits()

    state: Dict[str, Any] = {
        "mandate_summary": mandate_summary,
        "net_liq": net_liq,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": daily_pnl_pct,
        "peak_dd_pct": _peak_dd_pct(net_liq),
        "positions": _slim_positions(positions),
        "unprotected_symbols": unprotected,
        "open_orders_count": len(open_orders),
        "trades_today": trades_today,
        "halt": halt,
        "halt_reason": halt_reason,
        "recent_decisions": recent_decisions,
        "working_thesis": working_thesis,
    }
    if include_narrative:
        state["narrative"] = portfolio_narrative(state)
    return state


def build_portfolio_state(
    account: Optional[dict] = None,
    positions: Optional[list] = None,
    open_orders: Optional[list] = None,
    protection: Optional[dict] = None,
    *,
    include_narrative: bool = True,
) -> Dict[str, Any]:
    """Alias for build_book (compat)."""
    return build_book(
        account,
        positions,
        open_orders,
        protection,
        include_narrative=include_narrative,
    )


def build_book_from_snap(snap: dict) -> dict:
    """Build book state from a cycle/pro snap dict."""
    if not isinstance(snap, dict):
        return build_book()
    return build_book(
        account=snap.get("account") if isinstance(snap.get("account"), dict) else None,
        positions=snap.get("positions") if isinstance(snap.get("positions"), list) else None,
        open_orders=snap.get("open_orders") if isinstance(snap.get("open_orders"), list) else None,
        protection=snap.get("protection") if isinstance(snap.get("protection"), dict) else None,
    )
