"""Compatibility leftovers from clerk-as-runner.

Think is hosted on ``pro_engine``. The nap clock is ``wake_bus``.
This module keeps inventory / gate helpers that callers still import.
``run_cycle`` stays import-safe but does not snap, think, send, or arm a nap.
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
    snap,
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


async def run_cycle(*_a: Any, **_k: Any) -> dict:
    """Retired clerk launcher. Does not think, snap, send, or set a nap clock."""
    return {
        "strat": "skipped",
        "result": {"status": "skipped", "note": "cycle_shim_retired"},
        "pnl": 0.0,
        "equity": 0.0,
        "inventory": format_position_inventory([]),
        "validation": "cycle_shim_retired",
        "sends": 0,
    }
