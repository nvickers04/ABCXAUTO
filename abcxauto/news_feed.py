"""Shared news feed for Pro UI + agent cycle prompt."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"ts": 0.0, "items": [], "symbols": []}
_CACHE_TTL_S = 90.0
_UNIVERSE_CAP = 14

# Fail fast. MDA's HTTP client allows 30s and a 12s per-symbol wait_for was
# the whole look: Grok sat through empty news() batches (HEI/WDAY/…) instead
# of a miss the think can skip. One try; a stall is a hard miss.
NEWS_SYMBOL_S = 2.0
NEWS_TRIES = 1


def reset_news_cache() -> None:
    _CACHE.update(ts=0.0, items=[], symbols=[])


def _universe(positions: list[dict] | None) -> list[str]:
    """Book underlyings only. No sandbox junk, no index pad.

    legal_symbols is the IBKR screen leftover (levered/micro junk). Polling
    that tape for headlines 404s MDA and starves the look's catalyst fetch.
    SPY/QQQ pads are canned names, not the book.
    """
    out: list[str] = []
    for p in positions or []:
        sym = str((p or {}).get("symbol") or "").upper()
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= _UNIVERSE_CAP:
            break
    return out


def _configured(client: Any) -> bool:
    flag = getattr(client, "is_configured", False)
    return bool(flag() if callable(flag) else flag)


def _get_client() -> Any:
    from abcxauto.marketdata.client import get_marketdata_client

    return get_marketdata_client()


def _miss(symbol: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "headline": f"(unavailable - {reason})",
        "error": reason,
    }


def news_hard_miss(items: list[dict] | None) -> str | None:
    """Timeout/error reason when nothing but misses landed. None if headlines or a completed empty."""
    why: str | None = None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if it.get("error"):
            why = why or str(it.get("error") or "timed out")
            continue
        if str(it.get("headline") or "").strip():
            return None
    return why


def _dedupe_headlines(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        hl = str(it.get("headline") or "").strip()
        if not hl or hl in seen:
            continue
        seen.add(hl)
        unique.append(it)
    return unique


async def _fetch_symbol_news(
    client: Any, sym: str, *, per_symbol: int
) -> tuple[list[dict], str | None]:
    """One symbol: bounded wait. Miss is not empty."""
    try:
        from abcxauto.prints import mda_worth_asking

        if not mda_worth_asking(sym):
            return [], None
    except Exception:
        logger.exception("mda_worth_asking failed for %s", sym)

    reason: str | None = None
    tries = max(1, int(NEWS_TRIES))
    timeout_s = float(NEWS_SYMBOL_S)
    for _attempt in range(tries):
        try:
            rows = await asyncio.wait_for(
                client.get_stock_news(sym, countback=per_symbol),
                timeout=timeout_s,
            )
            return list(rows or []), None
        except asyncio.TimeoutError:
            reason = "timed out"
            logger.warning("news %s timed out after %.0fs", sym, timeout_s)
        except Exception:
            reason = "error"
            logger.exception("news fetch failed for %s", sym)
    return [], reason


async def fetch_symbols_news(
    symbols: list[str] | None,
    *,
    per_symbol: int = 4,
) -> list[dict]:
    """Headlines for an explicit tape. Timeout/error is a miss item, not empty.

    Parallel, one try, per-symbol cap. A slow MDA must not eat a 12s look.
    """
    out: list[str] = []
    for raw in symbols or []:
        su = str(raw or "").upper().strip()
        if su and su not in out:
            out.append(su)
    if not out:
        return []

    client = _get_client()
    if not _configured(client):
        return []

    items: list[dict] = []
    misses: list[dict] = []
    try:
        batches = await asyncio.gather(
            *[_fetch_symbol_news(client, s, per_symbol=per_symbol) for s in out]
        )
        for sym, (batch, err) in zip(out, batches):
            if err:
                misses.append(_miss(sym, err))
            else:
                items.extend(batch)
    except Exception:
        logger.exception("fetch_symbols_news failed")
        return [_miss(s, "error") for s in out]

    unique = _dedupe_headlines(items)
    if misses:
        return unique + misses
    return unique


async def fetch_agent_news(
    positions: list[dict] | None = None,
    *,
    force: bool = False,
    per_symbol: int = 4,
) -> list[dict]:
    """Fetch / cache headlines for open-book underlyings.

    A timeout or transport miss is returned as an ``error`` item and is not
    cached. Empty headlines from a completed fetch stay empty.
    """
    now = time.monotonic()
    symbols = _universe(positions)
    if (
        not force
        and _CACHE["items"]
        and (now - float(_CACHE["ts"])) < _CACHE_TTL_S
        and _CACHE.get("symbols") == symbols
    ):
        return list(_CACHE["items"])

    if not symbols:
        return []

    unique = await fetch_symbols_news(symbols, per_symbol=per_symbol)
    if any(isinstance(it, dict) and it.get("error") for it in unique):
        return unique

    _CACHE.update(ts=now, items=unique, symbols=symbols)
    return list(unique)


def format_news_for_prompt(items: list[dict], *, limit: int = 18) -> str:
    """Compact NEWS block for the cycle prompt."""
    lines = [
        "NEWS (headlines — not orders):",
    ]
    real: list[dict] = []
    misses: list[dict] = []
    for it in items or []:
        if it.get("error"):
            misses.append(it)
            continue
        hl = str(it.get("headline") or "").strip()
        if hl:
            real.append(it)
    if not real:
        if misses:
            why = str(misses[0].get("error") or "timed out")
            lines.append(f"(news unavailable - fetch {why})")
        else:
            lines.append("(no headlines available)")
        return "\n".join(lines)
    for it in real[:limit]:
        sym = str(it.get("symbol") or "?").upper()
        hl = str(it.get("headline") or "").strip()
        lines.append(f"- [{sym}] {hl[:180]}")
    return "\n".join(lines)
