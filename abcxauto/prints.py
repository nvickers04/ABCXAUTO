"""Congruent market prints — IBKR live and MDA delayed on one schema.

Same symbol, same clock (unix + ISO UTC), same source/freshness/use labels.
Prices for send geometry live under ``ibkr``. MDA is nested context, never ``last``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

USE_IBKR = "ibkr_live_for_decisions"
USE_MDA = "mda_context_not_send_geometry"
USE_MDA_NEWS = "color_not_trigger"

_MDA_MISS: dict[str, float] = {}
_MDA_MISS_TTL_S = 1800.0


def reset_mda_miss_cache() -> None:
    """Tests."""
    _MDA_MISS.clear()


def parse_ibkr_bar_et(value: Any) -> datetime | None:
    """Hist / formatDate=1 wall clock is America/New_York, not UTC.

    Prefer this over ``t_iso`` when grouping a session: a compact
    ``20260825 09:35:00`` stamped UTC would look like 5:35 ET premarket
    and the opening low would be an afternoon bar.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=_ET)
        return value.astimezone(_ET)
    text = str(value).strip()
    if not text:
        return None
    collapsed = " ".join(text.replace("T", " ").replace("Z", "").split())
    if len(collapsed) >= 8 and collapsed[:8].isdigit() and "-" not in collapsed[:8]:
        try:
            if len(collapsed) == 8:
                dt = datetime.strptime(collapsed[:8], "%Y%m%d")
            else:
                dt = datetime.strptime(collapsed[:17], "%Y%m%d %H:%M:%S")
            return dt.replace(tzinfo=_ET)
        except ValueError:
            pass
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=_ET)
    return stamp.astimezone(_ET)


