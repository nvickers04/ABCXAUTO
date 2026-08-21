"""Overlay share guard. Tickets are ORDER EXAMPLES; send is the book."""

from __future__ import annotations

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
