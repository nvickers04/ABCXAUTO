"""Account returns from IBKR NetLiq history in the trade journal.

- NetLiq / daily PnL: live engine values overlay journal when provided
- 1W / 3M / 1Y: only when journal snapshots span that horizon (no MDA / SPY proxy)
"""

from __future__ import annotations

from typing import Any


def compute_account_returns(
    *,
    equity: float | None = None,
    daily_pnl: float | None = None,
    positions: list[dict] | None = None,  # unused; kept for call-site compat
    journal: Any = None,
) -> dict[str, Any]:
    """Build Account panel performance from journal IBKR NAV snapshots."""
    del positions  # horizons are account NAV, not book MTM
    if journal is None:
        try:
            from abcxauto.memory import get_journal
            journal = get_journal()
        except Exception:
            journal = None

    if journal is not None and hasattr(journal, "account_performance"):
        try:
            out = dict(journal.account_performance() or {})
        except Exception:
            out = {}
    else:
        out = {}

    result: dict[str, Any] = {
        "net_liquidation": out.get("net_liquidation"),
        "daily_pnl": out.get("daily_pnl"),
        "ret_1w": out.get("ret_1w"),
        "ret_3m": out.get("ret_3m"),
        "ret_1y": out.get("ret_1y"),
        "as_of": out.get("as_of"),
        "history_start": out.get("history_start"),
        "history_days": out.get("history_days"),
        "source": out.get("source") or "none",
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


async def compute_account_returns_async(
    *,
    equity: float | None = None,
    daily_pnl: float | None = None,
    positions: list[dict] | None = None,
    journal: Any = None,
) -> dict[str, Any]:
    """Async shim for callers that ``await`` returns refresh."""
    return compute_account_returns(
        equity=equity,
        daily_pnl=daily_pnl,
        positions=positions,
        journal=journal,
    )
