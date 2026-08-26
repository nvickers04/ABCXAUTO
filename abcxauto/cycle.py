"""Compatibility leftovers from clerk-as-runner.

Think is hosted on ``pro_engine``. Overnight park is ``park_clock``.
This module keeps inventory / gate helpers that callers still import.
``run_cycle`` and ``snap`` stay import-safe but do not look, think, send,
or arm a park clock.
"""

from __future__ import annotations

from typing import Any

from abcxauto.agent_loop import (  # noqa: F401
    ALLOWED_ACTIONS,
    AWARENESS_HEART,
    BLOCKED_STRAT,
    RULES,
    VALID_ACTIONS,
    equity_of,
    format_position_inventory,
    normalize_action,
    pnl_of,
    risk_label,
    simulate_close_impact,
    validate_action_against_inventory,
)

__all__ = [
    "ALLOWED_ACTIONS",
    "AWARENESS_HEART",
    "BLOCKED_STRAT",
    "RULES",
    "VALID_ACTIONS",
    "equity_of",
    "format_position_inventory",
    "normalize_action",
    "pnl_of",
    "risk_label",
    "run_cycle",
    "simulate_close_impact",
    "snap",
    "validate_action_against_inventory",
]


def _retired(note: str) -> dict:
    return {
        "strat": "skipped",
        "result": {"status": "skipped", "note": note},
        "pnl": 0.0,
        "equity": 0.0,
        "inventory": format_position_inventory([]),
        "validation": note,
        "sends": 0,
        "taken_at": "",
        "account": {},
        "positions": [],
        "open_orders": [],
        "market_hours": {},
        "spy_quote": {},
        "vix_quote": {},
        "protection": {},
        "reality_pulse": {},
        "portfolio_state": {},
        "book_unreliable": True,
    }


async def run_cycle(*_a: Any, **_k: Any) -> dict:
    """Retired clerk launcher. Does not think, snap, send, or set a nap clock."""
    return _retired("cycle_shim_retired")


async def snap(*_a: Any, **_k: Any) -> dict:
    """Retired clerk book look. Does not talk to IBKR or arm a wake."""
    return _retired("cycle_snap_retired")

