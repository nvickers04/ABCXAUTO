"""Grok tools: scan tape, option chain compact, AGENT_TOOLS, ``_run_tool``.

Clip, wake, and model calls stay in ``brain.py``. Look up clip and scan
entry points on ``abcxauto.brain`` so tests can monkeypatch them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from xai_sdk.chat import tool

from abcxauto.news_feed import is_real_headline
from abcxauto.opportunity_scan import normalize_tickers
from abcxauto.order_examples import ticket_strategy_names
from abcxauto.think_stream import emit as think_emit
from abcxauto.tools import run_readonly_tool
from abcxauto.tool_args import (
    CANDLE_CAP,
    CHAIN_CAP,
    OPTION_QUOTE_CAP,
    bind_send_card,
    fallback_quote_symbols,
    normalize_tool_call,
    option_quote_specs,
)
from abcxauto.world_state import WorldState

if TYPE_CHECKING:
    from abcxauto.brain import BrainTurn

logger = logging.getLogger(__name__)


def _hub():
    import abcxauto.brain as brain

    return brain

# One wake = one linear think. This ceiling is a runaway-spend guard, not a
# budget the model should feel — repeated reads are answered from the ledger
# below, so an honest think finishes long before it.
MAX_TOOL_STEPS = 64
TOOL_S = 20.0
SEND_S = 45.0
CHAIN_S = 60.0
CANDLE_S = 35.0
# Bars are gathered in parallel. The wait is the slowest name, not n times.
CANDLE_WAIT_S = 8.0
# One chain page. A multi-name chain does not get 22s each.
CHAIN_WAIT_S = 12.0
SCAN_S = 35.0
# Ranked 30-row page is ~5,811 chars. Provenance ~300, envelope ~400.
# with=news stays a short top-level list (no nested row.mda.news).
# Slim headlines ≈ 1,600. with=metrics on 8 rows ≈ 1,600.
# 28_000 leaves room for a ranked page plus optional with=news/metrics.
SCAN_CLIP_CHARS = 28_000
_QUOTE_SCHEMA = {"type": "string", "description": "Ticker, e.g. AAPL"}
_SYMBOLS_SCHEMA = {"type": "array", "items": {"type": "string"}}
_CANDLE_RESOLUTION_ENUM = ["D", "15", "5", "60"]
_OPTION_RIGHT_ENUM = ["C", "P"]
_STOCK_TYPE_ENUM = ["CORP", "ETF", "both"]
_SCAN_WITH_ENUM = ["news", "metrics"]
_XML_VERIFIED_FILTER_KEYS = frozenset(
    {"peRatioAbove", "peRatioBelow", "industry", "sector"}
)
_SCAN_ALWAYS_FILTERS = (
    "market_cap_above",
    "market_cap_below",
    "above_price",
    "below_price",
    "above_volume",
    "average_option_volume_above",
    "usdMarketCapAbove",
    "optVolumeAbove",
    "avgVolumeAbove",
    "stock_type",
)


def _scan_arena_keys() -> list[str]:
    try:
        from abcxauto.universe import known_screen_keys

        return known_screen_keys()
    except Exception:
        return [
            "most_active",
            "top_gainers",
            "top_losers",
            "hot_by_volume",
            "MOST_ACTIVE",
            "TOP_PERC_GAIN",
            "TOP_PERC_LOSE",
            "HOT_BY_VOLUME",
        ]


def _scan_code_keys() -> list[str]:
    try:
        from abcxauto.universe import known_scan_codes

        return known_scan_codes()
    except Exception:
        return [
            "MOST_ACTIVE",
            "TOP_PERC_GAIN",
            "TOP_PERC_LOSE",
            "HOT_BY_VOLUME",
        ]


def _clip_scan(data: Any) -> str:
    """Scan page + news must survive. Default 8k clip drops ranked rows."""
    if isinstance(data, dict):
        data = _scan_public_payload(data)
    return _hub()._clip(data, max_chars=SCAN_CLIP_CHARS)


def _scan_filter_error(
    args: dict[str, Any],
    parsed: dict[str, Any],
    *,
    pe_tags: frozenset[str] | None = None,
    industry_tags: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Unknown key stays unknown. Unverified XML filters name what is live."""
    err = str(parsed.get("error") or "bad scan filters")
    verified = set(pe_tags or ()) | set(industry_tags or ())
    asked = [
        key
        for key in _XML_VERIFIED_FILTER_KEYS
        if (args or {}).get(key) not in (None, "")
    ]
    unverified = [key for key in asked if key not in verified]
    if unverified and any(key in err for key in unverified):
        available = list(_SCAN_ALWAYS_FILTERS) + sorted(verified)
        err = (
            f"scan filter not XML-verified: {', '.join(unverified)}; "
            f"available={','.join(available)}"
        )
    return {"ok": False, "error": err}


