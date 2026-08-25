"""Polymarket implied probabilities — discovery signal, not send geometry."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
EVENT_CAP = 6
MARKET_CAP = 4
SEARCH_CAP = 4
_TIMEOUT_S = 8.0

# Search strings for names we actually trade. Not a strategy menu.
_ALIASES = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq",
    "IWM": "Russell 2000",
    "DIA": "Dow",
    "XLF": "banks Fed",
    "XLE": "oil",
    "XLK": "Magnificent 7",
    "JPM": "JPM banks",
    "VIX": "VIX",
}


def _gamma_url() -> str:
    raw = (os.environ.get("ABCXAUTO_ODDS_URL") or "").strip()
    return raw.rstrip("/") if raw else GAMMA


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _num(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _implied_px(raw: Any) -> float | None:
    """Crowd book share in [0, 1]. Not last, not cents, not a ticket price."""
    if isinstance(raw, bool):
        return None
    val = _num(raw)
    if val is None or val != val or val < 0.0 or val > 1.0:
        return None
    return val


def _queries(symbols: list[str], query: str) -> list[str]:
    out: list[str] = []
    q = (query or "").strip()
    if q:
        out.append(q)
    for sym in symbols:
        s = str(sym or "").upper().strip()
        if not s:
            continue
        alias = _ALIASES.get(s, s)
        if alias not in out:
            out.append(alias)
        if len(out) >= SEARCH_CAP:
            break
    return out[:SEARCH_CAP]


def compact_event(ev: dict[str, Any], *, market_cap: int = MARKET_CAP) -> dict[str, Any] | None:
    if not isinstance(ev, dict):
        return None
    if ev.get("closed") or ev.get("archived"):
        return None
    title = str(ev.get("title") or "").strip()
    if not title:
        return None
    slug = str(ev.get("slug") or "").strip()
    markets: list[dict[str, Any]] = []
    for m in (ev.get("markets") or [])[: market_cap * 2]:
        if not isinstance(m, dict):
            continue
        if m.get("closed") or m.get("archived"):
            continue
        outcomes = [str(x) for x in _json_list(m.get("outcomes"))]
        prices = _json_list(m.get("outcomePrices"))
        implied: list[dict[str, Any]] = []
        for name, px in zip(outcomes, prices):
            val = _implied_px(px)
            if val is None:
                continue
            implied.append({"name": name, "px": round(val, 4)})
        if not implied:
            continue
        q = str(m.get("question") or title).strip()
        markets.append({
            "q": q[:180],
            "implied": implied,
            "vol": _num(m.get("volume")),
            "end": m.get("endDate") or ev.get("endDate"),
        })
        if len(markets) >= market_cap:
            break
    if not markets:
        return None
    return {
        "title": title[:160],
        "end": ev.get("endDate"),
        "vol": _num(ev.get("volume")),
        "url": f"https://polymarket.com/event/{slug}" if slug else None,
        "markets": markets,
    }


def _merge_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in rows:
        compact = compact_event(ev)
        if not compact:
            continue
        key = str(compact.get("url") or compact.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(compact)
        if len(out) >= EVENT_CAP:
            break
    return out


async def fetch_odds(
    *,
    symbols: list[str] | None = None,
    query: str = "",
    positions: list[dict] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Crowd implied probs from Polymarket. Not IBKR last. Not a ticket."""
    syms = [str(s).upper() for s in (symbols or []) if str(s).strip()]
    if not syms and not (query or "").strip():
        for p in positions or []:
            s = str((p or {}).get("symbol") or "").upper().strip()
            if s and s not in syms:
                syms.append(s)
            if len(syms) >= SEARCH_CAP:
                break
    searches = _queries(syms, query)
    if not searches:
        return {
            "source": "polymarket",
            "freshness": "betting_book",
            "use": "crowd_odds_not_send_geometry",
            "searched": [],
            "events": [],
            "note": "no_query",
        }
    events: list[dict[str, Any]] = []
    own = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT_S)
    try:
        for q in searches:
            try:
                resp = await http.get(
                    f"{_gamma_url()}/public-search",
                    params={"q": q},
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.debug("odds search failed q=%s: %s", q, exc)
                continue
            if not isinstance(payload, dict):
                continue
            events.extend(list(payload.get("events") or [])[:EVENT_CAP])
    finally:
        if own:
            await http.aclose()
    rows = _merge_events(events)
    return {
        "source": "polymarket",
        "freshness": "betting_book",
        "use": "crowd_odds_not_send_geometry",
        "searched": searches,
        "events": rows,
    }
