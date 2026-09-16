"""Round IBKR limit/stop prices to the contract min-tick before placeOrder."""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any


def parse_min_tick(raw: Any, *, default: float = 0.01) -> float:
    try:
        tick = float(raw)
    except (TypeError, ValueError):
        return default
    if tick <= 0 or tick > 1e6:
        return default
    return tick


def round_to_min_tick(
    price: Any,
    tick: Any,
    *,
    default_tick: float = 0.01,
    action: Any = None,
) -> float:
    """Round to minTick.

    BUY limits ceil (toward/through the ask). SELL limits floor
    (toward/through the bid). A limit rounded the other way sits
    unfilled — that parked the NOK/XOM paper exits. Without action
    (protective stops / legacy) keep nearest ROUND_HALF_UP.
    """
    px = float(price)
    inc = parse_min_tick(tick, default=default_tick)
    d = Decimal(str(px))
    t = Decimal(str(inc))
    side = str(action or "").strip().upper()
    if side == "BUY":
        mode = ROUND_CEILING
    elif side == "SELL":
        mode = ROUND_FLOOR
    else:
        mode = ROUND_HALF_UP
    q = (d / t).quantize(Decimal("1"), rounding=mode) * t
    return float(q)
