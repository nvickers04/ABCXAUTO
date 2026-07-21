"""SCAN TAPE — unranked MDA metrics for Grok-operated scanning.

Code fetches candle metrics (typically delayed). Never places orders.
Internal list field remains ``opportunities`` for journal/UI compat.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"ts": 0.0, "key": "", "ideas": []}
_CACHE_TTL_S = 150.0

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,7}$")

QUOTE_SOURCES_BLOCK = (
    "QUOTE SOURCES (facts):\n"
    "- MDA: SCAN TAPE / candles / news — typically ~15m delayed (or delayed "
    "unless client proves live). Use for discovery only. Do NOT treat tape "
    "`last` as live for bracket geometry.\n"
    "- IBKR: book, fills, working orders, and LIVE last for stop/target "
    "geometry when TWS is connected. Rebuild hunt structure from IBKR live "
    "(or geometry fails closed)."
)


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


def _universe(positions: list[dict] | None, *, cap: int = 40) -> list[str]:
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


def metrics_for_symbol(candles: list[dict], symbol: str) -> dict[str, Any] | None:
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
    return {
        "symbol": str(symbol or "").upper(),
        "last": round(last, 4),
        "mda_last": round(last, 4),
        "sma20": round(sma20, 4),
        "sma50": round(sma50, 4) if sma50 is not None else None,
        "dist20": round(dist20, 5),
        "ret5": round(ret5, 5),
        "above_sma20": bool(last >= sma20),
        "source": "mda",
        "freshness": "delayed",
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
            "SCAN TAPE (unranked MDA metrics — typically delayed; "
            "Grok operates the scanner; not trade recommendations): "
            "(none — MDA thin or unconfigured)"
        )
    lines = [
        "SCAN TAPE (unranked MDA metrics — typically delayed / not live).",
        "Grok operates the scanner: pick hunt symbol from tape (or scan_request "
        "more symbols). Shell does not recommend a top idea.",
        "Do NOT use tape last for bracket geometry — use IBKR live on Act.",
        QUOTE_SOURCES_BLOCK,
    ]
    rows = sorted(
        ideas[: max(1, limit)],
        key=lambda x: str(x.get("symbol") or ""),
    )
    for idea in rows:
        sym = idea.get("symbol")
        src = idea.get("source") or "mda"
        fresh = idea.get("freshness") or "delayed"
        lines.append(
            f"- {sym} source={src} freshness={fresh} "
            f"mda_last={idea.get('mda_last') or idea.get('last')} "
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
    by_sym: dict[str, dict[str, Any]] = {}
    for row in list(base or []) + list(extra or []):
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        by_sym[sym] = row
    return [by_sym[k] for k in sorted(by_sym.keys())]


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
        for sym in syms:
            try:
                candles = await client.get_stock_candles(
                    sym, resolution="D", countback=120
                )
            except Exception:
                logger.exception("fetch_scan_metrics candles failed for %s", sym)
                candles = []
            row = metrics_for_symbol(candles or [], sym)
            if row:
                ideas.append(row)
    except Exception:
        logger.exception("fetch_scan_metrics failed")
        return []
    return merge_tape([], ideas)


async def scan_opportunities(
    positions: list[dict] | None = None,
    *,
    force: bool = False,
    cap: int = 40,
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
