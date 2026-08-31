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
SCAN_S = 35.0
_QUOTE_SCHEMA = {"type": "string", "description": "Ticker, e.g. AAPL"}
_SYMBOLS_SCHEMA = {"type": "array", "items": {"type": "string"}}


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
    """Bare news() reads this look's gap tape, not SPY, when the book is flat."""
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
)


def _record_scan_screen(snap: dict[str, Any], arena: str, scan_code: str) -> None:
    from abcxauto.lab_playbook import scan_screen_key

    key = scan_screen_key(arena, scan_code)
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
    """One look tape. Bare scan and the flush trio share a key. Not ``symbols[]``."""
    bag = args if isinstance(args, dict) else {}
    arena, code = _canonical_scan_screen(
        str(bag.get("arena") or "").strip(),
        str(bag.get("scan_code") or "").strip(),
    )
    if not arena and not code:
        return _LOOK_SCAN_CACHE_KEY
    try:
        from abcxauto.universe import is_flush_default_screen

        if is_flush_default_screen(arena, code):
            return _LOOK_SCAN_CACHE_KEY
    except Exception:
        pass
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

    Playbook when_on is not a floor. Levered / micro never occupy
    ``deepest``; those hits stay on the tape.
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
    deepest = None
    deepest_sym = ""
    deepest_signed = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("open_gap_pct") is None:
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
            by[sym] = dict(row)
            continue
        keep = dict(prev)
        if _open_gap_mag(row) > _open_gap_mag(prev):
            keep.update({k: v for k, v in row.items() if v not in (None, "")})
        else:
            for key, val in row.items():
                if keep.get(key) in (None, "") and val not in (None, ""):
                    keep[key] = val
        by[sym] = keep
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
    """True when this selector is already on the look tape (or the flush ran)."""
    from abcxauto.lab_playbook import scan_screen_key
    from abcxauto.universe import is_flush_default_screen

    if not arena and not code:
        return bool(snap.get("scan_flush"))
    if snap.get("scan_flush") and is_flush_default_screen(arena, code):
        return True
    key = scan_screen_key(arena, code)
    used = [str(x) for x in (snap.get("scan_screens") or [])]
    return bool(key and key in used)


