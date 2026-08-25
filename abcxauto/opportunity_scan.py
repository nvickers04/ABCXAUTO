"""SCAN TAPE — unranked MDA metrics for Grok-operated scanning.

Code fetches candle metrics (typically delayed). Never places orders.
Internal list field remains ``opportunities`` for journal/UI compat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from abcxauto.prints import USE_MDA, asof_fields, ibkr_block, parse_ibkr_bar_et

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "ideas": []}
_CACHE_TTL_S = 150.0
# Seed tape size matches the book tool payload (world_state).
TAPE_SEED_CAP = 12
# Top-N screen hits that get an IBKR last stamped on in the same call.
SCAN_QUOTE_CAP = 12

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

_DAILY_RES = frozenset({"D", "1D", "W", "1W", "M", "1M"})


def mda_bar_freshness(resolution: str | None) -> str:
    """Daily/weekly/monthly bars are not a 15-minute delayed last."""
    res = (resolution or "D").strip().upper() or "D"
    if res in _DAILY_RES:
        return "delayed_daily"
    return "delayed_15m"


def mda_last_kind(resolution: str | None) -> str:
    res = (resolution or "D").strip().upper() or "D"
    if res in ("D", "1D"):
        return "daily_bar_close"
    if res in ("W", "1W"):
        return "weekly_bar_close"
    if res in ("M", "1M"):
        return "monthly_bar_close"
    return "intrabar_close"


def scan_fetch_cap() -> int:
    raw = (os.environ.get("ABCXAUTO_SCAN_FETCH_CAP") or "").strip()
    if not raw:
        try:
            from abcxauto.config import get_config

            return max(1, int(getattr(get_config(), "scan_fetch_cap", 8) or 8))
        except Exception:
            return 8
    try:
        return max(1, min(32, int(raw)))
    except ValueError:
        return 8


def normalize_tickers(raw: Any, *, cap: int | None = None) -> list[str]:
    """Uppercase, dedupe, regex-validate; apply fetch cap."""
    limit = scan_fetch_cap() if cap is None else max(1, int(cap))
    out: list[str] = []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    for item in items:
        sym = str(item or "").upper().strip()
        if not sym or not _TICKER_RE.match(sym):
            continue
        if sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def _universe(positions: list[dict] | None, *, cap: int = TAPE_SEED_CAP) -> list[str]:
    """Book symbols (manage) + Universe sandbox legal set (unranked)."""
    out: list[str] = []
    for p in positions or []:
        sym = str((p or {}).get("symbol") or "").upper().strip()
        if sym and _TICKER_RE.match(sym) and sym not in out:
            out.append(sym)
    try:
        from abcxauto.universe import legal_symbols

        for sym in legal_symbols():
            if sym not in out:
                out.append(sym)
            if len(out) >= max(1, int(cap)):
                break
    except Exception:
        logger.exception("legal universe load failed")
        for sym in ("SPY", "QQQ", "IWM"):
            if sym not in out:
                out.append(sym)
    # Book first, then legal-set order — never alphabetize (A* tape bias).
    return out[: max(1, int(cap))]


def tape_seed_symbols(
    positions: list[dict] | None = None,
    *,
    cap: int = TAPE_SEED_CAP,
) -> list[str]:
    """Unranked day tape seed: open book first, then legal watchlist. Not a rank.

    Kept for internal/cache callers. format_wake must not print these names;
    empty scan() must not seed from this list.
    """
    return _universe(positions, cap=cap)


def _book_symbols(positions: list[dict] | None) -> set[str]:
    out: set[str] = set()
    for p in positions or []:
        sym = str((p or {}).get("symbol") or "").upper().strip()
        if sym and _TICKER_RE.match(sym):
            out.add(sym)
    return out


def overlay_hits(
    symbols: list[str],
    *,
    positions: list[dict] | None = None,
    turn_symbols: list[str] | None = None,
    scanner_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Hit rows: symbol + on_book, plus whatever the scanner already reported.

    ``scanner_rows`` carries IBKR's own rank and scanCode metric so a screen is
    triageable without spending a quote round on every name.
    """
    on_book = _book_symbols(positions)
    in_turn = {
        str(s or "").upper().strip()
        for s in (turn_symbols or [])
        if str(s or "").strip()
    }
    facts: dict[str, dict[str, Any]] = {}
    for row in scanner_rows or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if sym:
            facts[sym] = row
    rows: list[dict[str, Any]] = []
    for sym in symbols:
        s = str(sym or "").upper().strip()
        if not s:
            continue
        row: dict[str, Any] = {"symbol": s, "on_book": s in on_book}
        extra = facts.get(s) or {}
        for key in ("rank", "distance", "benchmark", "projection", "legs"):
            if extra.get(key) not in (None, ""):
                row[key] = extra[key]
        if in_turn:
            row["in_turn"] = s in in_turn
        rows.append(row)
    return rows


