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

# Per-symbol prints the What's-happening rail already painted. A 2s MDA
# stall is not "no headline" when this memory still has the print.
_HEADLINES: dict[str, dict[str, Any]] = {}
_HEADLINE_TTL_S = 15 * 60.0

# Fail fast. MDA's HTTP client allows 30s and a 12s per-symbol wait_for was
# the whole look: Grok sat through empty news() batches (HEI/WDAY/…) instead
# of a miss the think can skip. One try; a stall is a hard miss.
NEWS_SYMBOL_S = 2.0
NEWS_TRIES = 1


def reset_news_cache() -> None:
    _CACHE.update(ts=0.0, items=[], symbols=[])
    _HEADLINES.clear()


def is_real_headline(item: Any) -> bool:
    """True for a print. Timeout/error placeholders are not headlines."""
    if not isinstance(item, dict) or item.get("error"):
        return False
    hl = str(item.get("headline") or "").strip()
    if not hl:
        return False
    return not hl.startswith("(unavailable")


def remember_headlines(items: list[dict] | None) -> None:
    """Keep rail / think prints so news() can return them after a 2s miss."""
    now = time.monotonic()
    for it in items or []:
        if not is_real_headline(it):
            continue
        sym = str(it.get("symbol") or "").upper().strip()
        if not sym:
            continue
        bucket = _HEADLINES.setdefault(sym, {"ts": now, "items": []})
        bucket["ts"] = now
        rows = list(bucket.get("items") or [])
        seen = {str(x.get("headline") or "").strip() for x in rows}
        hl = str(it.get("headline") or "").strip()
        if hl and hl not in seen:
            rows.append(it)
        bucket["items"] = rows[:8]


def remembered_headlines(symbols: list[str] | None = None) -> list[dict]:
    """Headlines the rail or a prior fetch already has. Expired names drop."""
    now = time.monotonic()
    want: set[str] | None = None
    if symbols is not None:
        want = {
            str(s or "").upper().strip()
            for s in symbols
            if str(s or "").strip()
        }
    dead: list[str] = []
    out: list[dict] = []
    for sym, bucket in list(_HEADLINES.items()):
        age = now - float(bucket.get("ts") or 0.0)
        if age > _HEADLINE_TTL_S:
            dead.append(sym)
            continue
        if want is not None and sym not in want:
            continue
        for it in bucket.get("items") or []:
            if is_real_headline(it):
                out.append(it)
    for sym in dead:
        _HEADLINES.pop(sym, None)
    cache_age = now - float(_CACHE.get("ts") or 0.0)
    if _CACHE.get("items") and cache_age < _CACHE_TTL_S:
        for it in _CACHE["items"]:
            if not is_real_headline(it):
                continue
            su = str(it.get("symbol") or "").upper().strip()
            if want is not None and su not in want:
                continue
            out.append(it)
    return _dedupe_headlines(out)


def remember_look_news(world: Any = None, snap: dict[str, Any] | None = None) -> None:
    """Ingest prints this look already fetched (scan nest, world, snap)."""
    remember_headlines(getattr(world, "news_items", None) if world is not None else None)
    blob = snap if isinstance(snap, dict) else {}
    remember_headlines(blob.get("news_items") if isinstance(blob.get("news_items"), list) else None)
    hits = blob.get("scan_hits") if isinstance(blob.get("scan_hits"), dict) else {}
    remember_headlines(hits.get("news") if isinstance(hits.get("news"), list) else None)
    for row in hits.get("rows") or []:
        if not isinstance(row, dict):
            continue
        remember_headlines(row.get("news") if isinstance(row.get("news"), list) else None)
        mda = row.get("mda") if isinstance(row.get("mda"), dict) else {}
        remember_headlines(mda.get("news") if isinstance(mda.get("news"), list) else None)


def coalesce_news(
    items: list[dict] | None,
    symbols: list[str] | None = None,
) -> list[dict]:
    """Keep real prints. A timeout is not no-print when memory has that name."""
    real: list[dict] = []
    misses: list[dict] = []
    have: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        su = str(it.get("symbol") or "").upper().strip()
        if is_real_headline(it):
            real.append(it)
            if su:
                have.add(su)
            continue
        if it.get("error"):
            misses.append(it)
    for it in remembered_headlines(symbols):
        su = str(it.get("symbol") or "").upper().strip()
        if su and su not in have:
            real.append(it)
            have.add(su)
    leftover = [
        m
        for m in misses
        if str(m.get("symbol") or "").upper().strip() not in have
    ]
    return _dedupe_headlines(real) + leftover


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
            landed = list(rows or [])
            remember_headlines(landed)
            if landed:
                return landed, None
            cached = remembered_headlines([sym])
            return (cached, None) if cached else ([], None)
        except asyncio.TimeoutError:
            reason = "timed out"
            logger.warning("news %s timed out after %.0fs", sym, timeout_s)
        except Exception:
            reason = "error"
            logger.exception("news fetch failed for %s", sym)
    cached = remembered_headlines([sym])
    if cached:
        return cached, None
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
                cached = remembered_headlines([sym])
                if cached:
                    items.extend(cached)
                else:
                    misses.append(_miss(sym, err))
            else:
                items.extend(batch)
    except Exception:
        logger.exception("fetch_symbols_news failed")
        cached = remembered_headlines(out)
        return cached or [_miss(s, "error") for s in out]

    remember_headlines(items)
    unique = coalesce_news(_dedupe_headlines(items) + misses, out)
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
        remember_headlines(_CACHE["items"])
        return list(_CACHE["items"])

    if not symbols:
        return []

    unique = await fetch_symbols_news(symbols, per_symbol=per_symbol)
    remember_headlines(unique)
    unique = coalesce_news(unique, symbols)
    if any(isinstance(it, dict) and it.get("error") for it in unique):
        return unique

    _CACHE.update(ts=now, items=unique, symbols=symbols)
    return list(unique)


def format_news_for_prompt(items: list[dict], *, limit: int = 18) -> str:
    """Compact NEWS block for the cycle prompt."""
    lines = [
        "NEWS (color only — not a trigger):",
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