def _scan_out_from_snap(
    snap: dict[str, Any],
    qmap: dict[str, Any] | None,
    *,
    last_ok: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
    rows = _scan_paint_rows(merged, quotes=qmap)
    seed = last_ok if isinstance(last_ok, dict) else {}
    arenas = list(snap.get("scan_arenas") or [])
    out: dict[str, Any] = {
        "ok": True,
        "source": merged.get("source") or seed.get("source") or "ibkr",
        "symbols": [r.get("symbol") for r in rows if r.get("symbol")],
        "hits": rows,
        "rows": rows,
        "applied": seed.get("applied") or {},
        "persisted": False,
        "ranked": bool(merged.get("ranked") if merged else seed.get("ranked")),
        "rank_meaning": (merged.get("rank_meaning") if merged else None)
        or seed.get("rank_meaning"),
        "quoted": merged.get("quoted") or seed.get("quoted") or 0,
        "arenas": arenas,
    }
    if len(arenas) == 1:
        out["arena"] = merged.get("arena") or seed.get("arena")
        out["scan_code"] = merged.get("scan_code") or seed.get("scan_code")
    out.update(_scan_gate_facts(rows))
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
    for row in sort_scan_rows(rows):
        item = dict(row)
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
        if row.get("open_gap_pct") is not None:
            return row.get("open_gap_pct")
        ibkr = row.get("ibkr")
        if isinstance(ibkr, dict) and ibkr.get("open_gap_pct") is not None:
            return ibkr.get("open_gap_pct")
    qmap = snap.get("ibkr_live_quotes")
    if isinstance(qmap, dict):
        quote = qmap.get(want)
        if isinstance(quote, dict) and quote.get("open_gap_pct") is not None:
            return quote.get("open_gap_pct")
    return None


def _candle_res_from_tape(snap: dict[str, Any] | None) -> str:
    """Daily bars cannot answer an opening-low hold on a gap screen."""
    try:
        from abcxauto.lab_playbook import live_card_needs_session

        if live_card_needs_session():
            return "5"
    except Exception:
        pass
    hits = snap.get("scan_hits") if isinstance(snap, dict) else None
    if not isinstance(hits, dict):
        return "D"
    for row in hits.get("rows") or []:
        if isinstance(row, dict) and row.get("open_gap_pct") is not None:
            return "5"
    return "D"


def _stamp_session_size(session: dict[str, Any], world: WorldState) -> None:
    """Knob-sized shares if stop is this session low. Not a ticket."""
    if not isinstance(session, dict) or session.get("size"):
        return
    try:
        from abcxauto.protect import size_if_stop

        sized = size_if_stop(
            last=session.get("last"),
            stop=session.get("low"),
            equity=getattr(world, "net_liquidation", None),
        )
    except Exception:
        sized = {}
    if sized:
        session["size"] = sized
    try:
        from types import SimpleNamespace

        from abcxauto.lab_playbook import live_card_send_facts

        facts = live_card_send_facts()
    except Exception:
        facts = {}
    if not facts:
        return
    try:
        from abcxauto.protect import size_if_stop

        risk = facts.get("risk_pct")
        if risk is None:
            card_sized = {}
        else:
            card_sized = size_if_stop(
                last=session.get("last"),
                stop=session.get("low"),
                equity=getattr(world, "net_liquidation", None),
                cfg=SimpleNamespace(
                    max_risk_per_trade_pct=risk,
                    max_position_pct=(
                        facts.get("notional_pct")
                        if facts.get("notional_pct") is not None
                        else 100.0
                    ),
                ),
            )
    except Exception:
        card_sized = {}
    if facts.get("risk_pct") is not None or facts.get("notional_pct") is not None:
        session.setdefault("size", {})
        if facts.get("risk_pct") is not None:
            session["size"]["card_risk_pct"] = facts["risk_pct"]
        if facts.get("notional_pct") is not None:
            session["size"]["card_notional_pct"] = facts["notional_pct"]
    if card_sized:
        session.setdefault("size", {})
        session["size"]["card_qty"] = card_sized["qty"]


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


def _note_scan_news(turn: BrainTurn, payload: dict[str, Any]) -> None:
    """Headlines already on the screen are the card's news step."""
    from abcxauto.lab_playbook import _scan_carries_news

    if not _scan_carries_news(payload):
        return
    if "news" not in turn.tool_trace:
        turn.tool_trace.append("news")


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


# The branches under one order type. A card's parent key is its ticket, so it
# carries no ticket of its own — see lab_playbook for why identity is
# (type, name).
_CARD_BRANCH_SCHEMA = {
    "type": "array",
    "description": (
        "Hypotheses under this order type. A named write changes that card "
        "and keeps siblings; omit the key to keep the list. Do not rewrite a "
        "name to record a look. cards=[] clears this type. status=retired "
        "drops it from the hunt. A card that earns its sample belongs "
        "promoted into this type's gotchas / review / tool_order — same "
        "stanza, move it up."
    ),
    "items": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The setup, e.g. 'gap fade after 10:00'",
            },
            "thesis": {
                "type": "string",
                "description": "What you claim will happen, and why.",
            },
            "evidence": {
                "type": "object",
                "description": "Grounds for this card, not a look diary.",
                "properties": {
                    "scan": {
                        "type": "string",
                        "description": "arena / scan_code + filters that surfaced it.",
                    },
                    "news": {
                        "type": "string",
                        "description": "Headlines behind the thesis.",
                    },
                    "reads": {
                        "type": "string",
                        "description": "quote / candles / option_quote reads you took.",
                    },
                    "odds": {
                        "type": "string",
                        "description": "Prediction-market implied probs, where relevant.",
                    },
                },
            },
            "expect_hit_rate": {
                "type": "number",
                "description": (
                    "Win rate you expect, percent. Scored against what the card "
                    "actually hit; never gates graduation."
                ),
            },
            "fill_assumption": {
                "type": "string",
                "description": (
                    "How fills are assumed. paper_mid is stored and cannot "
                    "graduate. Graduation needs full_spread or conservative "
                    "and a conservative_pnl mark, not paper TWS realized."
                ),
            },
            "retire_if": {
                "type": "object",
                "description": (
                    "How this card dies. The clerk enforces exactly what you "
                    "declare and invents nothing."
                ),
                "properties": {
                    "sample": {
                        "type": "integer",
                        "description": (
                            "Resolved trades that settle the question. Operator "
                            "flattens and halt exits do not count toward it."
                        ),
                    },
                    "condition": {
                        "type": "string",
                        "description": "What would falsify the thesis.",
                    },
                    "max_loss_usd": {
                        "type": "number",
                        "description": "Resolved loss that kills it early.",
                    },
                    "max_losses": {
                        "type": "integer",
                        "description": "Losing resolved trades that kill it early.",
                    },
                    "max_hold_sessions": {
                        "type": "integer",
                        "description": (
                            "Weekday ET dates the ticket may stay open. "
                            "Past that, code flattens the lot and trips the card."
                        ),
                    },
                    "max_hold_hours": {
                        "type": "number",
                        "description": "Wall-clock hours the ticket may stay open.",
                    },
                },
                "required": ["sample", "condition"],
            },
            "when_on": {
                "type": "string",
                "description": "Conditions that turn this card on.",
            },
            "scan": {
                "type": "string",
                "description": "Screen that finds it (arena + filters).",
            },
            "shape": {"type": "string"},
            "invalidation": {"type": "string"},
            "status": {
                "type": "string",
                "description": (
                    "testing | working | retired. Graduation to live is the "
                    "clerk's verdict from resolved trades."
                ),
            },
            "note": {"type": "string"},
        },
        "required": ["name", "thesis", "retire_if"],
    },
}


