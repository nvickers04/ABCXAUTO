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
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "ideas": []}
_CACHE_TTL_S = 150.0
# Seed tape size matches the book tool payload (world_state / format_scan_tape).
TAPE_SEED_CAP = 12
# Top-N screen hits that get an IBKR last stamped on in the same call.
SCAN_QUOTE_CAP = 12

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

QUOTE_SOURCES_BLOCK = (
    "QUOTE SOURCES:\n"
    "- IBKR quote/option_quote/book: live TWS stream. Use for send geometry.\n"
    "- MDA scan: daily-bar metrics. mda_last is daily close, not a 15m last.\n"
    "- MDA candles: delayed OHLCV at the requested resolution (D = daily close).\n"
    "- MDA news: typically ~15 min delayed. Context only."
)

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
        for key in ("bid", "ask"):
            if q.get(key) is not None:
                row[key] = q[key]
        row["quote_source"] = "ibkr_live"
        n += 1
    return n


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
    }


def score_symbol(candles: list[dict], symbol: str) -> dict[str, Any] | None:
    """Compat alias — returns metrics only (no score). Prefer metrics_for_symbol."""
    return metrics_for_symbol(candles, symbol)


def tape_symbols(ideas: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for idea in ideas or []:
        sym = str(idea.get("symbol") or "").upper().strip()
        if sym and sym not in out:
            out.append(sym)
    return out


def dismiss_cites_tape(dismissed: str, ideas: list[dict[str, Any]] | None) -> bool:
    blob = (dismissed or "").upper()
    if not blob:
        return False
    for sym in tape_symbols(ideas):
        if sym and sym in blob:
            return True
    return False


def format_scan_tape(ideas: list[dict[str, Any]], *, limit: int = 12) -> str:
    """Unranked SCAN TAPE prompt block (MDA delayed facts)."""
    if not ideas:
        return (
            "SCAN TAPE (unranked MDA daily-bar metrics — mda_last is daily close, "
            "not live / not 15m last; Grok operates the scanner; not trade recommendations): "
            "(none — MDA thin or unconfigured)"
        )
    lines = [
        "SCAN TAPE (unranked MDA daily-bar metrics — mda_last is daily close, not live / not 15m last).",
        "Grok operates the scanner. Shell does not recommend a top idea.",
        "Do not use tape last for send geometry — use IBKR quote.",
        QUOTE_SOURCES_BLOCK,
    ]
    # Preserve seed / fetch order — never alphabetize (A* tape bias).
    rows = list(ideas[: max(1, limit)])
    for idea in rows:
        sym = idea.get("symbol")
        src = idea.get("source") or "mda"
        fresh = idea.get("freshness") or "delayed"
        bar = idea.get("bar") or "D"
        kind = idea.get("mda_last_is") or mda_last_kind(str(bar))
        lines.append(
            f"- {sym} source={src} freshness={fresh} bar={bar} "
            f"mda_last={idea.get('mda_last') or idea.get('last')} "
            f"mda_last_is={kind} "
            f"sma20={idea.get('sma20')} sma50={idea.get('sma50')} "
            f"dist20={idea.get('dist20')} ret5={idea.get('ret5')} "
            f"above_sma20={idea.get('above_sma20')}"
        )
    return "\n".join(lines)


def format_market_features(ideas: list[dict[str, Any]], *, limit: int = 12) -> str:
    """Alias — prefer ``format_scan_tape``."""
    return format_scan_tape(ideas, limit=limit)


def format_opportunities(ideas: list[dict[str, Any]], *, limit: int = 12) -> str:
    return format_scan_tape(ideas, limit=limit)


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
