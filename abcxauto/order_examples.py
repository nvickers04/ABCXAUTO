"""Agent-facing order API contract: one minimal valid example per sendable type.

``ORDER_EXAMPLES`` mirrors ``abcxauto.proposals.STRATEGIES`` param shapes
(plus ``hold``).
"""

from __future__ import annotations

import json
from typing import Any

from abcxauto.proposals import STRATEGIES

ORDER_EXAMPLES: dict[str, dict[str, Any]] = {
    "hold": {},
    "set_risk": {
        "max_risk_per_trade_pct": 1.5,
        "daily_loss_limit_pct": 5.0,
        "max_position_pct": 12.0,
        "max_peak_drawdown_pct": 12.0,
    },
    "market_bracket": {
        "symbol": "NVDA",
        "quantity": 10,
        "direction": "LONG",
        "stop_price": 97.0,
        "target_price": 106.0,
        "price_hint": 100.0,
    },
    "bracket": {
        "symbol": "NVDA",
        "quantity": 10,
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_price": 97.0,
        "target_price": 106.0,
        "price_hint": 100.0,
    },
    "oca": {
        "symbol": "NVDA",
        "quantity": 10,
        "direction": "LONG",
        "stop_price": 97.0,
        "target_price": 106.0,
        "price_hint": 100.0,
    },
    "modify_stop": {"order_id": 101, "new_stop_price": 97.5},
    "modify_target": {"order_id": 102, "new_limit_price": 112.0},
    "cancel_order": {"order_id": 103},
    "market_order": {
        "symbol": "AAPL",
        "action": "SELL",
        "quantity": 5,
        "closing_position": True,
    },
    "limit_order": {
        "symbol": "AAPL",
        "action": "SELL",
        "quantity": 10,
        "limit_price": 150.0,
        "closing_position": True,
    },
    "stop_order": {
        "symbol": "AAPL",
        "action": "SELL",
        "quantity": 10,
        "stop_price": 140.0,
        "closing_position": True,
    },
    "stop_limit": {
        "symbol": "AAPL",
        "action": "SELL",
        "quantity": 10,
        "stop_price": 140.0,
        "limit_price": 139.5,
        "closing_position": True,
    },
    "close_option": {
        "symbol": "SPY",
        "expiration": "20260709",
        "strike": 745.0,
        "right": "C",
        "quantity": 1,
    },
    "trailing_stop": {
        "symbol": "SPY",
        "quantity": 10,
        "direction": "LONG",
        "trail_percent": 2.0,
    },
    "trailing_stop_limit": {
        "symbol": "SPY",
        "quantity": 10,
        "direction": "LONG",
        "trail_percent": 2.0,
        "limit_offset": 0.10,
    },
    "market_on_close": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "closing_position": True,
    },
    "limit_on_close": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "limit_price": 500.0,
        "closing_position": True,
    },
    "market_on_open": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "closing_position": True,
    },
    "limit_on_open": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "limit_price": 500.0,
        "closing_position": True,
    },
    "adaptive": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "order_type": "MKT",
        "priority": "Normal",
        "closing_position": True,
    },
    "midprice": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "closing_position": True,
    },
    "relative": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "offset": 0.01,
        "closing_position": True,
    },
    "limit_order_gtd": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "limit_price": 500.0,
        "good_till_date": "20261231 16:00:00",
        "closing_position": True,
    },
    "fill_or_kill": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "limit_price": 500.0,
        "closing_position": True,
    },
    "immediate_or_cancel": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "limit_price": 500.0,
        "closing_position": True,
    },
    "vwap": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "max_pct_volume": 25.0,
        "closing_position": True,
    },
    "twap": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "closing_position": True,
    },
    "iceberg": {
        "symbol": "SPY",
        "action": "SELL",
        "total_quantity": 100,
        "display_size": 10,
        "limit_price": 500.0,
        "closing_position": True,
    },
    "snap_to_midpoint": {
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 10,
        "closing_position": True,
    },
    "vertical_spread": {
        "symbol": "SPY",
        "expiration": "20260718",
        "long_strike": 500.0,
        "short_strike": 505.0,
        "right": "C",
        "quantity": 1,
    },
    "iron_condor": {
        "symbol": "SPY",
        "expiration": "20260718",
        "put_long_strike": 480.0,
        "put_short_strike": 490.0,
        "call_short_strike": 510.0,
        "call_long_strike": 520.0,
        "quantity": 1,
    },
    "iron_butterfly": {
        "symbol": "SPY",
        "expiration": "20260718",
        "center_strike": 500.0,
        "wing_width": 10.0,
        "quantity": 1,
    },
    "straddle": {
        "symbol": "SPY",
        "expiration": "20260718",
        "strike": 500.0,
        "quantity": 1,
        "action": "BUY",
    },
    "strangle": {
        "symbol": "SPY",
        "expiration": "20260718",
        "put_strike": 490.0,
        "call_strike": 510.0,
        "quantity": 1,
        "action": "BUY",
    },
    "butterfly": {
        "symbol": "SPY",
        "expiration": "20260718",
        "lower_strike": 490.0,
        "middle_strike": 500.0,
        "upper_strike": 510.0,
        "right": "C",
        "quantity": 1,
    },
    "calendar_spread": {
        "symbol": "SPY",
        "strike": 500.0,
        "near_expiration": "20260718",
        "far_expiration": "20260815",
        "right": "C",
        "quantity": 1,
    },
    "diagonal_spread": {
        "symbol": "SPY",
        "near_strike": 500.0,
        "far_strike": 505.0,
        "near_expiration": "20260718",
        "far_expiration": "20260815",
        "right": "C",
        "quantity": 1,
    },
    "buy_option": {
        "symbol": "SPY",
        "expiration": "20260718",
        "strike": 500.0,
        "right": "C",
        "quantity": 1,
    },
    "covered_call": {
        "symbol": "SPY",
        "expiration": "20260718",
        "strike": 510.0,
        "shares": 100,
    },
    "cash_secured_put": {
        "symbol": "SPY",
        "expiration": "20260718",
        "strike": 490.0,
        "contracts": 1,
    },
    "protective_put": {
        "symbol": "SPY",
        "expiration": "20260718",
        "strike": 490.0,
        "shares": 100,
    },
    "collar": {
        "symbol": "SPY",
        "expiration": "20260718",
        "put_strike": 490.0,
        "call_strike": 510.0,
        "shares": 100,
    },
    "ratio_spread": {
        "symbol": "SPY",
        "expiration": "20260718",
        "long_strike": 500.0,
        "short_strike": 510.0,
        "right": "C",
        "ratio": 2,
        "quantity": 1,
    },
    "jade_lizard": {
        "symbol": "SPY",
        "expiration": "20260718",
        "put_strike": 490.0,
        "call_short_strike": 510.0,
        "call_long_strike": 520.0,
        "quantity": 1,
    },
    "roll_option": {
        "symbol": "SPY",
        "quantity": 1,
        "conId": 999001,
        "new_dte": 30,
        "roll_type": "ROLL_OUT",
    },
}

