"""Overlay share guard. Tickets are ORDER EXAMPLES; send is the book."""

from __future__ import annotations

from typing import Any

OVERLAY_SHARES_INSUFFICIENT = "overlay_shares_insufficient"
OVERLAY_NO_LONG_STOCK = "overlay_no_long_stock"


def long_share_lots(positions: list[dict] | None) -> dict[str, float]:
    """Symbol → long STK/ETF share quantity (shorts ignored)."""
    lots: dict[str, float] = {}
    for p in positions or []:
        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
        if sec not in ("STK", "ETF", ""):
            continue
        try:
            qty = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        lots[sym] = lots.get(sym, 0.0) + qty
    return lots


def world_hints_from_world(world: Any) -> dict[str, Any]:
    """Build filter hints from a WorldState-like object or dict."""
    if world is None:
        return {
            "flat": True,
            "needs_protection": False,
            "long_lots": {},
            "has_trade_plan": False,
        }
    if isinstance(world, dict):
        positions = world.get("positions") or []
        return {
            "flat": bool(world.get("flat", not positions)),
            "needs_protection": bool(world.get("needs_protection")),
            "long_lots": long_share_lots(positions),
            "has_trade_plan": bool(world.get("trade_plan")),
        }
    positions = getattr(world, "positions", None) or []
    return {
        "flat": bool(getattr(world, "flat", not positions)),
        "needs_protection": bool(getattr(world, "needs_protection", False)),
        "long_lots": long_share_lots(positions),
        "has_trade_plan": bool(getattr(world, "trade_plan", None)),
    }


def max_long_shares(long_lots: dict[str, float] | None) -> float:
    if not long_lots:
        return 0.0
    return max(long_lots.values())


def check_overlay_shares(
    strategy: str,
    params: dict | None,
    positions: list[dict] | None,
) -> tuple[bool, str, str]:
    """Validate covered_call/collar/protective_put against long stock.

    Returns (ok, reason_code, message).
    """
    strat = (strategy or "").strip().lower()
    if strat not in ("covered_call", "collar", "protective_put"):
        return True, "ok", "n/a"
    params = params or {}
    sym = str(params.get("symbol") or "").upper()
    lots = long_share_lots(positions)
    if not sym:
        return False, OVERLAY_NO_LONG_STOCK, f"{strat} requires symbol with long stock"
    have = float(lots.get(sym) or 0)
    if have <= 0:
        return (
            False,
            OVERLAY_NO_LONG_STOCK,
            f"{strat} on {sym}: no long STK shares in book",
        )
    try:
        need = float(params.get("shares") or 100)
    except (TypeError, ValueError):
        need = 100.0
    if need <= 0:
        need = 100.0
    if have + 1e-9 < need:
        return (
            False,
            OVERLAY_SHARES_INSUFFICIENT,
            f"{strat} on {sym}: need {need:g} shares, book has {have:g}",
        )
    return True, "ok", "shares ok"
