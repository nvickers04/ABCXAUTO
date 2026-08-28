"""Grok owns the book via tools. The shell is facts + send gates.

Paper RTH / premarket stay-up continues the live chat across successful
looks. Overnight / after-close / empty-junk / dead stream drop it.
Tickets go through ``execute_ticket`` → ``send_action``. IBKR tools are
live. scan() is one criteria screen this look (hits + on_book); candles
are IBKR hist or the live 5s stream (error if both miss); news is ~15
min delayed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from xai_sdk.chat import developer, system, tool, tool_result, user

from abcxauto.llm import GrokClient, build_system_prompt
from abcxauto.opportunity_scan import criteria_scan, normalize_tickers
from abcxauto.order_examples import format_order_examples, ticket_strategy_names
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

logger = logging.getLogger(__name__)

# One wake = one linear think. This ceiling is a runaway-spend guard, not a
# budget the model should feel — repeated reads are answered from the ledger
# below, so an honest think finishes long before it.
MAX_TOOL_STEPS = 64
_MUTATING_TOOLS = frozenset(
    {"send", "self_tune", "write_lab_playbook", "write_desk_lessons"}
)
STREAM_CHUNK_S = 8.0
STREAM_IDLE_LIMIT = 6
STREAM_LOOP_UNIT = 12
STREAM_LOOP_COPIES = 6
STREAM_LOOP_SENTENCE_COPIES = 3
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


_SCAN_LOOK_SNAP_KEYS = (
    "scan_screens",
    "scan_hits",
    "scan_calls",
    "scan_fetched",
    "scan_at",
    "scan_news_attached",
    "scan_arenas",
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
    """Same look, same IBKR screen: scanCode / screen / symbols. Not ``with``."""
    bag = args if isinstance(args, dict) else {}
    arena, code = _canonical_scan_screen(
        str(bag.get("arena") or "").strip(),
        str(bag.get("scan_code") or "").strip(),
    )
    symbols = normalize_tickers(bag.get("symbols") or [])
    return json.dumps(
        {
            "arena": arena.strip().lower(),
            "scan_code": code.strip().upper(),
            "symbols": symbols,
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
    """Deepest |open_gap| on this tape. Playbook when_on is not a floor.

    Skip-class names (levered / micro) are not pinned as ``deepest`` when
    those skip cards are on the book. Hits stay on the tape; retrace is
    Grok's grade, not a card-prose refuse.
    """
    try:
        from abcxauto.think_stream import _signed_open_gap
    except Exception:
        return {}
    skip_rank = False
    try:
        from abcxauto.lab_playbook import skip_cards_on_book

        skip_rank = skip_cards_on_book(book)
    except Exception:
        skip_rank = False
    skip_of = None
    if skip_rank:
        try:
            from abcxauto.universe import scan_skip_class

            skip_of = scan_skip_class
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


def brain_system_prompt() -> str:
    from abcxauto.agent_loop import ALLOWED_ACTIONS, AWARENESS_HEART

    allowed = frozenset(a for a in ALLOWED_ACTIONS if a != "hold")
    return (
        build_system_prompt()
        + AWARENESS_HEART
        + "\n"
        + format_order_examples(allowed=allowed)
        + "\nsend changes the book; a look may end with no send."
    )


@dataclass
class BrainTurn:
    text: str = ""
    sends: list[dict[str, Any]] = field(default_factory=list)
    last_act: dict[str, Any] = field(default_factory=dict)
    last_result: dict[str, Any] = field(default_factory=dict)
    last_strat: str = ""
    tool_trace: list[str] = field(default_factory=list)
    lab_playbook: dict[str, Any] | None = None
    tool_budget_hit: bool = False
    parked: bool = False
    interrupted: bool = False
    failed: bool = False
    stream_error: str = ""
    steps: int = 0
    # Read results already fetched this think, keyed by tool + args. A repeat
    # ask is answered from here so the think moves forward instead of spinning.
    tool_cache: dict[str, str] = field(default_factory=dict)
    # IBKR screens this look, keyed by scanCode / screen / symbols. Survives
    # a stay-up poke so the same screen is not pulled four times.
    scan_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # IBKR allows one scanner sub at a time. Same-args scans in one tool
    # round used to gather and all miss the cache.
    scan_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def look_failed(self) -> bool:
        """True empty / lone '?' only. A real say or send/fill is not junk.

        A later empty assistant chunk, a leftover ``failed`` stamp, or a
        dead stream after a spoken/send look must not wipe the stay-up chat.
        """
        if self.parked:
            return False
        return _look_is_empty_or_question(self)


_OVERLOAD_MARKERS = (
    "resource_exhausted",
    "at capacity",
    "rate limit",
    "rate_limit",
    "too many requests",
    "overloaded",
    "unavailable",
    "429",
    "503",
)


def provider_overloaded(err: Any) -> bool:
    """True when xAI refused for capacity — back off long, do not re-ask."""
    blob = str(err or "").lower()
    if not blob:
        return False
    return any(m in blob for m in _OVERLOAD_MARKERS)


def _look_text_is_junk(text: str) -> bool:
    """True only for a true empty say or a lone '?'."""
    raw = (text or "").strip()
    return (not raw) or raw == "?"


def _look_has_send_or_fill(turn: "BrainTurn") -> bool:
    """True when this look dispatched a send (filled or working counts)."""
    if turn.sends:
        return True
    return _send_succeeded(turn.last_result)


def _look_is_empty_or_question(turn: "BrainTurn") -> bool:
    """Junk-drop: true empty assistant text or a lone '?', and no send/fill."""
    if _look_has_send_or_fill(turn):
        return False
    return _look_text_is_junk(turn.text)


def _send_succeeded(result: dict[str, Any] | None) -> bool:
    """True when send() actually dispatched — not a clerk block/reject."""
    if not isinstance(result, dict):
        return False
    status = str(result.get("status") or "").lower()
    if status in (
        "blocked", "rejected", "error", "failed", "held", "hold", "validated_block",
    ):
        return False
    if result.get("success") is False:
        return False
    return (
        result.get("success") is True
        or result.get("filled") is True
        or status in ("executed", "submitted", "ok", "filled", "success")
    )


async def _write_last_turn_after_send(
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    turn: "BrainTurn",
    act: dict[str, Any],
    strat: str,
) -> None:
    """Refresh last_turn from the live book immediately after a successful send."""
    positions = list(world.positions or snap.get("positions") or [])
    orders = list(world.open_orders or snap.get("open_orders") or [])
    if connector is not None:
        get_pos = getattr(connector, "get_positions", None)
        if callable(get_pos):
            try:
                live = await get_pos()
                if isinstance(live, list):
                    positions = live
                    world.positions = live
            except Exception:
                logger.debug("post-send position refresh failed", exc_info=True)
        get_ord = getattr(connector, "get_open_orders", None)
        if callable(get_ord):
            try:
                live_o = await get_ord()
                if isinstance(live_o, list):
                    orders = live_o
                    world.open_orders = live_o
            except Exception:
                logger.debug("post-send order refresh failed", exc_info=True)
    try:
        from abcxauto.world_state import book_is_flat

        world.flat = book_is_flat(positions, orders)
    except Exception:
        world.flat = not bool(positions)
    from abcxauto.think_stream import write_last_turn_after_send

    write_last_turn_after_send(
        strat=strat,
        sends=len(turn.sends),
        positions=positions,
        orders=orders,
        rationale=str(act.get("rationale") or ""),
        tool_trace=list(turn.tool_trace or []),
        net_liquidation=getattr(world, "net_liquidation", None),
        reality_pulse=snap.get("reality_pulse") or {},
        ibkr_live_last=getattr(world, "ibkr_live_last", None),
        ibkr_live_quotes=dict(getattr(world, "ibkr_live_quotes", None) or {}),
        scan_hits=snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {},
        session_range=(
            snap.get("session_range")
            if isinstance(snap.get("session_range"), dict)
            else {}
        ),
    )


PLAYBOOK_CLIP_CHARS = 48_000
# Compact 4×80 OHLC bars plus session still fit; the old 24k clip dropped
# the series to save the run sheet and Grok sized off a metadata stub.
CANDLES_CLIP_CHARS = 48_000

_CANDLES_LEAD = (
    "symbol",
    "source",
    "freshness",
    "resolution",
    "requested_resolution",
    "use",
    "error",
    "hist_error",
    "rt_error",
    "last",
    "bars",
    "series",
)


def _think_bar(bar: Any) -> dict[str, Any] | None:
    """OHLC/time for the think. Drop t_unix/t_iso twins that bloat the clip."""
    if not isinstance(bar, dict):
        return None
    out: dict[str, Any] = {}
    t = bar.get("t")
    if t in (None, ""):
        t = bar.get("t_iso") or bar.get("date")
    if t not in (None, ""):
        out["t"] = t
    for key in ("o", "h", "l", "c", "v"):
        val = bar.get(key)
        if val is not None:
            out[key] = val
    if out.get("c") is None and out.get("o") is None:
        return None
    return out


def _think_bars(bars: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for bar in bars or []:
        row = _think_bar(bar)
        if row:
            out.append(row)
    return out


def _with_think_bars(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    if isinstance(out.get("bars"), list):
        out["bars"] = _think_bars(out["bars"])
    series = out.get("series")
    if isinstance(series, list):
        slim: list[Any] = []
        for row in series:
            if not isinstance(row, dict):
                slim.append(row)
                continue
            item = dict(row)
            if isinstance(item.get("bars"), list):
                item["bars"] = _think_bars(item["bars"])
            slim.append(item)
        out["series"] = slim
    return out


def _candles_lead(data: dict[str, Any]) -> dict[str, Any]:
    lead = {k: data[k] for k in _CANDLES_LEAD if k in data}
    rest = {k: v for k, v in data.items() if k not in lead}
    return {**lead, **rest}


def _tape_payload(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("bars") or data.get("series"))


def _drop_key(row: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in row:
        return row
    out = dict(row)
    out.pop(key, None)
    return out


def _trim_bar_list(bars: list[Any], keep: int) -> list[Any]:
    if keep < 1 or len(bars) <= keep:
        return bars
    # Keep the open (head) and the live edge (tail). Oldest-only trim
    # dropped the 09:30 print that session.open is built from.
    head = max(1, min(keep // 4, 8))
    tail = keep - head
    if tail <= 0:
        return bars[-keep:]
    return list(bars[:head]) + list(bars[-tail:])


def _trim_payload_bars(data: dict[str, Any], keep: int) -> tuple[dict[str, Any], bool]:
    out = dict(data)
    trimmed = False
    if isinstance(out.get("bars"), list) and len(out["bars"]) > keep:
        out["bars"] = _trim_bar_list(out["bars"], keep)
        trimmed = True
    series = out.get("series")
    if isinstance(series, list):
        rows: list[Any] = []
        for row in series:
            if isinstance(row, dict) and isinstance(row.get("bars"), list) and len(row["bars"]) > keep:
                item = dict(row)
                item["bars"] = _trim_bar_list(item["bars"], keep)
                rows.append(item)
                trimmed = True
            else:
                rows.append(row)
        out["series"] = rows
    return out, trimmed


def _clip_candles(data: dict[str, Any], max_chars: int = CANDLES_CLIP_CHARS) -> str:
    """Bars are the payload. Never drop the series to save the run sheet."""
    payload = _candles_lead(_with_think_bars(dict(data)))
    text = json.dumps(payload, default=str)
    if len(text) <= max_chars:
        return text
    slim = dict(payload)
    for key in ("run", "metrics"):
        dropped = False
        if key in slim:
            slim.pop(key)
            dropped = True
        if isinstance(slim.get("series"), list):
            rows: list[Any] = []
            for row in slim["series"]:
                if isinstance(row, dict) and key in row:
                    row = _drop_key(row, key)
                    dropped = True
                rows.append(row)
            slim["series"] = rows
        if not dropped:
            continue
        slim["_clipped"] = key
        text = json.dumps(_candles_lead(slim), default=str)
        if len(text) <= max_chars:
            return text
    for keep in (80, 60, 40, 24, 16, 8, 5, 1):
        trial, trimmed = _trim_payload_bars(slim, keep)
        if not trimmed:
            continue
        trial["_clipped"] = "bars_tail"
        text = json.dumps(_candles_lead(trial), default=str)
        if len(text) <= max_chars:
            return text
        slim = trial
    kept: dict[str, Any] = {}
    for key in _CANDLES_LEAD:
        if key in slim:
            kept[key] = slim[key]
    if slim.get("error") and "error" not in kept:
        kept["error"] = slim["error"]
    kept["_clipped"] = "payload"
    text = json.dumps(_candles_lead(kept), default=str)
    if len(text) <= max_chars:
        return text
    kept, _ = _trim_payload_bars(kept, 1)
    return json.dumps(_candles_lead(kept), default=str)


# Fat scan / sessions / news / playbook essay — never the live book.
_FAT_CLIP_KEYS = (
    "hits",
    "news",
    "symbols",
    "rows",
    "scan_hits",
    "session_range",
    "sessions",
    "scan_tape",
    "types",
    "card_scores",
    "tree",
    "notes",
)
_FAT_NEST_FIRST = ("last_look", "world", "playbook", "day")
_LIVE_BOOK_ROOTS = frozenset(
    {"world", "day", "open_lots", "working_orders", "positions", "fills"}
)
_LIVE_BOOK_KEEP = (
    "day",
    "world",
    "desk_lessons",
    "open_lots",
    "working_orders",
    "positions",
    "fills",
    "ibkr_live_quotes",
    "sends_this_turn",
    "ibkr_connected",
    "trading_mode",
    "session",
    "combo",
    "freshness",
    "tradable_now",
    "countdown",
    "levers",
    "mode",
    "ibkr",
)


def _pop_fat_key(container: dict[str, Any]) -> str | None:
    """Drop the next fat key. Clip marker stays on this container."""
    for key in _FAT_CLIP_KEYS:
        if key not in container:
            continue
        container.pop(key)
        container["_clipped"] = key
        return key
    return None


def _clip_fat_once(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Pop one fat key: top-level scan first, then last_look / world / playbook."""
    slim = dict(data)
    if _pop_fat_key(slim):
        return slim, True
    for nest in _FAT_NEST_FIRST:
        inner = slim.get(nest)
        if not isinstance(inner, dict):
            continue
        inner = dict(inner)
        if _pop_fat_key(inner):
            slim[nest] = inner
            return slim, True
        for sub_key, sub in list(inner.items()):
            if sub_key in _LIVE_BOOK_ROOTS or not isinstance(sub, dict):
                continue
            sub = dict(sub)
            if _pop_fat_key(sub):
                inner[sub_key] = sub
                slim[nest] = inner
                return slim, True
    return slim, False


