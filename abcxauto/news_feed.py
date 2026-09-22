"""Shared news feed for Pro UI + wake facts."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CACHE: dict[str, Any] = {"ts": 0.0, "items": [], "symbols": []}
_CACHE_TTL_S = 90.0
_UNIVERSE_CAP = 14
# Last fetch's timeout/error reason. Public lists stay real headlines;
# news() may copy this onto a sibling error/note.
_LAST_FETCH_MISS: str | None = None
# Names that exhausted the shared batch budget on the last fetch.
_LAST_TIMED_OUT: list[str] = []

# Per-symbol prints the What's-happening rail already painted. A stall is
# not "no headline" when this memory still has the print.
_HEADLINES: dict[str, dict[str, Any]] = {}
_HEADLINE_TTL_S = 15 * 60.0
# One shared budget for the whole symbols[] batch — not 2s × N. One try.
# A later look refetches. A stall is a hard miss, not a fake headline.
NEWS_SYMBOL_S = 2.0
NEWS_TRIES = 1


class NewsFetch(list):
    """Headline rows plus names that missed the shared batch deadline."""

    __slots__ = ("timed_out",)

    def __init__(self, items=(), *, timed_out: list[str] | None = None):
        super().__init__(items)
        self.timed_out = [
            str(s).upper().strip()
            for s in (timed_out or [])
            if str(s or "").strip()
        ]


def reset_news_cache() -> None:
    global _LAST_FETCH_MISS, _LAST_TIMED_OUT
    _CACHE.update(ts=0.0, items=[], symbols=[])
    _HEADLINES.clear()
    _LAST_FETCH_MISS = None
    _LAST_TIMED_OUT = []


def news_timed_out() -> list[str]:
    """Symbols that hit the shared batch deadline on the last fetch."""
    return list(_LAST_TIMED_OUT)


def is_real_headline(item: Any) -> bool:
    """True for a print. Timeout/error placeholders are not headlines."""
    if not isinstance(item, dict) or item.get("error"):
        return False
    hl = str(item.get("headline") or "").strip()
    if not hl:
        return False
    return not hl.startswith("(unavailable")


# Comparable news page. Outlet is publisher; source is the feed (mda), not Yahoo.
_PUBLIC_NEWS_KEYS = (
    "symbol",
    "headline",
    "publisher",
    "published",
    "as_of",
    "asof_iso",
    "url",
    "source",
    "freshness",
    "use",
)
_FEED_SOURCES = frozenset({"mda", "ibkr", "web"})


def _publisher_and_url(item: dict[str, Any]) -> tuple[str, str]:
    """Outlet name + URL. MDA often stuffs a Yahoo link into publisher."""
    publisher = str(item.get("publisher") or "").strip()
    url = str(item.get("url") or "").strip()
    src = str(item.get("source") or "").strip()
    if not publisher and src.lower() not in _FEED_SOURCES:
        publisher = src
    raw = publisher
    if raw.lower().startswith(("http://", "https://")):
        if not url:
            url = raw
        host = str(urlparse(raw).hostname or "").strip().lower()
        publisher = host[4:] if host.startswith("www.") else host
    return publisher, url


def public_news_item(item: Any) -> dict[str, Any] | None:
    """One real headline Grok can cross-ref with web(url) / quote(symbol)."""
    if not is_real_headline(item):
        return None
    src = str(item.get("source") or "").strip()
    src_l = src.lower()
    feed = src if src_l in _FEED_SOURCES else "mda"
    publisher, url = _publisher_and_url(item)
    out: dict[str, Any] = {}
    for key in _PUBLIC_NEWS_KEYS:
        if key == "publisher":
            val = publisher
        elif key == "url":
            val = url
        elif key == "source":
            val = feed
        else:
            val = item.get(key)
        if val not in (None, ""):
            out[key] = val
    return out if out.get("headline") else None


def public_news_items(items: Any) -> list[dict[str, Any]]:
    """Real headlines only, same public keys as public_news_item."""
    rows = items if isinstance(items, (list, tuple)) else []
    out: list[dict[str, Any]] = []
    for it in rows:
        row = public_news_item(it)
        if row:
            out.append(row)
    return out


def news_need_symbols(already: list[str] | None = None) -> dict[str, Any]:
    """Bare news() is a choice. Do not poll SPY or the scan tape."""
    out: dict[str, Any] = {
        "ok": False,
        "need": "symbols[]",
        "use": "color_not_trigger",
        "freshness": "delayed_15m",
        "source": "mda",
        "items": [],
        "note": "pass symbols[]",
        "fetched": False,
    }
    names = [str(s).upper().strip() for s in (already or []) if str(s).strip()]
    if names:
        out["already"] = names[:12]
    return out


def remember_headlines(items: list[dict] | None) -> None:
    """Keep rail / think prints so news() can return them after an 8s miss."""
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
    """Real headlines only. Memory fills a timeout miss; leftovers are not items."""
    want: set[str] | None = None
    if symbols is not None:
        want = {
            str(s or "").strip().casefold()
            for s in symbols
            if str(s or "").strip()
        }
    real: list[dict] = []
    have: set[str] = set()
    for it in items or []:
        if not isinstance(it, dict) or not is_real_headline(it):
            continue
        su = str(it.get("symbol") or "").upper().strip()
        if want is not None and su.casefold() not in want:
            continue
        real.append(it)
        if su:
            have.add(su)
    for it in remembered_headlines(symbols):
        su = str(it.get("symbol") or "").upper().strip()
        if su and su not in have:
            real.append(it)
            have.add(su)
    return _dedupe_headlines(real)


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
    if why:
        return why
    if not items:
        return _LAST_FETCH_MISS
    return None


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


async def _call_stock_news(
    client: Any, sym: str, *, per_symbol: int, timeout: float
) -> list[dict]:
    """Call get_stock_news with remaining batch budget; tolerate older mocks."""
    import inspect

    fn = client.get_stock_news
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "timeout" in params:
        rows = await fn(sym, countback=per_symbol, timeout=timeout)
    else:
        rows = await asyncio.wait_for(fn(sym, countback=per_symbol), timeout=timeout)
    return list(rows or [])


async def _fetch_symbol_news(
    client: Any,
    sym: str,
    *,
    per_symbol: int,
    deadline: float,
) -> tuple[list[dict], str | None]:
    """One symbol against the shared batch deadline. Miss is not empty."""
    try:
        from abcxauto.prints import mda_worth_asking

        if not mda_worth_asking(sym):
            return [], None
    except Exception:
        logger.exception("mda_worth_asking failed for %s", sym)

    reason: str | None = None
    tries = max(1, int(NEWS_TRIES))
    loop = asyncio.get_running_loop()
    for _attempt in range(tries):
        remaining = float(deadline) - loop.time()
        if remaining <= 0:
            reason = "timed out"
            logger.warning(
                "news %s timed out after %.0fs", sym, float(NEWS_SYMBOL_S)
            )
            break
        try:
            landed = await _call_stock_news(
                client, sym, per_symbol=per_symbol, timeout=remaining
            )
            remember_headlines(landed)
            if landed:
                return landed, None
            cached = remembered_headlines([sym])
            return (cached, None) if cached else ([], None)
        except (asyncio.TimeoutError, TimeoutError):
            reason = "timed out"
            logger.warning(
                "news %s timed out after %.0fs", sym, float(NEWS_SYMBOL_S)
            )
            break
        except Exception:
            reason = "error"
            logger.exception("news fetch failed for %s", sym)
            break
    cached = remembered_headlines([sym])
    if cached:
        return cached, None
    return [], reason


async def fetch_symbols_news(
    symbols: list[str] | None,
    *,
    per_symbol: int = 4,
) -> list[dict]:
    """Headlines for an explicit tape. Public list is real prints only.

    One shared ~2s deadline for the whole batch. Whatever finished lands;
    the rest are timed_out — not a fresh 2s wait per leftover name.
    A total miss stays a hard miss (news_hard_miss), not a fake empty ok.
    """
    global _LAST_FETCH_MISS, _LAST_TIMED_OUT
    _LAST_FETCH_MISS = None
    _LAST_TIMED_OUT = []
    out: list[str] = []
    for raw in symbols or []:
        su = str(raw or "").upper().strip()
        if su and su not in out:
            out.append(su)
    if not out:
        return NewsFetch([])

    client = _get_client()
    if not _configured(client):
        return NewsFetch([])

    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(NEWS_SYMBOL_S)
    items: list[dict] = []
    misses: list[dict] = []
    timed_out: list[str] = []

    async def _one(sym: str) -> tuple[str, list[dict], str | None]:
        batch, err = await _fetch_symbol_news(
            client, sym, per_symbol=per_symbol, deadline=deadline
        )
        return sym, batch, err

    tasks = {
        asyncio.create_task(_one(s), name=f"news:{s}"): s for s in out
    }
    pending: set[asyncio.Task] = set(tasks)
    try:
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.ALL_COMPLETED,
            )
            for task in done:
                sym, batch, err = task.result()
                if err:
                    cached = remembered_headlines([sym])
                    if cached:
                        items.extend(cached)
                    else:
                        timed_out.append(sym)
                        misses.append(_miss(sym, err))
                else:
                    items.extend(batch)
        # Budget gone: do not start a new 2s wait for leftovers.
        for task in list(pending):
            sym = tasks[task]
            if task.done() and not task.cancelled():
                try:
                    sym, batch, err = task.result()
                except Exception:
                    timed_out.append(sym)
                    misses.append(_miss(sym, "timed out"))
                    logger.warning(
                        "news %s timed out after %.0fs",
                        sym,
                        float(NEWS_SYMBOL_S),
                    )
                    continue
                if err:
                    cached = remembered_headlines([sym])
                    if cached:
                        items.extend(cached)
                    else:
                        timed_out.append(sym)
                        misses.append(_miss(sym, err))
                else:
                    items.extend(batch)
                continue
            task.cancel()
            timed_out.append(sym)
            cached = remembered_headlines([sym])
            if cached:
                items.extend(cached)
            else:
                misses.append(_miss(sym, "timed out"))
                logger.warning(
                    "news %s timed out after %.0fs", sym, float(NEWS_SYMBOL_S)
                )
        if pending:
            await asyncio.wait(pending, timeout=0.05)
    except Exception:
        logger.exception("fetch_symbols_news failed")
        for task in pending:
            task.cancel()
        cached = remembered_headlines(out)
        combined = list(cached) if cached else [_miss(s, "error") for s in out]
        _LAST_FETCH_MISS = news_hard_miss(combined)
        _LAST_TIMED_OUT = list(out)
        return NewsFetch(coalesce_news(combined, out), timed_out=list(out))

    remember_headlines(items)
    combined = _dedupe_headlines(items) + misses
    # Dedupe timed_out while preserving ask order.
    seen_to: set[str] = set()
    ordered_to: list[str] = []
    for sym in out:
        if sym in timed_out and sym not in seen_to:
            seen_to.add(sym)
            ordered_to.append(sym)
    _LAST_TIMED_OUT = ordered_to
    _LAST_FETCH_MISS = news_hard_miss(combined)
    return NewsFetch(coalesce_news(combined, out), timed_out=ordered_to)


async def fetch_agent_news(
    positions: list[dict] | None = None,
    *,
    force: bool = False,
    per_symbol: int = 4,
) -> list[dict]:
    """Fetch / cache headlines for open-book underlyings.

    A timeout or transport miss is not cached and is not a headline item.
    Empty headlines from a completed fetch stay empty.
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
        return NewsFetch(list(_CACHE["items"]))

    if not symbols:
        return NewsFetch([])

    unique = await fetch_symbols_news(symbols, per_symbol=per_symbol)
    timed_out = list(getattr(unique, "timed_out", None) or _LAST_TIMED_OUT)
    remember_headlines(unique)
    unique = coalesce_news(unique, symbols)
    if _LAST_FETCH_MISS or any(
        isinstance(it, dict) and it.get("error") for it in unique
    ):
        return NewsFetch(unique, timed_out=timed_out)

    _CACHE.update(ts=now, items=unique, symbols=symbols)
    return NewsFetch(list(unique), timed_out=timed_out)


def format_news_for_prompt(items: list[dict], *, limit: int = 18) -> str:
    """Compact NEWS block for the wake."""
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