def _news_symbols_for_scan(
    merged: dict[str, Any] | None,
    pulled: list[str] | None,
) -> list[str]:
    """Headlines for the gap tape, not the first MOST_ACTIVE page."""
    from abcxauto.prints import mda_worth_asking

    order: list[str] = []
    rows = (merged or {}).get("rows") if isinstance(merged, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        su = str(row.get("symbol") or "").upper().strip()
        if su and su not in order:
            order.append(su)
    for raw in pulled or []:
        su = str(raw or "").upper().strip()
        if su and su not in order:
            order.append(su)
    return [s for s in order[:8] if mda_worth_asking(s)]


def _news_symbols_this_look(
    world: Any,
    snap: dict[str, Any] | None,
    asked: list[str] | None,
) -> list[str]:
    """Names already on this look. Bare news() does not fetch them."""
    if asked:
        return list(asked)
    from abcxauto.prints import mda_worth_asking

    order: list[str] = []

    def _add(raw: Any) -> None:
        su = str(raw or "").upper().strip()
        if su and su not in order:
            order.append(su)

    for s in getattr(world, "scan_fetched", None) or []:
        _add(s)
    blob = snap if isinstance(snap, dict) else {}
    hits = blob.get("scan_hits") if isinstance(blob.get("scan_hits"), dict) else {}
    for row in hits.get("rows") or []:
        if isinstance(row, dict):
            _add(row.get("symbol"))
    return [s for s in order[:12] if mda_worth_asking(s)]


_LOOK_SCAN_CACHE_KEY = "look"
_SCAN_LOOK_SNAP_KEYS = (
    "scan_screens",
    "scan_hits",
    "scan_calls",
    "scan_fetched",
    "scan_at",
    "scan_news_attached",
    "scan_arenas",
    "scan_flush",
    "scan_streamed",
    "scan_provenance",
)


def _scan_screen_key(arena: str = "", scan_code: str = "") -> str:
    name = str(arena or "").strip()
    code = str(scan_code or "").strip()
    if name and code:
        return f"{name}:{code}"
    return name or code


def _record_scan_screen(snap: dict[str, Any], arena: str, scan_code: str) -> None:
    key = _scan_screen_key(arena, scan_code)
    if not key:
        return
    seen_screens = [str(x) for x in (snap.get("scan_screens") or [])]
    if key not in seen_screens:
        seen_screens.append(key)
    snap["scan_screens"] = seen_screens
    if arena:
        seen = [str(x) for x in (snap.get("scan_arenas") or [])]
        if arena not in seen:
            seen.append(arena)
        snap["scan_arenas"] = seen


def _canonical_scan_screen(arena: str = "", scan_code: str = "") -> tuple[str, str]:
    """Resolve arena/scan_code aliases to one screen identity."""
    raw_arena = str(arena or "").strip()
    raw_code = str(scan_code or "").strip()
    if not raw_arena and not raw_code:
        return "", ""
    try:
        from abcxauto.universe import resolve_screen

        resolved = resolve_screen(
            arena=raw_arena or None,
            scan_code=raw_code or None,
        )
    except Exception:
        return raw_arena, raw_code
    if not resolved.get("ok"):
        return raw_arena, raw_code
    return (
        str(resolved.get("arena_id") or raw_arena),
        str(resolved.get("scan_code") or raw_code),
    )


def _scan_look_key(args: dict[str, Any] | None) -> str:
    """One key per asked screen. Bare scan() is the catalog, not a fetch."""
    bag = args if isinstance(args, dict) else {}
    arena, code = _canonical_scan_screen(
        str(bag.get("arena") or "").strip(),
        str(bag.get("scan_code") or "").strip(),
    )
    if not arena and not code:
        return _LOOK_SCAN_CACHE_KEY
    return json.dumps(
        {
            "arena": arena.strip().lower(),
            "scan_code": code.strip().upper(),
        },
        sort_keys=True,
    )


def _scan_snap_bag(snap: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snap, dict):
        return {}
    return {k: deepcopy(snap[k]) for k in _SCAN_LOOK_SNAP_KEYS if k in snap}


def _restore_scan_snap(snap: dict[str, Any] | None, bag: dict[str, Any] | None) -> None:
    if not isinstance(snap, dict) or not bag:
        return
    for key, val in bag.items():
        snap[key] = val


def _scan_gate_facts(
    rows: list[Any] | None,
    book: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deepest |open_gap| on this tape after ``scan_skip_class``.

    Levered / micro never occupy ``deepest``; those hits stay on the tape.
    """
    _ = book
    try:
        from abcxauto.think_stream import _signed_open_gap
    except Exception:
        return {}
    try:
        from abcxauto.universe import scan_skip_class as skip_of
    except Exception:
        skip_of = None
    try:
        from abcxauto.opportunity_scan import row_gap_pct
    except Exception:
        row_gap_pct = None  # type: ignore[assignment]
    deepest = None
    deepest_sym = ""
    deepest_signed = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        gap = row_gap_pct(row) if row_gap_pct is not None else row.get("open_gap_pct")
        if gap is None and row.get("gap%") is None and row.get("open_gap_pct") is None:
            continue
        if skip_of and skip_of(row):
            continue
        signed = _signed_open_gap(row)
        mag = abs(signed)
        if deepest is None or mag > deepest:
            deepest = mag
            deepest_signed = signed
            deepest_sym = str(row.get("symbol") or "").upper()
    return {
        "deepest_open_gap_pct": deepest_signed,
        "deepest_symbol": deepest_sym or None,
    }


def _union_scan_hits(prior: Any, incoming: Any) -> dict[str, Any]:
    """Merge this look's screens by symbol. No page cap — one tape."""
    from abcxauto.opportunity_scan import scrub_aliased_gap_pct
    from abcxauto.think_stream import sort_scan_rows, _open_gap_mag

    old = prior if isinstance(prior, dict) else {}
    new = incoming if isinstance(incoming, dict) else {}
    by: dict[str, dict[str, Any]] = {}
    for row in list(old.get("rows") or []) + list(new.get("rows") or []):
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        prev = by.get(sym)
        if prev is None:
            by[sym] = scrub_aliased_gap_pct(_strip_hit_news(dict(row)))
            continue
        keep = dict(prev)
        if _open_gap_mag(row) > _open_gap_mag(prev):
            keep.update({k: v for k, v in row.items() if v not in (None, "")})
        else:
            for key, val in row.items():
                if keep.get(key) in (None, "") and val not in (None, ""):
                    keep[key] = val
        by[sym] = scrub_aliased_gap_pct(_strip_hit_news(keep))
    rows = sort_scan_rows(list(by.values()))
    quoted = sum(1 for r in rows if r.get("last") is not None)
    meta = new if new.get("rows") else old
    source = str(new.get("source") or old.get("source") or "")
    if source == "empty" and old.get("source") and old.get("source") != "empty":
        source = str(old.get("source"))
    return {
        "source": source,
        "arena": meta.get("arena") or new.get("arena") or old.get("arena"),
        "scan_code": meta.get("scan_code") or new.get("scan_code") or old.get("scan_code"),
        "ranked": bool(old.get("ranked") or new.get("ranked")),
        "rank_meaning": new.get("rank_meaning") or old.get("rank_meaning") or "",
        "quoted": quoted if quoted else (new.get("quoted") or old.get("quoted") or 0),
        "rows": rows,
    }


def _scan_screen_on_look(snap: dict[str, Any], arena: str, code: str) -> bool:
    """True when this selector was already fetched this look."""
    if not arena and not code:
        return False
    key = _scan_screen_key(arena, code)
    used = [str(x) for x in (snap.get("scan_screens") or [])]
    return bool(key and key in used)


def _scan_out_from_snap(
    snap: dict[str, Any],
    qmap: dict[str, Any] | None,
    *,
    last_ok: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Public page is THIS job. Snap may still union internally."""
    seed = last_ok if isinstance(last_ok, dict) else {}
    job_rows = [
        r for r in (seed.get("hits") or seed.get("rows") or []) if isinstance(r, dict)
    ]
    rows = _scan_paint_rows({"rows": job_rows}, quotes=qmap)
    already = [str(x) for x in (snap.get("scan_screens") or []) if str(x).strip()]
    arenas = list(snap.get("scan_arenas") or [])
    symbols_job = _scan_is_symbols_job(seed)
    if symbols_job:
        ranked = bool(seed.get("ranked"))
        rank_meaning = seed.get("rank_meaning") or "not ranked"
        if str(rank_meaning) == "not ranked":
            ranked = False
        prov = seed.get("provenance") if isinstance(seed.get("provenance"), dict) else None
        source = str(seed.get("source") or "symbols")
    else:
        ranked = bool(seed.get("ranked"))
        rank_meaning = seed.get("rank_meaning") or ""
        prov = seed.get("provenance")
        if not isinstance(prov, dict):
            stored = snap.get("scan_provenance")
            prov = stored if isinstance(stored, dict) else None
        source = seed.get("source") or "ibkr"
    applied = seed.get("applied") or {}
    if not applied and isinstance(prov, dict) and isinstance(prov.get("filters"), dict):
        applied = prov["filters"]
    quoted = sum(1 for r in rows if r.get("last") is not None)
    out: dict[str, Any] = {
        "ok": True,
        "source": source,
        "symbols": [r.get("symbol") for r in rows if r.get("symbol")],
        "hits": rows,
        "rows": rows,
        "applied": applied,
        "persisted": False,
        "ranked": ranked,
        "rank_meaning": rank_meaning,
        "quoted": quoted or seed.get("quoted") or 0,
        "already": already,
    }
    if arenas:
        out["arenas"] = arenas
    if not symbols_job:
        if seed.get("arena") not in (None, ""):
            out["arena"] = seed.get("arena")
        if seed.get("scan_code") not in (None, ""):
            out["scan_code"] = seed.get("scan_code")
    if isinstance(prov, dict):
        out["provenance"] = prov
    if seed.get("empty") is not None:
        out["empty"] = bool(seed.get("empty"))
    elif isinstance(prov, dict) and prov.get("empty") is not None:
        out["empty"] = bool(prov.get("empty"))
    else:
        out["empty"] = not bool(rows)
    if symbols_job:
        screen = seed.get("screen")
    else:
        screen = seed.get("screen") or (
            prov.get("screen") if isinstance(prov, dict) else None
        )
    if screen not in (None, ""):
        out["screen"] = screen
    if seed.get("thin") is not None:
        out["thin"] = bool(seed.get("thin"))
    else:
        from abcxauto.opportunity_scan import is_thin_ranked_row

        if rows and all(is_thin_ranked_row(r) for r in rows):
            out["thin"] = True
    if seed.get("criteria") is not None:
        out["criteria"] = seed.get("criteria")
    if seed.get("sort") is not None:
        out["sort"] = seed.get("sort")
    out.update(_scan_gate_facts(rows))
    return out


def _scan_catalog_payload() -> dict[str, Any]:
    from abcxauto.universe import scan_screen_catalog

    return {
        "ok": True,
        "need": "arena | scan_code | symbols[]",
        "screens": scan_screen_catalog(),
        "with": list(_SCAN_WITH_ENUM),
        "note": "one screen per call",
        "fetched": False,
        "send_geometry": False,
    }


_SCAN_NEWS_KEEP = (
    "symbol",
    "headline",
    "publisher",
    "published",
    "as_of",
    "asof_iso",
    "url",
    "use",
    "freshness",
    "source",
)


def _public_news_item(item: Any) -> dict[str, Any] | None:
    """One headline Grok can cross-ref. Outlet is publisher, feed is source."""
    try:
        from abcxauto.news_feed import public_news_item

        row = public_news_item(item)
        if isinstance(row, dict) and row.get("headline"):
            return row
        if row is None and not is_real_headline(item):
            return None
    except Exception:
        row = None
    if not is_real_headline(item) or not isinstance(item, dict):
        return None
    slim: dict[str, Any] = {}
    for key in _SCAN_NEWS_KEEP:
        val = item.get(key)
        if val not in (None, ""):
            slim[key] = val
    if "publisher" not in slim:
        outlet = str(item.get("publisher") or "").strip()
        src = str(item.get("source") or "").strip()
        if not outlet and src.lower() not in ("mda", "ibkr", "web"):
            outlet = src
        if outlet:
            slim["publisher"] = outlet
    if slim.get("source") in (None, "") or str(slim.get("source") or "").lower() not in (
        "mda",
        "ibkr",
        "web",
    ):
        slim["source"] = "mda"
    slim.setdefault("freshness", "delayed_15m")
    slim.setdefault("use", "color_not_trigger")
    return slim if slim.get("headline") else None


def _public_news_items(items: Any) -> list[dict[str, Any]]:
    try:
        from abcxauto.news_feed import public_news_items

        rows = public_news_items(items)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict) and r.get("headline")]
    except Exception:
        pass
    out: list[dict[str, Any]] = []
    for it in items or []:
        slim = _public_news_item(it)
        if slim:
            out.append(slim)
    return out


def _slim_scan_news(items: Any) -> list[dict[str, Any]]:
    """Headline color only. Timeouts and error rows are not headlines."""
    return _public_news_items(items)


def _quote_need(already: list[str] | None = None) -> dict[str, Any]:
    """Bare quote/candles is a choice when the book is flat."""
    out: dict[str, Any] = {
        "ok": False,
        "need": "symbol | symbols[]",
        "source": "ibkr",
        "freshness": "live",
        "fetched": False,
        "note": "pass symbol or symbols[]",
    }
    names = [str(s).upper().strip() for s in (already or []) if str(s).strip()]
    if names:
        out["already"] = names[:8]
    return out


def _public_quote_row(row: dict[str, Any] | None) -> dict[str, Any]:
    """IBKR last/bid/ask plus mid/spread when both sides exist."""
    if not isinstance(row, dict):
        return {}
    out = dict(row)
    bid, ask = out.get("bid"), out.get("ask")
    try:
        bid_f = float(bid) if bid is not None else None
        ask_f = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        bid_f = ask_f = None
    if bid_f is not None and ask_f is not None and bid_f > 0 and ask_f > 0:
        out.setdefault("mid", round((bid_f + ask_f) / 2.0, 4))
        out["spread"] = round(ask_f - bid_f, 4)
    out.setdefault("source", "ibkr")
    out.setdefault("freshness", "live")
    return out


def _public_quote_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    quotes = out.get("quotes")
    if isinstance(quotes, list):
        out["quotes"] = [
            _public_quote_row(q) if isinstance(q, dict) else q for q in quotes
        ]
    elif out.get("symbol") or out.get("last") is not None:
        out = _public_quote_row(out)
    return out


def _candle_read(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Surface already-computed structure. Do not invent new indicators."""
    if not isinstance(row, dict):
        return None
    read: dict[str, Any] = {}
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    for key in (
        "bar_last",
        "sma20",
        "sma50",
        "dist20",
        "ret5",
        "above_sma20",
        "asof_iso",
        "as_of",
    ):
        if metrics.get(key) not in (None, ""):
            read[key] = metrics[key]
    bars = row.get("bars") if isinstance(row.get("bars"), list) else []
    last = bars[-1] if bars and isinstance(bars[-1], dict) else {}
    if last:
        bar = {
            k: last[k]
            for k in ("t", "t_iso", "o", "h", "l", "c", "v")
            if last.get(k) not in (None, "")
        }
        if bar:
            read["last_bar"] = bar
    return read or None


def _strip_hit_news(row: dict[str, Any]) -> dict[str, Any]:
    """Ranked rows keep metrics, not a second copy of headlines."""
    mda = row.get("mda")
    if not isinstance(mda, dict) or ("news" not in mda and "news_use" not in mda):
        return row
    item = dict(row)
    nest = dict(mda)
    nest.pop("news", None)
    nest.pop("news_use", None)
    if nest:
        item["mda"] = nest
    else:
        item.pop("mda", None)
    return item


def _scan_is_symbols_job(seed: dict[str, Any] | None) -> bool:
    """True when this call is symbols[] drill-down, not a ranked screen."""
    if not isinstance(seed, dict) or not seed:
        return False
    if str(seed.get("source") or "") == "symbols":
        return True
    criteria = seed.get("criteria")
    if isinstance(criteria, dict) and criteria.get("symbols") is not None:
        if not criteria.get("arena") and not criteria.get("scan_code"):
            return True
    if seed.get("provenance") is not None:
        return False
    if seed.get("screen") not in (None, "") or seed.get("arena") not in (
        None,
        "",
    ) or seed.get("scan_code") not in (None, ""):
        return False
    return str(seed.get("rank_meaning") or "") == "not ranked"


def _scan_public_hit(row: dict[str, Any]) -> dict[str, Any]:
    from abcxauto.opportunity_scan import scrub_aliased_gap_pct

    item = _strip_hit_news(row)
    if item is row:
        item = dict(item)
    item.pop("session", None)
    return scrub_aliased_gap_pct(item)


def _scan_public_payload(out: dict[str, Any]) -> dict[str, Any]:
    """One hit list, headlines once at the top. Snap may still keep rows."""
    slim = dict(out) if isinstance(out, dict) else {}
    slim.pop("sessions", None)
    if slim.get("rows") == slim.get("hits"):
        slim.pop("rows", None)
    hits = slim.get("hits")
    if isinstance(hits, list):
        slim["hits"] = [
            _scan_public_hit(row) if isinstance(row, dict) else row for row in hits
        ]
    news = slim.get("news")
    if isinstance(news, list):
        slim["news"] = _slim_scan_news(news)
        if not slim["news"]:
            slim.pop("news", None)
    if str(slim.get("rank_meaning") or "") == "not ranked":
        slim["ranked"] = False
    if _scan_is_symbols_job(slim):
        slim.pop("screen", None)
        slim.pop("arena", None)
        slim.pop("scan_code", None)
        prov = slim.get("provenance")
        if isinstance(prov, dict) and prov.get("screen") not in (None, ""):
            slim.pop("provenance", None)
    return slim


_SCAN_REUSE_NOTE = (
    "this look already has that screen — rows are on the first scan() page"
)


def _scan_reuse_stub(
    snap: dict[str, Any] | None = None,
    *,
    asked_arena: str = "",
    asked_code: str = "",
    cached: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pointer, not another copy of the look tape.

    A second scan() this look used to re-append the merged hits (and news).
    Grok then paid to parse the same bag and called it messy.
    """
    bag = snap if isinstance(snap, dict) else {}
    seed = cached if isinstance(cached, dict) else {}
    merged = bag.get("scan_hits") if isinstance(bag.get("scan_hits"), dict) else {}
    rows = [
        r
        for r in list(merged.get("rows") or seed.get("hits") or seed.get("rows") or [])
        if isinstance(r, dict)
    ]
    symbols = [
        str(r.get("symbol")).upper()
        for r in rows
        if r.get("symbol")
    ]
    if not symbols:
        symbols = [str(s).upper() for s in (seed.get("symbols") or []) if s]
    arenas = [
        str(x)
        for x in (bag.get("scan_arenas") or seed.get("already") or seed.get("arenas") or [])
        if str(x).strip()
    ]
    n = len(symbols)
    if not n:
        try:
            n = int(seed.get("hits_n") or 0)
        except (TypeError, ValueError):
            n = 0
    facts = _scan_gate_facts(rows) if rows else {
        "deepest_open_gap_pct": seed.get("deepest_open_gap_pct"),
        "deepest_symbol": seed.get("deepest_symbol"),
    }
    asked_arena = str(asked_arena or "").strip()
    asked_code = str(asked_code or "").strip()
    if not asked_arena and not asked_code and isinstance(seed.get("asked"), dict):
        asked_arena = str(seed["asked"].get("arena") or "").strip()
        asked_code = str(seed["asked"].get("scan_code") or "").strip()
    out: dict[str, Any] = {
        "ok": True,
        "source": str(merged.get("source") or seed.get("source") or "ibkr"),
        "reused": True,
        "repeat_of_this_think": True,
        "empty": n == 0,
        "note": _SCAN_REUSE_NOTE,
        "already": arenas,
        "hits_n": n,
        "symbols": symbols,
    }
    if asked_arena or asked_code:
        out["asked"] = {"arena": asked_arena, "scan_code": asked_code}
    for key, val in facts.items():
        if val is not None:
            out[key] = val
    return out


def _emit_scan_look_line(snap: dict[str, Any], out: dict[str, Any]) -> None:
    """One trophy line per look. Not per page. No screens=N."""
    if snap.get("scan_streamed"):
        return
    snap["scan_streamed"] = True
    n = len([s for s in (out.get("symbols") or []) if s])
    deepest = out.get("deepest_open_gap_pct")
    deep_s = f"{deepest:+.1f}%" if isinstance(deepest, (int, float)) else "n/a"
    bits = [f"hits={n}", f"deepest={deep_s}"]
    sym = str(out.get("deepest_symbol") or "").strip()
    if sym:
        bits.append(sym)
    bits.append(f"src={out.get('source') or 'empty'}")
    think_emit("tool", " ".join(bits) + "\n")


def _ingest_scan_payload(
    *,
    world: WorldState,
    snap: dict[str, Any],
    payload: dict[str, Any],
    job: dict[str, Any],
) -> None:
    syms = list(payload.get("symbols") or [])
    fetched = list(getattr(world, "scan_fetched", None) or [])
    seen = {str(x).upper() for x in fetched if x}
    for raw in syms:
        su = str(raw or "").upper().strip()
        if su and su not in seen:
            fetched.append(su)
            seen.add(su)
    world.scan_fetched = fetched
    snap["scan_fetched"] = list(fetched)
    stub_ideas = [
        {"symbol": s, "source": payload.get("source") or "scan"} for s in syms
    ]
    if stub_ideas:
        from abcxauto.opportunity_scan import merge_tape

        ideas = merge_tape(list(world.opportunities or []), stub_ideas)
        world.opportunities = ideas
        snap["opportunities"] = ideas
    hits = [r for r in (payload.get("hits") or []) if isinstance(r, dict)]
    for row in hits:
        if row.get("last") is not None:
            _hub()._stash_live(
                world,
                snap,
                {
                    "symbol": row.get("symbol"),
                    "last": row.get("last"),
                    "source": "ibkr",
                },
                mark=False,
            )
    incoming = {
        "source": str(payload.get("source") or ""),
        "arena": payload.get("arena"),
        "scan_code": payload.get("scan_code"),
        "ranked": bool(payload.get("ranked")),
        "rank_meaning": str(payload.get("rank_meaning") or ""),
        "quoted": int(payload.get("quoted") or 0),
        "rows": hits,
    }
    prior = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else None
    snap["scan_hits"] = _union_scan_hits(prior, incoming)
    if isinstance(payload.get("provenance"), dict):
        snap["scan_provenance"] = payload["provenance"]
    rec_arena, rec_code = _canonical_scan_screen(
        str(job.get("arena") or payload.get("arena") or ""),
        str(job.get("scan_code") or payload.get("scan_code") or ""),
    )
    _record_scan_screen(snap, rec_arena, rec_code)


def _quote_last(raw: Any) -> float | None:
    if isinstance(raw, (int, float)) and raw == raw:
        val = float(raw)
        return val if val > 0 else None
    if isinstance(raw, dict):
        for key in ("last", "price", "mid"):
            if raw.get(key) is None:
                continue
            try:
                val = float(raw[key])
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
    return None


def _scan_paint_rows(
    hits: dict[str, Any] | None,
    fallback: list[Any] | None = None,
    quotes: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from abcxauto.think_stream import sort_scan_rows

    rows = [r for r in ((hits or {}).get("rows") or []) if isinstance(r, dict)]
    if not rows:
        rows = [r for r in (fallback or []) if isinstance(r, dict)][:24]
    painted: list[dict[str, Any]] = []
    qmap = quotes if isinstance(quotes, dict) else {}
    from abcxauto.opportunity_scan import (
        is_thin_ranked_row,
        scrub_aliased_gap_pct,
        thin_ranked_row,
    )
    from abcxauto.universe import scan_skip_class

    for row in sort_scan_rows(rows):
        if is_thin_ranked_row(row):
            slim = thin_ranked_row(row)
            if slim:
                painted.append(slim)
            continue
        item = scrub_aliased_gap_pct(_strip_hit_news(dict(row)))
        sym = str(item.get("symbol") or "").upper().strip()
        px = _quote_last(qmap.get(sym))
        if px is not None:
            item["last"] = px
            close = item.get("close")
            try:
                prior = float(close) if close is not None else 0.0
            except (TypeError, ValueError):
                prior = 0.0
            if prior > 0:
                item["change_pct"] = round((px / prior - 1.0) * 100.0, 3)
        if "skip_class" not in item:
            item["skip_class"] = scan_skip_class(item)
        if not item.get("source"):
            item["source"] = "ibkr"
        painted.append(item)
    return painted


def _scan_open(snap: dict[str, Any] | None, symbol: str) -> Any:
    if not isinstance(snap, dict):
        return None
    want = str(symbol or "").strip().upper()
    hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
    for row in hits.get("rows") or []:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != want:
            continue
        if row.get("open") is not None:
            return row.get("open")
        ibkr = row.get("ibkr")
        if isinstance(ibkr, dict) and ibkr.get("open") is not None:
            return ibkr.get("open")
    qmap = snap.get("ibkr_live_quotes")
    if isinstance(qmap, dict):
        quote = qmap.get(want)
        if isinstance(quote, dict) and quote.get("open") is not None:
            return quote.get("open")
    return None


def _scan_gap_pct(snap: dict[str, Any] | None, symbol: str) -> Any:
    if not isinstance(snap, dict):
        return None
    want = str(symbol or "").strip().upper()
    hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
    for row in hits.get("rows") or []:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != want:
            continue
        if row.get("gap%") is not None:
            return row.get("gap%")
        if row.get("open_gap_pct") is not None:
            return row.get("open_gap_pct")
        ibkr = row.get("ibkr")
        if isinstance(ibkr, dict):
            if ibkr.get("gap%") is not None:
                return ibkr.get("gap%")
            if ibkr.get("open_gap_pct") is not None:
                return ibkr.get("open_gap_pct")
    qmap = snap.get("ibkr_live_quotes")
    if isinstance(qmap, dict):
        quote = qmap.get(want)
        if isinstance(quote, dict) and quote.get("open_gap_pct") is not None:
            return quote.get("open_gap_pct")
    return None


def _candle_res_from_tape(snap: dict[str, Any] | None) -> str:
    """Daily bars cannot answer an opening-low hold on a gap screen."""
    hits = snap.get("scan_hits") if isinstance(snap, dict) else None
    if not isinstance(hits, dict):
        return "D"
    for row in hits.get("rows") or []:
        if isinstance(row, dict) and (
            row.get("open_gap_pct") is not None or row.get("gap%") is not None
        ):
            return "5"
    return "D"


def _stamp_session_size(session: dict[str, Any], world: WorldState) -> None:
    """Drop invented size. Candles report range facts, not a proposed qty."""
    _ = world
    if isinstance(session, dict):
        session.pop("size", None)


def _stamp_session_ticket(session: dict[str, Any]) -> None:
    """Playbook is notes. Do not stamp a card onto candles as a ticket."""
    _ = session
    return


def _snap_is_rth(snap: dict[str, Any] | None) -> bool:
    s = snap if isinstance(snap, dict) else {}
    pulse = s.get("reality_pulse") if isinstance(s.get("reality_pulse"), dict) else {}
    sess = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
    if not sess:
        sess = s.get("session") if isinstance(s.get("session"), dict) else {}
    status = str(sess.get("status") or sess.get("session") or "").lower()
    if status == "regular":
        return True
    if status in ("premarket", "closed", "postmarket"):
        return False
    try:
        from abcxauto.opportunity_scan import rth_now

        return rth_now()
    except Exception:
        return False


def _live_open_session(
    snap: dict[str, Any] | None,
    symbol: str,
    *,
    last: Any = None,
    open_px: Any = None,
    open_gap_pct: Any = None,
) -> dict[str, Any] | None:
    from abcxauto.opportunity_scan import session_range_from_live_open

    want = str(symbol or "").upper()
    last_px = last
    open_last = open_px
    gap = open_gap_pct if open_gap_pct is not None else _scan_gap_pct(snap, want)
    hits = snap.get("scan_hits") if isinstance(snap, dict) and isinstance(snap.get("scan_hits"), dict) else {}
    for row in hits.get("rows") or []:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != want:
            continue
        if last_px is None:
            last_px = row.get("last")
        if open_last is None:
            open_last = row.get("open")
        if gap is None:
            gap = row.get("open_gap_pct")
        break
    return session_range_from_live_open(
        last=last_px,
        rth_open=open_last,
        open_gap_pct=gap,
        regular=_snap_is_rth(snap),
    )


def _finish_live_session(
    rng: dict[str, Any],
    *,
    snap: dict[str, Any],
    world: WorldState,
    symbol: str,
    tape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    src = tape if isinstance(tape, dict) else {}
    for key in ("bid", "ask", "spread", "spread_pct"):
        if src.get(key) is not None and rng.get(key) is None:
            rng[key] = src[key]
    _remember_session(snap, symbol, rng)
    kept = (snap.get("session_range") or {}).get(str(symbol).upper()) or rng
    _stamp_session_size(kept, world)
    _stamp_session_ticket(kept)
    return kept


def _session_rank(rng: dict[str, Any] | None) -> tuple:
    """Prefer today's multi-bar RTH range over a 1-print live open."""
    if not isinstance(rng, dict):
        return (0, 0, 0, 0.0)
    today = 1 if rng.get("today") is True else 0
    try:
        n = int(rng.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    hist = 0 if str(rng.get("print") or "") == "live_open" or n <= 1 else 1
    try:
        low = float(rng["low"]) if rng.get("low") is not None else None
        high = float(rng["high"]) if rng.get("high") is not None else None
        span = abs(high - low) if low is not None and high is not None else 0.0
    except (TypeError, ValueError):
        span = 0.0
    return (today, hist, n, span)


def _refresh_session_last(
    kept: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    out = dict(kept)
    if incoming.get("last") is None:
        return out
    try:
        last_px = float(incoming["last"])
    except (TypeError, ValueError):
        return out
    out["last"] = last_px
    try:
        open_px = float(out["open"]) if out.get("open") is not None else None
    except (TypeError, ValueError):
        open_px = None
    try:
        low_px = float(out["low"]) if out.get("low") is not None else None
    except (TypeError, ValueError):
        low_px = None
    if open_px is not None:
        out["vs_open"] = round(last_px - open_px, 4)
        out["above_open"] = last_px >= open_px
    if low_px is not None:
        out["vs_low"] = round(last_px - low_px, 4)
        out["above_low"] = last_px > low_px
    return out


def _remember_session(
    snap: dict[str, Any],
    symbol: str,
    session: dict[str, Any],
) -> None:
    store = snap.setdefault("session_range", {})
    if not isinstance(store, dict):
        return
    key = str(symbol).upper()
    prev = store.get(key)
    if isinstance(prev, dict) and _session_rank(prev) > _session_rank(session):
        store[key] = _refresh_session_last(prev, session)
        return
    store[key] = session


def _apply_candle_session(
    out: dict[str, Any],
    *,
    sym: str,
    snap: dict[str, Any],
    world: WorldState,
    last: Any,
) -> None:
    from abcxauto.opportunity_scan import session_range_from_bars
    from abcxauto.structure_grade import session_usable

    hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
    want = str(sym or "").upper()
    tape = None
    for row in hits.get("rows") or []:
        if isinstance(row, dict) and str(row.get("symbol") or "").upper() == want:
            tape = row
            break
    rth_open = None
    if isinstance(tape, dict) and tape.get("open") is not None:
        rth_open = tape.get("open")
    if rth_open is None:
        rth_open = _scan_open(snap, sym)
    last_px = _quote_last(last)
    rng = session_range_from_bars(
        out.get("bars"),
        last=last_px if last_px is not None else last,
        open_gap_pct=_scan_gap_pct(snap, sym),
        rth_open=rth_open,
    )
    if not session_usable(rng):
        live = _live_open_session(snap, sym, last=last, open_px=rth_open)
        if live:
            rng = live
    if not rng:
        return
    out["session"] = _finish_live_session(
        rng, snap=snap, world=world, symbol=sym, tape=tape
    )


def _scan_carries_news(raw: Any) -> bool:
    """True only when real headlines are present. Timeouts do not count."""
    if isinstance(raw, list):
        return any(is_real_headline(item) for item in raw)
    if not isinstance(raw, dict):
        return False
    items = raw.get("news")
    if isinstance(items, list) and any(is_real_headline(it) for it in items):
        return True
    for row in list(raw.get("hits") or []) + list(raw.get("rows") or []):
        if not isinstance(row, dict):
            continue
        nest = row.get("news")
        if isinstance(nest, list) and any(is_real_headline(it) for it in nest):
            return True
        if is_real_headline(row):
            return True
        mda = row.get("mda")
        if isinstance(mda, dict):
            mda_news = mda.get("news")
            if isinstance(mda_news, list) and any(
                is_real_headline(it) for it in mda_news
            ):
                return True
    return False


def _attach_run_sheet(
    out: dict[str, Any],
    *,
    turn: BrainTurn,
    world: WorldState,
    tool: str,
    quoted: Any = None,
) -> None:
    _ = (out, turn, world, tool, quoted)
    return


def _attach_scan_run(
    out: dict[str, Any],
    *,
    turn: BrainTurn,
    world: WorldState,
) -> None:
    _attach_run_sheet(out, turn=turn, world=world, tool="scan", quoted=out)


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def _send_tool(strategy_names: list[str] | None = None) -> Any:
    """Build the send tool. Hold is never a ticket."""
    names = list(strategy_names) if strategy_names is not None else ticket_strategy_names()
    return tool(
        name="send",
        description=(
            "One IBKR ticket per call. Call send again this turn for another ticket. "
            "strategy name + fields match ORDER EXAMPLES. "
            "Size (% of NL) and book width (self_tune max_open_positions) are together, not pick-one. "
            "Knobs are self_tune, not a ticket. Hard risk is code. "
            "New risk needs this-look candles and news|web|option_facts on the name."
        ),
        parameters=_schema(
            {
                "strategy": {
                    "type": "string",
                    "enum": names,
                    "description": (
                        "Ticket name from ORDER EXAMPLES. "
                        "buy_option right=P is a long put, right=C a long call. "
                        "vertical_spread right=P is a put vertical, right=C a call vertical. "
                        "cancel_order is order_id only."
                    ),
                },
                "symbol": _QUOTE_SCHEMA,
                "quantity": {"type": "number"},
                "direction": {"type": "string", "description": "LONG or SHORT"},
                "stop_price": {"type": "number"},
                "target_price": {"type": "number"},
                "entry_price": {"type": "number"},
                "limit_price": {"type": "number"},
                "order_id": {"type": "integer"},
                "expiration": {"type": "string", "description": "YYYYMMDD"},
                "strike": {"type": "number"},
                "right": {
                    "type": "string",
                    "description": "C call or P put. buy_option right=P is a long put.",
                },
                "params": {
                    "type": "object",
                    "description": "Extra ticket fields from ORDER EXAMPLES if not top-level.",
                },
                "target_conId": {"type": "string"},
                "card": {
                    "type": "string",
                    "description": (
                        "Play name for this ticket. Required on new risk; "
                        "optional on exits, protection, modifies and cancels. "
                        "Scorecard label, not a catalog."
                    ),
                },
                "rationale": {"type": "string"},
                "preview": {
                    "type": "boolean",
                    "description": (
                        "Readonly dry-run: pass/refuse, max_loss, would_refuse, "
                        "preview_token. Never places."
                    ),
                },
                "preview_token": {
                    "type": "string",
                    "description": (
                        "Single-use token from preview. Required to place new "
                        "risk. Bound to ticket hash (legs, qty, side, card, limit)."
                    ),
                },
            },
            ["strategy"],
        ),
    )


_WEB_HIT_CAP = 5
_WEB_TEXT_CLIP = 2_000


def _slim_web_hits(rows: Any) -> list[dict[str, Any]]:
    """Title/url/handle only. Up to five hits so _clip cannot drop search to empty."""
    out: list[dict[str, Any]] = []
    for raw in list(rows or [])[:_WEB_HIT_CAP]:
        if not isinstance(raw, dict):
            continue
        hit: dict[str, Any] = {}
        source = str(raw.get("source") or "").strip()
        if source:
            hit["source"] = source
        title = str(raw.get("title") or "").strip()
        if title:
            hit["title"] = title[:180]
        url = str(raw.get("url") or "").strip()
        if url:
            hit["url"] = url[:400]
        handle = str(raw.get("handle") or "").strip().lstrip("@")
        if handle:
            hit["handle"] = handle
        if hit.get("url") or hit.get("title"):
            out.append(hit)
    return out


def _clip_web(page: dict[str, Any]) -> str:
    """Clip a web/search payload. Search hits survive when text is fat."""
    slim = dict(page)
    text = slim.get("text")
    if isinstance(text, str) and len(text) > _WEB_TEXT_CLIP:
        slim["text"] = text[:_WEB_TEXT_CLIP]
    hits: list[dict[str, Any]] | None = None
    if isinstance(slim.get("results"), list):
        hits = _slim_web_hits(slim["results"])
        slim["results"] = hits
        slim["n"] = len(hits)
    raw = _hub()._clip(slim)
    if not hits:
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    kept = data.get("results") if isinstance(data, dict) else None
    if isinstance(kept, list) and kept:
        return raw
    rescued: dict[str, Any] = {
        "source": slim.get("source") or "web",
        "use": slim.get("use"),
        "query": slim.get("query"),
        "where": slim.get("where"),
        "results": hits,
        "n": len(hits),
        "_clipped": "text",
    }
    return json.dumps(
        {k: v for k, v in rescued.items() if v is not None},
        default=str,
    )


def _web_tool() -> Any:
    """Public page fetch or a short web/X search. COLOR. Not a crawler. Not a send trigger."""
    return tool(
        name="web",
        description=(
            "Color only, never a live trigger, not send geometry. "
            "Pass query to search the public web and X (titles, urls, snippets). "
            "Pass url to fetch one public http(s) page."
        ),
        parameters=_schema(
            {
                "query": {
                    "type": "string",
                    "description": "Search words, e.g. a name plus what you want.",
                },
                "where": {
                    "type": "string",
                    "enum": ["web", "x", "both"],
                    "description": "web, x, or both. Default both.",
                },
                "handles": {
                    "type": "string",
                    "description": "Optional X handles without @, comma-separated. Only with where x or both.",
                },
                "url": {
                    "type": "string",
                    "description": "Public http(s) page (press release, IR, SEC).",
                },
            },
            [],
        ),
    )


# Catalog for this look.
AGENT_TOOLS = [
    tool(
        name="book",
        description=(
            "Live IBKR blotter: lots, working orders, fills, protection, NL, PnL, "
            "idle-cash leftover vs lots. Not tape. Scan/news/odds/web stay their own tools."
        ),
        parameters=_schema({}, []),
    ),
    tool(
        name="status",
        description="IBKR/MDA/xAI link, trading mode, idle-cash leftover vs lots.",
        parameters=_schema({}, []),
    ),
    tool(
        name="quote",
        description=(
            "IBKR live last/bid/ask/mid/spread (TWS stream). "
            "Pass symbol or symbols[] (max 8). Bare quote() quotes open STK lots; "
            "flat book needs a name. Not MDA."
        ),
        parameters=_schema(
            {"symbol": _QUOTE_SCHEMA, "symbols": _SYMBOLS_SCHEMA},
            [],
        ),
    ),
    tool(
        name="fills",
        description="IBKR session fills/executions.",
        parameters=_schema({}, []),
    ),
    tool(
        name="news",
        description=(
            "MDA headlines (~15 min delayed): symbol, headline, publisher, published. "
            "Color only, never a trigger. Pass symbols[]. Bare news() does not invent names."
        ),
        parameters=_schema({"symbols": _SYMBOLS_SCHEMA}, []),
    ),
    tool(
        name="odds",
        description=(
            "Prediction-market implied probs (Polymarket): title, implied, url, as_of. "
            "Pass query or symbols[]. Bare odds() does not search the book. "
            "Not IBKR last."
        ),
        parameters=_schema(
            {
                "symbols": _SYMBOLS_SCHEMA,
                "query": {"type": "string", "description": "Event search, e.g. Fed September"},
            },
            [],
        ),
    ),
    tool(
        name="scan",
        description=(
            "One IBKR screen per call. Bare scan() lists arena/scan_code/metric. "
            "arena and/or scan_code fetches that sort. symbols[] is a fat drill-down. "
            "Ranked hits stay thin: symbol, rank, screen, scan_code, "
            "metric_name, metric_value, skip_class, source; "
            "last on the top hits when IBKR is up so tape can be compared to open lots "
            "and idle cash. "
            "skip_class is levered|micro|empty. "
            "with=news is a short top-level headline list. "
            "with=metrics nests delayed daily context on the top names. "
            "Repeat of the same screen this look is a pointer."
        ),
        parameters=_schema(
            {
                "arena": {
                    "type": "string",
                    "enum": _scan_arena_keys(),
                    "description": "Live IBKR screen id or standing scanCode.",
                },
                "scan_code": {
                    "type": "string",
                    "enum": _scan_code_keys(),
                    "description": "IBKR scanCode sort order.",
                },
                "symbols": _SYMBOLS_SCHEMA,
                "stock_type": {
                    "type": "string",
                    "enum": _STOCK_TYPE_ENUM,
                    "description": "ScannerSubscription.stockTypeFilter.",
                },
                "market_cap_above": {
                    "type": "number",
                    "description": "ScannerSubscription.marketCapAbove (raw USD)",
                },
                "market_cap_below": {
                    "type": "number",
                    "description": "ScannerSubscription.marketCapBelow (raw USD)",
                },
                "above_price": {
                    "type": "number",
                    "description": "ScannerSubscription.abovePrice",
                },
                "below_price": {
                    "type": "number",
                    "description": "ScannerSubscription.belowPrice",
                },
                "above_volume": {
                    "type": "integer",
                    "description": "ScannerSubscription.aboveVolume",
                },
                "average_option_volume_above": {
                    "type": "integer",
                    "description": "ScannerSubscription.averageOptionVolumeAbove",
                },
                "usdMarketCapAbove": {
                    "type": "string",
                    "description": "IBKR TagValue. Always accepted this look.",
                },
                "optVolumeAbove": {
                    "type": "string",
                    "description": "IBKR TagValue. Always accepted this look.",
                },
                "avgVolumeAbove": {
                    "type": "string",
                    "description": "IBKR TagValue. Always accepted this look.",
                },
                "peRatioAbove": {
                    "type": "string",
                    "description": (
                        "IBKR TagValue. Accepted only when live "
                        "reqScannerParameters XML lists peRatioAbove."
                    ),
                },
                "peRatioBelow": {
                    "type": "string",
                    "description": (
                        "IBKR TagValue. Accepted only when live "
                        "reqScannerParameters XML lists peRatioBelow."
                    ),
                },
                "industry": {
                    "type": "string",
                    "description": (
                        "Industry TagValue. Accepted only when live "
                        "reqScannerParameters XML lists the industry code."
                    ),
                },
                "sector": {
                    "type": "string",
                    "description": (
                        "Sector TagValue. Accepted only when live "
                        "reqScannerParameters XML lists the sector code."
                    ),
                },
                "with": {
                    "type": "array",
                    "items": {"type": "string", "enum": _SCAN_WITH_ENUM},
                    "description": (
                        "Attach MDA delayed news and/or metrics to this "
                        "ranked page. Color only, never a trigger."
                    ),
                },
            },
            [],
        ),
    ),
    tool(
        name="candles",
        description=(
            "IBKR hist or live 5s plus structure already on the bars (sma/dist/ret). "
            "Error if both miss. Not MDA. Pass symbol or symbols[] (max 8). "
            "Bare candles() uses open STK lots; flat book needs a name. "
            "resolution D = daily; 15/5/60 = hist size (stream is always 5s)."
        ),
        parameters=_schema(
            {
                "symbol": _QUOTE_SCHEMA,
                "symbols": _SYMBOLS_SCHEMA,
                "resolution": {
                    "type": "string",
                    "enum": _CANDLE_RESOLUTION_ENUM,
                    "description": (
                        "D = daily hist; 15/5/60 = hist size. "
                        "Live stream is always 5s."
                    ),
                },
                "countback": {"type": "integer"},
            },
            [],
        ),
    ),
    tool(
        name="option_chain",
        description=(
            "IBKR option expirations and strikes. "
            "One symbol or symbols[] (max 4)."
        ),
        parameters=_schema(
            {
                "symbol": _QUOTE_SCHEMA,
                "symbols": _SYMBOLS_SCHEMA,
                "min_dte": {"type": "integer"},
                "max_dte": {"type": "integer"},
            },
            [],
        ),
    ),
    tool(
        name="option_quote",
        description=(
            "IBKR live bid/ask/last for one option, contracts[] (max 8), "
            "or a vertical BAG net (long_strike + short_strike). "
            "MDA greeks delayed if present — not send geometry."
        ),
        parameters=_schema(
            {
                "symbol": _QUOTE_SCHEMA,
                "expiration": {"type": "string", "description": "YYYYMMDD"},
                "strike": {"type": "number"},
                "right": {
                    "type": "string",
                    "enum": _OPTION_RIGHT_ENUM,
                    "description": "C or P",
                },
                "long_strike": {
                    "type": "number",
                    "description": "Vertical long (BUY) strike. With short_strike: live BAG net.",
                },
                "short_strike": {
                    "type": "number",
                    "description": "Vertical short (SELL) strike. With long_strike: live BAG net.",
                },
                "contracts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": _QUOTE_SCHEMA,
                            "expiration": {"type": "string"},
                            "strike": {"type": "number"},
                            "right": {
                                "type": "string",
                                "enum": _OPTION_RIGHT_ENUM,
                            },
                        },
                    },
                },
            },
            [],
        ),
    ),
    tool(
        name="option_facts",
        description="Open option legs: identity from IBKR book; MDA greeks delayed if present.",
        parameters=_schema({}, []),
    ),
    _send_tool(),
    tool(
        name="self_tune",
        description=(
            "Retune knobs now. Floor cannot be weakened. "
            "size_pct_nl tightens the explore/exploit mode band. "
            "session_look_cap tightens only. Not a ticket — send is the book."
        ),
        parameters=_schema(
            {
                "max_peak_drawdown_pct": {"type": "number"},
                "max_symbol_concentration_pct": {"type": "number"},
                "size_pct_nl": {
                    "type": "number",
                    "description": (
                        "Working size ceiling as % of current NL. "
                        "Tighten inside the explore/exploit mode band. "
                        "send.apply_size_pct_nl uses the same band."
                    ),
                },
                "session_look_cap": {"type": "integer"},
                "rationale": {"type": "string"},
            },
            [],
        ),
    ),
    tool(
        name="recall",
        description="Durable notes and cards. list/get fetch; write stores; invalidate retires.",
        parameters=_schema(
            {
                "op": {"enum": ["list", "get", "write", "invalidate"]},
                "store": {"enum": ["notes", "cards"]},
                "ids": {"type": "array", "items": {"type": "string"}},
                "id": {},
                "tags": {"type": "array", "items": {"type": "string"}},
                "kind": {"enum": ["fact", "event", "invalidate"]},
                "symbol": {},
                "body": {"description": "max 160"},
                "evidence": {
                    "description": (
                        "store=cards: list of objects with optional tool (string) "
                        "and facts (object). store=notes: string."
                    ),
                },
                "invalidate": {},
                "label": {},
                "screen": {},
                "scan_code": {},
                "direction": {},
                "expectation": {},
            },
            [],
        ),
    ),
    tool(
        name="research_brief",
        description="This look's gathered color plus a prior-session stub. Color only.",
        parameters=_schema({}, []),
    ),
]


def _send_strategy_names_for_look(*, session: str = "") -> list[str]:
    """send enum this look. Hold is never a ticket. Kill RTH is vertical_spread only."""
    try:
        from abcxauto.thin_rth_kill_look import send_strategy_names

        names = send_strategy_names(session=session)
        if names is not None:
            return list(names)
    except Exception:
        logger.debug("kill-look send enum failed", exc_info=True)
    return [n for n in ticket_strategy_names() if n != "hold"]


def agent_tools(*, session: str = "") -> list:
    """Tools this look. Overnight park is code. Stay-up has no sit clock.

    ``send`` is offered in every session (paper stay-up). Cash / defined-risk /
    daily-loss / research_thin still gate place. ``web`` is COLOR (not a live
    trigger).
    """
    names = _send_strategy_names_for_look(session=session)
    out: list = []
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        name = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        if name == "send":
            out.append(_send_tool(names))
        else:
            out.append(t)
    out.append(_web_tool())
    return out

def _stash_live(
    world: WorldState,
    snap: dict[str, Any],
    data: dict[str, Any],
    *,
    mark: bool = True,
) -> None:
    """Record IBKR lasts. ``mark`` is the desk print — quote() yes, scan sweep no.

    A 40-name screen used to leave last_turn.ibkr_live_last as the last junk
    ticker (QBTX 8.07) while the book was flat.
    """
    if not isinstance(data, dict):
        return
    rows = data.get("quotes")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                _hub()._stash_live(world, snap, row, mark=mark)
        return
    if data.get("source") != "ibkr":
        return
    if data.get("error") and data.get("last") is None and data.get("mid") is None:
        return
    sym = str(data.get("symbol") or "").upper()
    last = data.get("last") if data.get("last") is not None else data.get("mid")
    try:
        px = float(last)
    except (TypeError, ValueError):
        return
    if not sym or px <= 0:
        return
    qmap = snap.get("ibkr_live_quotes")
    if not isinstance(qmap, dict):
        qmap = {}
        snap["ibkr_live_quotes"] = qmap
    qmap[sym] = px
    live = getattr(world, "ibkr_live_quotes", None)
    if not isinstance(live, dict):
        world.ibkr_live_quotes = {}
        live = world.ibkr_live_quotes
    live[sym] = px
    if not mark:
        return
    snap["ibkr_live_symbol"] = sym
    snap["ibkr_live_last"] = px
    world.ibkr_live_symbol = sym
    world.ibkr_live_last = px


def _stash_vol_bars(snap: dict[str, Any], series: list[Any]) -> None:
    from abcxauto.vol_fact import stash_look_bars

    for row in series or []:
        if not isinstance(row, dict) or not row.get("bars"):
            continue
        stash_look_bars(
            snap,
            str(row.get("symbol") or ""),
            row.get("bars"),
            resolution=str(row.get("resolution") or ""),
        )


def _stash_vol_chain(snap: dict[str, Any], chain: Any) -> None:
    from abcxauto.vol_fact import stash_look_chain

    stash_look_chain(snap, chain)


def _stash_vol_quote_iv(snap: dict[str, Any], data: Any) -> None:
    from abcxauto.vol_fact import stash_look_iv

    if not isinstance(data, dict):
        return
    if isinstance(data.get("quotes"), list):
        for row in data["quotes"]:
            _stash_vol_quote_iv(snap, row)
        return
    stash_look_iv(snap, str(data.get("symbol") or ""), data.get("iv"))


def _stash_vol_option_quote(snap: dict[str, Any], row: Any) -> None:
    from abcxauto.vol_fact import stash_look_iv

    if not isinstance(row, dict):
        return
    su = str(row.get("symbol") or "").upper().strip()
    mda = row.get("mda") if isinstance(row.get("mda"), dict) else {}
    ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else {}
    raw = mda.get("iv")
    if raw is None:
        raw = ibkr.get("iv")
    stash_look_iv(snap, su, raw)


def _publish_vol(world: WorldState, snap: dict[str, Any]) -> None:
    from abcxauto.vol_fact import publish_vol_facts

    publish_vol_facts(world, snap)


def _compact_chain(raw: dict[str, Any], *, last: float | None = None) -> dict[str, Any]:
    strikes = list(raw.get("strikes") or [])
    exps = list(raw.get("expirations") or [])[:10]
    out = {
        "symbol": raw.get("symbol"),
        "exchange": raw.get("exchange"),
        "multiplier": raw.get("multiplier"),
        "source": raw.get("source") or "ibkr",
        "freshness": raw.get("freshness") or "live",
        "expirations": exps,
        "n_strikes": len(strikes),
    }
    if raw.get("error"):
        out["error"] = raw["error"]
        return out
    if last and last > 0 and strikes:
        band = [s for s in strikes if abs(float(s) - last) / last <= 0.12]
        if band:
            out["strikes"] = band[:40]
            return out
    if len(strikes) > 40:
        mid = strikes[len(strikes) // 2]
        out["strikes"] = [
            s for s in strikes if abs(float(s) - float(mid)) <= float(mid) * 0.12
        ][:40]
        out["strike_note"] = (
            "clipped; quote this underlying to center ATM"
            if not last
            else "last did not match this chain; centered on median strike"
        )
    else:
        out["strikes"] = strikes
    return out


async def _mda_news(symbols: list[str], *, per_symbol: int = 4) -> list[dict[str, Any]]:
    """Headlines for named symbols. Timeout is a miss item, not an empty success."""
    from abcxauto.news_feed import NEWS_LOOK_BUDGET_S, fetch_symbols_news
    from abcxauto.prints import mda_worth_asking

    syms = [s for s in symbols[:8] if s and mda_worth_asking(s)]
    if not syms:
        return []
    return await fetch_symbols_news(
        syms, per_symbol=per_symbol, budget_s=NEWS_LOOK_BUDGET_S
    )


def _combo_quote_spec(args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Vertical BAG quote args. long_strike + short_strike, not a single strike."""
    src = args if isinstance(args, dict) else {}
    syms = normalize_tickers(src.get("symbol"))
    if not syms:
        return None
    exp = src.get("expiration") or src.get("expiry")
    right = str(src.get("right") or "").upper().strip()
    if right in {"CALL"}:
        right = "C"
    elif right in {"PUT"}:
        right = "P"
    else:
        right = right[:1]
    long_k = src.get("long_strike")
    short_k = src.get("short_strike")
    if long_k in (None, "") or short_k in (None, "") or not exp or right not in {"C", "P"}:
        return None
    try:
        long_f = float(long_k)
        short_f = float(short_k)
    except (TypeError, ValueError):
        return None
    return {
        "symbol": syms[0],
        "expiration": str(exp).replace("-", "").strip(),
        "long_strike": long_f,
        "short_strike": short_f,
        "right": right,
    }


async def _one_combo_quote(connector: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Live IBKR BAG net via get_live_vertical_bag_quote. Never MDA."""
    live_fn = getattr(connector, "get_live_vertical_bag_quote", None)
    if not callable(live_fn):
        return {
            "error": "IBKR combo quote unavailable",
            "source": "ibkr",
            "sec": "BAG",
            **spec,
        }
    live = await live_fn(
        spec["symbol"],
        spec["expiration"],
        spec["long_strike"],
        spec["short_strike"],
        spec["right"],
    )
    if not isinstance(live, dict):
        live = {}
    out = dict(live)
    out.update({
        "symbol": spec["symbol"],
        "expiration": spec.get("expiration") or live.get("expiration"),
        "long_strike": spec["long_strike"],
        "short_strike": spec["short_strike"],
        "right": spec["right"],
        "sec": live.get("sec") or "BAG",
        "use": "ibkr_live_combo_net",
    })
    return out


async def _one_option_quote(connector: Any, spec: dict[str, Any]) -> dict[str, Any]:
    from abcxauto.option_facts import mda_greeks_only, occ_symbol

    syms = normalize_tickers(spec.get("symbol"))
    if not syms:
        return {"error": "symbol required", "source": "ibkr", **spec}
    live_fn = getattr(connector, "get_live_option_quote", None)
    live: dict[str, Any] = {}
    if callable(live_fn):
        live = await live_fn(
            syms[0],
            str(spec.get("expiration") or ""),
            spec.get("strike"),
            str(spec.get("right") or ""),
        ) or {}
    if not isinstance(live, dict):
        live = {}
    occ = occ_symbol(
        syms[0],
        str(spec.get("expiration") or ""),
        str(spec.get("right") or ""),
        spec.get("strike"),
    )
    mda_greeks: dict[str, Any] = {}
    if occ:
        try:
            from abcxauto.marketdata.client import get_marketdata_client

            oq = await get_marketdata_client().get_option_quote(occ)
        except Exception:
            oq = None
        mda_greeks = mda_greeks_only(oq if isinstance(oq, dict) else None, occ=occ)
    return {
        "symbol": syms[0],
        "expiration": spec.get("expiration"),
        "strike": spec.get("strike"),
        "right": spec.get("right"),
        "ibkr": live or {"error": "IBKR option quote unavailable", "source": "ibkr"},
        "mda": mda_greeks or None,
        "use": "ibkr_live_for_decisions; mda_greeks_delayed",
    }


async def _run_tool(
    name: str,
    args: dict[str, Any],
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    turn: BrainTurn,
) -> str:
    from abcxauto.agent_loop import execute_ticket

    name, args = normalize_tool_call(
        name,
        args if isinstance(args, dict) else {},
        fallback_symbols=fallback_quote_symbols(world, snap),
    )

    if name == "book":
        payload = _hub()._book_payload(world, tool_trace=turn.tool_trace, snap=snap)
        payload["sends_this_turn"] = len(turn.sends)
        world_facts = payload.get("world")
        if isinstance(world_facts, dict):
            world_facts["sends_this_turn"] = len(turn.sends)
        try:
            from abcxauto.look_snapshot import record_look_tool

            record_look_tool(snap, "book", payload)
        except Exception:
            logger.debug("look snapshot book record failed", exc_info=True)
        return _hub()._clip(payload)
    if name == "status":
        from abcxauto.connections import connection_status
        from abcxauto.marketdata.market_hours import get_session_info

        st = connection_status(connector)
        try:
            st["session"] = get_session_info()
        except Exception:
            st["session"] = {"session": world.session_status}
        st["sends_this_turn"] = len(turn.sends)
        try:
            from abcxauto.desk_mode import desk_mode

            sess = str(
                (st.get("session") or {}).get("session")
                or world.session_status
                or ""
            )
            st["desk_mode"] = desk_mode(sess)
            # Session label is not a send ban — hard gates still refuse.
            st["send_allowed"] = True
        except Exception:
            st["desk_mode"] = ""
            st["send_allowed"] = True
        try:
            from abcxauto.world_state import compact_working_orders, lot_labels

            st["open_lots"] = lot_labels(getattr(world, "positions", None))
            st["working_orders"] = compact_working_orders(
                getattr(world, "open_orders", None),
                positions=getattr(world, "positions", None),
            )
        except Exception:
            st["open_lots"] = []
            st["working_orders"] = []
        try:
            from abcxauto.world_state import allocation_facts, allocation_line

            cash = None
            port = getattr(world, "portfolio_risk", None) or {}
            inner = port.get("capital_liquidity") if isinstance(port, dict) else {}
            if isinstance(inner, dict) and inner.get("total_cash") is not None:
                cash = inner.get("total_cash")
            alloc = allocation_facts(
                list(getattr(world, "positions", None) or []),
                net_liq=getattr(world, "net_liquidation", None),
                total_cash=cash,
                quotes=getattr(world, "ibkr_live_quotes", None),
                orders=list(getattr(world, "open_orders", None) or []),
            )
            line = allocation_line(alloc)
            if line:
                st["allocation_line"] = line
            from abcxauto.world_state import range_compare_line, to_high_line

            range_src = (
                snap.get("session_range")
                if isinstance(snap.get("session_range"), dict)
                else getattr(world, "session_range", None)
            )
            range_line = range_compare_line(range_src)
            if range_line:
                st["range_line"] = range_line
            high_line = to_high_line(range_src)
            if high_line:
                st["to_high_line"] = high_line
        except Exception:
            logger.debug("status allocation_line failed", exc_info=True)
        pulse = snap.get("reality_pulse") if isinstance(snap.get("reality_pulse"), dict) else {}
        if not pulse:
            pulse = getattr(world, "pulse", None) or {}
        if isinstance(pulse, dict) and pulse:
            sess = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
            if sess.get("countdown_to") or sess.get("countdown_human"):
                st["countdown"] = {
                    "to": sess.get("countdown_to"),
                    "s": sess.get("countdown_s"),
                    "human": sess.get("countdown_human"),
                }
            if pulse.get("tradable_now") is not None:
                st["tradable_now"] = pulse.get("tradable_now")
            fresh = pulse.get("data_freshness") if isinstance(pulse.get("data_freshness"), dict) else {}
            if fresh:
                ibkr_fresh = {
                    "ibkr_connected": fresh.get("ibkr_connected"),
                    "ibkr_snapshot_age_s": fresh.get("ibkr_snapshot_age_s"),
                }
                if any(v is not None for v in ibkr_fresh.values()):
                    st["freshness"] = ibkr_fresh
        return _hub()._clip(st)
    if name == "quote":
        asked = normalize_tickers(
            args.get("symbols") or args.get("symbol"), cap=8
        )
        if not asked:
            return _hub()._clip(_quote_need())
        raw = await run_readonly_tool("quote", args, connector)
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
            data = _public_quote_payload(data)
            raw = data
            _hub()._stash_live(world, snap, data)
            _stash_vol_quote_iv(snap, data)
            live = _live_open_session(
                snap,
                str(data.get("symbol") or ""),
                last=data.get("last") if data.get("last") is not None else data.get("mid"),
                open_px=data.get("open"),
                open_gap_pct=data.get("open_gap_pct"),
            )
            if live:
                data["session"] = _finish_live_session(
                    live,
                    snap=snap,
                    world=world,
                    symbol=str(data.get("symbol") or ""),
                    tape=data,
                )
                raw = _hub()._clip(data)
        try:
            from abcxauto.look_snapshot import record_look_tool

            record_look_tool(snap, "quote", data)
        except Exception:
            logger.debug("look snapshot quote record failed", exc_info=True)
        if isinstance(data, dict):
            try:
                from abcxauto.world_state import attach_tool_math

                attach_tool_math(data, world)
            except Exception:
                logger.debug("quote math page failed", exc_info=True)
            raw = data
        return raw if isinstance(raw, str) else _hub()._clip(raw)
    if name == "fills":
        fn = getattr(connector, "get_fills", None) or getattr(connector, "get_recent_executions", None)
        if not callable(fn):
            return json.dumps({"error": "IBKR fills unavailable", "source": "ibkr"})
        rows = await fn()
        return _hub()._clip({"source": "ibkr", "freshness": "live", "fills": list(rows or [])[:40]})
    if name == "news":
        from abcxauto.news_feed import (
            coalesce_news,
            news_hard_miss,
            news_need_symbols,
            remember_headlines,
            remember_look_news,
        )

        remember_look_news(world, snap)
        asked = normalize_tickers(args.get("symbols"))
        if not asked:
            already = _news_symbols_this_look(world, snap, [])
            return _hub()._clip(news_need_symbols(already))
        items = await _hub()._mda_news(asked)
        items = coalesce_news(items, asked)
        remember_headlines(items)
        page = _public_news_items(items)
        world.news_items = list(page)
        snap["news_items"] = list(page)
        payload = {
            "source": "mda",
            "freshness": "delayed_15m",
            "use": "color_not_trigger",
            "items": page[:24],
        }
        miss = news_hard_miss(items)
        if miss:
            payload["error"] = f"news unavailable - {miss}"
        _attach_run_sheet(payload, turn=turn, world=world, tool="news", quoted=snap)
        return _hub()._clip(payload)
    if name == "odds":
        from abcxauto.prediction_odds import fetch_odds

        asked = normalize_tickers(args.get("symbols"))
        q = str(args.get("query") or "").strip()
        payload = await fetch_odds(symbols=asked, query=q)
        if isinstance(payload, dict):
            payload.setdefault("as_of", datetime.now(timezone.utc).isoformat())
            payload.pop("path", None)
        if isinstance(snap, dict):
            snap["odds"] = dict(payload)
        return _hub()._clip(payload)
    if name == "scan":
        with_raw = args.get("with") or []
        if isinstance(with_raw, str):
            with_bits = [with_raw.strip().lower()]
        elif isinstance(with_raw, (list, tuple)):
            with_bits = [str(x).strip().lower() for x in with_raw if str(x).strip()]
        else:
            with_bits = []
        want_news = "news" in with_bits
        want_metrics = any(b in ("metrics", "mda") for b in with_bits)
        turn_syms: list[str] = []
        for s in list(getattr(world, "scan_fetched", None) or []):
            if s and s not in turn_syms:
                turn_syms.append(str(s).upper())
        qmap = dict(getattr(world, "ibkr_live_quotes", None) or {})
        if isinstance(snap.get("ibkr_live_quotes"), dict):
            qmap.update(snap["ibkr_live_quotes"])
        for s in qmap:
            su = str(s or "").upper().strip()
            if su and su not in turn_syms:
                turn_syms.append(su)
        from abcxauto.universe import (
            parse_scan_filters,
            resolve_screen,
            verified_industry_tags,
            verified_pe_tags,
        )

        pe_tags = await verified_pe_tags(connector)
        industry_tags = await verified_industry_tags(connector)
        parsed = parse_scan_filters(
            args, pe_tags=pe_tags, industry_tags=industry_tags
        )
        if not parsed.get("ok"):
            return _clip_scan(
                _scan_filter_error(
                    args,
                    parsed,
                    pe_tags=pe_tags,
                    industry_tags=industry_tags,
                )
            )
        asked_symbols = normalize_tickers(args.get("symbols") or [])
        c_arena, c_code = _canonical_scan_screen(
            str(args.get("arena") or "").strip(),
            str(args.get("scan_code") or "").strip(),
        )
        has_screen = bool(c_arena or c_code)
        lock = getattr(turn, "scan_lock", None)
        if not isinstance(lock, asyncio.Lock):
            lock = asyncio.Lock()
            try:
                turn.scan_lock = lock
            except Exception:
                pass

        async def _attach_optional_news(out: dict[str, Any]) -> None:
            if not want_news:
                return
            existing = out.get("news")
            if existing:
                real = _slim_scan_news(existing)
                if real:
                    out["news"] = real
                    return
                out.pop("news", None)

            news_syms = _news_symbols_for_scan(
                snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {},
                [str(s) for s in (out.get("symbols") or []) if s],
            )
            news = _slim_scan_news(await _hub()._mda_news(news_syms))
            if not news:
                return
            out["news"] = news
            out["news_freshness"] = "delayed_15m"
            out["news_use"] = "color_not_trigger"
            snap["scan_news_attached"] = True
            if not world.news_items:
                world.news_items = list(news)
                snap["news_items"] = list(news)

        async def _finish_look_bag(
            last_ok: dict[str, Any] | None,
            *,
            emit_line: bool,
            silent: bool = False,
        ) -> str:
            from abcxauto.opportunity_scan import (
                SILENT_SCAN_NOTE,
                is_thin_ranked_row,
            )

            out = _scan_out_from_snap(snap, qmap, last_ok=last_ok)
            if _snap_is_rth(snap):
                sessions: dict[str, Any] = {}
                for row in out.get("rows") or []:
                    if not isinstance(row, dict) or is_thin_ranked_row(row):
                        continue
                    name = str(row.get("symbol") or "").upper()
                    if not name:
                        continue
                    live = _live_open_session(
                        snap,
                        name,
                        last=row.get("last"),
                        open_px=row.get("open"),
                        open_gap_pct=row.get("open_gap_pct") or row.get("gap%"),
                    )
                    if not live:
                        continue
                    row["session"] = _finish_live_session(
                        live, snap=snap, world=world, symbol=name, tape=row
                    )
                    sessions[name] = row["session"]
                if sessions:
                    out["sessions"] = sessions
            hits = [r for r in (out.get("hits") or []) if isinstance(r, dict)]
            if want_metrics and hits:
                from abcxauto.opportunity_scan import attach_mda_metrics

                await attach_mda_metrics(hits)
            await _attach_optional_news(out)
            painted = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
            snap["scan_hits"] = _union_scan_hits(
                painted, {**painted, "rows": out["rows"]}
            )
            if silent:
                out["note"] = SILENT_SCAN_NOTE
            _attach_scan_run(out, turn=turn, world=world)
            if emit_line:
                _emit_scan_look_line(snap, out)
            turn.scan_cache[_LOOK_SCAN_CACHE_KEY] = deepcopy(out)
            return _clip_scan(_scan_public_payload(out))

        async def _repeat_look_bag() -> str:
            think_emit("tool", "\n[scan = already have it]\n")
            stub = _scan_reuse_stub(
                snap, asked_arena=c_arena, asked_code=c_code
            )
            _attach_scan_run(stub, turn=turn, world=world)
            return _clip_scan(stub)

        async with lock:
            if asked_symbols:
                payload = await _hub().criteria_scan(
                    symbols=asked_symbols,
                    positions=list(world.positions or snap.get("positions") or []),
                    connector=connector,
                    turn_symbols=turn_syms,
                    filters=None,
                )
                if not payload.get("ok"):
                    err = dict(payload) if isinstance(payload, dict) else {
                        "ok": False,
                        "error": "scan symbols failed",
                    }
                    err.setdefault("ok", False)
                    _attach_scan_run(err, turn=turn, world=world)
                    return _clip_scan(err)
                _ingest_scan_payload(
                    world=world,
                    snap=snap,
                    payload=payload,
                    job={"arena": "", "scan_code": ""},
                )
                snap["scan_at"] = datetime.now(timezone.utc).isoformat()
                return await _finish_look_bag(payload, emit_line=False)

            if not has_screen:
                out = _scan_catalog_payload()
                _attach_scan_run(out, turn=turn, world=world)
                return _clip_scan(out)

            resolved = resolve_screen(
                arena=str(args.get("arena") or "").strip() or None,
                scan_code=str(args.get("scan_code") or "").strip() or None,
            )
            if not resolved.get("ok"):
                err = {
                    "ok": False,
                    "error": resolved.get("error") or "unknown screen",
                    "arenas": resolved.get("arenas"),
                }
                _attach_scan_run(err, turn=turn, world=world)
                return _clip_scan(err)

            if _scan_screen_on_look(snap, c_arena, c_code):
                return await _repeat_look_bag()

            snap["scan_calls"] = int(snap.get("scan_calls") or 0) + 1
            payload = await _hub().criteria_scan(
                arena=args.get("arena"),
                scan_code=args.get("scan_code"),
                positions=list(world.positions or snap.get("positions") or []),
                connector=connector,
                turn_symbols=turn_syms,
                filters=parsed,
            )
            if not payload.get("ok"):
                err = dict(payload) if isinstance(payload, dict) else {
                    "ok": False,
                    "error": "scan failed",
                }
                err.setdefault("ok", False)
                _attach_scan_run(err, turn=turn, world=world)
                return _clip_scan(err)
            _ingest_scan_payload(
                world=world,
                snap=snap,
                payload=payload,
                job={"arena": args.get("arena"), "scan_code": args.get("scan_code")},
            )
            snap["scan_at"] = datetime.now(timezone.utc).isoformat()
            return await _finish_look_bag(payload, emit_line=True)
    if name == "candles":
        from abcxauto.broker.bars import ibkr_bar_freshness

        syms = normalize_tickers(
            args.get("symbols") or args.get("symbol"), cap=CANDLE_CAP
        )
        if not syms:
            return _hub()._clip(_quote_need())
        try:
            countback = int(args.get("countback") or 60)
        except (TypeError, ValueError):
            countback = 60
        countback = max(5, min(countback, 120))
        from abcxauto.broker.bars import (
            HIST_RESOLUTIONS,
            normalize_resolution,
            session_countback,
        )

        raw_res = args.get("resolution")
        res = str(raw_res if raw_res is not None else "").strip()
        explicit_res = bool(res)
        if not res:
            res = _candle_res_from_tape(snap)
        else:
            res = normalize_resolution(res)
        if res not in HIST_RESOLUTIONS:
            return _hub()._clip(
                {
                    "error": f"unsupported resolution {raw_res!r}",
                    "requested_resolution": str(raw_res or "").strip(),
                    "source": "ibkr",
                    "freshness": "ibkr_miss",
                }
            )
        if res in ("5", "15", "60"):
            countback = min(
                120,
                max(countback, session_countback(res, n_symbols=len(syms))),
            )
            bar_cap = countback
        else:
            bar_cap = 40 if len(syms) > 1 else 80
        hist = getattr(connector, "get_historical_bars", None)
        realtime = getattr(connector, "get_realtime_bars", None)
        peek = getattr(connector, "realtime_bar_buffer", None)
        ibkr_path = connector is not None and (callable(hist) or callable(realtime))
        qmap = dict(getattr(world, "ibkr_live_quotes", None) or {})
        if isinstance(snap.get("ibkr_live_quotes"), dict):
            qmap.update(snap["ibkr_live_quotes"])
        t0 = time.monotonic()
        budget = CANDLE_WAIT_S

        def _live_last(sym: str) -> Any:
            return qmap.get(sym)

        async def _one_candles(sym: str) -> dict[str, Any]:
            hist_err = ""
            rt_err = ""
            warm = False
            if callable(peek):
                try:
                    warm = bool(peek(sym))
                except Exception:
                    warm = False
            if callable(hist):
                try:
                    raw = await hist(sym, resolution=res, countback=countback)
                except Exception as exc:
                    raw = {"error": str(exc), "source": "ibkr", "symbol": sym}
                if isinstance(raw, dict) and raw.get("bars"):
                    out = dict(raw)
                    # Never accept silent daily under an intraday ask.
                    got = out.get("resolution")
                    if (
                        res in ("5", "15", "60")
                        and got is not None
                        and normalize_resolution(str(got)) == "D"
                    ):
                        hist_err = f"daily bars for requested {res}"
                    else:
                        out["bars"] = list(out.get("bars") or [])[-bar_cap:]
                        out.setdefault("source", "ibkr")
                        out.setdefault("freshness", ibkr_bar_freshness(res))
                        out.setdefault("resolution", res)
                        if explicit_res and out.get("resolution") != res:
                            out.setdefault("requested_resolution", res)
                        last_bar = (out["bars"] or [{}])[-1]
                        if last_bar.get("t_unix"):
                            out.setdefault("asof", last_bar["t_unix"])
                            if last_bar.get("t_iso"):
                                out.setdefault("asof_iso", last_bar["t_iso"])
                        if out.get("freshness") != "ibkr_rt_5s":
                            from abcxauto.opportunity_scan import structure_from_bars

                            metrics = structure_from_bars(
                                out["bars"],
                                sym,
                                resolution=res,
                                source="ibkr",
                                freshness=str(out.get("freshness") or "ibkr_rth"),
                            )
                            if metrics:
                                out["metrics"] = metrics
                        _apply_candle_session(
                            out, sym=sym, snap=snap, world=world, last=_live_last(sym)
                        )
                        return out
                if not hist_err:
                    hist_err = str((raw or {}).get("error") or "no IBKR bars")
            elif warm:
                hist_err = "skipped_hist_rt_warm"
            remain = max(0.0, budget - (time.monotonic() - t0) - 2.0)
            wait_s = min(7.0, remain)
            if callable(realtime):
                try:
                    raw = await realtime(
                        sym, resolution=res, countback=countback, wait_s=wait_s
                    )
                except TypeError:
                    try:
                        raw = await realtime(sym, resolution=res, countback=countback)
                    except Exception as exc:
                        raw = {"error": str(exc), "source": "ibkr", "symbol": sym}
                except Exception as exc:
                    raw = {"error": str(exc), "source": "ibkr", "symbol": sym}
                if isinstance(raw, dict) and raw.get("bars"):
                    out = dict(raw)
                    out["bars"] = list(out.get("bars") or [])[-bar_cap:]
                    out.setdefault("source", "ibkr")
                    out.setdefault("freshness", ibkr_bar_freshness("5s"))
                    out.setdefault("resolution", "5s")
                    out.setdefault("requested_resolution", res)
                    _apply_candle_session(
                        out, sym=sym, snap=snap, world=world, last=_live_last(sym)
                    )
                    return out
                rt_err = str((raw or {}).get("error") or "no IBKR realtime bars")
            # A missing bar feed is a broken link, not licence to answer a
            # live-structure question with yesterday's delayed tape. On
            # 2026-08-20 the bars mixin was off the connector MRO, so this fell
            # through to MDA and handed Grok the prior session as if it were
            # today — it spent the turn discovering that instead of trading.
            if not ibkr_path:
                hist_err = hist_err or "connector exposes no bar feed"
            logger.info(
                "candles %s hist=%s rt=%s path=ibkr_error",
                sym,
                hist_err or "n/a",
                rt_err or "n/a",
            )
            err: dict[str, Any] = {
                "symbol": sym,
                "source": "ibkr",
                "error": rt_err or hist_err or "no IBKR bars",
                "freshness": "ibkr_miss",
                "hist_error": hist_err or None,
                "rt_error": rt_err or None,
            }
            last = _live_last(sym)
            if last is not None:
                err["last"] = last
            return err

        rows = await asyncio.gather(
            *[_one_candles(sym) for sym in syms], return_exceptions=True
        )
        series: list[dict[str, Any]] = []
        for sym, row in zip(syms, rows):
            if isinstance(row, Exception):
                series.append({"symbol": sym, "error": str(row)})
            else:
                series.append(row)
        kinds: set[str] = set()
        for row in series:
            if not isinstance(row, dict):
                continue
            src = str(row.get("source") or "")
            fresh = str(row.get("freshness") or "")
            # A miss carries source=ibkr too; counting it as hist labelled a
            # batch of nothing as RTH structure.
            if fresh == "ibkr_miss" or not row.get("bars"):
                continue
            if fresh == "ibkr_rt_5s":
                kinds.add("rt")
            elif src == "ibkr":
                kinds.add("hist")
        if kinds == {"hist"}:
            source, freshness, use, out_res = (
                "ibkr",
                ibkr_bar_freshness(res),
                "ibkr_rth_structure",
                res,
            )
        elif kinds == {"rt"}:
            source, freshness, use, out_res = (
                "ibkr",
                ibkr_bar_freshness("5s"),
                "live_5s_not_hist",
                "5s",
            )
        elif kinds:
            source, freshness, use, out_res = "ibkr", "ibkr_hist_or_rt", "prefer_hist_then_5s", res
        else:
            source, freshness, use, out_res = "ibkr", "ibkr_miss", "no_bars_use_quote", res
        # Stamp the world so last_turn / the next wake do not say candles=none
        # after this look already fetched bars.
        try:
            world.candle_source = source if kinds else "ibkr_miss"
            snap["candle_source"] = world.candle_source
        except Exception:
            pass
        payload: dict[str, Any] = {
            "resolution": out_res,
            "source": source,
            "freshness": freshness,
            "use": use,
        }
        if out_res != res:
            payload["requested_resolution"] = res
        if len(series) == 1:
            payload["symbol"] = series[0].get("symbol")
            if series[0].get("error") and not series[0].get("bars"):
                payload["error"] = series[0]["error"]
                if series[0].get("last") is not None:
                    payload["last"] = series[0]["last"]
                if series[0].get("hist_error"):
                    payload["hist_error"] = series[0]["hist_error"]
                if series[0].get("rt_error"):
                    payload["rt_error"] = series[0]["rt_error"]
            else:
                payload["bars"] = series[0].get("bars") or []
            if series[0].get("source"):
                payload["source"] = series[0]["source"]
                payload["freshness"] = series[0].get("freshness") or payload["freshness"]
                if series[0].get("resolution"):
                    payload["resolution"] = series[0]["resolution"]
                if series[0].get("requested_resolution"):
                    payload["requested_resolution"] = series[0]["requested_resolution"]
                if series[0].get("use"):
                    payload["use"] = series[0]["use"]
                if series[0].get("metrics"):
                    payload["metrics"] = series[0]["metrics"]
                if series[0].get("asof") is not None:
                    payload["asof"] = series[0]["asof"]
                if series[0].get("asof_iso"):
                    payload["asof_iso"] = series[0]["asof_iso"]
                if series[0].get("session"):
                    payload["session"] = series[0]["session"]
                read = _candle_read(series[0])
                if read:
                    payload["read"] = read
        else:
            payload["series"] = series
            for row in payload["series"]:
                if not isinstance(row, dict):
                    continue
                read = _candle_read(row)
                if read:
                    row["read"] = read
        # A miss is an error, not a stub payload.
        if kinds:
            _attach_run_sheet(
                payload, turn=turn, world=world, tool="candles", quoted=snap
            )
            _stash_vol_bars(snap, series)
            _hub()._publish_vol(world, snap)
        return _hub()._clip(payload, max_chars=_hub().CANDLES_CLIP_CHARS)
    if name == "option_chain":
        fn = getattr(connector, "get_option_chain", None)
        if not callable(fn):
            return json.dumps({"error": "IBKR option chain unavailable", "source": "ibkr"})
        syms = normalize_tickers(
            args.get("symbols") or args.get("symbol"), cap=CHAIN_CAP
        )
        if not syms:
            return json.dumps({"error": "symbol required", "source": "ibkr"})
        try:
            min_dte = int(args.get("min_dte") or 7)
            max_dte = int(args.get("max_dte") or 45)
        except (TypeError, ValueError):
            min_dte, max_dte = 7, 45

        async def _one_chain(sym: str) -> dict[str, Any]:
            raw = await fn(sym, min_dte=min_dte, max_dte=max_dte)
            last = (world.ibkr_live_quotes or {}).get(sym)
            return _hub()._compact_chain(raw if isinstance(raw, dict) else {}, last=last)

        rows = await asyncio.gather(
            *[_one_chain(sym) for sym in syms], return_exceptions=True
        )
        chains: list[dict[str, Any]] = []
        for sym, row in zip(syms, rows):
            if isinstance(row, Exception):
                chains.append({"symbol": sym, "error": str(row), "source": "ibkr"})
            else:
                chains.append(row)
        for row in chains:
            _stash_vol_chain(snap, row)
        _hub()._publish_vol(world, snap)
        chain_note = (
            "vertical send needs option_quote with long_strike and short_strike; "
            "chain prices are not a live limit."
        )
        if len(chains) == 1:
            chains[0]["note"] = chain_note
            return _hub()._clip(chains[0])
        return _hub()._clip(
            {"source": "ibkr", "chains": chains, "note": chain_note}
        )
    if name == "option_quote":
        combo = _combo_quote_spec(args)
        if combo:
            row = await _one_combo_quote(connector, combo)
            try:
                from abcxauto.look_snapshot import record_look_tool

                record_look_tool(snap, "option_quote", row)
            except Exception:
                logger.debug("look snapshot option_quote record failed", exc_info=True)
            if isinstance(row, dict):
                try:
                    from abcxauto.world_state import attach_tool_math

                    attach_tool_math(row, world)
                except Exception:
                    logger.debug("combo math page failed", exc_info=True)
            return _hub()._clip(row)
        specs = option_quote_specs(args)
        if not specs:
            return json.dumps({"error": "symbol, expiration, strike, right required", "source": "ibkr"})
        rows = await asyncio.gather(
            *[_one_option_quote(connector, spec) for spec in specs[:OPTION_QUOTE_CAP]]
        )
        for row in rows:
            _stash_vol_option_quote(snap, row)
        _hub()._publish_vol(world, snap)
        try:
            from abcxauto.look_snapshot import record_look_tool

            if len(rows) == 1:
                record_look_tool(snap, "option_quote", rows[0])
            else:
                record_look_tool(
                    snap,
                    "option_quote",
                    {
                        "quotes": list(rows),
                        "use": "ibkr_live_for_decisions; mda_greeks_delayed",
                    },
                )
        except Exception:
            logger.debug("look snapshot option_quote record failed", exc_info=True)
        if len(rows) == 1:
            one = _public_quote_row(rows[0])
            try:
                from abcxauto.world_state import attach_tool_math

                attach_tool_math(one, world)
            except Exception:
                logger.debug("option quote math page failed", exc_info=True)
            return _hub()._clip(one)
        packed = {
            "quotes": [_public_quote_row(r) if isinstance(r, dict) else r for r in rows],
            "use": "ibkr_live_for_decisions; mda_greeks_delayed",
        }
        try:
            from abcxauto.world_state import attach_tool_math

            attach_tool_math(packed, world)
        except Exception:
            logger.debug("option quote math page failed", exc_info=True)
        return _hub()._clip(packed)
    if name == "option_facts":
        from abcxauto.option_facts import fetch_option_facts

        facts = await fetch_option_facts(
            world.positions or snap.get("positions") or [],
            connector=connector,
        )
        world.option_facts = facts
        snap["option_facts"] = facts
        _hub()._publish_vol(world, snap)
        return _hub()._clip({
            "source": "ibkr_live+mda_greeks",
            "freshness": "ibkr_live; greeks_delayed_15m",
            "use": "ibkr_live_for_decisions; mda_greeks_delayed",
            "facts": facts,
        })
    if name == "send":
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        act = {
            "action": str(args.get("strategy") or args.get("action") or "").strip(),
            "strategy": str(args.get("strategy") or args.get("action") or "").strip(),
            "params": dict(params),
            "rationale": str(args.get("rationale") or ""),
        }
        bind_send_card(act, extra=args.get("card"))
        if args.get("target_conId"):
            act["target_conId"] = str(args.get("target_conId"))
        if args.get("preview") in (True, 1, "1", "true", "True", "yes", "on"):
            act["preview"] = True
        token = args.get("preview_token") or args.get("place_token")
        if token not in (None, ""):
            act["preview_token"] = str(token).strip()
        result = await execute_ticket(act, connector, world, snap)
        strat = str(act.get("strategy") or result.get("strategy") or "")
        if not isinstance(result, dict):
            result = {"raw": result}
        else:
            result = dict(result)
        from abcxauto.world_state import COMBO_FACT, COMBO_STRATS

        result["sends_this_turn"] = len(turn.sends) + 1
        if strat in COMBO_STRATS or "IBKR combo" in str(result.get("note") or ""):
            result["combo"] = COMBO_FACT
        err = result.get("error") or result.get("tws_error")
        if err:
            result["tws_error"] = err
        turn.sends.append({"act": dict(act), "result": result, "strat": strat})
        turn.last_act = dict(act)
        turn.last_result = result
        turn.last_strat = strat
        if _hub()._send_succeeded(result):
            try:
                await _hub()._write_last_turn_after_send(
                    connector=connector,
                    world=world,
                    snap=snap,
                    turn=turn,
                    act=act,
                    strat=strat,
                )
            except Exception:
                logger.debug("post-send last_turn write failed", exc_info=True)
        return _hub()._clip(result)
    if name == "self_tune":
        from abcxauto.self_tune import apply_self_tune

        blob = dict(args)
        if isinstance(blob.get("params"), dict):
            nested = dict(blob.pop("params"))
            nested.update(blob)
            blob = nested
        rationale = str(blob.pop("rationale", "") or "")
        result = apply_self_tune(blob, persist=True, rationale=rationale)
        if not isinstance(result, dict):
            result = {"raw": result}
        else:
            result = dict(result)
        strat = "self_tune"
        act = {"action": strat, "strategy": strat, "params": blob, "rationale": rationale}
        turn.sends.append({"act": dict(act), "result": result, "strat": strat})
        turn.last_act = dict(act)
        turn.last_result = result
        turn.last_strat = strat
        return _hub()._clip(result)
    if name == "web":
        from abcxauto.desk_mode import WEB_USE, fetch_public_page, search_public

        url = str(args.get("url") or "").strip()
        query = str(args.get("query") or args.get("q") or "").strip()
        if url:
            page = await fetch_public_page(url)
        elif query:
            page = await search_public(
                query,
                where=str(args.get("where") or "both"),
                handles=args.get("handles"),
            )
        else:
            page = {
                "error": "web needs query or url",
                "source": "web",
                "use": WEB_USE,
            }
        if not isinstance(page, dict):
            page = {"error": str(page), "source": "web", "use": WEB_USE}
        else:
            page.setdefault("use", WEB_USE)
        if isinstance(snap, dict):
            snap["research_web"] = dict(page)
        return _clip_web(page)
    if name == "recall":
        from abcxauto.memory.notes import recall_tool

        return _hub()._clip(recall_tool(args if isinstance(args, dict) else {}))
    if name == "research_brief":
        from abcxauto.desk_mode import load_research_brief, research_brief_tool_payload

        return _hub()._clip(
            research_brief_tool_payload(
                load_research_brief(),
                snap=snap if isinstance(snap, dict) else None,
                world=world,
            )
        )
    return json.dumps({"error": f"unknown tool {name}"})



__all__ = [
    '_hub',
    'MAX_TOOL_STEPS',
    'TOOL_S',
    'SEND_S',
    'CHAIN_S',
    'CANDLE_S',
    'CANDLE_WAIT_S',
    'CHAIN_WAIT_S',
    'SCAN_S',
    'SCAN_CLIP_CHARS',
    '_clip_scan',
    '_clip_web',
    '_slim_web_hits',
    '_QUOTE_SCHEMA',
    '_SYMBOLS_SCHEMA',
    '_scan_arena_keys',
    '_scan_code_keys',
    '_news_symbols_for_scan',
    '_news_symbols_this_look',
    '_LOOK_SCAN_CACHE_KEY',
    '_SCAN_LOOK_SNAP_KEYS',
    '_record_scan_screen',
    '_canonical_scan_screen',
    '_scan_look_key',
    '_scan_snap_bag',
    '_restore_scan_snap',
    '_scan_gate_facts',
    '_union_scan_hits',
    '_scan_screen_on_look',
    '_scan_out_from_snap',
    '_scan_public_payload',
    '_slim_scan_news',
    '_strip_hit_news',
    '_SCAN_REUSE_NOTE',
    '_scan_reuse_stub',
    '_emit_scan_look_line',
    '_ingest_scan_payload',
    '_quote_last',
    '_scan_paint_rows',
    '_scan_open',
    '_scan_gap_pct',
    '_candle_res_from_tape',
    '_stamp_session_size',
    '_stamp_session_ticket',
    '_snap_is_rth',
    '_live_open_session',
    '_finish_live_session',
    '_session_rank',
    '_refresh_session_last',
    '_remember_session',
    '_apply_candle_session',
    '_scan_carries_news',
    '_attach_run_sheet',
    '_attach_scan_run',
    '_schema',
    '_send_tool',
    'AGENT_TOOLS',
    '_send_strategy_names_for_look',
    'agent_tools',
    '_web_tool',
    '_stash_live',
    '_stash_vol_bars',
    '_stash_vol_chain',
    '_stash_vol_quote_iv',
    '_stash_vol_option_quote',
    '_publish_vol',
    '_compact_chain',
    '_mda_news',
    '_combo_quote_spec',
    '_one_combo_quote',
    '_one_option_quote',
    '_run_tool',
]
