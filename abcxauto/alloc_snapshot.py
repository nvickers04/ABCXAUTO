"""One IBKR allocation snapshot per look: quotes, stops, account, daily bars."""

from __future__ import annotations

from typing import Any

__all__ = ("build_allocation_snapshot", "drop_forming_bars")


def _sym(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _account_float(account: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in account and account[key] is not None:
            v = _finite(account[key])
            if v is not None:
                return v
        lower = key.lower()
        for ak, av in account.items():
            if str(ak).lower() == lower and av is not None:
                v = _finite(av)
                if v is not None:
                    return v
                break
    return None


def _position_qty(pos: dict[str, Any]) -> float:
    raw = pos.get("quantity")
    if raw is None:
        raw = pos.get("position")
    if raw is None:
        raw = pos.get("qty")
    v = _finite(raw)
    return 0.0 if v is None else v


def _bar_date(bar: dict[str, Any]) -> str:
    t = bar.get("date") or bar.get("t") or bar.get("t_iso") or ""
    text = str(t).strip()
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text[:10] if text else ""


def _bar_close(bar: dict[str, Any]) -> float | None:
    if bar.get("close") is not None:
        return _finite(bar.get("close"))
    return _finite(bar.get("c"))


def drop_forming_bars(bars: list[Any] | None, today: str) -> list:
    """Drop daily bars dated ``today`` (forming session — not a close)."""
    today_s = str(today or "").strip()[:10]
    out: list[Any] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        day = _bar_date(bar)
        if today_s and day == today_s:
            continue
        out.append(bar)
    return out


def _completed_date_close(bars: list[Any] | None, today: str) -> list[dict[str, Any]]:
    """Completed sessions only: ``{date, close}``. No inherited fills."""
    out: list[dict[str, Any]] = []
    for bar in drop_forming_bars(bars, today):
        if not isinstance(bar, dict):
            continue
        day = _bar_date(bar)
        close = _bar_close(bar)
        if not day or close is None or close <= 0:
            continue
        out.append({"date": day, "close": close})
    return out


def _scan_symbol(raw: Any) -> str:
    """Symbol only from a scan name. Never last / open / gap."""
    if isinstance(raw, dict):
        return _sym(raw.get("symbol") or raw.get("sym") or raw.get("ticker"))
    return _sym(raw)


def _working_stp_aux(orders: list[Any] | None) -> dict[str, float]:
    """Working STP / STP LMT auxPrice keyed by symbol from this order list."""
    out: dict[str, float] = {}
    dead = frozenset(
        {"filled", "cancelled", "canceled", "inactive", "apicancelled", "rejected"}
    )
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "").strip().lower()
        if status in dead:
            continue
        typ = str(
            order.get("orderType")
            or order.get("order_type")
            or order.get("type")
            or ""
        ).strip().upper()
        if typ not in ("STP", "STP LMT"):
            continue
        sym = _sym(order.get("symbol"))
        px = _finite(
            order.get("auxPrice")
            or order.get("aux_price")
            or order.get("stop_price")
            or order.get("stopPrice")
            or order.get("stop")
        )
        if sym and px is not None and px > 0:
            out[sym] = px
    return out


def _quote_asof_iso(quote: dict[str, Any]) -> str:
    raw = quote.get("asof_iso") or quote.get("asof") or ""
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if "T" in text or text.endswith("Z"):
            return text
    if isinstance(raw, (int, float)):
        try:
            from datetime import datetime, timezone

            return (
                datetime.fromtimestamp(float(raw), tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except (OSError, OverflowError, ValueError):
            return ""
    return str(raw).strip() if raw not in (None, "") else ""


def _panel_symbols(
    positions: list[Any] | None,
    scan_symbols: list[Any] | None,
) -> tuple[list[str], dict[str, int]]:
    qty_by: dict[str, int] = {}
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        sym = _sym(pos.get("symbol"))
        if not sym:
            continue
        qty = _position_qty(pos)
        if abs(qty) < 1e-9:
            continue
        qty_by[sym] = int(qty_by.get(sym, 0) + int(qty))
    ordered: list[str] = []
    seen: set[str] = set()
    for sym in ("SPY", *qty_by, *(_scan_symbol(s) for s in (scan_symbols or []))):
        if not sym or sym in seen:
            continue
        seen.add(sym)
        ordered.append(sym)
    return ordered, qty_by


async def _fetch_quotes(
    connector: Any,
    symbols: list[str],
) -> tuple[bool, dict[str, dict[str, Any]], str]:
    """One quote round. Returns (read_this_call, by_symbol, asof_iso)."""
    by_sym: dict[str, dict[str, Any]] = {}
    asof = ""
    get_batch = getattr(connector, "get_live_quotes", None)
    get_one = getattr(connector, "get_live_quote", None)
    if not callable(get_batch) and not callable(get_one):
        return False, by_sym, asof

    rows: list[Any] = []
    if callable(get_batch):
        payload = await get_batch(list(symbols), fresh=True)
        if isinstance(payload, dict):
            rows = list(payload.get("quotes") or [])
        elif isinstance(payload, list):
            rows = payload
    else:
        for sym in symbols:
            row = await get_one(sym, fresh=True)
            rows.append(row)

    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = _sym(row.get("symbol"))
        if not sym:
            continue
        by_sym[sym] = row
        if not asof:
            asof = _quote_asof_iso(row)
    if "SPY" in by_sym:
        spy_asof = _quote_asof_iso(by_sym["SPY"])
        if spy_asof:
            asof = spy_asof
    return True, by_sym, asof


async def _fetch_bars(
    connector: Any,
    symbols: list[str],
    today: str,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {sym: [] for sym in symbols}
    hist = getattr(connector, "get_historical_bars", None)
    if not callable(hist):
        return out
    for sym in symbols:
        try:
            payload = await hist(sym, resolution="D", countback=260)
        except Exception:
            continue
        raw: list[Any] = []
        if isinstance(payload, dict):
            raw = list(payload.get("bars") or [])
        elif isinstance(payload, list):
            raw = payload
        out[sym] = _completed_date_close(raw, today)
    return out


async def build_allocation_snapshot(
    connector: Any,
    *,
    positions: list[Any] | None,
    orders: list[Any] | None,
    account: dict[str, Any] | None,
    scan_symbols: list[Any] | None,
    today: str,
) -> dict[str, Any]:
    """IBKR allocation panel for one look. Scan contributes symbols only."""
    acct = account if isinstance(account, dict) else {}
    today_s = str(today or "").strip()[:10]
    symbols, qty_by = _panel_symbols(positions, scan_symbols)
    stops = _working_stp_aux(orders)

    nl = _account_float(acct, "NetLiquidation", "netliquidation", "net_liquidation")
    cash = _account_float(
        acct, "TotalCashValue", "totalcashvalue", "total_cash", "TotalCash"
    )
    buying_power = _account_float(acct, "AvailableFunds", "availablefunds")

    quotes_read, quotes, asof = await _fetch_quotes(connector, symbols)
    ok = nl is not None and quotes_read

    bars_by = await _fetch_bars(connector, symbols, today_s)
    spy_bars = bars_by.get("SPY") or []
    bar_date = spy_bars[-1]["date"] if spy_bars else ""

    names: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        quote = quotes.get(sym) if quotes_read else None
        last = bid = None
        if quotes_read and isinstance(quote, dict):
            last = _finite(quote.get("last"))
            bid = _finite(quote.get("bid"))
            if last is not None and last <= 0:
                last = None
            if bid is not None and bid <= 0:
                bid = None
        completed = list(bars_by.get(sym) or [])
        last_completed = completed[-1]["date"] if completed else ""
        aligned = bool(bar_date and last_completed and last_completed == bar_date)
        names[sym] = {
            "last": last,
            "bid": bid,
            "stop": stops.get(sym),
            "qty": int(qty_by.get(sym, 0)),
            "bars": completed,
            "aligned": aligned,
        }

    return {
        "ok": bool(ok),
        "asof": asof if ok else "",
        "bar_date": bar_date,
        "nl": nl,
        "cash": cash,
        "buying_power": buying_power,
        "names": names,
    }
