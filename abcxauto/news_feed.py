"""Shared news feed for Pro UI + wake facts."""

from __future__ import annotations

import asyncio
import logging
import os
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

# MDA httpx client allows 30s; news_feed caps below that so a stall cannot
# eat the look. Cold first fetch pays TLS; warm reuse keepalive (30s expiry).
NEWS_SYMBOL_S = 6.0
NEWS_SYMBOL_COLD_S = 10.0
NEWS_BATCH_S = 12.0
NEWS_TRIES = 1

# After consecutive misses, stop re-hitting MDA for a cooldown. A timeout
# still costs a full tool round-trip; the breaker returns source-down fast.
NEWS_BREAKER_FAILURES = 4
NEWS_BREAKER_COOLDOWN_S = 90.0

_BREAKERS: dict[str, dict[str, Any]] = {}
_FEED_BREAKER: dict[str, Any] = {"failures": 0, "open_until": 0.0, "logged_open": False}
_WARM_SYMBOLS: set[str] = set()


def news_symbol_s() -> float:
    """Warm per-symbol wait (pooled MDA). Env override; clamp 1–15s."""
    raw = (os.environ.get("ABCXAUTO_NEWS_SYMBOL_S") or "").strip()
    if raw:
        try:
            return max(1.0, min(15.0, float(raw)))
        except ValueError:
            pass
    return float(NEWS_SYMBOL_S)


def news_symbol_cold_s() -> float:
    """First fetch per symbol in session. Env override; never below warm cap."""
    raw = (os.environ.get("ABCXAUTO_NEWS_SYMBOL_COLD_S") or "").strip()
    if raw:
        try:
            return max(news_symbol_s(), min(20.0, float(raw)))
        except ValueError:
            pass
    return max(news_symbol_s(), float(NEWS_SYMBOL_COLD_S))


def news_batch_s() -> float:
    """Wall-clock ceiling for one parallel news() batch. Env override; 4–20s."""
    raw = (os.environ.get("ABCXAUTO_NEWS_BATCH_S") or "").strip()
    if raw:
        try:
            return max(4.0, min(20.0, float(raw)))
        except ValueError:
            pass
    return float(NEWS_BATCH_S)


def news_breaker_failures() -> int:
    raw = (os.environ.get("ABCXAUTO_NEWS_BREAKER_FAILURES") or "").strip()
    if raw:
        try:
            return max(2, min(12, int(raw)))
        except ValueError:
            pass
    return int(NEWS_BREAKER_FAILURES)


def news_breaker_cooldown_s() -> float:
    raw = (os.environ.get("ABCXAUTO_NEWS_BREAKER_COOLDOWN_S") or "").strip()
    if raw:
        try:
            return max(15.0, min(600.0, float(raw)))
        except ValueError:
            pass
    return float(NEWS_BREAKER_COOLDOWN_S)


def _symbol_timeout_s(sym: str) -> float:
    if sym in _WARM_SYMBOLS:
        return news_symbol_s()
    return news_symbol_cold_s()


def reset_news_cache() -> None:
    _CACHE.update(ts=0.0, items=[], symbols=[])
    _HEADLINES.clear()
    _BREAKERS.clear()
    _WARM_SYMBOLS.clear()
    _FEED_BREAKER.update(failures=0, open_until=0.0, logged_open=False)


def is_real_headline(item: Any) -> bool:
    """True for a print. Timeout/error placeholders are not headlines."""
    if not isinstance(item, dict) or item.get("error"):
        return False
    hl = str(item.get("headline") or "").strip()
    if not hl:
        return False
    return not hl.startswith("(unavailable")


def _strip_stale(it: dict) -> dict:
    row = dict(it)
    row.pop("stale", None)
    row.pop("stale_age_s", None)
    return row


def remember_headlines(items: list[dict] | None) -> None:
    """Keep rail / think prints so news() can return them after a fetch miss."""
    now = time.monotonic()
    for it in items or []:
        if not is_real_headline(it):
            continue
        sym = str(it.get("symbol") or "").upper().strip()
        if not sym:
            continue
        bucket = _HEADLINES.setdefault(sym, {"ts": now, "items": []})
        rows = list(bucket.get("items") or [])
        seen = {str(x.get("headline") or "").strip() for x in rows}
        hl = str(it.get("headline") or "").strip()
        was_stale = bool(it.get("stale"))
        row = _strip_stale(it)
        if hl and hl not in seen:
            rows.append(row)
            bucket["items"] = rows[:8]
            bucket["ts"] = now
        elif hl in seen and not was_stale:
            bucket["ts"] = now


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
        bucket_rows: list[dict] = []
        for it in bucket.get("items") or []:
            if is_real_headline(it):
                bucket_rows.append(it)
        if bucket_rows:
            out.extend(_mark_stale(bucket_rows, age))
    for sym in dead:
        _HEADLINES.pop(sym, None)
    cache_age = now - float(_CACHE.get("ts") or 0.0)
    if _CACHE.get("items") and cache_age < _CACHE_TTL_S:
        cache_rows: list[dict] = []
        for it in _CACHE["items"]:
            if not is_real_headline(it):
                continue
            su = str(it.get("symbol") or "").upper().strip()
            if want is not None and su not in want:
                continue
            cache_rows.append(it)
        if cache_rows:
            out.extend(_mark_stale(cache_rows, cache_age))
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


def _breaker_unavailable(
    symbol: str,
    *,
    retry_after_s: float,
    reason: str = "source down",
) -> dict:
    return {
        "symbol": symbol,
        "status": "source_unavailable",
        "source_status": "circuit_open",
        "error": reason,
        "headline": f"(unavailable - news source down, retry in {int(retry_after_s)}s)",
        "retry_after_s": int(max(0.0, retry_after_s)),
    }


