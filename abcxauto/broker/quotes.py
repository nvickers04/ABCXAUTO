"""IBKR ticker → quote normalization for send geometry."""

from __future__ import annotations

from typing import Any

from abcxauto.prints import stamp


def quote_batch_cap() -> int:
    """How many live names one IBKR quote/scan sweep may stamp."""
    try:
        from abcxauto.opportunity_scan import scan_quote_cap

        return max(8, int(scan_quote_cap() or 12))
    except Exception:
        return 12


def _finite_px(value: Any) -> float | None:
    try:
        px = float(value)
    except (TypeError, ValueError):
        return None
    if px != px or px <= 0:
        return None
    return px


def quote_from_ticker(ticker: Any, *, symbol: str | None = None) -> dict[str, Any]:
    """IBKR stream snapshot: last/bid/ask. Missing ticks omitted."""
    contract = getattr(ticker, "contract", None)
    sym = (symbol or getattr(contract, "symbol", None) or "").upper()
    last = _finite_px(getattr(ticker, "last", None))
    bid = _finite_px(getattr(ticker, "bid", None))
    ask = _finite_px(getattr(ticker, "ask", None))
    mid = None
    if bid is not None and ask is not None:
        mid = round((bid + ask) / 2.0, 4)
    if last is None:
        last = mid
    tick_t = getattr(ticker, "time", None)
    if tick_t is None:
        tick_t = getattr(ticker, "timestamp", None)
    out: dict[str, Any] = stamp(
        {
            "symbol": sym,
            "last": last,
            "bid": bid,
            "ask": ask,
            "mid": mid,
        },
        source="ibkr",
        freshness="live",
        use="ibkr_live_for_decisions",
        asof=tick_t,
        fallback_now=True,
    )
    # Prior close / session open are tape facts, never a stand-in for last.
    close = _finite_px(getattr(ticker, "close", None))
    open_px = _finite_px(getattr(ticker, "open_", None))
    if open_px is None:
        open_px = _finite_px(getattr(ticker, "open", None))
    if close is not None:
        out["close"] = close
    if open_px is not None:
        out["open"] = open_px
    if last is not None and close is not None and close > 0:
        out["change_pct"] = round((last / close - 1.0) * 100.0, 3)
    if open_px is not None and close is not None and close > 0:
        out["open_gap_pct"] = round((open_px / close - 1.0) * 100.0, 3)
    iv = _finite_px(getattr(ticker, "impliedVolatility", None))
    if iv is not None:
        out["iv"] = iv
    greeks = getattr(ticker, "modelGreeks", None)
    if greeks is not None:
        for key in ("delta", "gamma", "theta", "vega"):
            val = _finite_px(getattr(greeks, key, None))
            if val is not None:
                out[key] = val
    return out