def scan_quote_cap() -> int:
    """How many top hits get an IBKR last attached. 0 disables the sweep."""
    raw = (os.environ.get("ABCXAUTO_SCAN_QUOTE_CAP") or "").strip()
    if not raw:
        return SCAN_QUOTE_CAP
    try:
        return max(0, min(24, int(raw)))
    except ValueError:
        return SCAN_QUOTE_CAP


async def attach_live_quotes(
    rows: list[dict[str, Any]],
    *,
    connector: Any = None,
    cap: int | None = None,
) -> int:
    """Stamp IBKR last/bid/ask onto the top hits in place. Returns rows quoted.

    A screen with no prices costs Grok a quote round per name. IBKR is already
    connected here, so the sweep is one batched call.
    """
    limit = scan_quote_cap() if cap is None else max(0, int(cap))
    if not rows or limit <= 0 or connector is None:
        return 0
    targets = [str(r.get("symbol") or "") for r in rows[:limit] if r.get("symbol")]
    if not targets:
        return 0
    batch = getattr(connector, "get_live_quotes", None)
    single = getattr(connector, "get_live_quote", None)
    payload: Any = None
    try:
        if callable(batch):
            payload = await batch(targets)
        elif callable(single):
            payload = {
                "quotes": list(
                    await asyncio.gather(
                        *[single(s) for s in targets], return_exceptions=True
                    )
                )
            }
    except Exception:
        logger.exception("scan quote sweep failed")
        return 0
    quotes: list[Any]
    if isinstance(payload, dict):
        quotes = list(payload.get("quotes") or [payload])
    elif isinstance(payload, list):
        quotes = list(payload)
    else:
        return 0
    by_sym: dict[str, dict[str, Any]] = {}
    for q in quotes:
        if not isinstance(q, dict):
            continue
        sym = str(q.get("symbol") or "").upper().strip()
        if sym:
            by_sym[sym] = q
    n = 0
    for row in rows:
        q = by_sym.get(str(row.get("symbol") or "").upper())
        if not q:
            continue
        last = q.get("last") if q.get("last") is not None else q.get("mid")
        try:
            px = float(last)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        row["last"] = px
        for key in ("bid", "ask", "open", "close", "change_pct", "open_gap_pct"):
            if q.get(key) is not None:
                row[key] = q[key]
        from abcxauto.prints import spread_fields

        row.update(spread_fields(row.get("bid"), row.get("ask"), px))
        row["quote_source"] = "ibkr_live"
        live = ibkr_block(q)
        if live:
            live["last"] = px
            row["ibkr"] = live
        n += 1
    return n


async def attach_mda_metrics(
    rows: list[dict[str, Any]],
    *,
    cap: int = 8,
) -> int:
    """Nest delayed daily metrics on the top hits. Never writes ``last``."""
    from abcxauto.prints import merge_mda_metrics, mda_worth_asking

    targets = [
        str(r.get("symbol") or "")
        for r in (rows or [])[: max(0, int(cap))]
        if r.get("symbol") and mda_worth_asking(str(r.get("symbol") or ""))
    ]
    if not targets:
        return 0
    ideas = await fetch_scan_metrics(targets, cap=cap)
    return merge_mda_metrics(rows, ideas)