def _mark_stale(items: list[dict], age_s: float) -> list[dict]:
    out: list[dict] = []
    for it in items:
        row = dict(it)
        row["stale"] = True
        row["stale_age_s"] = int(max(0.0, age_s))
        out.append(row)
    return out


def _sym_breaker_open(sym: str, now: float) -> bool:
    br = _BREAKERS.get(sym)
    return bool(br and float(br.get("open_until") or 0.0) > now)


def _feed_breaker_open(now: float) -> bool:
    return float(_FEED_BREAKER.get("open_until") or 0.0) > now


def _breaker_retry_after(sym: str, now: float) -> float:
    sym_until = float((_BREAKERS.get(sym) or {}).get("open_until") or 0.0)
    feed_until = float(_FEED_BREAKER.get("open_until") or 0.0)
    return max(0.0, max(sym_until, feed_until) - now)


def _note_symbol_failure(sym: str, *, reason: str, timeout_s: float) -> None:
    now = time.monotonic()
    br = _BREAKERS.setdefault(sym, {"failures": 0, "open_until": 0.0, "logged_open": False})
    br["failures"] = int(br.get("failures") or 0) + 1
    threshold = news_breaker_failures()
    cooldown = news_breaker_cooldown_s()
    if br["failures"] >= threshold:
        br["open_until"] = now + cooldown
        if not br.get("logged_open"):
            br["logged_open"] = True
            logger.warning(
                "news %s circuit open for %.0fs after %d %s",
                sym,
                cooldown,
                threshold,
                reason,
            )
    elif br["failures"] == 1:
        logger.warning("news %s %s after %.0fs", sym, reason, timeout_s)


def _note_symbol_success(sym: str) -> None:
    _BREAKERS.pop(sym, None)
    _WARM_SYMBOLS.add(sym)
    _FEED_BREAKER.update(failures=0, open_until=0.0, logged_open=False)


def _note_feed_batch_outcome(*, had_success: bool, had_failure: bool) -> None:
    if had_success:
        _FEED_BREAKER.update(failures=0, open_until=0.0, logged_open=False)
        return
    if not had_failure:
        return
    now = time.monotonic()
    n = int(_FEED_BREAKER.get("failures") or 0) + 1
    _FEED_BREAKER["failures"] = n
    threshold = news_breaker_failures()
    cooldown = news_breaker_cooldown_s()
    if n >= threshold:
        _FEED_BREAKER["open_until"] = now + cooldown
        if not _FEED_BREAKER.get("logged_open"):
            _FEED_BREAKER["logged_open"] = True
            logger.warning(
                "news feed circuit open for %.0fs after %d all-miss batches",
                cooldown,
                threshold,
            )


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

    now = time.monotonic()
    if _feed_breaker_open(now) or _sym_breaker_open(sym, now):
        cached = remembered_headlines([sym])
        if cached:
            return cached, None
        return [], "source down"

    reason: str | None = None
    tries = max(1, int(NEWS_TRIES))
    timeout_s = _symbol_timeout_s(sym)
    for _attempt in range(tries):
        try:
            rows = await asyncio.wait_for(
                client.get_stock_news(sym, countback=per_symbol),
                timeout=timeout_s,
            )
            landed = list(rows or [])
            remember_headlines(landed)
            _note_symbol_success(sym)
            if landed:
                return landed, None
            cached = remembered_headlines([sym])
            return (cached, None) if cached else ([], None)
        except asyncio.TimeoutError:
            reason = "timed out"
            _note_symbol_failure(sym, reason="timed out", timeout_s=timeout_s)
        except Exception:
            reason = "error"
            _note_symbol_failure(sym, reason="error", timeout_s=timeout_s)
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
    had_success = False
    had_failure = False
    now = time.monotonic()
    task_map = {
        asyncio.create_task(_fetch_symbol_news(client, s, per_symbol=per_symbol)): s
        for s in out
    }
    pending = set(task_map.keys())
    batches_by_sym: dict[str, tuple[list[dict], str | None]] = {}
    deadline = time.monotonic() + news_batch_s()
    try:
        while pending:
            wait_s = deadline - time.monotonic()
            if wait_s <= 0:
                break
            done, pending = await asyncio.wait(pending, timeout=wait_s)
            for task in done:
                sym = task_map[task]
                try:
                    batches_by_sym[sym] = task.result()
                except Exception:
                    logger.exception("news fetch task failed for %s", sym)
                    batches_by_sym[sym] = ([], "error")
        for task in pending:
            sym = task_map[task]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("news fetch cancel failed for %s", sym)
            timeout_s = _symbol_timeout_s(sym)
            _note_symbol_failure(sym, reason="timed out", timeout_s=timeout_s)
            batches_by_sym.setdefault(sym, ([], "timed out"))
        for sym, (batch, err) in (
            (s, batches_by_sym.get(s, ([], "timed out"))) for s in out
        ):
            if err:
                had_failure = True
                if batch:
                    items.extend(batch)
                    had_success = True
                elif err == "source down":
                    cached = remembered_headlines([sym])
                    if cached:
                        items.extend(cached)
                        had_success = True
                    else:
                        misses.append(
                            _breaker_unavailable(
                                sym,
                                retry_after_s=_breaker_retry_after(sym, now),
                                reason="source down",
                            )
                        )
                else:
                    cached = remembered_headlines([sym])
                    if cached:
                        items.extend(cached)
                        had_success = True
                    else:
                        misses.append(_miss(sym, err))
            else:
                had_success = True
                items.extend(batch)
        _note_feed_batch_outcome(had_success=had_success, had_failure=had_failure)
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