def _is_live_book(data: dict[str, Any]) -> bool:
    """book() / status shaped payloads — never payload-clip away the book."""
    if any(key in data for key in _LIVE_BOOK_ROOTS):
        return True
    if "desk_lessons" in data and any(
        key in data
        for key in (
            "ibkr_connected",
            "trading_mode",
            "session",
            "levers",
            "sends_this_turn",
            "path",
            "score_windows",
        )
    ):
        return True
    return False


def _keep_live_book(data: dict[str, Any]) -> dict[str, Any]:
    """Emergency book core. Lots, orders, and desk_lessons stay; fat look does not."""
    out: dict[str, Any] = {}
    for key in _LIVE_BOOK_KEEP:
        if key in data:
            out[key] = data[key]
    playbook = data.get("playbook")
    if isinstance(playbook, dict):
        kept_pb = {k: playbook[k] for k in ("lab", "cards", "mode") if k in playbook}
        if playbook.get("_clipped"):
            kept_pb["_clipped"] = playbook["_clipped"]
        if kept_pb:
            out["playbook"] = kept_pb
    look = data.get("last_look")
    if isinstance(look, dict) and look.get("_clipped"):
        out["last_look"] = {
            k: look[k]
            for k in ("fresh", "send_calls", "tools", "_clipped")
            if k in look
        }
    return out