async def criteria_scan(
    *,
    arena: str | None = None,
    scan_code: str | None = None,
    symbols: list[str] | None = None,
    positions: list[dict] | None = None,
    connector: Any = None,
    turn_symbols: list[str] | None = None,
    cap: int | None = None,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One screen this look: arena | scan_code | symbols[]. No persist, no MDA daily-120."""
    asked = normalize_tickers(symbols or [], cap=cap)
    has_arena = bool(str(arena or "").strip())
    has_code = bool(str(scan_code or "").strip())
    filt = filters if isinstance(filters, dict) else None
    if not has_arena and not has_code and not asked:
        return {
            "ok": False,
            "error": "scan requires arena | scan_code | symbols[]",
        }

    hits_syms: list[str] = []
    scanner_rows: list[dict[str, Any]] = []
    source = "symbols"
    arena_id = None
    code_out = None
    applied: dict[str, Any] = dict((filt or {}).get("applied") or {})
    if has_arena or has_code:
        from abcxauto.universe import pull_one_screen

        pulled = await pull_one_screen(
            connector,
            arena=arena if has_arena else None,
            scan_code=scan_code if has_code else None,
            filters=filt,
        )
        if not pulled.get("ok"):
            err: dict[str, Any] = {
                "ok": False,
                "error": pulled.get("error") or "unknown screen",
                "arenas": pulled.get("arenas"),
            }
            if pulled.get("applied") is not None:
                err["applied"] = pulled.get("applied")
            return err
        hits_syms = list(pulled.get("symbols") or [])
        scanner_rows = list(pulled.get("rows") or [])
        source = str(pulled.get("source") or "empty")
        arena_id = pulled.get("arena_id")
        code_out = pulled.get("scan_code")
        applied = dict(pulled.get("applied") or applied)
    elif filt and (filt.get("applied") or filt.get("native") or filt.get("tags")):
        return {
            "ok": False,
            "error": "scan filters require arena | scan_code",
            "applied": applied,
        }
    else:
        hits_syms = list(asked)

    rows = overlay_hits(
        hits_syms,
        positions=positions,
        turn_symbols=turn_symbols,
        scanner_rows=scanner_rows,
    )
    quoted = await attach_live_quotes(rows, connector=connector)
    try:
        from abcxauto.lab_playbook import drop_hits_off_card

        rows, dropped = drop_hits_off_card(rows)
        hits_syms = [str(r.get("symbol") or "") for r in rows if r.get("symbol")]
        if dropped:
            applied = dict(applied)
            applied["dropped"] = dropped[:16]
        quoted = sum(
            1
            for r in rows
            if r.get("last") is not None
            or (isinstance(r.get("ibkr"), dict) and r["ibkr"].get("last") is not None)
        )
    except Exception:
        logger.debug("card hit drop failed", exc_info=True)
    ranked = bool(scanner_rows) and source == "ibkr"
    return {
        "ok": True,
        "source": source,
        "arena": arena_id,
        "scan_code": code_out,
        "symbols": [r["symbol"] for r in rows],
        "hits": rows,
        "applied": applied,
        "persisted": False,
        "ranked": ranked,
        "rank_meaning": (
            "IBKR scanCode sort order; distance/benchmark are that code's metric"
            if ranked
            else "not ranked"
        ),
        "quoted": quoted,
    }


def _closes(candles: list[dict]) -> list[float]:
    out: list[float] = []
    for row in candles or []:
        try:
            c = float(row.get("c"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            out.append(c)
    return out


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    window = values[-n:]
    return sum(window) / float(n)


def metrics_for_symbol(
    candles: list[dict],
    symbol: str,
    *,
    resolution: str = "D",
) -> dict[str, Any] | None:
    """Raw MDA candle metrics — no score, no shell bias tip."""
    closes = _closes(candles)
    if len(closes) < 30:
        return None
    last = closes[-1]
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50) if len(closes) >= 50 else _sma(closes, 30)
    if sma20 is None or last <= 0:
        return None
    ret5 = (last / closes[-6] - 1.0) if len(closes) >= 6 else 0.0
    dist20 = (last - sma20) / sma20
    last_t = None
    for row in reversed(candles or []):
        try:
            c = float(row.get("c"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            last_t = row.get("t")
            break
    res = (resolution or "D").strip() or "D"
    extra = asof_fields(last_t)
    return {
        "symbol": str(symbol or "").upper(),
        "mda_last": round(last, 4),
        "mda_last_is": mda_last_kind(res),
        "mda_last_t": last_t,
        "bar": res,
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4) if sma50 is not None else None,
        "dist20": round(dist20, 5),
        "ret5": round(ret5, 5),
        "above_sma20": bool(last >= sma20),
        "source": "mda",
        "freshness": mda_bar_freshness(res),
        "use": USE_MDA,
        **extra,
    }


def _gap_levels(open_px: float, gap_pct: Any) -> dict[str, Any]:
    """Prior close and 30/50 retrace of the open gap. Tape math, not a ticket."""
    try:
        pct = float(gap_pct)
    except (TypeError, ValueError):
        return {}
    if pct <= -100.0:
        return {}
    prior = open_px / (1.0 + pct / 100.0)
    fill = prior - open_px
    return {
        "prior_close": round(prior, 4),
        "gap_pts": round(open_px - prior, 4),
        "gap_pct": round(pct, 4),
        "retrace_30": round(open_px + 0.3 * fill, 4),
        "retrace_50": round(open_px + 0.5 * fill, 4),
    }


_RTH_START_MIN = 9 * 60 + 30
_RTH_END_MIN = 16 * 60


def _bar_has_clock(raw: str) -> bool:
    return "T" in raw or " " in raw or ":" in raw


def _bar_et(bar: dict[str, Any]) -> datetime | None:
    """Session clock from the original print. ``t_iso`` is last — it can be UTC-wrong."""
    wall = str(bar.get("t") or bar.get("date") or "").strip()
    if wall:
        return parse_ibkr_bar_et(wall)
    iso = str(bar.get("t_iso") or "").strip()
    return parse_ibkr_bar_et(iso) if iso else None


def _bar_minutes_et(bar: dict[str, Any]) -> int | None:
    wall = str(bar.get("t") or bar.get("date") or "").strip()
    if wall and not _bar_has_clock(wall):
        return None
    stamp = _bar_et(bar)
    if stamp is None:
        return None
    return stamp.hour * 60 + stamp.minute


def _rth_bars(session: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """RTH bars if any timed prints exist. Premarket-only is not the opening low."""
    rth: list[dict[str, Any]] = []
    timed: list[dict[str, Any]] = []
    untimed: list[dict[str, Any]] = []
    for bar in session:
        mins = _bar_minutes_et(bar)
        if mins is None:
            untimed.append(bar)
            continue
        timed.append(bar)
        if _RTH_START_MIN <= mins < _RTH_END_MIN:
            rth.append(bar)
    if rth:
        return rth, "rth"
    if timed:
        return timed, "premarket"
    return untimed or session, "daily"


def _et_calendar_day(now: datetime | date | None = None) -> str:
    if isinstance(now, date) and not isinstance(now, datetime):
        return now.isoformat()
    clock = now if isinstance(now, datetime) else datetime.now(ZoneInfo("America/New_York"))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        clock = clock.astimezone(ZoneInfo("America/New_York"))
    return clock.date().isoformat()


def _prior_rth_close(
    rows: list[dict[str, Any]],
    day: str,
    day_of,
    px,
) -> float | None:
    """Last RTH close before ``day``. The card's gap is vs this print."""
    if not day:
        return None
    prior = [b for b in rows if day_of(b) and day_of(b) < day]
    if not prior:
        return None
    prev_day = day_of(prior[-1])
    prev = [b for b in prior if day_of(b) == prev_day]
    prev, _kind = _rth_bars(prev)
    closes = [p for p in (px(b, "c", "close") for b in prev) if p is not None]
    if not closes:
        return None
    last = closes[-1]
    return last if last > 0 else None


def session_range_from_bars(
    bars: list[dict[str, Any]] | None,
    *,
    last: Any = None,
    open_gap_pct: Any = None,
    rth_open: Any = None,
    now: datetime | date | None = None,
) -> dict[str, Any] | None:
    """Open / high / low / last for the last calendar day in the series.

    Facts only. The card's opening-low stop is this low, not SMA.
    ``today`` is whether that bar date is the current New York session date.
    ``rth_open`` is the ticker / scan regular-session open. A midday 5-min
    window's first bar is not that print — hold-above-open uses this.
    """
    rows = [b for b in (bars or []) if isinstance(b, dict)]
    if not rows:
        return None

    def _day(bar: dict[str, Any]) -> str:
        stamp = _bar_et(bar)
        if stamp is not None:
            return stamp.date().isoformat()
        raw = str(bar.get("t") or bar.get("date") or bar.get("t_iso") or "")
        return raw[:10] if len(raw) >= 10 and raw[4:5] == "-" else ""

    def _px(bar: dict[str, Any], *keys: str) -> float | None:
        for key in keys:
            if bar.get(key) is not None:
                try:
                    return float(bar[key])
                except (TypeError, ValueError):
                    continue
        return None

    day = _day(rows[-1])
    session = [b for b in rows if _day(b) == day] if day else rows
    if not session:
        session = rows
    session, kind = _rth_bars(session)
    opens = [p for p in (_px(b, "o", "open") for b in session) if p is not None]
    highs = [p for p in (_px(b, "h", "high") for b in session) if p is not None]
    lows = [p for p in (_px(b, "l", "low") for b in session) if p is not None]
    closes = [p for p in (_px(b, "c", "close") for b in session) if p is not None]
    if not opens or not lows:
        return None
    last_px = None
    if last is not None:
        try:
            last_px = float(last)
        except (TypeError, ValueError):
            last_px = None
    if last_px is None and closes:
        last_px = closes[-1]
    open_px = opens[0]
    if rth_open is not None:
        try:
            pinned = float(rth_open)
        except (TypeError, ValueError):
            pinned = None
        if pinned is not None and pinned > 0:
            open_px = pinned
    gap = open_gap_pct
    if gap is None and kind != "premarket":
        prior = _prior_rth_close(rows, day, _day, _px)
        if prior:
            gap = (open_px / prior - 1.0) * 100.0
    high_px = max(highs) if highs else None
    if high_px is not None:
        high_px = max(high_px, open_px)
    out: dict[str, Any] = {
        "date": day or None,
        "open": open_px,
        "high": high_px,
        "low": min(lows),
        "last": last_px,
        "n": len(session),
    }
    if last_px is not None:
        out["vs_open"] = round(last_px - open_px, 4)
        out["vs_low"] = round(last_px - min(lows), 4)
        out["above_open"] = last_px >= open_px
        out["above_low"] = last_px > min(lows)
    out.update(_gap_levels(open_px, gap))
    if day:
        out["today"] = day == _et_calendar_day(now)
    if kind == "premarket":
        out["today"] = False
        out["rth"] = False
    elif kind == "rth":
        out["rth"] = True
    return out


def rth_now(*, now: datetime | None = None) -> bool:
    """True during the NYSE regular session on a weekday."""
    clock = now if isinstance(now, datetime) else datetime.now(ZoneInfo("America/New_York"))
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ZoneInfo("America/New_York"))
    else:
        clock = clock.astimezone(ZoneInfo("America/New_York"))
    if clock.weekday() >= 5:
        return False
    mins = clock.hour * 60 + clock.minute
    return _RTH_START_MIN <= mins < _RTH_END_MIN


