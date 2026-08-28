"""Overlay share + last-stop guard. Not a notebook, clock, tape, or ticket."""

from __future__ import annotations

import math

OVERLAY_SHARES_INSUFFICIENT = "overlay_shares_insufficient"
OVERLAY_NO_LONG_STOCK = "overlay_no_long_stock"
OVERLAY_SHARES_UNSPECIFIED = "overlay_shares_unspecified"
OVERLAY_ALREADY_PROTECTED = "overlay_already_protected"

# Share-cover overlays. cash_secured_put is new-risk unless it is on a name
# that already has a last-stop-covered STK lot — then it does not reduce risk.
_SHARE_OVERLAYS = ("covered_call", "collar", "protective_put")
_OVERLAY_STRATS = _SHARE_OVERLAYS + ("cash_secured_put",)

__all__ = (
    "OVERLAY_ALREADY_PROTECTED",
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


def _long_stk_rows(positions: list[dict] | None, symbol: str) -> list[dict]:
    """Long STK/ETF rows for one symbol (same filter as long_share_lots)."""
    want = str(symbol or "").strip().upper()
    if not want:
        return []
    rows: list[dict] = []
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sec = str(p.get("secType") or p.get("sec_type") or "").strip().upper()
        if sec not in ("STK", "ETF"):
            continue
        if str(p.get("symbol") or "").strip().upper() != want:
            continue
        raw = p.get("quantity") if p.get("quantity") is not None else p.get("position")
        try:
            qty = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(qty) or qty <= 0:
            continue
        rows.append(p)
    return rows


def _symbol_last_stop_covers(
    symbol: str,
    positions: list[dict] | None,
    orders: list[dict] | None,
) -> bool:
    """True when every long STK lot of ``symbol`` has a covering last-stop."""
    from abcxauto.protect_reconciler import last_stop_covers_lot

    lots = _long_stk_rows(positions, symbol)
    if not lots:
        return False
    return all(last_stop_covers_lot(lot, orders) for lot in lots)


def check_overlay_shares(
    strategy: str,
    params: dict | None,
    positions: list[dict] | None,
    orders: list[dict] | None = None,
) -> tuple[bool, str, str]:
    """Validate overlay vs long stock and last-stop.

    Returns (ok, reason_code, message). Does not mutate params.
    Missing or unreadable ``shares`` fails closed — size is not invented.
    Overlay on a name whose long STK already has a covering last-stop is
    refused (it does not reduce risk). Unprotected STK still overlays.
    cash_secured_put on a different name is not this gate.
    """
    strat = (strategy or "").strip().lower()
    if strat not in _OVERLAY_STRATS:
        return True, "ok", "n/a"
    params = params or {}
    sym = str(params.get("symbol") or "").strip().upper()
    if strat in _SHARE_OVERLAYS:
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
    if _symbol_last_stop_covers(sym, positions, orders):
        return (
            False,
            OVERLAY_ALREADY_PROTECTED,
            f"{strat} on {sym}: last-stop already covers the lot",
        )
    if strat not in _SHARE_OVERLAYS:
        return True, "ok", "n/a"
    return True, "ok", "shares ok"
