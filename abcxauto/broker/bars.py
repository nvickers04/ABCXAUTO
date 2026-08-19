"""IBKR bars for the candles tool. Shape matches MDA OHLCV."""

from __future__ import annotations

from typing import Any


def hist_spec(resolution: str) -> tuple[str, str]:
    """IBKR (barSizeSetting, durationStr) for a Grok candles resolution."""
    key = str(resolution or "D").strip().upper()
    if key in ("60", "60M", "1H", "H"):
        return "1 hour", "10 D"
    if key in ("15", "15M"):
        return "15 mins", "5 D"
    if key in ("5", "5M"):
        return "5 mins", "3 D"
    return "1 day", "6 M"


def _bar_stamp(bar: Any) -> str:
    dt = getattr(bar, "date", None)
    if dt is None:
        dt = getattr(bar, "time", None)
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt or "")


def _bar_open(bar: Any) -> Any:
    val = getattr(bar, "open", None)
    if val is None:
        val = getattr(bar, "open_", None)
    return val


def bars_from_ibkr(raw: Any) -> list[dict[str, Any]]:
    """Normalize ib_insync BarData / RealTimeBar list to {t,o,h,l,c,v}."""
    out: list[dict[str, Any]] = []
    for bar in raw or []:
        stamp = _bar_stamp(bar)
        try:
            close = float(getattr(bar, "close"))
        except (TypeError, ValueError):
            continue
        try:
            vol = int(getattr(bar, "volume", 0) or 0)
        except (TypeError, ValueError):
            vol = 0
        row: dict[str, Any] = {"t": stamp, "c": close, "v": vol}
        for src, key in (("high", "h"), ("low", "l")):
            try:
                row[key] = float(getattr(bar, src))
            except (TypeError, ValueError):
                pass
        try:
            row["o"] = float(_bar_open(bar))
        except (TypeError, ValueError):
            pass
        out.append(row)
    return out


def ibkr_bar_freshness(resolution: str) -> str:
    key = str(resolution or "D").strip().upper()
    if key in ("5S", "RT", "RT5"):
        return "ibkr_rt_5s"
    if key in ("D", "1D", "1DAY", "DAY"):
        return "ibkr_rth"
    return "ibkr_rth"