def session_range_from_live_open(
    *,
    last: Any,
    rth_open: Any,
    open_gap_pct: Any = None,
    now: datetime | None = None,
    regular: bool | None = None,
) -> dict[str, Any] | None:
    """Today's RTH open from the live quote when hist has no completed bar yet.

    IBKR 5-min useRTH hist does not include the in-progress 09:30 bar, so a
    look at 09:30:20 would otherwise pin yesterday. The ticker open is the
    regular-session open. Premarket must not use this.
    """
    if regular is False:
        return None
    if regular is not True and not rth_now(now=now):
        return None
    try:
        last_px = float(last)
        open_px = float(rth_open)
    except (TypeError, ValueError):
        return None
    if last_px <= 0 or open_px <= 0:
        return None
    # Ticker open that has not rolled to today's RTH print still shows
    # yesterday. At the bell, last is next to the open; a 12%+ gap
    # between them is a stale open, not the card's hold.
    if abs(last_px - open_px) / open_px > 0.12:
        return None
    low = min(open_px, last_px)
    high = max(open_px, last_px)
    out: dict[str, Any] = {
        "date": _et_calendar_day(now),
        "open": open_px,
        "high": high,
        "low": low,
        "last": last_px,
        "n": 1,
        "today": True,
        "rth": True,
        "print": "live_open",
        "vs_open": round(last_px - open_px, 4),
        "vs_low": round(last_px - low, 4),
        "above_open": last_px >= open_px,
        "above_low": last_px > low,
    }
    out.update(_gap_levels(open_px, open_gap_pct))
    return out


