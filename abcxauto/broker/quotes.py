"""IBKR ticker → quote normalization for send geometry."""

from __future__ import annotations

from typing import Any


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
    out: dict[str, Any] = {
        "symbol": sym,
        "last": last,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "source": "ibkr",
        "freshness": "live",
    }
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
