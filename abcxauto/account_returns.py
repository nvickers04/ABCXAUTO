"""Account returns from IBKR NetLiq history in the trade journal.

- NetLiq / daily PnL: live engine values overlay journal when provided
- 1W / 3M / 1Y: only when journal snapshots span that horizon (no MDA / SPY proxy)
- Horizons are account NAV. Open-lot MTM is never a fallback or overlay.
"""

from __future__ import annotations

from typing import Any

_NAV_SOURCE = "ibkr_nav"


def _account_performance_blob(journal: Any) -> dict[str, Any]:
    """Raw journal.account_performance() dict, or empty. Does not read a book."""
    if journal is None:
        try:
            from abcxauto.memory import get_journal

            journal = get_journal()
        except Exception:
            journal = None
    if journal is None or not hasattr(journal, "account_performance"):
        return {}
    try:
        raw = journal.account_performance() or {}
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def compute_account_returns(
    *,
    equity: float | None = None,
    daily_pnl: float | None = None,
    positions: list[dict] | None = None,  # unused; kept for call-site compat
    journal: Any = None,
) -> dict[str, Any]:
    """Build Account panel performance from journal IBKR NAV snapshots."""
    del positions  # horizons are account NAV, not book MTM
    out = _account_performance_blob(journal)
    # Caller-facing horizons / journal NAV only when the blob is IBKR NAV.
    # A book-MTM or sourceless ret_* must not ship as an account return.
    trusted = str(out.get("source") or "").strip().lower() == _NAV_SOURCE
    result: dict[str, Any] = {
        "net_liquidation": out.get("net_liquidation") if trusted else None,
        "daily_pnl": out.get("daily_pnl") if trusted else None,
        "ret_1w": out.get("ret_1w") if trusted else None,
        "ret_3m": out.get("ret_3m") if trusted else None,
        "ret_1y": out.get("ret_1y") if trusted else None,
        "as_of": out.get("as_of") if trusted else None,
        "history_start": out.get("history_start") if trusted else None,
        "history_days": out.get("history_days") if trusted else None,
        "source": _NAV_SOURCE if trusted else "none",
    }
    if equity is not None:
        try:
            result["net_liquidation"] = float(equity)
        except (TypeError, ValueError):
            pass
    if daily_pnl is not None:
        try:
            result["daily_pnl"] = float(daily_pnl)
        except (TypeError, ValueError):
            pass
    return result