def parse_asof(value: Any) -> datetime | None:
    """Unix seconds/ms, ISO, IBKR ``YYYYMMDD HH:MM:SS``, or datetime → UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:
            n /= 1000.0
        if n < 1e9:
            # Bar indexes / small ints are not Unix asof.
            return None
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return parse_asof(float(text))
    except (TypeError, ValueError):
        pass
    collapsed = " ".join(text.replace("T", " ").replace("Z", "").split())
    if len(collapsed) >= 8 and collapsed[:8].isdigit() and "-" not in collapsed[:8]:
        wall = parse_ibkr_bar_et(text)
        if wall is not None:
            return wall
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def asof_fields(value: Any = None, *, fallback_now: bool = False) -> dict[str, Any]:
    """``asof`` unix seconds + ``asof_iso`` UTC. Empty dict if unknown."""
    dt = parse_asof(value)
    if dt is None and fallback_now:
        dt = datetime.now(timezone.utc)
    if dt is None:
        return {}
    utc = dt.astimezone(timezone.utc)
    return {
        "asof": int(utc.timestamp()),
        "asof_iso": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def stamp(
    row: dict[str, Any] | None,
    *,
    source: str,
    freshness: str,
    use: str,
    asof: Any = None,
    fallback_now: bool = False,
) -> dict[str, Any]:
    out = dict(row) if isinstance(row, dict) else {}
    out["source"] = source
    out["freshness"] = freshness
    out["use"] = use
    extra = asof_fields(asof if asof is not None else out.get("asof") or out.get("updated") or out.get("t"), fallback_now=fallback_now)
    for key, val in extra.items():
        out.setdefault(key, val)
    return out


def bar_time_fields(value: Any) -> dict[str, Any]:
    """OHLCV ``t`` stays the original stamp; add unix/ISO so MDA and IBKR overlay."""
    clock = value
    if isinstance(value, datetime) and value.tzinfo is None:
        clock = value.replace(tzinfo=_ET)
    raw = value
    if hasattr(value, "isoformat"):
        raw = value.isoformat()
    elif value is not None:
        raw = str(value)
    out: dict[str, Any] = {"t": raw or ""}
    extra = asof_fields(clock)
    if extra:
        out["t_unix"] = extra["asof"]
        out["t_iso"] = extra["asof_iso"]
    return out


def ibkr_block(quote: dict[str, Any] | None) -> dict[str, Any]:
    """Live tape subset. Missing ticks omitted."""
    if not isinstance(quote, dict):
        return {}
    out: dict[str, Any] = {
        "source": "ibkr",
        "freshness": str(quote.get("freshness") or "live"),
        "use": USE_IBKR,
    }
    for key in (
        "last",
        "bid",
        "ask",
        "mid",
        "open",
        "close",
        "change_pct",
        "open_gap_pct",
        "asof",
        "asof_iso",
        "iv",
        "delta",
        "gamma",
        "theta",
        "vega",
    ):
        if quote.get(key) not in (None, ""):
            out[key] = quote[key]
    if quote.get("symbol"):
        out["symbol"] = str(quote["symbol"]).upper()
    extra = spread_fields(out.get("bid"), out.get("ask"), out.get("last") or out.get("mid"))
    out.update(extra)
    return out if "last" in out or "mid" in out or "bid" in out else {}


def spread_fields(bid: Any, ask: Any, last: Any = None) -> dict[str, Any]:
    """Live bid/ask width. Missing ticks omitted."""
    try:
        b = float(bid)
        a = float(ask)
    except (TypeError, ValueError):
        return {}
    if b <= 0 or a <= b:
        return {}
    width = round(a - b, 4)
    out: dict[str, Any] = {"spread": width}
    try:
        px = float(last) if last is not None else (a + b) / 2.0
    except (TypeError, ValueError):
        px = (a + b) / 2.0
    if px > 0:
        out["spread_pct"] = round(100.0 * width / px, 4)
    return out


def live_limit_px(quote: dict[str, Any] | None) -> float | None:
    """IBKR mid/last/bid-ask for a resting limit. None if the quote is unusable."""
    if not isinstance(quote, dict) or quote.get("error"):
        return None
    if str(quote.get("source") or "").lower() not in ("", "ibkr"):
        return None
    for key in ("mid", "last"):
        try:
            px = float(quote.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return round(px, 2)
    try:
        bid = float(quote.get("bid") or 0)
        ask = float(quote.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2.0, 2)
    return None


def note_mda_miss(symbol: str) -> None:
    sym = str(symbol or "").upper().strip()
    if sym:
        _MDA_MISS[sym] = time.monotonic()


def mda_recent_miss(symbol: str) -> bool:
    sym = str(symbol or "").upper().strip()
    ts = _MDA_MISS.get(sym)
    if ts is None:
        return False
    if time.monotonic() - ts > _MDA_MISS_TTL_S:
        _MDA_MISS.pop(sym, None)
        return False
    return True


def mda_worth_asking(symbol: str) -> bool:
    """Skip scanner junk and names MDA already 404'd this half hour."""
    from abcxauto.universe import is_common_equity_symbol

    sym = str(symbol or "").upper().strip()
    if not is_common_equity_symbol(sym):
        return False
    return not mda_recent_miss(sym)


def attach_mda_news(rows: list[dict[str, Any]], items: list[dict[str, Any]] | None) -> int:
    """Nest headlines under ``row['mda']['news']`` keyed by symbol."""
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for it in items or []:
        if not isinstance(it, dict) or it.get("error"):
            continue
        sym = str(it.get("symbol") or "").upper().strip()
        if sym:
            by_sym.setdefault(sym, []).append(it)
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        heads = by_sym.get(str(row.get("symbol") or "").upper())
        if not heads:
            continue
        mda = dict(row.get("mda") or {})
        mda["news"] = heads
        mda.setdefault("source", "mda")
        mda.setdefault("freshness", "delayed_15m")
        mda.setdefault("use", USE_MDA)
        mda["news_use"] = USE_MDA_NEWS
        row["mda"] = mda
        n += 1
    return n


def merge_mda_metrics(rows: list[dict[str, Any]], ideas: list[dict[str, Any]] | None) -> int:
    """Nest MDA daily metrics under ``row['mda']``. Never copies ``last``."""
    by_sym: dict[str, dict[str, Any]] = {}
    for idea in ideas or []:
        if not isinstance(idea, dict):
            continue
        sym = str(idea.get("symbol") or "").upper().strip()
        if sym:
            by_sym[sym] = idea
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        idea = by_sym.get(str(row.get("symbol") or "").upper())
        if not idea:
            continue
        mda = dict(row.get("mda") or {})
        for key, val in idea.items():
            if key in ("symbol", "last"):
                continue
            mda[key] = val
        mda.setdefault("source", "mda")
        mda.setdefault("use", USE_MDA)
        row["mda"] = mda
        n += 1
    return n
