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

# Per-symbol cap. MDA's client allows 30s, which outlasts the 20s news tool
# budget; a stall must not look like "no headlines." One retry, then miss.
NEWS_SYMBOL_S = 12.0
NEWS_TRIES = 2


def reset_news_cache() -> None:
    _CACHE.update(ts=0.0, items=[], symbols=[])


def _universe(positions: list[dict] | None) -> list[str]:
    """Book underlyings first, then Universe legal sample. No index pad."""
    out: list[str] = []
    for p in positions or []:
        sym = str((p or {}).get("symbol") or "").upper()
        if sym and sym not in out:
            out.append(sym)
    try:
        from abcxauto.universe import legal_symbols

        for sym in legal_symbols():
            if sym not in out:
                out.append(sym)
            if len(out) >= _UNIVERSE_CAP:
                break
    except Exception:
        logger.exception("news universe legal_symbols failed")
    return out


def _configured(client: Any) -> bool:
    flag = getattr(client, "is_configured", False)
    return bool(flag() if callable(flag) else flag)


def _miss(symbol: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "headline": f"(unavailable — {reason})",
        "error": reason,
    }


async def _fetch_symbol_news(
    client: Any, sym: str, *, per_symbol: int
) -> tuple[list[dict], str | None]:
    """One symbol: try, retry once on timeout/error. Miss is not empty."""
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


async def fetch_agent_news(
    positions: list[dict] | None = None,
    *,
    force: bool = False,
    per_symbol: int = 4,
) -> list[dict]:
    """Fetch / cache headlines for book + sandbox sample.

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

    items: list[dict] = []
    misses: list[dict] = []
    try:
        from abcxauto.marketdata.client import get_marketdata_client

        client = get_marketdata_client()
        if not _configured(client):
            return []

        batches = await asyncio.gather(
            *[_fetch_symbol_news(client, s, per_symbol=per_symbol) for s in symbols]
        )
        for sym, (batch, err) in zip(symbols, batches):
            if err:
                misses.append(_miss(sym, err))
            else:
                items.extend(batch)
    except Exception:
        logger.exception("fetch_agent_news failed")
        return [_miss(s, "error") for s in symbols]

    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        hl = str(it.get("headline") or "").strip()
        if not hl or hl in seen:
            continue
        seen.add(hl)
        unique.append(it)

    if misses:
        return unique + misses

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
            lines.append(f"(news unavailable — fetch {why})")
        else:
            lines.append("(no headlines available)")
        return "\n".join(lines)
    for it in real[:limit]:
        sym = str(it.get("symbol") or "?").upper()
        hl = str(it.get("headline") or "").strip()
        lines.append(f"- [{sym}] {hl[:180]}")
    return "\n".join(lines)
