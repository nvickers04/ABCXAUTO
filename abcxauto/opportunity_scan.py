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
    """Max MDA symbols per scan. Goes through ``get_config()`` so Settings /
    ``self_tune`` beat a leftover env value.
    """
    try:
        from abcxauto.config import get_config

        raw = int(getattr(get_config(), "scan_fetch_cap", 8) or 8)
        return max(1, min(32, raw))
    except Exception:
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
    """Book symbols only. No watchlist, no invented SPY/QQQ/IWM."""
    out: list[str] = []
    for p in positions or []:
        sym = str((p or {}).get("symbol") or "").upper().strip()
        if sym and _TICKER_RE.match(sym) and sym not in out:
            out.append(sym)
        if len(out) >= max(1, int(cap)):
            break
    return out[: max(1, int(cap))]


def tape_seed_symbols(
    positions: list[dict] | None = None,
    *,
    cap: int = TAPE_SEED_CAP,
) -> list[str]:
    """Unranked day tape seed: open book only. Not a rank.

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
        for key in (
            "rank",
            "distance",
            "benchmark",
            "projection",
            "legs",
            "last",
            "volume",
            "market_cap",
            "stock_type",
            "long_name",
            "scan_code",
            "screen",
            "metric_name",
            "metric_value",
            "skip_class",
        ):
            if extra.get(key) not in (None, ""):
                row[key] = extra[key]
        if in_turn:
            row["in_turn"] = s in in_turn
        rows.append(row)
    return rows


# Ranked screen row. last/volume/market_cap only when the scanner supplied them.
RANKED_ROW_KEYS = frozenset(
    {
        "symbol",
        "rank",
        "screen",
        "scan_code",
        "metric_name",
        "metric_value",
        "gap_pct",
        "last",
        "volume",
        "market_cap",
        "stock_type",
        "skip_class",
        "source",
    }
)
# Compat alias — old tests / painters still import this name.
THIN_RANKED_KEYS = RANKED_ROW_KEYS
_FAT_SCAN_KEYS = frozenset(
    {
        "bid",
        "ask",
        "open",
        "close",
        "ibkr",
        "mda",
        "session",
        "spread",
        "spread_pct",
        "quote_source",
    }
)
# Real open-gap fields only. Scanner distance / generic gap_pct / change are
# not an open gap unless universe.scan_metric_name says this screen is a gap.
_OPEN_GAP_KEYS = ("gap%", "open_gap_pct")
_GAP_SCREEN_DISTANCE_KEYS = ("gap_pct", "distance", "metric_value")
_GAP_METRIC_NAMES = frozenset({"open_gap", "open_percent_change"})
_GAP_SCAN_CODES = frozenset(
    {
        "HIGH_OPEN_GAP",
        "LOW_OPEN_GAP",
        "TOP_OPEN_PERC_GAIN",
        "TOP_OPEN_PERC_LOSE",
    }
)
THIN_RANK_MEANING = (
    "metric_name/metric_value are IBKR distance|benchmark for this scanCode; "
    "skip_class is levered|micro|empty; last only if the scanner supplied it"
)
SILENT_SCAN_NOTE = (
    "flush default screens (MOST_ACTIVE, TOP_PERC_LOSE, TOP_PERC_GAIN); "
    "pass arena|scan_code to state criteria and order — not a fat dump"
)


def parse_scan_gap(raw: Any) -> float | None:
    """Parse a scanner/quote metric into a percent-like number."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if val == val else None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        val = float(text)
    except ValueError:
        return None
    return val if val == val else None