def _send_tool(strategy_names: list[str] | None = None) -> Any:
    """Build the send tool. Hold is never a ticket."""
    names = list(strategy_names) if strategy_names is not None else ticket_strategy_names()
    return tool(
        name="send",
        description=(
            "One IBKR ticket per call. Call send again this turn for another ticket. "
            "strategy name + fields match ORDER EXAMPLES. "
            "Size (% of NL) and book width (self_tune max_open_positions) are together, not pick-one. "
            "Knobs are self_tune, not a ticket. Hard risk is code."
        ),
        parameters=_schema(
            {
                "strategy": {
                    "type": "string",
                    "enum": names,
                    "description": "Ticket name from ORDER EXAMPLES.",
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
                "right": {"type": "string", "description": "C or P"},
                "params": {
                    "type": "object",
                    "description": "Extra ticket fields from ORDER EXAMPLES if not top-level.",
                },
                "target_conId": {"type": "string"},
                "card": {
                    "type": "string",
                    "description": (
                        "Playbook card this ticket comes from. Required on new "
                        "risk (must name an existing lab card so the fill is "
                        "scored); optional on exits, protection, modifies and "
                        "cancels. Scorecard label, not law — card prose is not "
                        "a send gate."
                    ),
                },
                "rationale": {"type": "string"},
            },
            ["strategy"],
        ),
    )


# Catalog for this look.
AGENT_TOOLS = [
    tool(
        name="book",
        description=(
            "Live IBKR book: positions, working orders, protection, tape, "
            "scorecard, desk lessons."
        ),
        parameters=_schema({}, []),
    ),
    tool(
        name="status",
        description="IBKR/MDA/xAI link and trading mode.",
        parameters=_schema({}, []),
    ),
    tool(
        name="quote",
        description="IBKR live last/bid/ask (TWS stream). One symbol or symbols[] (max 8). Not MDA.",
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
            "MDA headlines (~15 min delayed). Color only, never a trigger. "
            "Anything time-sensitive at +15 minutes is already in the price. "
            "Bare news() uses this look's scan tape when present, not SPY."
        ),
        parameters=_schema({"symbols": _SYMBOLS_SCHEMA}, []),
    ),
    tool(
        name="odds",
        description=(
            "Prediction-market implied probs (Polymarket). Crowd odds for events, "
            "not IBKR last."
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
            "IBKR scanner. arena+scan_code, or symbols[]. Hits are the union "
            "with IBKR live last on the top names — triage from these, do not "
            "re-quote."
        ),
        parameters=_schema(
            {
                "arena": {
                    "type": "string",
                    "description": "arenas=" + ",".join(_scan_arena_keys()),
                },
                "scan_code": {
                    "type": "string",
                    "description": "|".join(_scan_code_keys()),
                },
                "symbols": _SYMBOLS_SCHEMA,
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
                "with": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: news and/or metrics. MDA delayed color, "
                        "never a trigger, not send geometry."
                    ),
                },
            },
            [],
        ),
    ),
    tool(
        name="candles",
        description=(
            "IBKR hist or live 5s. Error if both miss. Not MDA. "
            "One symbol or symbols[] (max 8). "
            "resolution D = daily; 15/5/60 = hist size (stream is always 5s)."
        ),
        parameters=_schema(
            {
                "symbol": _QUOTE_SCHEMA,
                "symbols": _SYMBOLS_SCHEMA,
                "resolution": {"type": "string"},
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
            "IBKR live bid/ask/last for one option or contracts[] (max 8). "
            "MDA greeks delayed if present — not send geometry."
        ),
        parameters=_schema(
            {
                "symbol": _QUOTE_SCHEMA,
                "expiration": {"type": "string", "description": "YYYYMMDD"},
                "strike": {"type": "number"},
                "right": {"type": "string", "description": "C or P"},
                "contracts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": _QUOTE_SCHEMA,
                            "expiration": {"type": "string"},
                            "strike": {"type": "number"},
                            "right": {"type": "string"},
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
            "max_open_positions is concurrent lots for this book's NL — "
            "not a baked 15/25. Size (size_pct_nl on send / self_tune) "
            "and slots are together, not pick-one. size_pct_nl tightens "
            "the explore/exploit mode band. session_look_cap / "
            "session_token_cap tighten only. Not a ticket — send is the book."
        ),
        parameters=_schema(
            {
                "max_risk_per_trade_pct": {"type": "number"},
                "daily_loss_limit_pct": {"type": "number"},
                "max_position_pct": {"type": "number"},
                "max_peak_drawdown_pct": {"type": "number"},
                "max_option_premium_pct": {"type": "number"},
                "max_symbol_concentration_pct": {"type": "number"},
                "max_arena_concentration_pct": {"type": "number"},
                "max_open_positions": {"type": "integer"},
                "size_pct_nl": {
                    "type": "number",
                    "description": (
                        "Working size ceiling as % of current NL. "
                        "Tighten inside the explore/exploit mode band. "
                        "send.apply_size_pct_nl uses the same band."
                    ),
                },
                "session_look_cap": {"type": "integer"},
                "session_token_cap": {"type": "integer"},
                "enabled_arenas": _SYMBOLS_SCHEMA,
                "custom_symbols": _SYMBOLS_SCHEMA,
                "exclude_symbols": _SYMBOLS_SCHEMA,
                "rationale": {"type": "string"},
            },
            [],
        ),
    ),
    tool(
        name="playbook",
        description=(
            "Your notes plus how they scored since the write. "
            "revision= is an old card's outcome (edge, lots), not the old essay."
        ),
        parameters=_schema(
            {
                "revision": {"type": "integer"},
                "full": {"type": "boolean"},
            },
            [],
        ),
    ),
    tool(
        name="write_lab_playbook",
        description=(
            "Paper only: your book, one tree. Keyed by order type — each type "
            "holds what you learned executing it (durable) and the cards "
            "branching under it (disposable, one hypothesis each). A card's "
            "type is what it sends, so it needs no ticket of its own. "
            "instructions is free notes. A named rewrite changes the card; "
            "do not write to record that you looked. A revision is a card, "
            "type, or mode change — a same-book rescan note is dropped, not "
            "a new revision. Not a wake clock."
        ),
        parameters=_schema(
            {
                "types": {
                    "type": "object",
                    "description": (
                        "Keyed by sendable strategy name. Only what you learned "
                        "running that structure — the schema is already in ORDER "
                        "EXAMPLES. Omitted types keep what you last wrote, cards "
                        "included."
                    ),
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "tool_order": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Tool sequence that actually works for this "
                                    "structure, in order."
                                ),
                            },
                            "gotchas": {
                                "type": "string",
                                "description": (
                                    "Execution traps you hit: rejects, fill lag, "
                                    "leg order, protection timing."
                                ),
                            },
                            "review": {
                                "type": "string",
                                "description": (
                                    "How you check the result after this structure "
                                    "is on and after it comes off."
                                ),
                            },
                            "note": {"type": "string"},
                            "cards": _CARD_BRANCH_SCHEMA,
                        },
                    },
                },
                "instructions": {
                    "type": "string",
                    "description": "Free notes: regime, observations, what to watch.",
                },
                "mode": {
                    "type": "string",
                    "description": (
                        "Size bit: explore or exploit — not a personality label"
                    ),
                },
                "ready_to_promote": {"type": "boolean"},
            },
            [],
        ),
    ),
    tool(
        name="write_desk_lessons",
        description=(
            "Durable tool facts across looks. book() returns the shelf. "
            "Not a playbook card, not law, not a wake job. A fact is a "
            "tool mechanic you already hit — no tickers, no skip list."
        ),
        parameters=_schema(
            {
                "lessons": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "fact": {
                                "type": "string",
                                "description": "Tool fact only. Not a card, not law.",
                            },
                        },
                    },
                    "description": "Upsert extras. The seed lesson stays.",
                },
                "fact": {
                    "type": "string",
                    "description": "Add or replace one tool fact.",
                },
                "id": {"type": "string"},
            },
            [],
        ),
    ),
]