def structure_from_bars(
    candles: list[dict],
    symbol: str,
    *,
    resolution: str = "D",
    source: str = "ibkr",
    freshness: str = "ibkr_rth",
) -> dict[str, Any] | None:
    """Same sma/dist/ret keys as MDA metrics; last is ``bar_last`` when not MDA."""
    idea = metrics_for_symbol(candles, symbol, resolution=resolution)
    if not idea:
        return None
    out = dict(idea)
    out["source"] = source
    out["freshness"] = freshness
    if str(source).lower() != "mda":
        out["bar_last"] = out.pop("mda_last", None)
        out["bar_last_is"] = out.pop("mda_last_is", None)
        out["bar_last_t"] = out.pop("mda_last_t", None)
        out["use"] = "ibkr_rth_structure"
    return out


def merge_tape(
    base: list[dict[str, Any]], extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Dedupe by symbol; keep first-seen order (book seed before legal set)."""
    by_sym: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in list(base or []) + list(extra or []):
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        if sym not in by_sym:
            order.append(sym)
        by_sym[sym] = row
    return [by_sym[k] for k in order]


async def fetch_scan_metrics(
    symbols: list[str] | None,
    *,
    cap: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch MDA candle metrics for Grok-proposed or seed symbols."""
    syms = normalize_tickers(symbols or [], cap=cap)
    if not syms:
        return []
    ideas: list[dict[str, Any]] = []
    try:
        from abcxauto.marketdata.client import get_marketdata_client

        client = get_marketdata_client()
        configured = getattr(client, "is_configured", False)
        if callable(configured):
            configured = configured()
        if not configured:
            return []

        async def _one(sym: str) -> dict[str, Any] | None:
            try:
                candles = await client.get_stock_candles(
                    sym, resolution="D", countback=120
                )
            except Exception:
                logger.exception("fetch_scan_metrics candles failed for %s", sym)
                candles = []
            return metrics_for_symbol(candles or [], sym, resolution="D")

        rows = await asyncio.gather(*[_one(s) for s in syms], return_exceptions=True)
        for row in rows:
            if isinstance(row, dict) and row.get("symbol"):
                ideas.append(row)
            elif isinstance(row, Exception):
                logger.exception("fetch_scan_metrics gather failed: %s", row)
    except Exception:
        logger.exception("fetch_scan_metrics failed")
        return []
    return merge_tape([], ideas)


async def scan_opportunities(
    positions: list[dict] | None = None,
    *,
    force: bool = False,
    cap: int = TAPE_SEED_CAP,
) -> list[dict[str, Any]]:
    """Seed SCAN TAPE: book + Universe sandbox legal set, unranked (cached)."""
    symbols = _universe(positions, cap=cap)
    key = ",".join(symbols)
    now = time.monotonic()
    if (
        not force
        and _CACHE["ideas"]
        and _CACHE.get("key") == key
        and (now - float(_CACHE["ts"])) < _CACHE_TTL_S
    ):
        return list(_CACHE["ideas"])

    ideas = await fetch_scan_metrics(symbols, cap=cap)
    _CACHE.update(ts=now, key=key, ideas=list(ideas))
    return ideas


def reset_opportunity_cache() -> None:
    """Tests."""
    _CACHE.update(ts=0.0, key="", ideas=[])
