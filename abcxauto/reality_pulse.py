"""Reality Pulse — desktop clock + session view of the IBKR snap.

Built fresh every snap from IBKR + market hours + quote freshness.
Feeds the Market Clock and WorldState.session_status. Not a Grok tool
and not injected as a think. Countdown / freshness / tradable_now ride
on the status tool.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SESSION_LABELS = {
    "premarket": "Pre-market",
    "regular": "Regular",
    "postmarket": "Post-market",
    "closed": "Closed",
}


def _fmt_countdown(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _parse_ts(value: Any) -> datetime | None:
    """Parse MDA/IBKR timestamps (unix, ISO, IBKR bar stamps)."""
    from abcxauto.prints import parse_asof

    return parse_asof(value)


def _age_s(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    try:
        return max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _session_block(now_utc: datetime, hours: dict | None) -> dict:
    """Session status + countdown from market_hours tool or local provider."""
    hours = hours or {}
    session = str(hours.get("session") or "").lower()
    is_trading = hours.get("is_trading_day")
    early = bool(hours.get("early_close"))

    # Prefer live provider when tool payload is thin
    if not session or session == "unknown":
        try:
            from abcxauto.marketdata.market_hours import get_session_info

            hours = {**get_session_info(), **hours}
            session = str(hours.get("session") or "closed").lower()
            if is_trading is None:
                is_trading = hours.get("is_trading_day")
            early = early or bool(hours.get("early_close"))
        except Exception:
            session = session or "closed"

    et = now_utc.astimezone(ET)
    countdown_to = None
    countdown_s = None
    mins_open = hours.get("minutes_to_open")
    mins_close = hours.get("minutes_to_close")
    if session == "regular" and mins_close is not None:
        countdown_to = "close"
        countdown_s = int(float(mins_close) * 60)
    elif session in ("premarket", "closed", "postmarket") and mins_open is not None:
        countdown_to = "open"
        countdown_s = int(float(mins_open) * 60)
    elif session == "regular":
        # Fallback: 16:00 ET close (or 13:00 early)
        close_h, close_m = (13, 0) if early else (16, 0)
        close_et = et.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        countdown_to = "close"
        countdown_s = max(0, int((close_et - et).total_seconds()))
    elif session == "premarket":
        open_et = et.replace(hour=9, minute=30, second=0, microsecond=0)
        countdown_to = "open"
        countdown_s = max(0, int((open_et - et).total_seconds()))

    is_holiday = is_trading is False and et.weekday() < 5
    return {
        "status": session or "closed",
        "label": SESSION_LABELS.get(session, session or "Closed"),
        "is_trading_day": bool(is_trading) if is_trading is not None else et.weekday() < 5,
        "is_holiday": is_holiday,
        "holiday_name": hours.get("holiday_name"),
        "early_close": early,
        "countdown_to": countdown_to,
        "countdown_s": countdown_s,
        "countdown_human": _fmt_countdown(countdown_s),
        "exchange": hours.get("exchange") or "NYSE",
        "current_time_et": hours.get("current_time_et") or et.strftime("%H:%M:%S"),
    }


def _tradable_now(session: str) -> dict:
    s = (session or "closed").lower()
    equity_rth = s == "regular"
    equity_ext = s in ("premarket", "postmarket")
    if equity_rth:
        liq = "full"
    elif equity_ext:
        liq = "thin"
    else:
        liq = "closed"
    return {
        "equity_rth": equity_rth,
        "equity_extended": equity_ext,
        "options": equity_rth,  # conservative: RTH only for equity options
        "futures_overnight": not equity_rth,
        "liquidity_flag": liq,
        "notes": (
            "Full RTH liquidity"
            if equity_rth
            else (
                "Extended hours — thinner liquidity"
                if equity_ext
                else "Cash equity session closed; prefer hold or futures only"
            )
        ),
    }


def _ledger_rows(positions: list) -> list[dict]:
    rows = []
    for p in positions or []:
        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
        row = {
            "conId": p.get("conId") or p.get("con_id"),
            "symbol": p.get("symbol"),
            "secType": sec,
            "quantity": p.get("quantity", p.get("position")),
            "avgCost": p.get("avgCost", p.get("avg_cost", p.get("averageCost"))),
            "marketPrice": p.get("marketPrice", p.get("market_price")),
            "marketValue": p.get("marketValue", p.get("market_value")),
            "unrealizedPNL": p.get("unrealizedPNL", p.get("unrealized_pnl")),
            "realizedPNL": p.get("realizedPNL", p.get("realized_pnl")),
            "multiplier": p.get("multiplier"),
            "exchange": p.get("exchange") or p.get("primaryExchange"),
            "currency": p.get("currency") or "USD",
        }
        if sec.startswith("OPT"):
            row["expiry"] = p.get("expiration") or p.get("lastTradeDateOrContractMonth")
            row["strike"] = p.get("strike")
            row["right"] = p.get("right")
        rows.append(row)
    return rows


def _account_summary(acct: dict | None) -> dict:
    acct = acct or {}
    keys = (
        "netliquidation",
        "NetLiquidation",
        "totalcashvalue",
        "TotalCashValue",
        "buyingpower",
        "BuyingPower",
        "availablefunds",
        "AvailableFunds",
        "maintmarginreq",
        "MaintMarginReq",
        "unrealizedpnl",
        "UnrealizedPnL",
        "realizedpnl",
        "RealizedPnL",
    )
    out = {}
    for k in keys:
        if acct.get(k) is not None:
            out[k.lower()] = acct.get(k)
    return out or dict(acct)


def build_narrative(pulse: dict) -> str:
    """One-line 'Current reality: …' the agent must echo in reasoning."""
    t = pulse.get("time") or {}
    sess = pulse.get("session") or {}
    ledger = pulse.get("position_ledger") or []
    fresh = pulse.get("data_freshness") or {}
    bits = [
        f"{t.get('local_clock', '?')} {t.get('timezone', 'ET')}",
        f"{t.get('day_of_week', '?')}",
        f"{sess.get('label', '?')} session",
    ]
    if sess.get("countdown_to") and sess.get("countdown_human"):
        bits.append(f"{sess['countdown_to']} in {sess['countdown_human']}")
    age = fresh.get("mda_spy_quote_age_s")
    spy_src = (fresh.get("sources") or {}).get("spy")
    if spy_src == "ibkr_live":
        bits.append("SPY from IBKR live")
    elif age is not None:
        bits.append(f"MDA data {age:.0f}s old")
    else:
        bits.append("MDA age n/a")
    if not ledger:
        bits.append("positions: (none)")
    else:
        pos_bits = []
        for r in ledger[:6]:
            qty = r.get("quantity")
            try:
                qty_s = f"{float(qty):+g}"
            except (TypeError, ValueError):
                qty_s = str(qty)
            leg = f"conId={r.get('conId')} {r.get('symbol')} {r.get('secType')} {qty_s}"
            if str(r.get("secType", "")).startswith("OPT"):
                leg += f" {r.get('expiry', '')} {r.get('strike', '')}{r.get('right', '')}"
            pos_bits.append(leg)
        bits.append("positions: " + " | ".join(pos_bits))
    return "Current reality: " + ", ".join(bits)


def build_reality_pulse(
    *,
    account: dict | None = None,
    positions: list | None = None,
    open_orders: list | None = None,
    market_hours: dict | None = None,
    spy_quote: dict | None = None,
    vix_quote: dict | None = None,
    protection: dict | None = None,
    ibkr_connected: bool | None = None,
    taken_at: str | None = None,
) -> dict:
    """Assemble the Reality Pulse JSON — heart of every decision cycle."""
    now = datetime.now(timezone.utc)
    et = now.astimezone(ET)
    sess = _session_block(now, market_hours)
    spy = spy_quote or {}
    spy_src = str(spy.get("source") or "").lower()
    quote_ts = _parse_ts(
        spy.get("asof")
        or spy.get("asof_iso")
        or spy.get("timestamp")
        or spy.get("updated")
        or spy.get("time")
    )
    mda_age = _age_s(quote_ts, now)
    ibkr_spy = spy_src.startswith("ibkr")
    snapshot_ts = _parse_ts(taken_at) or now
    snap_age = _age_s(snapshot_ts, now) or 0.0

    vix_level = None
    if vix_quote:
        for k in ("last", "price", "close", "mid"):
            if vix_quote.get(k) is not None:
                try:
                    vix_level = float(vix_quote[k])
                    break
                except (TypeError, ValueError):
                    pass

    ledger = _ledger_rows(positions or [])
    tradable = _tradable_now(sess.get("status", "closed"))
    acct = _account_summary(account)

    pulse: dict[str, Any] = {
        "time": {
            "utc": now.isoformat(),
            "local": et.isoformat(),
            "local_clock": et.strftime("%I:%M%p").lstrip("0").lower() + " EDT"
            if et.dst()
            else et.strftime("%I:%M%p").lstrip("0").lower() + " EST",
            "timezone": "America/New_York",
            "day_of_week": et.strftime("%A"),
            "date": et.date().isoformat(),
        },
        "calendar": {
            "is_trading_day": sess.get("is_trading_day"),
            "is_holiday": sess.get("is_holiday"),
            "holiday_name": sess.get("holiday_name"),
            "early_close": sess.get("early_close"),
        },
        "session": {
            "status": sess.get("status"),
            "label": sess.get("label"),
            "countdown_to": sess.get("countdown_to"),
            "countdown_s": sess.get("countdown_s"),
            "countdown_human": sess.get("countdown_human"),
            "exchange": sess.get("exchange"),
        },
        "tradable_now": tradable,
        "data_freshness": {
            "pulse_built_at": now.isoformat(),
            "mda_spy_quote_age_s": mda_age,
            "ibkr_snapshot_age_s": snap_age,
            "ibkr_connected": ibkr_connected,
            "sources": {
                "mda_spy": (
                    "unused"
                    if ibkr_spy
                    else (
                        "fresh"
                        if mda_age is not None and mda_age < 30
                        else ("stale" if mda_age is not None else "unknown")
                    )
                ),
                "spy": "ibkr_live" if ibkr_spy else ("mda" if spy else "unknown"),
                "ibkr": "live" if ibkr_connected else "unknown",
            },
            "spy_last": spy.get("last") or spy.get("price"),
            "vix": vix_level,
        },
        "position_ledger": ledger,
        "account": acct,
        "open_orders_count": len(open_orders or []),
        "protection": {
            "unprotected_symbols": (protection or {}).get("unprotected_symbols") or [],
        },
    }
    pulse["narrative"] = build_narrative(pulse)
    pulse["awareness_checklist"] = [
        "Is this instrument tradable right now given session?",
        "Is data fresh enough (MDA / IBKR ages)?",
        "Does the action respect session liquidity?",
        "If closing: exact conId match against position_ledger (never symbol alone)?",
        "After close: target conId → zero, no other conIds touched?",
    ]
    return pulse


def pulse_clock_view(pulse: dict | None) -> dict:
    """Compact fields for the Market Clock widget."""
    pulse = pulse or {}
    t = pulse.get("time") or {}
    s = pulse.get("session") or {}
    f = pulse.get("data_freshness") or {}
    mda_age = f.get("mda_spy_quote_age_s")
    ibkr_age = f.get("ibkr_snapshot_age_s")
    to = s.get("countdown_to")
    human = s.get("countdown_human")
    return {
        "clock": t.get("local_clock") or "—",
        "day": t.get("day_of_week") or "—",
        "session": s.get("label") or "—",
        "session_status": s.get("status") or "closed",
        "countdown": (
            f"{to} {human}".strip() if human else "—"
        ),
        "countdown_to": to,
        "countdown_human": human or "—",
        "data_age": f"{mda_age:.0f}s" if mda_age is not None else "n/a",
        "ibkr_refresh": f"{ibkr_age:.0f}s" if ibkr_age is not None else "n/a",
        "narrative": pulse.get("narrative") or "—",
    }
