"""Overlay share guard only. Not a notebook, clock, tape, or ticket."""

from __future__ import annotations

import math

OVERLAY_SHARES_INSUFFICIENT = "overlay_shares_insufficient"
OVERLAY_NO_LONG_STOCK = "overlay_no_long_stock"
OVERLAY_SHARES_UNSPECIFIED = "overlay_shares_unspecified"

__all__ = (
    "OVERLAY_NO_LONG_STOCK",
    "OVERLAY_SHARES_INSUFFICIENT",
    "OVERLAY_SHARES_UNSPECIFIED",
    "check_overlay_shares",
    "long_share_lots",
)


def long_share_lots(positions: list[dict] | None) -> dict[str, float]:
    """Symbol → long STK/ETF share quantity (shorts and untyped lots ignored)."""
    lots: dict[str, float] = {}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sec = str(p.get("secType") or p.get("sec_type") or "").strip().upper()
        if sec not in ("STK", "ETF"):
            continue
        raw = p.get("quantity") if p.get("quantity") is not None else p.get("position")
        try:
            qty = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(qty) or qty <= 0:
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym:
            continue
        lots[sym] = lots.get(sym, 0.0) + qty
    return lots


def check_overlay_shares(
    strategy: str,
    params: dict | None,
    positions: list[dict] | None,
) -> tuple[bool, str, str]:
    """Validate covered_call/collar/protective_put against long stock.

    Returns (ok, reason_code, message). Does not mutate params.
    Missing or unreadable ``shares`` fails closed — the clerk does not invent a size.
    """
    strat = (strategy or "").strip().lower()
    if strat not in ("covered_call", "collar", "protective_put"):
        return True, "ok", "n/a"
    params = params or {}
    sym = str(params.get("symbol") or "").strip().upper()
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
    raw_shares = params.get("shares")
    if isinstance(raw_shares, bool) or raw_shares in (None, ""):
        return (
            False,
            OVERLAY_SHARES_UNSPECIFIED,
            f"{strat} on {sym}: shares required (not invented)",
        )
    try:
        need = float(raw_shares)
    except (TypeError, ValueError):
        return (
            False,
            OVERLAY_SHARES_UNSPECIFIED,
            f"{strat} on {sym}: shares unreadable",
        )
    if not math.isfinite(need) or need <= 0:
        return (
            False,
            OVERLAY_SHARES_UNSPECIFIED,
            f"{strat} on {sym}: shares unreadable",
        )
    if have + 1e-9 < need:
        return (
            False,
            OVERLAY_SHARES_INSUFFICIENT,
            f"{strat} on {sym}: need {need:g} shares, book has {have:g}",
        )
    return True, "ok", "shares ok"