def _first_gap(src: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        val = parse_scan_gap(src.get(key))
        if val is not None:
            return val
    return None


def _scan_row_metric_name(row: dict[str, Any]) -> str:
    code = str(row.get("scan_code") or row.get("scanCode") or "").strip().upper()
    try:
        from abcxauto.universe import scan_metric_name

        named = scan_metric_name(code)
        if named:
            return str(named).strip()
    except Exception:
        pass
    return str(row.get("metric_name") or "").strip()


def _is_open_gap_metric(name: str | None) -> bool:
    n = str(name or "").strip().lower().replace("-", "_")
    if not n:
        return False
    if n in _GAP_METRIC_NAMES:
        return True
    return "gap" in n


def _row_is_gap_screen(row: dict[str, Any]) -> bool:
    """HIGH/LOW_OPEN_GAP, TOP_OPEN_PERC_GAIN/LOSE, or scan_metric_name is a gap."""
    code = str(row.get("scan_code") or row.get("scanCode") or "").strip().upper()
    if code in _GAP_SCAN_CODES:
        return True
    return _is_open_gap_metric(_scan_row_metric_name(row))


def row_gap_pct(row: dict[str, Any] | None) -> float | None:
    """Return a real open gap, or None.

    Always: ``gap%``, ``open_gap_pct``, or nested ibkr/quote open_gap.
    Gap screens (HIGH_OPEN_GAP, LOW_OPEN_GAP, TOP_OPEN_PERC_GAIN,
    TOP_OPEN_PERC_LOSE, or ``universe.scan_metric_name`` in
    {open_gap, open_percent_change} / name contains ``gap``): IBKR
    ``distance`` / ``metric_value`` / ``gap_pct`` may be the gap.
    Non-gap metrics (option_volume, percent_change, volume, …): do not
    fall through to ``distance``, ``metric_value``, ``change``, or a
    generic ``gap_pct`` that merely copies that scanner distance.
    """
    if not isinstance(row, dict):
        return None
    gap = _first_gap(row, _OPEN_GAP_KEYS)
    if gap is not None:
        return gap
    for nest_key in ("ibkr", "quote"):
        nest = row.get(nest_key)
        if isinstance(nest, dict):
            gap = _first_gap(nest, _OPEN_GAP_KEYS)
            if gap is not None:
                return gap
    if _row_is_gap_screen(row):
        gap = _first_gap(row, _GAP_SCREEN_DISTANCE_KEYS)
        if gap is not None:
            return gap
        for nest_key in ("ibkr", "quote"):
            nest = row.get(nest_key)
            if isinstance(nest, dict):
                gap = _first_gap(nest, ("gap_pct", "distance"))
                if gap is not None:
                    return gap
        return None
    stored = parse_scan_gap(row.get("gap_pct"))
    if stored is None:
        return None
    # Thin-row slot after a real open_gap was copied. Ignore the lie where
    # thin_ranked_row used to write the non-gap scanner distance into gap_pct.
    metric_val = _first_gap(row, ("metric_value", "distance"))
    if metric_val is not None and stored == metric_val:
        return None
    return stored


def scrub_aliased_gap_pct(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pop leftover ``gap_pct`` that only copies scanner distance.

    Mutates ``row`` in place and returns the same object.

    Non-gap screens (option_volume, percent_change, volume, …): if
    ``gap_pct`` equals ``metric_value`` or ``distance``, drop it. Real
    ``open_gap_pct`` stays. Gap screens keep ``gap_pct`` even when it
    matches the scanner metric.
    """
    if not isinstance(row, dict):
        return row
    if "gap_pct" not in row:
        return row
    if _row_is_gap_screen(row):
        return row
    stored = parse_scan_gap(row.get("gap_pct"))
    if stored is None:
        return row
    metric_val = _first_gap(row, ("metric_value", "distance"))
    if metric_val is None or stored != metric_val:
        return row
    row.pop("gap_pct", None)
    return row


def _drop_nested_mda_news(row: dict[str, Any]) -> dict[str, Any]:
    """News is top-level only. Same nest keys as ``brain_tools._strip_hit_news``."""
    mda = row.get("mda")
    if not isinstance(mda, dict) or ("news" not in mda and "news_use" not in mda):
        return row
    nest = dict(mda)
    nest.pop("news", None)
    nest.pop("news_use", None)
    if nest:
        row["mda"] = nest
    else:
        row.pop("mda", None)
    return row


def public_scan_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Thin or fat public hit after scrub. No nested ``mda.news``.

    Mutates fat rows in place (same object). Thin rows are scrubbed, not rebuilt.
    """
    if not isinstance(row, dict):
        return row
    if not is_thin_ranked_row(row):
        _drop_nested_mda_news(row)
    return scrub_aliased_gap_pct(row)


def is_thin_ranked_row(row: Any) -> bool:
    """True when the row is the ranked-screen contract (no quote-heavy fat).

    ``symbols[]`` drill-down always carries ``on_book`` from overlay and is
    not thin even when quotes missed.
    """
    if not isinstance(row, dict):
        return False
    keys = set(row)
    if keys & _FAT_SCAN_KEYS:
        return False
    # Incoming scanner facts may still carry distance/benchmark before thin.
    if not keys <= (RANKED_ROW_KEYS | {"on_book", "gap%", "arena", "distance", "benchmark"}):
        return False
    if "skip_class" in keys or "source" in keys:
        return True
    if "gap_pct" in keys or "gap%" in keys or "rank" in keys:
        return True
    return keys <= {"symbol", "screen", "arena", "scan_code"}


def thin_ranked_row(
    row: dict[str, Any] | None,
    *,
    screen: str | None = None,
    scan_code: str | None = None,
) -> dict[str, Any] | None:
    """Emit the ranked-row contract. ``skip_class`` is always present."""
    if not isinstance(row, dict):
        return None
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return None
    from abcxauto.universe import scan_metric_name, scan_skip_class

    out: dict[str, Any] = {
        "symbol": sym,
        "skip_class": scan_skip_class(row),
        "source": "ibkr",
    }
    screen_id = str(row.get("screen") or row.get("arena") or screen or "").strip()
    if screen_id:
        out["screen"] = screen_id
    code = str(row.get("scan_code") or scan_code or "").strip().upper()
    if code:
        out["scan_code"] = code
    rank = row.get("rank")
    if rank not in (None, ""):
        try:
            out["rank"] = int(rank)
        except (TypeError, ValueError):
            out["rank"] = rank
    metric_val = None
    for key in ("metric_value", "distance", "benchmark"):
        metric_val = parse_scan_gap(row.get(key))
        if metric_val is not None:
            break
    if metric_val is not None:
        name = scan_metric_name(code) or str(row.get("metric_name") or "").strip()
        if name:
            out["metric_name"] = name
        out["metric_value"] = metric_val
    probe = dict(row)
    if code:
        probe["scan_code"] = code
    if out.get("metric_name"):
        probe["metric_name"] = out["metric_name"]
    # row_gap_pct ignores option_volume / percent_change / volume distance on
    # non-gap screens. Keep a previously published real open gap.
    gap = row_gap_pct(probe)
    if gap is not None:
        out["gap_pct"] = gap
    last = row.get("last")
    try:
        if last is not None and float(last) > 0:
            out["last"] = float(last)
    except (TypeError, ValueError):
        pass
    volume = row.get("volume")
    try:
        if volume is not None and float(volume) > 0:
            out["volume"] = int(float(volume))
    except (TypeError, ValueError):
        pass
    cap = row.get("market_cap")
    if cap is None:
        cap = row.get("marketCap")
    try:
        if cap is not None and float(cap) > 0:
            out["market_cap"] = float(cap)
    except (TypeError, ValueError):
        pass
    stock_type = str(row.get("stock_type") or row.get("stockType") or "").strip()
    if stock_type:
        out["stock_type"] = stock_type
    return out


def thin_ranked_hits(
    rows: list[Any] | None,
    *,
    screen: str | None = None,
    scan_code: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        item = (
            thin_ranked_row(row, screen=screen, scan_code=scan_code)
            if isinstance(row, dict)
            else None
        )
        if item:
            out.append(scrub_aliased_gap_pct(item) or item)
    return out


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
    ibkr_rows = 0
    kept = 0
    empty = False
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
                "arenas": pulled.get("arenas") or pulled.get("screens"),
                "screens": pulled.get("screens") or pulled.get("arenas"),
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
        ibkr_rows = int(pulled.get("ibkr_rows") or len(scanner_rows) or 0)
        kept = int(pulled.get("kept") or len(hits_syms))
        empty = bool(pulled.get("empty") if pulled.get("empty") is not None else kept == 0)
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
    if arena_id:
        label = str(arena_id)
        for row in rows:
            if isinstance(row, dict) and row.get("symbol"):
                row.setdefault("screen", label)
                row.setdefault("arena", label)
            if code_out and isinstance(row, dict) and row.get("symbol"):
                row.setdefault("scan_code", str(code_out).upper())
    screen = bool(has_arena or has_code)
    if screen:
        quoted = 0
        rows = thin_ranked_hits(
            rows,
            screen=str(arena_id or "").strip() or None,
            scan_code=str(code_out or "").strip() or None,
        )
        empty = bool(empty or not rows)
        kept = len(rows)
    else:
        quoted = await attach_live_quotes(rows, connector=connector)
        empty = False
    rows = [
        item
        for item in (
            public_scan_row(r) if isinstance(r, dict) else None for r in rows
        )
        if isinstance(item, dict)
    ]
    ranked = bool(scanner_rows) and source == "ibkr" and not empty
    out: dict[str, Any] = {
        "ok": True,
        "source": source,
        "arena": arena_id,
        "screen": arena_id,
        "scan_code": code_out,
        "symbols": [r["symbol"] for r in rows],
        "hits": rows,
        "applied": applied,
        "persisted": False,
        "ranked": ranked,
        "quoted": quoted,
        "thin": screen,
        "empty": empty if screen else False,
        "criteria": (
            {"arena": arena_id, "scan_code": code_out}
            if screen
            else {"symbols": [r["symbol"] for r in rows]}
        ),
        "sort": code_out if screen else None,
    }
    if screen:
        out["provenance"] = {
            "screen": arena_id,
            "scan_code": code_out,
            "filters": applied,
            "ibkr_rows": ibkr_rows,
            "kept": kept,
            "empty": empty,
        }
        if empty:
            out["rank_meaning"] = "empty screen"
            out["source"] = "empty"
        else:
            out["rank_meaning"] = THIN_RANK_MEANING if ranked else "not ranked"
    else:
        out["rank_meaning"] = "not ranked"
        out["note"] = "fat drill-down; ranked arena/scan_code screens stay thin"
    return out


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
        start, end = _RTH_START_MIN, _RTH_END_MIN
        stamp = _bar_et(bar)
        if stamp is not None:
            try:
                from abcxauto.marketdata.market_hours import rth_minute_bounds

                start, end = rth_minute_bounds(stamp)
            except Exception:
                pass
        if start <= mins < end:
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
    """True during the NYSE regular session (holidays and early closes)."""
    from abcxauto.marketdata.market_hours import rth_now as session_rth_now

    return session_rth_now(now=now)


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
    """Dedupe by symbol; keep first-seen order (book seed first)."""
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
    """Seed SCAN TAPE: book symbols only, unranked (cached)."""
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
