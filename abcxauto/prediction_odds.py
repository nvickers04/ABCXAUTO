"""Polymarket implied probabilities — discovery signal, not send geometry."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
EVENT_CAP = 6
MARKET_CAP = 4
SEARCH_CAP = 4
RELATED_CAP = 4
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
    "NVDA": "Nvidia",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
    "GOOGL": "Google",
    "GOOG": "Google",
    "META": "Meta",
    "TSLA": "Tesla",
    "AVGO": "Broadcom",
}

_INDEX_SYMS = frozenset({"SPY", "QQQ", "IWM", "DIA", "VIX"})
_EARN_HINTS = (
    "earnings",
    "eps",
    "beat estimates",
    "quarterly",
    "q1",
    "q2",
    "q3",
    "q4",
    "revenue",
    "gross margin",
)
_RATE_HINTS = (
    "fed",
    "fomc",
    "cpi",
    "powell",
    "inflation",
    "rate cut",
    "rate hike",
    "interest rate",
    "rates",
    "federal reserve",
)
_INDEX_HINTS = ("s&p", "s & p", "spx", "nasdaq", "russell", "dow", "vix")
_WEAK_ALIAS_PARTS = frozenset({"banks", "fed", "oil", "magnificent", "the", "and"})


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


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        s = str(raw or "").strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _wordish(blob: str, hint: str) -> bool:
    text = (blob or "").lower()
    h = hint.lower()
    if any(ch in h for ch in " &"):
        return h in text
    return re.search(rf"\b{re.escape(h)}\b", text) is not None


def _has_hint(blob: str, hints: tuple[str, ...]) -> bool:
    return any(_wordish(blob, h) for h in hints)


def _display_name(sym: str) -> str:
    s = str(sym or "").upper().strip()
    return _ALIASES.get(s, s) if s else ""


def _resolve_ask(text: str) -> str:
    q = (text or "").strip()
    if not q:
        return ""
    return _ALIASES.get(q.upper(), q)


def _points_to_macro(sym: str, name: str) -> bool:
    su = str(sym or "").upper().strip()
    if su in _INDEX_SYMS:
        return True
    return _has_hint(f"{su} {name}", _RATE_HINTS) or _has_hint(f"{su} {name}", _INDEX_HINTS)


def _should_add_earnings(name: str) -> bool:
    n = (name or "").strip()
    if not n or _has_hint(n, _EARN_HINTS):
        return False
    if _has_hint(n, _RATE_HINTS) and not _has_hint(n, _INDEX_HINTS):
        return False
    return True


def _covers(have: list[str], term: str) -> bool:
    t = term.lower()
    return any(t in h.lower() for h in have)


def _company_hints() -> tuple[str, ...]:
    hints: list[str] = []
    for sym, alias in _ALIASES.items():
        if sym in _INDEX_SYMS:
            continue
        if len(sym) >= 2:
            hints.append(sym.lower())
        for part in re.findall(r"[a-z0-9]+", str(alias).lower()):
            if part not in _WEAK_ALIAS_PARTS and len(part) > 2:
                hints.append(part)
    return tuple(dict.fromkeys(hints))


_COMPANY_HINTS = _company_hints()


def _event_kind(title: str, questions: list[str]) -> str:
    blob = " ".join([title, *questions])
    if _has_hint(blob, _EARN_HINTS):
        return "earnings"
    if _has_hint(blob, _RATE_HINTS):
        return "rates"
    if _has_hint(blob, _INDEX_HINTS):
        return "index"
    if _has_hint(blob, _COMPANY_HINTS):
        return "company"
    return "other"


def related_search_set(symbols: list[str], query: str = "") -> list[str]:
    """Company / ticker / earnings / pointed macro. Empty ask stays empty — no index tape."""
    lead: list[str] = []
    names: list[str] = []
    earnings: list[str] = []
    tickers: list[str] = []
    macro: list[str] = []

    raw_q = (query or "").strip()
    q = _resolve_ask(raw_q)
    if q:
        lead.append(q)
        raw_sym = raw_q.upper()
        if _should_add_earnings(q):
            earnings.append(f"{q} earnings")
        if raw_sym in _ALIASES and _ALIASES[raw_sym].lower() != raw_sym.lower():
            tickers.append(raw_sym)
        else:
            for sym, alias in _ALIASES.items():
                if alias.lower() == q.lower() and sym.lower() != q.lower():
                    tickers.append(sym)
                    break
        q_sym = raw_sym if raw_sym in _ALIASES or raw_sym in _INDEX_SYMS else ""
        if _points_to_macro(q_sym, q):
            for term in ("Fed", "CPI"):
                if not _covers(lead, term):
                    macro.append(term)

    for sym in symbols:
        s = str(sym or "").upper().strip()
        if not s:
            continue
        name = _display_name(s)
        names.append(name)
        if _should_add_earnings(name):
            earnings.append(f"{name} earnings")
        if name.lower() != s.lower():
            tickers.append(s)
        if _points_to_macro(s, name):
            for term in ("Fed", "CPI"):
                if not _covers(lead + names + macro, term):
                    macro.append(term)

    return _unique(lead + names + earnings + tickers + macro)


def _queries(symbols: list[str], query: str) -> tuple[list[str], list[str]]:
    fan = related_search_set(symbols, query)
    searched = fan[:SEARCH_CAP]
    skip = {s.lower() for s in searched}
    related = [q for q in fan[SEARCH_CAP:] if q.lower() not in skip]
    return searched, related[:RELATED_CAP]


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
        "kind": _event_kind(title, [str(m.get("q") or "") for m in markets]),
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


def _empty_odds(*, note: str = "no_query") -> dict[str, Any]:
    return {
        "source": "polymarket",
        "freshness": "betting_book",
        "use": "crowd_odds_not_send_geometry",
        "searched": [],
        "related_queries": [],
        "events": [],
        "note": note,
    }


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
    searches, related = _queries(syms, query)
    if not searches:
        return _empty_odds()
    events: list[dict[str, Any]] = []
    own = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT_S)
    take = max(2, EVENT_CAP // max(1, len(searches)))
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
            events.extend(list(payload.get("events") or [])[:take])
    finally:
        if own:
            await http.aclose()
    rows = _merge_events(events)
    return {
        "source": "polymarket",
        "freshness": "betting_book",
        "use": "crowd_odds_not_send_geometry",
        "searched": searches,
        "related_queries": related,
        "events": rows,
    }