SENDABLE_TYPES = frozenset(ORDER_EXAMPLES)


def format_order_examples(*, allowed: frozenset[str] | set[str] | None = None) -> str:
    """Compact prompt section: how to send each Act-allowlisted order type.

    When ``allowed`` is None, uses ``abcxauto.agent_loop.ALLOWED_ACTIONS`` so
    prompts never teach strategies Act will block.
    """
    if allowed is None:
        try:
            from abcxauto.agent_loop import ALLOWED_ACTIONS as _allowed
        except Exception:
            _allowed = frozenset(ORDER_EXAMPLES)
        allowed = _allowed
    lines = [
        "ORDER EXAMPLES (how to send — Act allowlist only)",
        "Emit strategy + params. Stock entries need stop+target. Bare stock orders are exit-only.",
        "Use direction LONG|SHORT for bracket/market_bracket/oca/trailing. hold params are {}.",
        "Stock exits: target_conId + quantity (partial trim OK; omit qty = full). After trim check stop_qty_fact.",
        "close_option: prefer conId; quantity may be partial. roll_option for lifecycle.",
        "Option multi-leg / CSP: match param shapes below; gates may reject unlimited risk.",
        "set_risk retunes capital knobs inside the operator risk_posture envelope (no broker send).",
        "",
    ]
    for name in sorted(ORDER_EXAMPLES):
        if name not in allowed:
            continue
        params = ORDER_EXAMPLES[name]
        lines.append(f"{name}: {json.dumps(params, separators=(',', ':'))}")
    return "\n".join(lines)


def assert_examples_cover_strategies() -> None:
    """Every STRATEGIES key has an example; hold/set_risk are allowed extras."""
    missing = sorted(set(STRATEGIES) - set(ORDER_EXAMPLES))
    if missing:
        raise AssertionError(f"ORDER_EXAMPLES missing STRATEGIES keys: {missing}")
    if "hold" not in ORDER_EXAMPLES:
        raise AssertionError("ORDER_EXAMPLES must include hold")
    if "set_risk" not in ORDER_EXAMPLES:
        raise AssertionError("ORDER_EXAMPLES must include set_risk")