def _clip(data: Any, max_chars: int = 24_000) -> str:
    """Keep the live book when the payload overflows. Fat scan clips first."""
    if _tape_payload(data):
        return _clip_candles(data, max_chars=max_chars)
    text = json.dumps(data, default=str)
    if len(text) <= max_chars:
        return text
    if isinstance(data, dict):
        slim = dict(data)
        while len(json.dumps(slim, default=str)) > max_chars:
            slim, changed = _clip_fat_once(slim)
            if not changed:
                break
            text = json.dumps(slim, default=str)
            if len(text) <= max_chars:
                return text
        if _is_live_book(slim):
            return json.dumps(_keep_live_book(slim), default=str)
        kept: dict[str, Any] = {}
        if "lab" in slim:
            kept["lab"] = slim["lab"]
        # Catalog (including locked starters) so Grok can pick a name to
        # rewrite after overflow. Tree/types can be huge; cards is the
        # pick-list and must survive the emergency clip.
        if "cards" in slim:
            kept["cards"] = slim["cards"]
        if slim.get("run") is not None:
            kept["run"] = slim["run"]
        if "desk_lessons" in slim:
            kept["desk_lessons"] = slim["desk_lessons"]
        if kept:
            kept["ok"] = slim.get("ok")
            kept["_clipped"] = "payload"
            return json.dumps(kept, default=str)[:max_chars]
    return text[:max_chars] + "... [truncated]"


_CADENCE_LOOP = re.compile(
    r"cycle\s+\d+\s+complete|ready for cycle\s+\d+",
    re.IGNORECASE,
)


def _fold_loop_text(text: str) -> str:
    t = (text or "").replace("?", "'")
    t = re.sub(r"\d+", "N", t)
    return re.sub(r"\s+", " ", t).strip()


def _same_phrase_loop(text: str, *, unit: int, copies: int) -> bool:
    if not text or copies < 2:
        return False
    words = text.split()
    if len(words) >= copies * 2:
        pair = (words[-2], words[-1])
        tail = words[-(copies * 2) :]
        hits = sum(
            1 for i in range(len(tail) - 1) if (tail[i], tail[i + 1]) == pair
        )
        if hits >= copies:
            return True
    if unit >= 4 and len(text) >= unit * copies:
        chunk = text[-unit:]
        if chunk.strip() and text[-unit * copies :].count(chunk) >= copies:
            return True
    return False