def _send_strategy_names_for_look() -> list[str]:
    """send enum this look. Hold is never a ticket."""
    return [n for n in ticket_strategy_names() if n != "hold"]


def agent_tools(*, session: str = "") -> list:
    """Tools this look. Overnight park is code, not a Grok clock."""
    _ = session
    names = _send_strategy_names_for_look()
    out: list = []
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        name = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        if name == "send":
            out.append(_send_tool(names))
        else:
            out.append(t)
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
    from abcxauto.news_feed import fetch_symbols_news
    from abcxauto.prints import mda_worth_asking

    syms = [s for s in symbols[:8] if s and mda_worth_asking(s)]
    if not syms:
        return []
    return await fetch_symbols_news(syms, per_symbol=per_symbol)


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
        from abcxauto.world_state import COMBO_FACT

        st = connection_status(connector)
        try:
            st["session"] = get_session_info()
        except Exception:
            st["session"] = {"session": world.session_status}
        try:
            from abcxauto.self_tune import levers_snapshot

            st["levers"] = levers_snapshot()
        except Exception:
            st["levers"] = {}
        st["combo"] = COMBO_FACT
        st["sends_this_turn"] = len(turn.sends)
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
            from abcxauto.desk_lessons import desk_lessons_payload

            st["desk_lessons"] = desk_lessons_payload()
        except Exception:
            st["desk_lessons"] = []
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
                st["freshness"] = {
                    "ibkr_connected": fresh.get("ibkr_connected"),
                    "ibkr_snapshot_age_s": fresh.get("ibkr_snapshot_age_s"),
                    "mda_spy_quote_age_s": fresh.get("mda_spy_quote_age_s"),
                    "spy_last": fresh.get("spy_last"),
                    "vix": fresh.get("vix"),
                }
        return _hub()._clip(st)
    if name == "quote":
        raw = await run_readonly_tool("quote", args, connector)
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
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
            fetch_agent_news,
            news_hard_miss,
            remember_headlines,
            remember_look_news,
        )

        remember_look_news(world, snap)
        asked = normalize_tickers(args.get("symbols"))
        tape = _news_symbols_this_look(world, snap, asked)
        if tape:
            items = await _hub()._mda_news(tape)
        else:
            items = await fetch_agent_news(world.positions or snap.get("positions") or [])
        items = coalesce_news(items, tape or asked or None)
        remember_headlines(items)
        world.news_items = list(items)
        snap["news_items"] = list(items)
        payload = {
            "source": "mda",
            "freshness": "delayed_15m",
            "use": "color_not_trigger",
            "note": "delayed MDA; time-sensitive at +15m is already in the price",
            "items": items[:24],
        }
        miss = news_hard_miss(items)
        if miss:
            payload["error"] = f"news unavailable - {miss}"
        _attach_run_sheet(payload, turn=turn, world=world, tool="news", quoted=snap)
        return _hub()._clip(payload)
    if name == "odds":
        from abcxauto.config import get_config
        from abcxauto.prediction_odds import fetch_odds

        asked = normalize_tickers(args.get("symbols"))
        q = str(args.get("query") or "").strip()
        payload = await fetch_odds(
            symbols=asked,
            query=q,
            positions=list(world.positions or snap.get("positions") or []),
        )
        payload["path"] = _hub()._path_block(world, get_config())
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
            flush_cap_filters,
            flush_default_jobs,
            is_flush_default_screen,
            parse_scan_filters,
            resolve_screen,
            verified_pe_tags,
        )

        pe_tags = await verified_pe_tags(connector)
        parsed = parse_scan_filters(args, pe_tags=pe_tags)
        if not parsed.get("ok"):
            return _hub()._clip({"ok": False, "error": parsed.get("error") or "bad scan filters"})
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
            if not want_news or out.get("news"):
                return
            from abcxauto.prints import attach_mda_news

            news_syms = _news_symbols_for_scan(
                snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {},
                [str(s) for s in (out.get("symbols") or []) if s],
            )
            out["news"] = await _hub()._mda_news(news_syms)
            out["news_freshness"] = "delayed_15m"
            out["news_use"] = "color_not_trigger"
            attach_mda_news(out.get("hits") or out.get("rows") or [], out["news"])
            if out["news"]:
                snap["scan_news_attached"] = True
                if not world.news_items:
                    world.news_items = list(out["news"])
                    snap["news_items"] = list(out["news"])

        async def _finish_look_bag(
            last_ok: dict[str, Any] | None,
            *,
            emit_line: bool,
        ) -> str:
            out = _scan_out_from_snap(snap, qmap, last_ok=last_ok)
            if want_metrics and out.get("hits"):
                from abcxauto.opportunity_scan import attach_mda_metrics

                await attach_mda_metrics(out["hits"])
            await _attach_optional_news(out)
            painted = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
            snap["scan_hits"] = _union_scan_hits(
                painted, {**painted, "rows": out["rows"]}
            )
            if _snap_is_rth(snap):
                sessions: dict[str, Any] = {}
                for row in out.get("rows") or []:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("symbol") or "").upper()
                    if not name:
                        continue
                    live = _live_open_session(
                        snap,
                        name,
                        last=row.get("last"),
                        open_px=row.get("open"),
                        open_gap_pct=row.get("open_gap_pct"),
                    )
                    if not live:
                        continue
                    row["session"] = _finish_live_session(
                        live, snap=snap, world=world, symbol=name, tape=row
                    )
                    sessions[name] = row["session"]
                if sessions:
                    out["sessions"] = sessions
            _note_scan_news(turn, out)
            _attach_scan_run(out, turn=turn, world=world)
            if emit_line:
                _emit_scan_look_line(snap, out)
            turn.scan_cache[_LOOK_SCAN_CACHE_KEY] = deepcopy(out)
            return _hub()._clip(out)

        async def _repeat_look_bag() -> str:
            reused = _scan_out_from_snap(snap, qmap)
            reused["repeat_of_this_think"] = True
            reused["reused"] = True
            reused["note"] = "this screen already fetched this look"
            await _attach_optional_news(reused)
            _attach_scan_run(reused, turn=turn, world=world)
            think_emit("tool", "\n[scan = already have it]\n")
            turn.scan_cache[_LOOK_SCAN_CACHE_KEY] = deepcopy(reused)
            return _hub()._clip(reused)

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
                    return _hub()._clip(err)
                _ingest_scan_payload(
                    world=world,
                    snap=snap,
                    payload=payload,
                    job={"arena": "", "scan_code": ""},
                )
                snap["scan_at"] = datetime.now(timezone.utc).isoformat()
                return await _finish_look_bag(payload, emit_line=False)

            if has_screen:
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
                    return _hub()._clip(err)

            flush_done = bool(snap.get("scan_flush"))
            if flush_done and _scan_screen_on_look(snap, c_arena, c_code):
                return await _repeat_look_bag()

            jobs: list[dict[str, Any]] = []
            job_filters: list[dict[str, Any]] = []
            ran_flush = False
            if not flush_done:
                jobs.extend(flush_default_jobs())
                cap = flush_cap_filters(parsed)
                job_filters.extend(cap for _ in jobs)
                ran_flush = True
                if has_screen and not is_flush_default_screen(c_arena, c_code):
                    jobs.append({"arena": args.get("arena"), "scan_code": args.get("scan_code")})
                    job_filters.append(parsed)
            else:
                jobs.append({"arena": args.get("arena"), "scan_code": args.get("scan_code")})
                job_filters.append(parsed)

            last_ok: dict[str, Any] | None = None
            last_err: dict[str, Any] | None = None
            for job, filt in zip(jobs, job_filters):
                snap["scan_calls"] = int(snap.get("scan_calls") or 0) + 1
                payload = await _hub().criteria_scan(
                    arena=job.get("arena"),
                    scan_code=job.get("scan_code"),
                    positions=list(world.positions or snap.get("positions") or []),
                    connector=connector,
                    turn_symbols=turn_syms,
                    filters=filt,
                )
                if not payload.get("ok"):
                    last_err = payload
                    continue
                last_ok = payload
                _ingest_scan_payload(
                    world=world, snap=snap, payload=payload, job=job
                )
            if ran_flush and last_ok is not None:
                snap["scan_flush"] = True
            if last_ok is None:
                err = last_err or {
                    "ok": False,
                    "error": "scan requires arena | scan_code | symbols[]",
                }
                if isinstance(err, dict):
                    err = dict(err)
                    err.setdefault("ok", False)
                    _attach_scan_run(err, turn=turn, world=world)
                return _hub()._clip(err)
            snap["scan_at"] = datetime.now(timezone.utc).isoformat()
            return await _finish_look_bag(last_ok, emit_line=True)
    if name == "candles":
        from abcxauto.broker.bars import ibkr_bar_freshness

        syms = normalize_tickers(
            args.get("symbols") or args.get("symbol"), cap=CANDLE_CAP
        )
        if not syms:
            return json.dumps({"error": "symbol required", "source": "ibkr"})
        try:
            countback = int(args.get("countback") or 60)
        except (TypeError, ValueError):
            countback = 60
        countback = max(5, min(countback, 120))
        from abcxauto.broker.bars import normalize_resolution, session_countback

        res = str(args.get("resolution") or "").strip()
        if not res:
            res = _candle_res_from_tape(snap)
        else:
            res = normalize_resolution(res)
        if normalize_resolution(res) in ("5", "15", "60"):
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
        budget = min(CANDLE_S, max(28.0, 12.0 + 8.0 * len(syms)))

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
                    out["bars"] = list(out.get("bars") or [])[-bar_cap:]
                    out.setdefault("source", "ibkr")
                    out.setdefault("freshness", ibkr_bar_freshness(res))
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
        else:
            payload["series"] = series
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
        if len(chains) == 1:
            return _hub()._clip(chains[0])
        return _hub()._clip({"source": "ibkr", "chains": chains})
    if name == "option_quote":
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
            return _hub()._clip(rows[0])
        return _hub()._clip({
            "quotes": list(rows),
            "use": "ibkr_live_for_decisions; mda_greeks_delayed",
        })
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
                from abcxauto.lab_playbook import record_card_send

                params = act.get("params") if isinstance(act.get("params"), dict) else {}
                record_card_send(
                    card=str(params.get("card") or args.get("card") or ""),
                    strategy=strat,
                    symbol=str(params.get("symbol") or ""),
                    result=result,
                    params=params,
                )
            except Exception:
                logger.debug("card send log failed", exc_info=True)
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
    if name == "playbook":
        from abcxauto.lab_playbook import playbook_payload

        full = args.get("full")
        if isinstance(full, str):
            full = full.strip().lower() in ("1", "true", "yes", "on")
        pos = getattr(world, "positions", None)
        if pos is None:
            pos = snap.get("positions") or []
        orders = getattr(world, "open_orders", None)
        if orders is None:
            orders = snap.get("open_orders") or []
        return _hub()._clip(
            playbook_payload(
                args.get("revision"),
                full=bool(full),
                positions=list(pos),
                orders=list(orders),
            ),
            max_chars=_hub().PLAYBOOK_CLIP_CHARS,
        )
    if name == "write_lab_playbook":
        from abcxauto.lab_playbook import apply_from_judgment, grounding_error

        note = grounding_error(args, tool_trace=turn.tool_trace)
        if note:
            return _hub()._clip({"status": "rejected", "note": note})
        args = dict(args)
        judgment = {"lab_playbook": args}
        state = apply_from_judgment(judgment)
        turn.lab_playbook = state
        return _hub()._clip(state or {"status": "ignored", "note": "live cannot rewrite lab"})
    if name == "write_desk_lessons":
        from abcxauto.desk_lessons import apply_desk_lessons

        return _hub()._clip(apply_desk_lessons(args if isinstance(args, dict) else {}))
    return json.dumps({"error": f"unknown tool {name}"})



__all__ = [
    '_hub',
    'MAX_TOOL_STEPS',
    'TOOL_S',
    'SEND_S',
    'CHAIN_S',
    'CANDLE_S',
    'SCAN_S',
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
    '_note_scan_news',
    '_attach_run_sheet',
    '_attach_scan_run',
    '_schema',
    '_CARD_BRANCH_SCHEMA',
    '_send_tool',
    'AGENT_TOOLS',
    '_send_strategy_names_for_look',
    'agent_tools',
    '_stash_live',
    '_stash_vol_bars',
    '_stash_vol_chain',
    '_stash_vol_quote_iv',
    '_stash_vol_option_quote',
    '_publish_vol',
    '_compact_chain',
    '_mda_news',
    '_one_option_quote',
    '_run_tool',
]