def _tail_chunk_loop(text: str, *, min_unit: int = 24, copies: int = 3) -> bool:
    if not text or copies < 2:
        return False
    n = len(text)
    max_unit = min(180, n // copies)
    for unit in range(max_unit, min_unit - 1, -1):
        chunk = text[-unit:]
        if chunk.strip() and text[-unit * copies :].count(chunk) >= copies:
            return True
    return False


def _repeated_sentence_loop(
    text: str, *, copies: int = STREAM_LOOP_SENTENCE_COPIES
) -> bool:
    if not text or copies < 2:
        return False
    tail = text[-2400:]
    parts = [p.strip() for p in re.split(r"[.!;]", tail) if len(p.strip()) >= 24]
    if len(parts) >= copies:
        last = parts[-1]
        if last and parts[-copies:].count(last) >= copies:
            return True
        if last and tail.count(last) >= copies:
            return True
    words = tail.split()
    if len(words) >= copies * 8:
        unit = " ".join(words[-8:])
        window = " ".join(words[-(copies * 8) :])
        if unit and window.count(unit) >= copies:
            return True
    return False


def stream_is_looping(
    text: str,
    *,
    unit: int = STREAM_LOOP_UNIT,
    copies: int = STREAM_LOOP_COPIES,
) -> bool:
    """True if the tail is the same short phrase pasted many times."""
    if not text or copies < 2:
        return False
    cadence = _CADENCE_LOOP.findall(text)
    if len(cadence) >= copies:
        return True
    raw = text.replace("?", "'")
    if _same_phrase_loop(raw, unit=unit, copies=copies):
        return True
    folded = _fold_loop_text(text)
    if _tail_chunk_loop(folded) or _repeated_sentence_loop(folded):
        return True
    return folded != raw.strip() and _same_phrase_loop(
        folded, unit=unit, copies=copies
    )


def _delta(prev: str, incoming: str) -> tuple[str, str]:
    if not incoming:
        return prev, ""
    if incoming.startswith(prev):
        return incoming, incoming[len(prev) :]
    return prev + incoming, incoming


def _piece(obj: Any, *names: str) -> str:
    for name in names:
        raw = getattr(obj, name, None)
        if raw:
            return str(raw)
    return ""


async def stream_round(chat: Any, *, stage: str = "grok") -> tuple[str, Any, str]:
    """Stream one model step. Returns (assistant text, response, stop_reason)."""
    think_emit("stage", stage)
    o = ""
    saw_think = False
    saw_say = False
    think_acc = ""
    say_acc = ""
    last_ch: Any = None
    last_resp: Any = None
    agen = chat.stream().__aiter__()
    idle = 0
    reason = "ok"
    while True:
        try:
            from abcxauto.park_clock import peek_interrupt

            if peek_interrupt() is not None:
                reason = "interrupt"
                break
        except Exception:
            pass
        try:
            resp, ch = await asyncio.wait_for(anext(agen), timeout=STREAM_CHUNK_S)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            idle += 1
            if idle >= STREAM_IDLE_LIMIT:
                think_emit("tool", "\n[stream stalled]\n")
                reason = "stalled"
                break
            continue
        idle = 0
        last_ch = ch
        last_resp = resp
        rc = _piece(ch, "reasoning_content", "reasoning")
        think_acc, think_piece = _delta(think_acc, rc)
        if think_piece:
            if not saw_think:
                think_emit("say", "\n[think]\n")
                saw_think = True
            think_emit("think", think_piece)
        content = _piece(ch, "content")
        if content:
            say_acc, say_piece = _delta(say_acc, content)
            if say_piece:
                if not saw_say:
                    think_emit("say", "\n[say]\n")
                    saw_say = True
                o += say_piece
                think_emit("say", say_piece)
        if stream_is_looping(think_acc) or stream_is_looping(o):
            think_emit("tool", "\n[stream loop]\n")
            reason = "loop"
            break
    try:
        fr = ""
        if last_ch is not None:
            choices = list(getattr(last_ch, "choices", None) or [])
            raw_fr = getattr(choices[0], "finish_reason", None) if choices else None
            fr = str(getattr(raw_fr, "name", None) or raw_fr or "")
        if "LENGTH" in fr.upper() or "MAX_TOKEN" in fr.upper():
            think_emit("tool", "\n[truncated: max_tokens]\n")
    except Exception:
        logger.debug("finish_reason probe failed", exc_info=True)
    think_emit("stage_end", stage)
    if not o:
        # Some SDK finishes put the spoken say on the completed message only.
        for obj in (last_ch, last_resp):
            extra = _piece(obj, "content")
            if extra:
                o = extra
                break
    try:
        from abcxauto.memory import get_journal
        from abcxauto.scorecard import estimate_cost_usd, usage_from_response

        used = usage_from_response(
            last_resp, last_ch, think_text=think_acc, say_text=o
        )
        from abcxauto.config import get_config

        get_journal().record_model_usage(
            stage=stage,
            model=str(getattr(get_config(), "model", "") or ""),
            input_tokens=int(used.get("input_tokens") or 0),
            output_tokens=int(used.get("output_tokens") or 0),
            cached_tokens=int(used.get("cached_tokens") or 0),
            cost_usd=estimate_cost_usd(
                int(used.get("input_tokens") or 0),
                int(used.get("output_tokens") or 0),
                cached_tokens=int(used.get("cached_tokens") or 0),
            ),
        )
    except Exception:
        logger.debug("model usage journal failed", exc_info=True)
    return o, last_resp, reason


async def grok(g: GrokClient, p: str, *, stage: str = "grok") -> str:
    """One-shot streamed reply (tests / no tools). Hot path is grok_turn."""
    create_kw: dict[str, Any] = {
        "model": g.model,
        "messages": [system(build_system_prompt()), user(p)],
        "temperature": g.temperature,
        "max_tokens": int(g.max_tokens or 8192),
        "include": ["verbose_streaming"],
    }
    try:
        chat = g.client.chat.create(**create_kw)
    except TypeError:
        create_kw.pop("include", None)
        chat = g.client.chat.create(**create_kw)
    text, _, _ = await stream_round(chat, stage=stage)
    return text


def _reset_chat(g: GrokClient) -> None:
    g.chat = None
    g._wake_n = 0


def drop_live_chat(g: Any | None) -> None:
    """Overnight / park / empty/? / dead stream: the next think is a new conversation."""
    if g is None:
        return
    _reset_chat(g)


def drop_refused_send_targets(turn: BrainTurn) -> None:
    """Rejected clerk tickets are not live send targets on the next look."""
    turn.last_act = {}
    turn.last_result = {}
    turn.last_strat = ""
    turn.sends = []


def _stay_up_session_label(session: str) -> str:
    """Paper stay-up needs regular/premarket. Blank/unknown fill from the clock."""
    from abcxauto.park_clock import resolve_stay_up_session

    sess = str(session or "").strip().lower()
    if sess in ("", "unknown"):
        return resolve_stay_up_session("")
    return resolve_stay_up_session(sess)


def _finish_look_chat(g: GrokClient, turn: BrainTurn, *, session: str) -> None:
    """Keep the live chat when the look actually said something or sent.

    Park, overnight, and a true empty / lone '?' drop it so the next
    think is a cold start. A ``failed`` / dead-stream stamp on a real
    say or send/fill is not a drop.
    """
    if turn.parked or _look_is_empty_or_question(turn):
        _reset_chat(g)
        return
    try:
        from abcxauto.park_clock import paper_stay_up

        if paper_stay_up(_stay_up_session_label(session)):
            return
    except Exception:
        logger.debug("stay-up chat keep check failed", exc_info=True)
    _reset_chat(g)


def _new_chat(g: GrokClient, *, session: str = "") -> Any:
    create_kw: dict[str, Any] = {
        "model": g.model,
        "messages": [system(brain_system_prompt())],
        "tools": list(agent_tools(session=session)),
        "temperature": g.temperature,
        "max_tokens": int(g.max_tokens or 8192),
        "include": ["verbose_streaming"],
    }
    try:
        chat = g.client.chat.create(**create_kw)
    except TypeError:
        create_kw.pop("include", None)
        chat = g.client.chat.create(**create_kw)
    g.chat = chat
    g._wake_n = 1
    return chat


def _ensure_chat(g: GrokClient, *, kind: str = "", session: str = "") -> Any:
    """Cold start a think. Stay-up resume uses ``_open_wake(..., resume=True)``."""
    _ = kind
    return _new_chat(g, session=session)


def _open_wake(
    g: GrokClient,
    wake: str,
    *,
    reset: bool = False,
    session: str = "",
    resume: bool = False,
) -> Any:
    """Start this look, or continue the live stay-up chat.

    A cold start is a new chat (system prompt + developer wake). Stay-up
    resume appends book facts to the existing chat so Grok does not reboot
    as a new agent. A pending live poke owns the next developer turn.
    """
    live = None if reset else getattr(g, "chat", None)
    if resume and live is not None:
        pending = False
        try:
            from abcxauto.park_clock import peek_interrupt

            pending = peek_interrupt() is not None
        except Exception:
            pending = False
        if not pending:
            live.append(developer(wake))
        g._wake_n = int(getattr(g, "_wake_n", 0) or 0) + 1
        return live
    chat = _new_chat(g, session=session)
    chat.append(developer(wake))
    return chat


async def _inject_live_poke(
    chat: Any,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    turn: BrainTurn,
) -> bool:
    """Apply fill/order_change/unprotected/stop_dist to the open think — same chat."""
    from abcxauto.park_clock import live_poke_clears_tool_cache, note_wake, take_interrupt
    from abcxauto.world_state import day_facts, format_wake

    ev = take_interrupt()
    if ev is None:
        return False
    note_wake(ev)
    turn.interrupted = True
    # This look's IBKR screens did not change. Quotes/book refetch only when
    # the poke actually moved the book (fill / order_change / unprotected).
    scan_snap = _scan_snap_bag(snap)
    if live_poke_clears_tool_cache(ev):
        # Fill / real order fill-cancel / unprotected: the book moved under us.
        turn.tool_cache.clear()
        try:
            from abcxauto.look_snapshot import begin_look

            begin_look(snap)
        except Exception:
            logger.debug("look snapshot reset on poke failed", exc_info=True)
    think_emit("tool", f"\n[{ev.kind}]\n")
    # Refresh book facts when we can — thin poke, not a second wake dump.
    day: dict[str, Any] | None = None
    try:
        if connector is not None:
            from abcxauto.agent_loop import snap as take_snap

            fresh = await take_snap(connector)
            if isinstance(fresh, dict):
                snap.clear()
                snap.update(fresh)
                _restore_scan_snap(snap, scan_snap)
                world.net_liquidation = (
                    fresh.get("net_liquidation")
                    or (fresh.get("account") or {}).get("netliquidation")
                    or world.net_liquidation
                )
                world.positions = list(fresh.get("positions") or world.positions or [])
                world.flat = not bool(world.positions)
                prot = fresh.get("protection") if isinstance(fresh.get("protection"), dict) else {}
                world.unprotected = list(
                    prot.get("unprotected_symbols") or world.unprotected or []
                )
                world.session_status = str(
                    ((fresh.get("market_hours") or {}).get("session") or {}).get("status")
                    or world.session_status
                    or ""
                )
    except Exception:
        logger.debug("live poke snap refresh failed", exc_info=True)
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
        day = day_facts(world, sc)
    except Exception:
        day = day_facts(world, None)
    poke = format_wake(
        cycle=0,
        session=str(getattr(world, "session_status", "") or ""),
        flat=bool(getattr(world, "flat", False)),
        unprotected=list(getattr(world, "unprotected", None) or []),
        ibkr_up=bool(getattr(connector, "connected", False)),
        day=day,
    )
    try:
        chat.append(developer(poke))
    except Exception:
        logger.debug("live poke append failed", exc_info=True)
        return False
    return True


def _book_facts(world: WorldState) -> dict[str, Any]:
    from abcxauto.world_state import (
        COMBO_FACT,
        compact_position,
        compact_working_orders,
        open_upnl_of,
    )

    return {
        "session": world.session_status,
        "flat": world.flat,
        "needs_protection": world.needs_protection,
        "unprotected": list(world.unprotected or []),
        "net_liquidation": world.net_liquidation,
        "daily_pnl": world.daily_pnl,
        "ibkr_daily_pnl": world.daily_pnl,
        "open_upnl": open_upnl_of(world.positions),
        "posture": world.effective_posture or world.risk_posture,
        "gates": world.gates,
        "envelope": world.envelope,
        "capacity": dict(world.capacity or {}),
        "quote_source": "IBKR live",
        "ibkr_live_quotes": dict(world.ibkr_live_quotes or {}),
        "combo": COMBO_FACT,
        "book_reconciled": bool(getattr(world, "book_reconciled", False)),
        "positions": [
            compact_position(p) for p in (world.positions or [])[:16]
        ],
        "working_orders": compact_working_orders(
            world.open_orders, positions=world.positions
        ),
        "fills": [
            {
                "symbol": f.get("symbol"),
                "sec": f.get("sec_type") or f.get("secType"),
                "side": f.get("side") or f.get("action"),
                "qty": f.get("quantity") or f.get("shares"),
                "px": f.get("price") or f.get("avg_price"),
            }
            for f in (getattr(world, "fills", None) or [])[:8]
            if isinstance(f, dict)
        ],
        "stop_qty_fact": world.stop_qty_fact,
        "scan_tape": [
            {
                "symbol": o.get("symbol"),
                "source": o.get("source") or "mda",
                "freshness": o.get("freshness") or "delayed",
                "mda_last": o.get("mda_last") or o.get("last"),
            }
            for o in (world.opportunities or [])[:12]
        ],
        "option_facts": list(world.option_facts or [])[:16],
        "vol": list(getattr(world, "vol_facts", None) or [])[:6],
        "news": [
            f"[{n.get('symbol')}] {n.get('headline')}"
            for n in (world.news_items or [])[:8]
            if n.get("headline")
        ],
        "trade_plan": world.trade_plan,
        "book_unreliable": bool((world.gates or {}).get("book_unreliable")),
        "structure_cooldown": dict(getattr(world, "structure_cooldown", None) or {}),
        # Why the last tickets were rejected — a cooldown without its reason
        # teaches nothing, so the same geometry gets rebuilt next session.
        "structure_lessons": [
            {
                "strategy": ev.get("strategy"),
                "symbol": ev.get("symbol"),
                "reason_code": ev.get("reason_code") or ev.get("outcome"),
                "message": str(ev.get("message") or "")[:200],
            }
            for ev in (getattr(world, "structure_lessons", None) or [])[:5]
            if isinstance(ev, dict)
        ],
    }


def _book_payload(
    world: WorldState,
    tool_trace: list[str] | None = None,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from abcxauto.config import get_config
    from abcxauto.lab_playbook import (
        _card_label,
        _flat_card_projection,
        _lab_view_without_types,
        card_facts,
        lab_facts,
        load_lab,
        notebook_text,
        playbook_glance,
        playbook_mode,
    )
    from abcxauto.self_tune import levers_snapshot
    from abcxauto.world_state import day_facts

    cfg = get_config()
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
    except Exception:
        sc = {}
    facts = _book_facts(world)
    glance = playbook_glance(sc)
    last_look: dict[str, Any] = {}
    try:
        from abcxauto.think_stream import last_look_facts

        last_look = last_look_facts()
    except Exception:
        last_look = {}
    _ = (tool_trace, snap)
    try:
        from abcxauto.trade_playbook import overlay_types_to_hide

        hidden = overlay_types_to_hide(
            getattr(world, "positions", None),
            getattr(world, "open_orders", None),
        )
    except Exception:
        hidden = frozenset()
    try:
        lab = _lab_view_without_types(load_lab(), hidden)
        scored = [
            {
                k: v
                for k, v in row.items()
                if k
                not in (
                    "looks_without_trigger",
                    "days_without_trigger",
                    "max_looks_without_trigger",
                    "looks",
                    "days",
                )
            }
            for row in card_facts(lab)
        ]
        glance = dict(glance)
        glance["mode"] = playbook_mode()
        glance["cards"] = _flat_card_projection(lab)
        glance["unfiled_cards"] = list(lab.get("unfiled_cards") or [])
        glance["card_scores"] = scored
        glance["graduated"] = [_card_label(r) for r in scored if r.get("graduated")]
        glance["tripped"] = [_card_label(r) for r in scored if r.get("tripped")]
        glance["needs_declaration"] = [
            _card_label(r)
            for r in scored
            if r.get("needs_retire_if")
            or r.get("needs_thesis")
            or r.get("needs_numeric_kill")
            or r.get("needs_conservative_fill")
        ]
        glance["notes"] = notebook_text(lab)[:4000]
        glance["lab"] = lab_facts(lab, rows=scored, hide_types=hidden)
    except Exception:
        logger.debug("playbook block for book payload failed", exc_info=True)
    lessons: list[dict[str, str]] = []
    try:
        from abcxauto.desk_lessons import desk_lessons_payload

        lessons = desk_lessons_payload()
    except Exception:
        logger.debug("desk lessons for book payload failed", exc_info=True)
    out: dict[str, Any] = {
        "day": day_facts(world, sc),
        "world": facts,
        "ibkr_live_quotes": dict(world.ibkr_live_quotes or {}),
        "score_windows": {
            "fastest_beating": (sc or {}).get("fastest_beating"),
            "best_pace": (sc or {}).get("best_pace"),
            "windows": (sc or {}).get("windows") or {},
        },
        "levers": levers_snapshot(cfg),
        "playbook": glance,
        "desk_lessons": lessons,
        "path": _path_block(world, cfg),
    }
    if last_look:
        out["last_look"] = last_look
    return out


def _path_block(world: WorldState, cfg: Any) -> dict[str, Any]:
    try:
        from abcxauto.memory import get_journal
        from abcxauto.path_math import path_from_journal

        risk = getattr(cfg, "max_risk_per_trade_pct", None)
        return path_from_journal(
            get_journal(),
            equity=getattr(world, "net_liquidation", None),
            risk_pct=risk,
        )
    except Exception:
        return {"n": 0, "note": "path unavailable"}


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
                _stash_live(world, snap, row, mark=mark)
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
        payload = _book_payload(world, tool_trace=turn.tool_trace, snap=snap)
        payload["sends_this_turn"] = len(turn.sends)
        world_facts = payload.get("world")
        if isinstance(world_facts, dict):
            world_facts["sends_this_turn"] = len(turn.sends)
        try:
            from abcxauto.look_snapshot import record_look_tool

            record_look_tool(snap, "book", payload)
        except Exception:
            logger.debug("look snapshot book record failed", exc_info=True)
        return _clip(payload)
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
        return _clip(st)
    if name == "quote":
        raw = await run_readonly_tool("quote", args, connector)
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
            _stash_live(world, snap, data)
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
                raw = _clip(data)
        try:
            from abcxauto.look_snapshot import record_look_tool

            record_look_tool(snap, "quote", data)
        except Exception:
            logger.debug("look snapshot quote record failed", exc_info=True)
        return raw if isinstance(raw, str) else _clip(raw)
    if name == "fills":
        fn = getattr(connector, "get_fills", None) or getattr(connector, "get_recent_executions", None)
        if not callable(fn):
            return json.dumps({"error": "IBKR fills unavailable", "source": "ibkr"})
        rows = await fn()
        return _clip({"source": "ibkr", "freshness": "live", "fills": list(rows or [])[:40]})
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
            items = await _mda_news(tape)
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
        return _clip(payload)
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
        payload["path"] = _path_block(world, get_config())
        return _clip(payload)
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
        from abcxauto.universe import parse_scan_filters, verified_pe_tags

        pe_tags = await verified_pe_tags(connector)
        parsed = parse_scan_filters(args, pe_tags=pe_tags)
        if not parsed.get("ok"):
            return _clip({"ok": False, "error": parsed.get("error") or "bad scan filters"})
        bare = (
            not str(args.get("arena") or "").strip()
            and not str(args.get("scan_code") or "").strip()
            and not normalize_tickers(args.get("symbols") or [])
        )
        if bare:
            err = {
                "ok": False,
                "error": "scan requires arena | scan_code | symbols[]",
            }
            _attach_scan_run(err, turn=turn, world=world)
            return _clip(err)
        from abcxauto.lab_playbook import scan_screen_key

        look_key = _scan_look_key(args)
        c_arena, c_code = _canonical_scan_screen(
            str(args.get("arena") or "").strip(),
            str(args.get("scan_code") or "").strip(),
        )
        key = scan_screen_key(c_arena, c_code)
        lock = getattr(turn, "scan_lock", None)
        if not isinstance(lock, asyncio.Lock):
            lock = asyncio.Lock()
            try:
                turn.scan_lock = lock
            except Exception:
                pass
        async with lock:
            used = [str(x) for x in (snap.get("scan_screens") or [])]
            prior_hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
            cached_look = turn.scan_cache.get(look_key) if look_key else None
            if cached_look or (key and key in used):
                if cached_look:
                    reused = deepcopy(cached_look)
                else:
                    rows = _scan_paint_rows(prior_hits, quotes=qmap)
                    screens = list(snap.get("scan_screens") or [])
                    reused = {
                        "ok": True,
                        "screens_this_look": int(snap.get("scan_calls") or 0),
                        "screens": screens,
                        "source": prior_hits.get("source") or "ibkr",
                        "symbols": [r.get("symbol") for r in rows if r.get("symbol")],
                        "hits": rows,
                        "rows": rows,
                        "applied": {},
                        "persisted": False,
                        "ranked": bool(prior_hits.get("ranked")),
                        "rank_meaning": prior_hits.get("rank_meaning"),
                        "quoted": prior_hits.get("quoted") or 0,
                    }
                    reused.update(_scan_gate_facts(rows))
                    if rows:
                        painted = dict(prior_hits)
                        painted["rows"] = rows
                        snap["scan_hits"] = painted
                reused["reused"] = True
                reused["note"] = "this screen already fetched this look"
                if cached_look:
                    if reused.get("screens"):
                        snap["scan_screens"] = list(reused["screens"])
                    if reused.get("screens_this_look") is not None:
                        snap["scan_calls"] = int(reused["screens_this_look"] or 0)
                    if reused.get("rows"):
                        snap["scan_hits"] = {
                            "source": reused.get("source") or "ibkr",
                            "ranked": bool(reused.get("ranked")),
                            "rank_meaning": reused.get("rank_meaning") or "",
                            "quoted": reused.get("quoted") or 0,
                            "rows": list(reused["rows"]),
                        }
                if want_news and not reused.get("news"):
                    from abcxauto.prints import attach_mda_news

                    news_syms = _news_symbols_for_scan(
                        snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {},
                        [str(s) for s in (reused.get("symbols") or []) if s],
                    )
                    reused["news"] = await _mda_news(news_syms)
                    reused["news_freshness"] = "delayed_15m"
                    reused["news_use"] = "color_not_trigger"
                    attach_mda_news(reused.get("hits") or reused.get("rows") or [], reused["news"])
                _attach_scan_run(reused, turn=turn, world=world)
                think_emit("tool", "\n[scan = already have it]\n")
                return _clip(reused)
            jobs = [
                {
                    "arena": args.get("arena"),
                    "scan_code": args.get("scan_code"),
                    "symbols": args.get("symbols"),
                }
            ]
            last_ok: dict[str, Any] | None = None
            last_err: dict[str, Any] | None = None
            pulled_hits: list[dict[str, Any]] = []
            pulled_syms: list[str] = []
            from abcxauto.think_stream import merge_scan_hits

            for job in jobs:
                snap["scan_calls"] = int(snap.get("scan_calls") or 0) + 1
                payload = await criteria_scan(
                    arena=job.get("arena"),
                    scan_code=job.get("scan_code"),
                    symbols=job.get("symbols") or args.get("symbols"),
                    positions=list(world.positions or snap.get("positions") or []),
                    connector=connector,
                    turn_symbols=turn_syms,
                    filters=parsed,
                )
                if not payload.get("ok"):
                    last_err = payload
                    continue
                last_ok = payload
                syms = list(payload.get("symbols") or [])
                for s in syms:
                    su = str(s or "").upper().strip()
                    if su and su not in pulled_syms:
                        pulled_syms.append(su)
                # Union this look's screens. Last-scan-wins dropped names when a later
                # empty mega/large sort followed a tape that actually had hits.
                fetched = list(getattr(world, "scan_fetched", None) or [])
                seen = {str(x).upper() for x in fetched if x}
                for s in syms:
                    su = str(s or "").upper().strip()
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
                hits = payload.get("hits") or []
                for row in hits:
                    if not isinstance(row, dict):
                        continue
                    pulled_hits.append(row)
                    if row.get("last") is not None:
                        _stash_live(
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
                    "rows": [r for r in hits if isinstance(r, dict)][:24],
                }
                prior = (
                    snap.get("scan_hits")
                    if int(snap.get("scan_calls") or 0) > 1 or "scan" in turn.tool_trace
                    else None
                )
                snap["scan_hits"] = merge_scan_hits(prior, incoming)
                rec_arena, rec_code = _canonical_scan_screen(
                    str(job.get("arena") or payload.get("arena") or ""),
                    str(job.get("scan_code") or payload.get("scan_code") or ""),
                )
                _record_scan_screen(snap, rec_arena, rec_code)
            if last_ok is None:
                err = last_err or {
                    "ok": False,
                    "error": "scan requires arena | scan_code | symbols[]",
                }
                if isinstance(err, dict):
                    err = dict(err)
                    err.setdefault("ok", False)
                    _attach_scan_run(err, turn=turn, world=world)
                return _clip(err)
            snap["scan_at"] = datetime.now(timezone.utc).isoformat()
            merged = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
            rows = _scan_paint_rows(merged, pulled_hits, quotes=qmap)
            hits = rows
            syms = pulled_syms or [str(r.get("symbol")) for r in hits if r.get("symbol")]
            screens = list(snap.get("scan_screens") or [])
            out: dict[str, Any] = {
                "ok": True,
                "source": last_ok.get("source"),
                "symbols": syms,
                "hits": hits,
                "applied": last_ok.get("applied") or {},
                "persisted": False,
                "ranked": bool(merged.get("ranked") if merged else last_ok.get("ranked")),
                "rank_meaning": (merged.get("rank_meaning") if merged else None)
                or last_ok.get("rank_meaning"),
                "quoted": merged.get("quoted") or last_ok.get("quoted") or 0,
                "screens": screens,
                "screens_this_look": int(snap.get("scan_calls") or len(screens) or 0),
            }
            if len(screens) <= 1:
                out["arena"] = last_ok.get("arena")
                out["scan_code"] = last_ok.get("scan_code")
            out.update(_scan_gate_facts(rows))
            pulled_by = {
                str(r.get("symbol") or "").upper(): r
                for r in pulled_hits
                if isinstance(r, dict) and r.get("symbol")
            }
            hits = [pulled_by.get(str(r.get("symbol") or "").upper(), r) for r in rows]
            out["hits"] = hits
            out["rows"] = rows
            if want_metrics and hits:
                from abcxauto.opportunity_scan import attach_mda_metrics

                await attach_mda_metrics(hits)
            if want_news and (syms or hits):
                from abcxauto.prints import attach_mda_news

                news_syms = _news_symbols_for_scan(merged, pulled_syms)
                out["news"] = await _mda_news(news_syms)
                out["news_freshness"] = "delayed_15m"
                out["news_use"] = "color_not_trigger"
                attach_mda_news(hits, out["news"])
                if out["news"]:
                    snap["scan_news_attached"] = True
                    if not world.news_items:
                        world.news_items = list(out["news"])
                        snap["news_items"] = list(out["news"])
            snap["scan_hits"] = merge_scan_hits(merged, {**merged, "rows": out["rows"]})
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
            gate = _scan_gate_facts(out.get("rows"))
            deepest = gate.get("deepest_open_gap_pct")
            deep_s = f"{deepest:+.1f}%" if isinstance(deepest, (int, float)) else "n/a"
            think_emit(
                "tool",
                f"hits={len(syms)} screens={len(out.get('screens') or [])} "
                f"deepest={deep_s} {gate.get('deepest_symbol') or ''} "
                f"src={out.get('source') or 'empty'}\n",
            )
            if look_key:
                turn.scan_cache[look_key] = deepcopy(out)
            return _clip(out)
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
            _publish_vol(world, snap)
        return _clip(payload, max_chars=CANDLES_CLIP_CHARS)
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
            return _compact_chain(raw if isinstance(raw, dict) else {}, last=last)

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
        _publish_vol(world, snap)
        if len(chains) == 1:
            return _clip(chains[0])
        return _clip({"source": "ibkr", "chains": chains})
    if name == "option_quote":
        specs = option_quote_specs(args)
        if not specs:
            return json.dumps({"error": "symbol, expiration, strike, right required", "source": "ibkr"})
        rows = await asyncio.gather(
            *[_one_option_quote(connector, spec) for spec in specs[:OPTION_QUOTE_CAP]]
        )
        for row in rows:
            _stash_vol_option_quote(snap, row)
        _publish_vol(world, snap)
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
            return _clip(rows[0])
        return _clip({
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
        _publish_vol(world, snap)
        return _clip({
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
        if _send_succeeded(result):
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
                await _write_last_turn_after_send(
                    connector=connector,
                    world=world,
                    snap=snap,
                    turn=turn,
                    act=act,
                    strat=strat,
                )
            except Exception:
                logger.debug("post-send last_turn write failed", exc_info=True)
        return _clip(result)
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
        return _clip(result)
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
        return _clip(
            playbook_payload(
                args.get("revision"),
                full=bool(full),
                positions=list(pos),
                orders=list(orders),
            ),
            max_chars=PLAYBOOK_CLIP_CHARS,
        )
    if name == "write_lab_playbook":
        from abcxauto.lab_playbook import apply_from_judgment, grounding_error

        note = grounding_error(args, tool_trace=turn.tool_trace)
        if note:
            return _clip({"status": "rejected", "note": note})
        args = dict(args)
        judgment = {"lab_playbook": args}
        state = apply_from_judgment(judgment)
        turn.lab_playbook = state
        return _clip(state or {"status": "ignored", "note": "live cannot rewrite lab"})
    if name == "write_desk_lessons":
        from abcxauto.desk_lessons import apply_desk_lessons

        return _clip(apply_desk_lessons(args if isinstance(args, dict) else {}))
    return json.dumps({"error": f"unknown tool {name}"})


async def grok_turn(
    g: GrokClient,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    wake: str,
    resume: bool = False,
) -> BrainTurn:
    """One Grok tool loop. send() is the only broker path.

    ``resume`` is optional so older grok_turn mocks keep working. Stay-up
    continues the live chat after a spoken say or send/fill. True empty /
    lone '?' drop it so the next think is cold. A fresh BrainTurn still
    drops refused send tickets so they cannot be the next look's send target.
    """
    return await _grok_turn_impl(
        g,
        connector=connector,
        world=world,
        snap=snap,
        wake=wake,
        turn=BrainTurn(),
        resume=resume,
    )


def grok_turn_kwargs(
    fn: Any,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    wake: str,
    resume: bool = False,
) -> dict[str, Any]:
    """Keyword args for grok_turn. Omit resume when the callee does not accept it."""
    kwargs: dict[str, Any] = {
        "connector": connector,
        "world": world,
        "snap": snap,
        "wake": wake,
    }
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kwargs
    if "resume" in params:
        kwargs["resume"] = resume
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs["resume"] = resume
    return kwargs


def _parse_tool_call(
    tc: Any,
    *,
    world: WorldState,
    snap: dict[str, Any],
) -> tuple[str, dict[str, Any], Any, float]:
    fn = getattr(tc, "function", None)
    name = str(getattr(fn, "name", None) or "")
    raw_args = getattr(fn, "arguments", None) or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    except (TypeError, json.JSONDecodeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    name, args = normalize_tool_call(
        name,
        args,
        fallback_symbols=fallback_quote_symbols(world, snap),
    )
    timeout = SEND_S if name in _MUTATING_TOOLS else TOOL_S
    if name == "option_chain":
        n = max(1, len(normalize_tickers(args.get("symbols") or args.get("symbol"), cap=CHAIN_CAP)))
        timeout = min(90.0, max(CHAIN_S, 22.0 * n))
    if name == "candles":
        n = max(1, len(normalize_tickers(args.get("symbols") or args.get("symbol"), cap=CANDLE_CAP)))
        timeout = min(CANDLE_S, max(28.0, 12.0 + 8.0 * n))
    if name == "scan":
        # Criteria IBKR screen — same class as candles (~35s), not TOOL_S=20.
        timeout = SCAN_S
    return name, args, tc, timeout


async def _invoke_named_tool(
    name: str,
    args: dict[str, Any],
    timeout: float,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    turn: BrainTurn,
) -> str:
    think_emit("tool", f"\n[{name}]\n")
    turn.tool_trace.append(name)
    try:
        from abcxauto.park_clock import peek_interrupt

        tool_task = asyncio.create_task(
            _run_tool(
                name, args, connector=connector, world=world, snap=snap, turn=turn
            )
        )
        deadline = time.monotonic() + float(timeout)
        # A read is worth cancelling — the book moved, so the answer is stale
        # before it lands. A send is not: cancelling it mid-flight can leave an
        # entry on the book with no protection attached. The poke waits.
        droppable = name not in _MUTATING_TOOLS
        while True:
            if droppable and peek_interrupt() is not None:
                tool_task.cancel()
                try:
                    await tool_task
                except (asyncio.CancelledError, Exception):
                    pass
                _record_tool_deferred(
                    name, "book event cancelled the read in flight", args=args
                )
                return json.dumps({
                    "status": "interrupted",
                    "tool": name,
                    "note": _DEFERRED_READ_NOTE,
                })
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                tool_task.cancel()
                try:
                    await tool_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise asyncio.TimeoutError()
            done, _ = await asyncio.wait({tool_task}, timeout=min(0.25, remaining))
            if tool_task in done:
                exc = tool_task.exception()
                if exc is not None:
                    raise exc
                return str(tool_task.result())
    except asyncio.TimeoutError:
        logger.warning("tool %s timed out after %.0fs", name, timeout)
        return json.dumps({"error": f"{name} timed out", "timeout_s": timeout})
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return json.dumps({"error": f"{name} failed: {exc}"})


def _append_tool_result(chat: Any, tc: Any, result: str) -> None:
    try:
        chat.append(tool_result(result, tool_call_id=getattr(tc, "id", None)))
    except TypeError:
        chat.append(tool_result(result))


def _tool_key(name: str, args: dict[str, Any]) -> str:
    try:
        return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
    except (TypeError, ValueError):
        return f"{name}:?"


def _cached_read(turn: BrainTurn, name: str, args: dict[str, Any]) -> str | None:
    """Same read, same args, same think — hand back what we already fetched."""
    if name in _MUTATING_TOOLS:
        return None
    hit = turn.tool_cache.get(_tool_key(name, args))
    if hit is None:
        return None
    try:
        data = json.loads(hit)
    except (TypeError, json.JSONDecodeError, ValueError):
        return hit
    if isinstance(data, dict):
        data["repeat_of_this_think"] = True
        if _tape_payload(data):
            return _clip(data, max_chars=CANDLES_CLIP_CHARS)
        return _clip(data)
    return hit


# Handed back for a read the clerk dropped. A read that returns nothing must
# never look like a flat book or an empty tape — Grok would trade on it.
_DEFERRED_READ_NOTE = (
    "deferred: the book moved mid-message (fill / order change / unprotected "
    "lot). Nothing was fetched and nothing is implied - this is not an empty "
    "book, a flat quote, or a clean scan. Ask for this read again."
)


def _deferred_read_result(name: str) -> str:
    return json.dumps({
        "status": "deferred",
        "tool": name,
        "note": _DEFERRED_READ_NOTE,
    })


def _record_tool_deferred(
    name: str, why: str, *, args: dict[str, Any] | None = None
) -> None:
    """Durable record for a tool call the clerk dropped.

    A dropped call used to leave nothing at all — no marker, no trace, no log,
    no journal row. The operator reads logs/app.log and the journal, so it has
    to land in both or the drop is invisible again.
    """
    logger.warning("tool %s deferred - %s", name, why)
    try:
        from abcxauto.memory import get_journal

        get_journal().record_decision(
            action="tool_deferred",
            strategy=str(name),
            rationale=str(why)[:400],
            outcome={
                "status": "deferred",
                "tool": str(name),
                "reason": str(why),
                "args": args or {},
            },
        )
    except Exception:
        logger.debug("tool deferral journal failed", exc_info=True)


def _is_fact_result(result: str) -> bool:
    """False for a deferred / interrupted / errored read.

    Caching one of these would hand it back on the next ask stamped
    ``repeat_of_this_think``, which reads as a settled fact.
    """
    try:
        data = json.loads(result)
    except (TypeError, json.JSONDecodeError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    if data.get("error"):
        return False
    return str(data.get("status") or "") not in ("deferred", "interrupted")


async def _dispatch_tool_calls(
    calls: list[Any],
    *,
    chat: Any,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    turn: BrainTurn,
) -> bool:
    """Read tools in parallel; send / playbook stay serial and after facts.

    A book event mid-message defers the reads, never the writes. A read is
    stale the moment the book moves and Grok has to ask again anyway; the send
    carries geometry Grok already decided and cannot be reconstructed. Every
    tool_call_id still gets a result — a missing one makes the next round
    malformed.

    Returns True when a live poke is waiting for the think.
    """
    from abcxauto.park_clock import peek_interrupt

    parsed = [_parse_tool_call(tc, world=world, snap=snap) for tc in calls]
    reads = [p for p in parsed if p[0] not in _MUTATING_TOOLS]
    writes = [p for p in parsed if p[0] in _MUTATING_TOOLS]

    async def _one(item: tuple[str, dict[str, Any], Any, float]) -> tuple[Any, str]:
        name, args, tc, timeout = item
        cached = _cached_read(turn, name, args)
        if cached is not None:
            think_emit("tool", f"\n[{name} = already have it]\n")
            turn.tool_trace.append(name)
            return tc, cached
        result = await _invoke_named_tool(
            name,
            args,
            timeout,
            connector=connector,
            world=world,
            snap=snap,
            turn=turn,
        )
        if name not in _MUTATING_TOOLS and _is_fact_result(result):
            turn.tool_cache[_tool_key(name, args)] = result
        return tc, result

    def _defer_reads(why: str) -> None:
        for name, args, tc, _timeout in reads:
            _record_tool_deferred(name, why, args=args)
            think_emit("tool", f"\n[{name} deferred: book moved]\n")
            _append_tool_result(chat, tc, _deferred_read_result(name))

    if reads:
        if writes and peek_interrupt() is not None:
            _defer_reads("book event before the reads; the ticket takes the turn")
        else:
            # IBKR one scanner sub at a time. Same-args scans in one round
            # used to gather, all miss scan_cache, and paint four hits= lines.
            scan_idx = [i for i, p in enumerate(reads) if p[0] == "scan"]
            other_idx = [i for i, p in enumerate(reads) if p[0] != "scan"]
            rows: list[Any] = [None] * len(reads)

            async def _serial_scans() -> None:
                for i in scan_idx:
                    try:
                        rows[i] = await _one(reads[i])
                    except Exception as exc:
                        rows[i] = exc

            tasks: list[Any] = []
            if other_idx:
                tasks.append(
                    asyncio.gather(
                        *[_one(reads[i]) for i in other_idx],
                        return_exceptions=True,
                    )
                )
            if scan_idx:
                tasks.append(_serial_scans())
            if tasks:
                gathered = await asyncio.gather(*tasks, return_exceptions=True)
                if other_idx:
                    other_rows = gathered[0]
                    if isinstance(other_rows, Exception):
                        for i in other_idx:
                            rows[i] = other_rows
                    else:
                        for i, row in zip(other_idx, other_rows):
                            rows[i] = row
            for item, row in zip(reads, rows):
                if isinstance(row, Exception):
                    logger.exception("parallel tool failed")
                    _append_tool_result(
                        chat, item[2], json.dumps({"error": f"{item[0]} failed: {row}"})
                    )
                elif row is None:
                    _append_tool_result(
                        chat, item[2], json.dumps({"error": f"{item[0]} failed"})
                    )
                else:
                    _append_tool_result(chat, row[0], row[1])

    for item in writes:
        try:
            tc, result = await _one(item)
        except Exception as exc:
            # Never leave a write's tool_call_id unanswered, and never let the
            # failure be the only thing that is silent about it.
            logger.exception("write tool %s failed", item[0])
            _record_tool_deferred(item[0], f"write raised: {exc}", args=item[1])
            _append_tool_result(
                chat, item[2], json.dumps({"error": f"{item[0]} failed: {exc}"})
            )
            continue
        _append_tool_result(chat, tc, result)
        # The book just moved. Every cached read is now a pre-trade fact.
        turn.tool_cache.clear()
    return peek_interrupt() is not None


async def _grok_turn_impl(
    g: GrokClient,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    wake: str,
    turn: BrainTurn | None = None,
    resume: bool = False,
) -> BrainTurn:
    turn = turn or BrainTurn()
    # Rejected clerk tickets must not ride to the next look.
    drop_refused_send_targets(turn)
    try:
        from abcxauto.look_snapshot import begin_look

        begin_look(snap)
    except Exception:
        logger.debug("look snapshot begin failed", exc_info=True)
    if g is None:
        turn.last_act = {}
        turn.last_result = {"status": "error", "note": "no_grok_client"}
        turn.failed = True
        return turn
    session = str(getattr(world, "session_status", "") or "")
    try:
        chat = _open_wake(g, wake, session=session, resume=resume)
    except Exception as exc:
        logger.exception("chat start failed")
        turn.last_act = {}
        turn.last_result = {"status": "error", "note": f"chat_error: {exc}"}
        turn.failed = True
        turn.stream_error = str(exc)
        _finish_look_chat(g, turn, session=session)
        return turn
    lead = str(wake or "").splitlines()[0].strip() if wake else ""
    if lead:
        think_emit("tool", f"{lead}\n")
    ran_out = True
    while turn.steps < MAX_TOOL_STEPS:
        turn.steps += 1
        try:
            from abcxauto.park_clock import peek_interrupt

            if peek_interrupt() is not None:
                await _inject_live_poke(
                    chat, connector=connector, world=world, snap=snap, turn=turn
                )
                continue
            text, response, stop = await stream_round(chat)
        except Exception as exc:
            # A dead empty stream ends the look. A look that already spoke
            # or sent still keeps the stay-up chat.
            logger.exception("stream_round failed")
            think_emit("tool", f"\n[stream failed: {exc}]\n")
            turn.failed = True
            turn.stream_error = str(exc)
            ran_out = False
            break
        # Keep every spoken chunk, including a later empty/interrupt/loop
        # stop. Junk is the whole look, not the last assistant turn.
        if text:
            turn.text = (turn.text + "\n" + text).strip()
        if stop == "interrupt":
            await _inject_live_poke(
                chat, connector=connector, world=world, snap=snap, turn=turn
            )
            continue
        if stop == "loop":
            ran_out = False
            break
        if response is not None:
            try:
                chat.append(response)
            except Exception:
                logger.debug("chat.append(response) failed", exc_info=True)
        calls = list(getattr(response, "tool_calls", None) or []) if response is not None else []
        if not calls:
            # Grok stopped asking for facts — the think is done.
            ran_out = False
            break
        interrupted = await _dispatch_tool_calls(
            calls,
            chat=chat,
            connector=connector,
            world=world,
            snap=snap,
            turn=turn,
        )
        if interrupted:
            await _inject_live_poke(
                chat, connector=connector, world=world, snap=snap, turn=turn
            )
    if ran_out:
        turn.tool_budget_hit = True
        think_emit("tool", "\n[think stopped: step ceiling]\n")
    if not turn.parked and not turn.failed and _look_is_empty_or_question(turn):
        turn.failed = True
        logger.warning("look failed: empty or junk assistant text")
    _finish_look_chat(g, turn, session=session)
    if not turn.sends:
        if str(turn.last_strat or "").lower() == "hold":
            turn.last_strat = ""
        if str((turn.last_act or {}).get("strategy") or "").lower() == "hold":
            turn.last_act = {}
            turn.last_result = {}
    return turn
