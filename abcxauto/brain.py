"""Grok owns the book via tools. The shell is facts + send gates.

One look: create a chat, call the model. If tool_calls: run tools,
append results, call the model again on that same chat. Repeat until
there are no tool_calls. Words only: stop. After a successful stream,
append the response and persist ``response.id`` (xAI ``store_messages``).
The next look — same process or a later start — continues with
``previous_response_id`` and only the new wake/developer line. The chat
is never dropped: park, research↔RTH, empty looks, and ``drop_live_chat``
keep the same server conversation. Durable notes live in journal.db;
wake carries a pointer; recall fetches. Tickets go through
``execute_ticket`` → ``send_action``. IBKR tools are live. scan() is one
tape this look (merged hits + on_book); candles are IBKR hist or the
live 5s stream (error if both miss); news is ~15 min delayed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from xai_sdk.chat import developer, system, tool, tool_result, user

from abcxauto.llm import GrokClient, build_system_prompt, chat_create_kwargs, create_chat
from abcxauto.opportunity_scan import criteria_scan, normalize_tickers
from abcxauto.order_examples import format_order_examples, ticket_strategy_names
from abcxauto.think_stream import emit as think_emit
from abcxauto.tools import run_readonly_tool
from abcxauto.tool_args import (
    OPTION_QUOTE_CAP,
    bind_send_card,
    fallback_quote_symbols,
    normalize_tool_call,
    option_quote_specs,
)
from abcxauto.world_state import WorldState
from abcxauto.brain_tools import *  # noqa: F401,F403

logger = logging.getLogger(__name__)

_MUTATING_TOOLS = frozenset({"send", "self_tune"})
STREAM_CHUNK_S = 8.0
STREAM_IDLE_LIMIT = 6
STREAM_LOOP_UNIT = 12
STREAM_LOOP_COPIES = 6
STREAM_LOOP_SENTENCE_COPIES = 3


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
    tool_budget_hit: bool = False
    parked: bool = False
    interrupted: bool = False
    failed: bool = False
    stream_error: str = ""
    ended: bool = False
    steps: int = 0
    # Read results already fetched this think, keyed by tool + args. A repeat
    # ask is answered from here so the think moves forward instead of spinning.
    tool_cache: dict[str, str] = field(default_factory=dict)
    # One merged scan tape this look. Survives a stay-up poke so a later
    # scan() folds into the same bag instead of paging IBKR again.
    scan_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # IBKR allows one scanner sub at a time. Parallel scan() calls in one
    # think collapse through this lock into one bag.
    scan_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Last stream_round had no [say] after tools/send. Not a sit.
    trailing_empty_grok: bool = False
    # Paid reasoning, no [say]. Completed look, not a void.
    trailing_think_only: bool = False
    # Empty/silent ended with the same prompt still on the wire.
    skip_identical_retry: bool = False
    # Fill / order_change / unprotected / desk-fact inject landed on this chat.
    poked: bool = False
    kill_mode: str = ""
    f10_tripped: bool = False
    loop_halted: bool = False
    brief_loop_halted: bool = False
    # One AH-card debit per completed research look, not per stream_round.
    brief_billed: bool = False

    def look_failed(self) -> bool:
        """True empty / lone '?' only. A real say or send/fill is not junk.

        A later empty assistant chunk, a leftover ``failed`` stamp, or a
        dead stream after a spoken/send look must not wipe the stay-up chat.
        """
        if self.parked or self.ended or self.loop_halted:
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

# Transient stream death mid-look. Not capacity (llm.py already bursts that).
# Same chat, same look — do not sit dead with an empty tip.
STREAM_ABORT_TRIES = 3
STREAM_ABORT_BACKOFF_S = 1.0
_STREAM_ABORT_MARKERS = (
    "unavailable",
    "connection aborted",
    "iocp",
    "forcibly closed",
    "network name is no longer available",
    "semaphore timeout period has expired",
    "rst_stream",
    "connection reset by peer",
)

# Empty --- GROK --- after any completed tool (option_quote, quote, book,
# send, …). Short dead wait, then stream_round again on this chat. A
# send does not complete the look. [think] without [say] is hung.
# Not a sit clock, not _cold_next, not a new messages list.
# A poke / desk-fact inject on a kept chat is the same class — #153
# keyed recover on this-round tool_trace, so a post-poke empty sat.
EMPTY_GROK_TRIES = 2
EMPTY_GROK_DEAD_S = 2.0
EMPTY_GROK_RECOVER_TRIES = 2
# Idle wall-clock with no think and no say. Think chunks reset this.
# Matches STREAM_CHUNK_S * STREAM_IDLE_LIMIT. A live reasoning stream
# is not silent; THINK_ONLY_CEILING_S bounds a think-forever round.
SILENT_GROK_TIP_S = float(STREAM_CHUNK_S * STREAM_IDLE_LIMIT)
# 240s = 4 min. Observed ~2500 reasoning tok in 48s (~52 tok/s);
# 8192 max_tokens at half that rate is ~315s. 240s covers a full dump
# at today's rate (~157s) with margin and still ends a stuck think-only
# stream. Dead streams still die at SILENT_GROK_TIP_S.
THINK_ONLY_CEILING_S = 240.0


def provider_overloaded(err: Any) -> bool:
    """True when xAI refused for capacity — back off long, do not re-ask."""
    blob = str(err or "").lower()
    if not blob:
        return False
    return any(m in blob for m in _OVERLOAD_MARKERS)


def is_stream_abort_error(err: Any) -> bool:
    """True for UNAVAILABLE / connection-aborted / IOCP-style mid-look death."""
    blob = str(err or "").lower()
    if not blob:
        return False
    if is_capacity_error_blob(blob):
        return False
    return any(m in blob for m in _STREAM_ABORT_MARKERS)


def is_capacity_error_blob(blob: str) -> bool:
    """RESOURCE_EXHAUSTED / at-capacity — llm.py already retries those."""
    return "resource_exhausted" in blob or "at capacity" in blob


def empty_grok_dead_s() -> float:
    """Short hung-empty wait. Env override for tests. Never a sit clock."""
    raw = (os.environ.get("ABCXAUTO_EMPTY_GROK_DEAD_S") or "").strip()
    if raw:
        try:
            return max(0.0, min(30.0, float(raw)))
        except ValueError:
            pass
    return float(EMPTY_GROK_DEAD_S)


def silent_grok_tip_s() -> float:
    """Wall-clock to abort a GROK tip with no [say]. Env override for tests."""
    raw = (os.environ.get("ABCXAUTO_SILENT_GROK_TIP_S") or "").strip()
    if raw:
        try:
            return max(0.0, min(120.0, float(raw)))
        except ValueError:
            pass
    return float(SILENT_GROK_TIP_S)


def think_only_ceiling_s() -> float:
    """Overall bound for a think-only stream. Env override for tests."""
    raw = (os.environ.get("ABCXAUTO_THINK_ONLY_CEILING_S") or "").strip()
    if raw:
        try:
            return max(0.0, min(600.0, float(raw)))
        except ValueError:
            pass
    return float(THINK_ONLY_CEILING_S)


def _empty_grok_round_after_work(
    turn: "BrainTurn",
    text: str,
    stop: str,
    *,
    chat_had_work: bool = False,
    live_chat: bool = False,
    poked: bool = False,
) -> bool:
    """True when this stream_round had no [say] after work on this chat.

    Work is this-round tools/send, work already on the kept messages,
    a live poke / desk-fact inject, or a kept chat that already had GROK
    (``live_chat``). Recover re-enters grok_turn with a fresh BrainTurn,
    so tool_trace would otherwise be empty after option_quote or a poke
    already landed on the messages.

    A send/fill does not complete the look. A [say] this round does.
    [think] without [say] is hung — not a checkpoint. ``stop==empty`` is
    GROK-banner-then-silence. #148 required stop==empty, so think-only
    after option_quote sat. #153 required this-round tools, so a poke
    on a spoken look sat idle.
    """
    if not (
        turn.tool_trace
        or turn.sends
        or chat_had_work
        or live_chat
        or poked
        or bool(getattr(turn, "poked", False))
    ):
        return False
    if stop in ("interrupt", "loop"):
        return False
    if stop == "think_only":
        return False
    if stop in ("empty", "silent"):
        return True
    # This round produced no spoken say. Think-only / whitespace / stall
    # after a completed tool, poke, or prior GROK is hung — same class
    # as a bare banner. stop==ok / stalled still counts; do not require
    # stop==empty.
    return _look_text_is_junk(text)


def _look_text_is_junk(text: str) -> bool:
    """True only for a true empty say or a lone '?'."""
    raw = (text or "").strip()
    return (not raw) or raw == "?"


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
    if str(result.get("reason_code") or "").lower() == "preview_refuse":
        return False
    return (
        result.get("success") is True
        or result.get("filled") is True
        or status in ("executed", "submitted", "ok", "filled", "success")
    )


def _successful_send_count(sends: list | None) -> int:
    """Count finished broker sends. Preview refuse / blocked do not count."""
    n = 0
    for item in sends or []:
        if not isinstance(item, dict):
            continue
        res = item.get("result")
        if _send_succeeded(res if isinstance(res, dict) else None):
            n += 1
    return n


def _look_has_send_or_fill(turn: "BrainTurn") -> bool:
    """True when this look dispatched a send (filled or working counts)."""
    if _successful_send_count(getattr(turn, "sends", None)):
        return True
    return _send_succeeded(turn.last_result)


def _look_is_empty_or_question(turn: "BrainTurn") -> bool:
    """Junk-drop: true empty assistant text or a lone '?', and no send/fill."""
    if _look_has_send_or_fill(turn):
        return False
    return _look_text_is_junk(turn.text)


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
        sends=_successful_send_count(turn.sends),
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


# Result clips. A clipped blob stays on the kept chat and is re-billed on
# every later call in the look. chars/4 ≈ tokens; grok-4.6 input is $2/MTok.
# 8_000 chars ≈ 2_000 tok ≈ $0.004 per subsequent call (was 24k / $0.012).
# Candles need more OHLC: 16_000 ≈ 4_000 tok ≈ $0.008 (was 48k / $0.024).
CLIP_CHARS = 8_000
CANDLES_CLIP_CHARS = 16_000

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


def _scan_payload(data: Any) -> bool:
    """A ranked screen page. First delivery uses SCAN_CLIP_CHARS; a re-read is a stub."""
    if not isinstance(data, dict) or "hits" not in data:
        return False
    return bool(data.get("ranked") or data.get("provenance"))


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


def _last_think_bar(bars: Any) -> dict[str, Any] | None:
    if not isinstance(bars, list) or not bars:
        return None
    for bar in reversed(bars):
        row = _think_bar(bar)
        if row:
            return row
    return None


def _candles_residue(data: dict[str, Any]) -> dict[str, Any]:
    """Symbol / resolution / count / last print — never error=clipped."""
    bars = data.get("bars") if isinstance(data.get("bars"), list) else []
    series = data.get("series") if isinstance(data.get("series"), list) else []
    count = len(bars)
    last = _last_think_bar(bars)
    if series:
        for row in series:
            if not isinstance(row, dict):
                continue
            sb = row.get("bars")
            if isinstance(sb, list):
                count += len(sb)
                edge = _last_think_bar(sb)
                if edge:
                    last = edge
    out: dict[str, Any] = {}
    for key in (
        "ok",
        "symbol",
        "source",
        "freshness",
        "resolution",
        "requested_resolution",
        "use",
        "error",
        "hist_error",
        "rt_error",
    ):
        if key in data and data[key] not in (None, ""):
            out[key] = data[key]
    out["bar_count"] = int(count)
    if isinstance(last, dict):
        edge: dict[str, Any] = {}
        if last.get("t") not in (None, ""):
            edge["t"] = last["t"]
        if last.get("c") is not None:
            edge["c"] = last["c"]
        if edge:
            out["last_bar"] = edge
    out["_clipped"] = "payload"
    return out


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
        dropped = 0
        if isinstance(payload.get("bars"), list) and isinstance(trial.get("bars"), list):
            dropped += max(0, len(payload["bars"]) - len(trial["bars"]))
        old_series = payload.get("series")
        new_series = trial.get("series")
        if isinstance(old_series, list) and isinstance(new_series, list):
            for prev, nxt in zip(old_series, new_series):
                if isinstance(prev, dict) and isinstance(nxt, dict):
                    pb = prev.get("bars")
                    nb = nxt.get("bars")
                    if isinstance(pb, list) and isinstance(nb, list):
                        dropped += max(0, len(pb) - len(nb))
        if dropped:
            trial["_dropped"] = dropped
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
    text = json.dumps(_candles_lead(kept), default=str)
    if len(text) <= max_chars:
        return text
    return json.dumps(_candles_residue(payload), default=str)


_NEWS_HEADLINE_KEYS = ("symbol", "headline", "publisher")
_NEWS_LEAD_KEYS = (
    "ok",
    "source",
    "freshness",
    "use",
    "error",
    "note",
    "need",
    "fetched",
    "already",
)


def _news_tool_payload(data: Any) -> bool:
    """news() page: top-level items[] of headlines (not a ranked scan)."""
    if not isinstance(data, dict) or "hits" in data:
        return False
    return isinstance(data.get("items"), list)


def _slim_headline(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    out: dict[str, Any] = {}
    for key in _NEWS_HEADLINE_KEYS:
        val = row.get(key)
        if val not in (None, ""):
            out[key] = val
    return out if out.get("headline") else None


def _clip_news(data: dict[str, Any], max_chars: int = CLIP_CHARS) -> str:
    """Keep as many symbol/headline/publisher rows as fit. Never error=clipped."""
    text = json.dumps(data, default=str)
    if len(text) <= max_chars:
        return text
    slim = dict(data)
    _pop_scalar_fat(slim)
    text = json.dumps(slim, default=str)
    if len(text) <= max_chars:
        return text
    raw_items = slim.get("items") if isinstance(slim.get("items"), list) else []
    headlines: list[dict[str, Any]] = []
    for row in raw_items:
        keep = _slim_headline(row)
        if keep:
            headlines.append(keep)
    base = {k: slim[k] for k in _NEWS_LEAD_KEYS if k in slim}
    if "ok" in slim and "ok" not in base:
        base["ok"] = slim["ok"]
    best: dict[str, Any] = dict(base)
    best["items"] = []
    if raw_items:
        _note_clip(best, "items", len(raw_items))
    text = json.dumps(best, default=str)
    if len(text) > max_chars:
        best = {"items": [], "_clipped": "items"}
        if "ok" in slim:
            best["ok"] = slim["ok"]
        return json.dumps(best, default=str)
    for n in range(1, len(headlines) + 1):
        trial = dict(base)
        trial["items"] = headlines[:n]
        dropped = len(headlines) - n
        if dropped > 0 or len(raw_items) > len(headlines):
            trial["_clipped"] = "items"
            trial["_dropped"] = dropped + max(0, len(raw_items) - len(headlines))
        elif any(
            not isinstance(row, dict) or set(row) - set(_NEWS_HEADLINE_KEYS)
            for row in raw_items
        ):
            trial["_clipped"] = "items"
        packed = json.dumps(trial, default=str)
        if len(packed) > max_chars:
            break
        best = trial
        text = packed
    if best.get("items"):
        return text
    if not headlines:
        return text
    # One headline still too fat — truncate the print, keep the name.
    one = dict(headlines[0])
    hl = str(one.get("headline") or "")
    while hl:
        one["headline"] = hl
        trial = dict(base)
        trial["items"] = [one]
        trial["_clipped"] = "items"
        trial["_dropped"] = max(0, len(headlines) - 1)
        packed = json.dumps(trial, default=str)
        if len(packed) <= max_chars:
            return packed
        nxt = hl[: max(0, len(hl) // 2)].rstrip()
        if nxt == hl:
            break
        hl = nxt
    syms: list[str] = []
    for row in headlines:
        sym = str(row.get("symbol") or "").strip().upper()
        if sym and sym not in syms:
            syms.append(sym)
    residue = dict(base)
    residue["items"] = []
    if syms:
        residue["symbols"] = syms
    _note_clip(residue, "items", len(raw_items) or len(headlines))
    return json.dumps(residue, default=str)


def _scan_residue(data: dict[str, Any]) -> dict[str, Any]:
    """Arena + symbol list when the ranked page cannot fit."""
    out: dict[str, Any] = {}
    for key in ("ok", "arena", "scan_code", "source", "freshness", "error"):
        if key in data and data[key] not in (None, ""):
            out[key] = data[key]
    syms = data.get("symbols")
    names: list[str] = []
    if isinstance(syms, list):
        for raw in syms:
            sym = str(raw or "").strip().upper()
            if sym and sym not in names:
                names.append(sym)
    if not names:
        for row in data.get("hits") or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            if sym and sym not in names:
                names.append(sym)
    out["symbols"] = names
    out["_clipped"] = data.get("_clipped") or "payload"
    if data.get("_dropped"):
        out["_dropped"] = data["_dropped"]
    return out


def _clip_residue(data: dict[str, Any], max_chars: int) -> str:
    """Last resort factual stub. Chat must never see error=clipped alone."""
    if _news_tool_payload(data):
        return _clip_news(data, max_chars=max_chars)
    if _tape_payload(data):
        text = json.dumps(_candles_residue(data), default=str)
        if len(text) <= max_chars:
            return text
    if _scan_payload(data) or ("arena" in data and ("hits" in data or "symbols" in data)):
        res = _scan_residue(data)
        text = json.dumps(res, default=str)
        if len(text) <= max_chars:
            return text
        # Cap the symbol list until it fits.
        names = list(res.get("symbols") or [])
        full_n = len(names)
        while True:
            trial = dict(res)
            trial["symbols"] = list(names)
            if len(names) < full_n:
                trial["_clipped"] = "symbols"
            packed = json.dumps(trial, default=str)
            if len(packed) <= max_chars:
                return packed
            if not names:
                trial["symbols"] = []
                trial["_clipped"] = "payload"
                return json.dumps(trial, default=str)
            if len(names) == 1:
                names = []
            else:
                names = names[: max(1, len(names) // 2)]
    # Prefer news/items lists embedded on non-news pages (status, etc.).
    for key in ("items", "news"):
        rows = data.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        headlines = [h for h in (_slim_headline(r) for r in rows) if h]
        if not headlines:
            continue
        base = {k: data[k] for k in ("ok", "source", "freshness", "use", "ibkr_connected", "trading_mode") if k in data}
        best = dict(base)
        best[key] = []
        _note_clip(best, key, len(rows))
        for n in range(1, len(headlines) + 1):
            trial = dict(base)
            trial[key] = headlines[:n]
            if n < len(headlines):
                trial["_clipped"] = key
                trial["_dropped"] = len(headlines) - n
            packed = json.dumps(trial, default=str)
            if len(packed) > max_chars:
                break
            best = trial
        return json.dumps(best, default=str)
    kept: dict[str, Any] = {}
    if data.get("run") is not None:
        kept["run"] = data["run"]
    if "ok" in data:
        kept["ok"] = data["ok"]
    err = data.get("error")
    if err not in (None, "", "clipped"):
        kept["error"] = err
    _note_clip(kept, str(data.get("_clipped") or "payload"))
    if data.get("_dropped"):
        kept["_dropped"] = data["_dropped"]
    text = json.dumps(kept, default=str)
    if len(text) <= max_chars:
        return text
    return json.dumps({"_clipped": "payload"}, default=str)


# Fat scan / sessions / news / playbook essay — never the live book.
_FAT_CLIP_KEYS = (
    "hits",
    "news",
    "items",
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
    "open_lots",
    "working_orders",
    "positions",
    "fills",
    "marks",
    "allocation",
    "sends_this_turn",
    "ibkr_connected",
    "trading_mode",
    "session",
    "freshness",
    "tradable_now",
    "countdown",
    "mode",
    "ibkr",
)


def _note_clip(container: dict[str, Any], key: str, dropped: int = 0) -> None:
    """Short honest trim marker. Not prose."""
    container["_clipped"] = key
    extra = int(dropped or 0)
    if extra > 0:
        container["_dropped"] = int(container.get("_dropped") or 0) + extra


def _pop_fat_key(container: dict[str, Any]) -> str | None:
    """Drop the next fat key. Clip marker stays on this container."""
    keys = _FAT_CLIP_KEYS
    if "hits" in container and "news" in container:
        keys = ("news",) + tuple(k for k in _FAT_CLIP_KEYS if k != "news")
    for key in keys:
        if key not in container:
            continue
        val = container.pop(key)
        dropped = 0
        if isinstance(val, list):
            dropped = len(val)
        elif isinstance(val, dict) and isinstance(val.get("rows"), list):
            dropped = len(val["rows"])
        _note_clip(container, key, dropped)
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
    return False


def _keep_live_book(data: dict[str, Any]) -> dict[str, Any]:
    """Emergency book core. Lots, orders, and fills stay; fat look does not."""
    out: dict[str, Any] = {}
    for key in _LIVE_BOOK_KEEP:
        if key in data:
            out[key] = data[key]
    return out


# List fields: drop tail rows (ranked head stays). Never slice JSON mid-byte.
_ROW_LIST_KEYS = (
    "hits",
    "news",
    "items",
    "rows",
    "symbols",
    "sessions",
    "notes",
    "scan_tape",
)
_FAT_SCALAR_KEYS = ("pad", "essay", "tree", "metrics", "card_scores", "types")
_DICT_ROW_KEYS = ("session_range",)


def _iter_row_lists(
    data: dict[str, Any], *, skip_live: bool = True
) -> list[tuple[dict[str, Any], str, list[Any]]]:
    found: list[tuple[dict[str, Any], str, list[Any]]] = []
    for key, val in list(data.items()):
        if str(key).startswith("_"):
            continue
        if skip_live and key in _LIVE_BOOK_ROOTS:
            continue
        if key in _ROW_LIST_KEYS and isinstance(val, list):
            found.append((data, key, val))
        elif isinstance(val, dict):
            if isinstance(val.get("rows"), list):
                found.append((val, "rows", val["rows"]))
            found.extend(_iter_row_lists(val, skip_live=skip_live))
    return found


def _pop_scalar_fat(data: dict[str, Any]) -> bool:
    changed = False
    for key in _FAT_SCALAR_KEYS:
        if key in data:
            data.pop(key, None)
            _note_clip(data, key)
            changed = True
    for key, val in list(data.items()):
        if str(key).startswith("_") or key in _LIVE_BOOK_ROOTS:
            continue
        if isinstance(val, dict) and _pop_scalar_fat(val):
            if not data.get("_clipped"):
                _note_clip(data, key)
            changed = True
    return changed


def _trim_dict_row_key(container: dict[str, Any], key: str) -> int:
    blob = container.get(key)
    if not isinstance(blob, dict) or len(blob) <= 1:
        return 0
    items = [(k, v) for k, v in blob.items() if not str(k).startswith("_")]
    if len(items) <= 1:
        return 0
    keep = max(1, len(items) // 2)
    dropped = len(items) - keep
    kept = dict(items[:keep])
    for mark in ("_clipped", "_dropped"):
        if mark in blob:
            kept[mark] = blob[mark]
    container[key] = kept
    _note_clip(container, key, dropped)
    _note_clip(kept, key, dropped)
    return dropped


def _trim_one_row_list(data: dict[str, Any]) -> int:
    """Drop the tail of the longest row list. Returns rows dropped."""
    lists = _iter_row_lists(data)
    if not lists:
        dropped = 0
        for key in _DICT_ROW_KEYS:
            if isinstance(data.get(key), dict):
                dropped += _trim_dict_row_key(data, key)
            look = data.get("last_look")
            if isinstance(look, dict) and isinstance(look.get(key), dict):
                n = _trim_dict_row_key(look, key)
                if n:
                    _note_clip(data, key, n)
                dropped += n
        return dropped
    # Scan page: cut headlines before ranked rows so clip cannot evict the tape.
    if (
        isinstance(data.get("hits"), list)
        and isinstance(data.get("news"), list)
        and len(data["news"]) > 1
    ):
        news_lists = [item for item in lists if item[1] == "news"]
        if news_lists:
            owner, key, rows = max(news_lists, key=lambda item: len(item[2]))
            if len(rows) > 1:
                keep = max(1, len(rows) // 2)
                dropped = len(rows) - keep
                owner[key] = rows[:keep]
                _note_clip(owner, key, dropped)
                if owner is not data:
                    _note_clip(data, key, dropped)
                return dropped
    owner, key, rows = max(lists, key=lambda item: len(item[2]))
    if len(rows) <= 1:
        return 0
    keep = max(1, len(rows) // 2)
    dropped = len(rows) - keep
    owner[key] = rows[:keep]
    _note_clip(owner, key, dropped)
    if owner is not data:
        _note_clip(data, key, dropped)
    return dropped


def _clip(data: Any, max_chars: int | None = None) -> str:
    """Keep the live book when the payload overflows. Fat scan clips first."""
    if _tape_payload(data):
        cap = CANDLES_CLIP_CHARS if max_chars is None else int(max_chars)
        return _clip_candles(data, max_chars=cap)
    cap = CLIP_CHARS if max_chars is None else int(max_chars)
    if _news_tool_payload(data):
        return _clip_news(data, max_chars=cap)
    text = json.dumps(data, default=str)
    if len(text) <= cap:
        return text
    if isinstance(data, dict):
        slim = dict(data)
        _pop_scalar_fat(slim)
        text = json.dumps(slim, default=str)
        if len(text) <= cap:
            return text
        while len(json.dumps(slim, default=str)) > cap:
            if _trim_one_row_list(slim) <= 0:
                break
        text = json.dumps(slim, default=str)
        if len(text) <= cap:
            return text
        while len(json.dumps(slim, default=str)) > cap:
            slim, changed = _clip_fat_once(slim)
            if not changed:
                break
            text = json.dumps(slim, default=str)
            if len(text) <= cap:
                return text
        if _is_live_book(slim):
            return json.dumps(_keep_live_book(slim), default=str)
        kept: dict[str, Any] = {}
        if slim.get("run") is not None:
            kept["run"] = slim["run"]
        if kept:
            kept["ok"] = slim.get("ok")
            _note_clip(kept, "payload")
            if slim.get("_dropped"):
                kept["_dropped"] = slim["_dropped"]
            out = json.dumps(kept, default=str)
            if len(out) <= cap:
                return out
        # Prefer residue from the pre-fat-pop body so headlines/symbols survive.
        return _clip_residue(dict(data), max_chars=cap)
    return json.dumps({"_clipped": "payload"}, default=str)


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


async def stream_round(
    chat: Any, *, stage: str = "grok", emit_stage: bool = True
) -> tuple[str, Any, str]:
    """One model call on this chat. Returns (assistant text, response, stop_reason)."""
    if emit_stage:
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
    tip_t0 = time.monotonic()
    last_activity = tip_t0
    while True:
        try:
            from abcxauto.park_clock import peek_interrupt

            if peek_interrupt() is not None:
                reason = "interrupt"
                break
        except Exception:
            pass
        now = time.monotonic()
        ceiling_s = think_only_ceiling_s()
        if (
            ceiling_s > 0
            and saw_think
            and not saw_say
            and not o
            and (now - tip_t0) >= ceiling_s
        ):
            think_emit("tool", "\n[think-only ceiling]\n")
            reason = "think_only"
            break
        silent_s = silent_grok_tip_s()
        if (
            silent_s > 0
            and not saw_say
            and not o
            and (now - last_activity) >= silent_s
        ):
            # Idle since last think/say. A live reasoning stream resets
            # last_activity. No think and no say is still hung.
            think_emit("tool", "\n[stream silent]\n")
            reason = "think_only" if saw_think else "empty"
            break
        try:
            resp, ch = await asyncio.wait_for(anext(agen), timeout=STREAM_CHUNK_S)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            idle += 1
            if idle >= STREAM_IDLE_LIMIT:
                think_emit("tool", "\n[stream stalled]\n")
                if not saw_say and not o:
                    reason = "think_only" if saw_think else "empty"
                else:
                    reason = "stalled"
                break
            continue
        idle = 0
        last_ch = ch
        last_resp = resp
        rc = _piece(ch, "reasoning_content", "reasoning")
        think_acc, think_piece = _delta(think_acc, rc)
        if think_piece:
            last_activity = time.monotonic()
            if not saw_think:
                think_emit("say", "\n[think]\n")
                saw_think = True
            think_emit("think", think_piece)
        content = _piece(ch, "content")
        if content:
            say_acc, say_piece = _delta(say_acc, content)
            if say_piece:
                last_activity = time.monotonic()
                if not saw_say:
                    think_emit("say", "\n[say]\n")
                    saw_say = True
                o += say_piece
                think_emit("say", say_piece)
        if stream_is_looping(think_acc) or stream_is_looping(o):
            think_emit("tool", "\n[stream loop]\n")
            reason = "loop"
            break
    fr = ""
    try:
        if last_ch is not None:
            choices = list(getattr(last_ch, "choices", None) or [])
            raw_fr = getattr(choices[0], "finish_reason", None) if choices else None
            fr = str(getattr(raw_fr, "name", None) or raw_fr or "")
        if "LENGTH" in fr.upper() or "MAX_TOKEN" in fr.upper():
            think_emit("tool", "\n[truncated: max_tokens]\n")
    except Exception:
        logger.debug("finish_reason probe failed", exc_info=True)
    if emit_stage:
        think_emit("stage_end", stage)
    if not o:
        # Some SDK finishes put the spoken say on the completed message only.
        for obj in (last_ch, last_resp):
            extra = _piece(obj, "content")
            if extra:
                o = extra
                break
    # Hung GROK tip: no [say]. Think-only / stall / banner-then-silence
    # are the same class. Do not require stop==empty from the SDK.
    if reason in ("ok", "stalled") and not saw_say and not o:
        reason = "think_only" if saw_think else "empty"
    if reason in ("empty", "think_only"):
        for obj in (last_resp, last_ch):
            if obj is not None and list(getattr(obj, "tool_calls", None) or []):
                reason = "ok"
                break
    used: dict[str, int] = {}
    try:
        from abcxauto.scorecard import usage_from_response

        used = usage_from_response(
            last_resp, last_ch, think_text=think_acc, say_text=o
        )
    except Exception:
        logger.debug("usage probe failed", exc_info=True)
    try:
        logger.info(
            "grok call input=%s output=%s reasoning=%s finish_reason=%s stop=%s",
            int(used.get("input_tokens") or 0),
            int(used.get("output_tokens") or 0),
            int(used.get("reasoning_tokens") or 0),
            fr or "-",
            reason,
        )
    except Exception:
        logger.debug("grok call log failed", exc_info=True)
    try:
        from abcxauto.memory import get_journal
        from abcxauto.scorecard import estimate_cost_usd

        from abcxauto.config import get_config

        inn = int(used.get("input_tokens") or 0)
        out = int(used.get("output_tokens") or 0)
        cached = int(used.get("cached_tokens") or 0)
        model_id = str(getattr(get_config(), "model", "") or "")
        if inn <= 0 and out <= 0 and cached <= 0:
            logger.info("model usage journal skip: zero tokens (row would be dropped)")
        cost = estimate_cost_usd(inn, out, cached_tokens=cached)

        def _write_usage() -> None:
            get_journal().record_model_usage(
                stage=stage,
                model=model_id,
                input_tokens=inn,
                output_tokens=out,
                cached_tokens=cached,
                cost_usd=cost,
            )
            try:
                from abcxauto.look_meter import note_model_call

                note_model_call(used, model=model_id)
            except Exception:
                logger.exception("look_meter note_model_call failed")

        # Journal lock must not freeze the look. The UI reads the same db.
        writer = threading.Thread(target=_write_usage, name="usage-journal", daemon=True)
        writer.start()
        writer.join(2.0)
        if writer.is_alive():
            logger.warning("usage journal still running — look continues")
    except Exception:
        logger.exception("model usage journal failed")
    try:
        from abcxauto.look_ledger import append_call

        from abcxauto.config import get_config

        inn = int(used.get("input_tokens") or 0)
        out = int(used.get("output_tokens") or 0)
        model_id = str(getattr(get_config(), "model", "") or "")
        sess = ""
        try:
            from abcxauto.marketdata.market_hours import get_session_info

            sess = str((get_session_info() or {}).get("session") or "")
        except Exception:
            sess = ""
        usd: Any = "unknown"
        for blob in (used, getattr(last_resp, "usage", None), last_resp):
            if not isinstance(blob, dict):
                continue
            raw = blob.get("cost_usd")
            if raw is None:
                raw = blob.get("usd")
            if raw is None:
                raw = blob.get("total_cost")
            if raw is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if val == val and val not in (float("inf"), float("-inf")) and val >= 0:
                usd = val
                break
        asof = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        append_call(
            asof=asof,
            model=model_id,
            input_tokens=inn,
            output_tokens=out,
            usd=usd,
            session=sess,
        )
    except ImportError:
        pass
    except Exception:
        logger.debug("look_ledger append_call failed", exc_info=True)
    return o, last_resp, reason


def _model_usage_calls() -> int:
    try:
        from abcxauto.memory import get_journal

        return int((get_journal().model_usage_totals() or {}).get("calls") or 0)
    except Exception:
        return 0


def _bill_spoken_look_if_unbilled(turn: BrainTurn, *, before_calls: int) -> None:
    """If this look spoke and stream_round wrote no usage row, estimate it.

    Empty-token leftover $0.18 rows are refused. A real spoken look still bills.
    """
    if _look_text_is_junk(turn.text):
        return
    try:
        from abcxauto.config import get_config
        from abcxauto.memory import get_journal
        from abcxauto.scorecard import estimate_cost_usd, estimate_tokens

        journal = get_journal()
        after = int((journal.model_usage_totals() or {}).get("calls") or 0)
        if after > int(before_calls or 0):
            return
        out = estimate_tokens(turn.text)
        if out <= 0:
            return
        journal.record_model_usage(
            stage="grok",
            model=str(getattr(get_config(), "model", "") or ""),
            output_tokens=out,
            cost_usd=estimate_cost_usd(0, out),
        )
    except Exception:
        logger.debug("spoken-look usage backup failed", exc_info=True)


async def grok(g: GrokClient, p: str, *, stage: str = "grok") -> str:
    """One-shot streamed reply (tests / no tools). Hot path is grok_turn."""
    create_kw = chat_create_kwargs(
        g, messages=[system(build_system_prompt()), user(p)]
    )
    chat = create_chat(g.client, **create_kw)
    text, _, _ = await stream_round(chat, stage=stage)
    return text


def _reset_chat(g: GrokClient) -> None:
    """Kept for callers. The conversation is not dropped."""
    _ = g


def drop_live_chat(g: Any | None) -> None:
    """Kept for callers. The conversation is not dropped."""
    _ = g


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


def _remember_desk_fact(g: Any, chat: Any, wake: str) -> None:
    """Last collapsible lead fact this chat already heard."""
    text = str(wake or "")
    if g is not None:
        g._last_desk_fact = text
        g._wake_appended = True
    if chat is not None:
        try:
            chat._abcx_last_desk_fact = text
        except Exception:
            logger.debug("desk fact remember on chat failed", exc_info=True)


def _chat_last_desk_fact(g: Any, chat: Any = None) -> str:
    if chat is not None:
        hit = getattr(chat, "_abcx_last_desk_fact", None)
        if hit:
            return str(hit)
    if g is not None:
        return str(getattr(g, "_last_desk_fact", "") or "")
    return ""


def _persist_response_id(response: Any) -> None:
    """Save ``response.id`` so the next create can pass ``previous_response_id``."""
    if response is None:
        return
    rid = getattr(response, "id", None)
    if not rid:
        return
    try:
        from abcxauto.chat_cursor import save_previous_response_id

        save_previous_response_id(str(rid))
    except Exception:
        logger.debug("chat_cursor persist failed", exc_info=True)


def _finish_look_chat(g: GrokClient, turn: BrainTurn, *, session: str) -> None:
    """Keep the live chat. Park, overnight, and an empty say do not start a new one."""
    _ = g, turn, session


def _new_chat(g: GrokClient, *, session: str = "") -> Any:
    apply = getattr(g, "apply_session", None)
    if callable(apply) and str(session or "").strip():
        try:
            apply(session)
        except Exception:
            logger.debug("session knobs apply failed", exc_info=True)
    prev = ""
    try:
        from abcxauto.chat_cursor import load_previous_response_id

        prev = load_previous_response_id()
    except Exception:
        logger.debug("chat_cursor load failed", exc_info=True)
        prev = ""
    if prev:
        create_kw = chat_create_kwargs(
            g,
            tools=agent_tools(session=session),
            previous_response_id=prev,
        )
    else:
        create_kw = chat_create_kwargs(
            g,
            messages=[system(brain_system_prompt())],
            tools=agent_tools(session=session),
        )
    chat = create_chat(g.client, **create_kw)
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
    snap: dict[str, Any] | None = None,
) -> Any:
    """Start this look, or continue the live chat.

    A live chat stays one conversation — including when ``resume`` is false
    or ``reset`` is true. A pending live poke owns the next developer turn.
    When there is no local chat (new process), ``_new_chat`` continues via
    ``previous_response_id``.
    """
    _ = resume, reset
    g._wake_appended = False
    live = getattr(g, "chat", None)
    if live is not None:
        pending = False
        try:
            from abcxauto.park_clock import peek_interrupt

            pending = peek_interrupt() is not None
        except Exception:
            pending = False
        if not pending:
            from abcxauto.world_state import desk_fact_is_duplicate

            prev = _chat_last_desk_fact(g, live)
            keep_looking = False
            try:
                from abcxauto.desk_mode import (
                    research_keep_looking,
                    rth_flat_keep_looking,
                )

                keep_looking = research_keep_looking(session) or rth_flat_keep_looking(
                    session, snap, desk_fact=wake
                )
            except Exception:
                keep_looking = False
            if desk_fact_is_duplicate(prev, wake) and not keep_looking:
                # Same lead-fact identity (set / list / tick). A look may
                # end — do not append a fresh go-do-desk developer turn.
                # Research keep-looking still appends: no broker poke will
                # come. Words-only flat RTH waits for a real event.
                g._wake_n = int(getattr(g, "_wake_n", 0) or 0) + 1
                return live
            live.append(developer(wake))
            _remember_desk_fact(g, live, wake)
        g._wake_n = int(getattr(g, "_wake_n", 0) or 0) + 1
        return live
    chat = _new_chat(g, session=session)
    chat.append(developer(wake))
    _remember_desk_fact(g, chat, wake)
    return chat



_LOOK_SNAP_KEY = "_look_tool_snapshot"
_LOOK_STASH_ATTR = "_abcx_look_tool_snapshot"


def _look_tool_bag(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snap, dict):
        return None
    bag = snap.get(_LOOK_SNAP_KEY)
    return bag if isinstance(bag, dict) else None


def _look_bag_has_rows(bag: dict[str, Any] | None) -> bool:
    if not isinstance(bag, dict):
        return False
    quotes = bag.get("quote")
    oq = bag.get("option_quote")
    if isinstance(quotes, list) and quotes:
        return True
    if isinstance(oq, list) and oq:
        return True
    return bag.get("book") is not None


def _stash_look_tool_bag(holder: Any, snap: dict[str, Any] | None) -> None:
    if holder is None:
        return
    bag = _look_tool_bag(snap)
    keep = bag if _look_bag_has_rows(bag) else None
    try:
        setattr(holder, _LOOK_STASH_ATTR, keep)
    except Exception:
        logger.debug("look cache stash failed", exc_info=True)


def _restore_look_tool_bag(snap: dict[str, Any] | None, bag: dict[str, Any] | None) -> None:
    if not isinstance(snap, dict) or not isinstance(bag, dict):
        return
    snap[_LOOK_SNAP_KEY] = bag


def _clear_look_tool_stash(holder: Any) -> None:
    if holder is None:
        return
    try:
        setattr(holder, _LOOK_STASH_ATTR, None)
    except Exception:
        pass


def _begin_look_for_turn(
    snap: dict[str, Any] | None, *, same_look: bool, holder: Any = None
) -> None:
    """Wipe this-look quote cache only at a true look start.

    A stream-loop abort re-enters _grok_turn_impl on the same chat.
    Those IBKR prints were already paid for; begin_look would throw
    them away and the model would not re-fetch.
    """
    from abcxauto.look_snapshot import begin_look

    if same_look:
        if not _look_bag_has_rows(_look_tool_bag(snap)):
            stashed = getattr(holder, _LOOK_STASH_ATTR, None) if holder is not None else None
            if isinstance(stashed, dict):
                _restore_look_tool_bag(snap, stashed)
        return
    begin_look(snap)
    _clear_look_tool_stash(holder)


def _et_today() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _look_scan_symbols(snap: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        sym = str(raw or "").strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)

    for raw in snap.get("scan_fetched") or []:
        add(raw)
    hits = snap.get("scan_hits")
    if isinstance(hits, dict):
        for row in hits.get("rows") or []:
            if isinstance(row, dict):
                add(row.get("symbol"))
        for raw in hits.get("symbols") or []:
            add(raw)
    return out


def _look_scan_rows(snap: dict[str, Any]) -> list[dict[str, Any]]:
    hits = snap.get("scan_hits")
    if not isinstance(hits, dict):
        return []
    return [r for r in (hits.get("rows") or []) if isinstance(r, dict)]


def _closes_from_bars(bars: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        day = str(bar.get("date") or "").strip()[:10]
        try:
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if day and close == close and close > 0:
            out[day] = close
    return out


def _daily_returns(closes_by_date: dict[str, float]) -> list[float]:
    dates = sorted(closes_by_date)
    out: list[float] = []
    for i in range(1, len(dates)):
        prev = closes_by_date[dates[i - 1]]
        cur = closes_by_date[dates[i]]
        if prev and prev == prev and prev != 0:
            out.append(cur / prev - 1.0)
    return out


def _short_alloc_wake(
    sized: dict[str, Any] | None, ranked: list[dict[str, Any]] | None
) -> str:
    bits: list[str] = []
    if isinstance(sized, dict):
        for sym, panel in sized.items():
            if not isinstance(panel, dict) or panel.get("sized") is None:
                continue
            try:
                excess = int(panel.get("excess") or 0)
            except (TypeError, ValueError):
                excess = 0
            if excess <= 0:
                continue
            bits.append(
                f"{sym} sized={panel.get('sized')} held={panel.get('held')} excess={excess}"
            )
            if len(bits) >= 4:
                break
    if bits:
        return "alloc " + "; ".join(bits)
    if isinstance(ranked, list) and ranked:
        top = ranked[0]
        if isinstance(top, dict) and top.get("symbol"):
            vs = top.get("vs_spy")
            try:
                vs_s = f"{float(vs):+.3f}" if vs is not None else ""
            except (TypeError, ValueError):
                vs_s = ""
            return f"alloc rank1={top.get('symbol')}{(' ' + vs_s) if vs_s else ''}".strip()
    return ""


def _short_research_wake(dossiers: Any) -> str:
    if not isinstance(dossiers, dict) or not dossiers:
        return ""
    n = len(dossiers)
    unknown = 0
    for bag in dossiers.values():
        if not isinstance(bag, dict):
            continue
        if bag.get("earnings") in (None, "", "unknown"):
            unknown += 1
    if unknown:
        return f"research dossiers={n} earnings_unknown={unknown}"
    return f"research dossiers={n}"


def _stk_con_id(positions: list[Any], symbol: str) -> str:
    """Live stock conId for ``symbol``. Empty when the lot is not on the book."""
    want = str(symbol or "").strip().upper()
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        if str(pos.get("symbol") or "").strip().upper() != want:
            continue
        sec = str(pos.get("secType") or pos.get("sec_type") or "STK").upper()
        if not sec.startswith("STK"):
            continue
        con = str(pos.get("conId") or pos.get("con_id") or "").strip()
        if con and con not in ("0", "?"):
            return con
    return ""


def _order_limit_px(order: dict[str, Any]) -> float | None:
    for key in ("lmt", "lmtPrice", "limit", "limit_price", "price"):
        raw = order.get(key)
        if raw in (None, ""):
            continue
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return None


def _working_limit_sell_qty(
    orders: list[Any],
    symbol: str,
    *,
    at_or_below: float | None = None,
) -> int:
    """Open limit sells at or under ``at_or_below``. A far target is not a trim.

    Stops and trails never count. A profit target sitting well above the bid
    must not block the excess sell.
    """
    want = str(symbol or "").strip().upper()
    ceiling = None
    if at_or_below is not None:
        try:
            ceiling = float(at_or_below)
        except (TypeError, ValueError):
            ceiling = None
        if ceiling is not None and ceiling <= 0:
            ceiling = None
    total = 0
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if str(order.get("symbol") or "").strip().upper() != want:
            continue
        action = str(order.get("action") or order.get("side") or "").upper()
        if action not in ("SELL", "S"):
            continue
        typ = str(
            order.get("order_type") or order.get("orderType") or order.get("type") or ""
        ).upper()
        if "STP" in typ or "STOP" in typ or "TRAIL" in typ:
            continue
        if typ and "LMT" not in typ and "LIMIT" not in typ:
            continue
        limit_px = _order_limit_px(order)
        if ceiling is not None and (limit_px is None or limit_px > ceiling + 0.05):
            continue
        try:
            qty = int(float(order.get("quantity") or order.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            total += qty
    return total


# Process-lifetime: one alloc excess sell per symbol. Survives look snaps.
_ALLOC_TRIM_SENT: dict[str, int] = {}


def _alloc_trim_sent_bag(snap: dict[str, Any]) -> dict[str, int]:
    """Mirror process trim memory onto ``snap['alloc_trim_sent']``."""
    bag = dict(_ALLOC_TRIM_SENT)
    existing = snap.get("alloc_trim_sent")
    if isinstance(existing, dict):
        for key, raw in existing.items():
            sym = str(key or "").strip().upper()
            if not sym:
                continue
            try:
                qty = int(raw)
            except (TypeError, ValueError):
                continue
            if qty > 0:
                bag[sym] = qty
    snap["alloc_trim_sent"] = bag
    _ALLOC_TRIM_SENT.clear()
    _ALLOC_TRIM_SENT.update(bag)
    return bag


def _record_alloc_trim_sent(snap: dict[str, Any], symbol: str, qty: int) -> None:
    sym = str(symbol or "").strip().upper()
    if not sym or qty <= 0:
        return
    bag = _alloc_trim_sent_bag(snap)
    bag[sym] = int(qty)
    _ALLOC_TRIM_SENT[sym] = int(qty)
    snap["alloc_trim_sent"] = bag


def _sell_exec_count(connector: Any, symbol: str) -> int:
    """Count SLD/SELL rows already captured on the connector for ``symbol``."""
    want = str(symbol or "").strip().upper()
    if not want or connector is None:
        return 0
    store = getattr(connector, "_executions", None)
    if not isinstance(store, dict):
        return 0
    rows = store.get(want) or store.get(symbol) or []
    n = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        side = str(row.get("side") or "").upper()
        if side in ("SLD", "SELL"):
            n += 1
    return n


def _stk_held_qty(positions: list[Any], symbol: str) -> int:
    want = str(symbol or "").strip().upper()
    total = 0
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        if str(pos.get("symbol") or "").strip().upper() != want:
            continue
        sec = str(pos.get("secType") or pos.get("sec_type") or "STK").upper()
        if not sec.startswith("STK"):
            continue
        try:
            qty = int(
                float(
                    pos.get("quantity")
                    if pos.get("quantity") is not None
                    else (
                        pos.get("position")
                        if pos.get("position") is not None
                        else pos.get("qty") or 0
                    )
                )
            )
        except (TypeError, ValueError):
            qty = 0
        total += qty
    return abs(total)


async def apply_pre_model_look_systems(
    *,
    connector: Any,
    world: Any,
    snap: dict[str, Any],
    day: dict[str, Any] | None,
) -> None:
    """Allocation → size/trim → dossiers after world/snap, before model research.

    Missing sibling modules are ImportError-guarded so a partial tree does not
    crash the desk. Does not call note_brief_turn or set brief_loop_halted.
    """
    if not isinstance(snap, dict):
        return
    day_bag = day if isinstance(day, dict) else {}
    today = _et_today()
    scan_symbols = _look_scan_symbols(snap)
    scan_rows = _look_scan_rows(snap)
    positions = list(snap.get("positions") or getattr(world, "positions", None) or [])
    orders = list(snap.get("open_orders") or getattr(world, "open_orders", None) or [])
    account = snap.get("account") if isinstance(snap.get("account"), dict) else {}

    allocation: dict[str, Any] | None = None
    try:
        from abcxauto.alloc_snapshot import build_allocation_snapshot

        allocation = await build_allocation_snapshot(
            connector,
            positions=positions,
            orders=orders,
            account=account,
            scan_symbols=scan_symbols,
            today=today,
        )
        if isinstance(allocation, dict):
            snap["allocation_snapshot"] = allocation
    except ImportError:
        logger.debug("alloc_snapshot not installed")
    except Exception:
        logger.debug("build_allocation_snapshot failed", exc_info=True)

    sized: dict[str, Any] | None = None
    ranked: list[dict[str, Any]] = []
    scores: dict[str, dict] = {}
    if isinstance(allocation, dict):
        try:
            from abcxauto.alloc_rank import heat_groups, rank_board, score_name
            from abcxauto.alloc_size import sized_book

            names = (
                allocation.get("names")
                if isinstance(allocation.get("names"), dict)
                else {}
            )
            spy_panel = names.get("SPY") if isinstance(names.get("SPY"), dict) else {}
            spy_by = _closes_from_bars(spy_panel.get("bars"))
            returns_by: dict[str, list[float]] = {}
            asof = str(allocation.get("asof") or "")
            bar_date = str(allocation.get("bar_date") or "")
            for sym, panel in names.items():
                if not isinstance(panel, dict):
                    continue
                closes = _closes_from_bars(panel.get("bars"))
                panel["asof"] = asof
                panel["price_asof"] = asof
                if bar_date:
                    panel["bar_date"] = bar_date
                if str(sym).upper() != "SPY":
                    scores[str(sym)] = score_name(closes, spy_by)
                    panel["vs_spy"] = scores[str(sym)].get("vs_spy")
                rets = _daily_returns(closes)
                if rets:
                    returns_by[str(sym)] = rets
            ranked = rank_board(scores)
            snap["alloc_rank"] = ranked
            groups = heat_groups(returns_by)
            sized = sized_book(allocation, scores, groups)
            snap["sized"] = sized
        except ImportError:
            logger.debug("alloc_rank/alloc_size not installed")
        except Exception:
            logger.debug("alloc score/size failed", exc_info=True)

    if isinstance(sized, dict):
        try:
            from abcxauto.alloc_trim import trim_tickets

            bids: dict[str, float] = {}
            names = (
                allocation.get("names")
                if isinstance(allocation, dict)
                and isinstance(allocation.get("names"), dict)
                else {}
            )
            for sym, panel in names.items():
                if not isinstance(panel, dict):
                    continue
                try:
                    bid = float(panel.get("bid") or 0)
                except (TypeError, ValueError):
                    continue
                if bid > 0:
                    bids[str(sym)] = bid
            tickets = trim_tickets(sized, bids=bids)
            snap["trim_tickets"] = list(tickets or [])
            try:
                from abcxauto.agent_loop import execute_ticket

                sent_bag = _alloc_trim_sent_bag(snap)
                refresh_positions = False
                for ticket in tickets or []:
                    if not isinstance(ticket, dict):
                        continue
                    sym = str(ticket.get("symbol") or "").strip().upper()
                    try:
                        excess = int(ticket.get("quantity") or 0)
                    except (TypeError, ValueError):
                        excess = 0
                    try:
                        trim_px = float(ticket.get("limit_price") or 0)
                    except (TypeError, ValueError):
                        trim_px = 0.0
                    if not sym or excess <= 0:
                        continue
                    if sym in sent_bag:
                        logger.info(
                            "alloc trim skip %s — already sent qty=%s this process",
                            sym,
                            sent_bag.get(sym),
                        )
                        continue
                    if _working_limit_sell_qty(
                        orders, sym, at_or_below=trim_px
                    ) >= excess:
                        logger.info(
                            "alloc trim skip %s excess=%s — limit sell already at the bid",
                            sym,
                            excess,
                        )
                        continue
                    panel = sized.get(sym) if isinstance(sized.get(sym), dict) else {}
                    try:
                        sized_qty = int(panel.get("sized") or 0)
                    except (TypeError, ValueError):
                        sized_qty = 0
                    if refresh_positions and connector is not None:
                        get_pos = getattr(connector, "get_positions", None)
                        if callable(get_pos):
                            try:
                                live = await get_pos()
                            except Exception:
                                live = None
                                logger.debug(
                                    "alloc trim position refresh failed",
                                    exc_info=True,
                                )
                            if isinstance(live, list):
                                positions = live
                                snap["positions"] = list(live)
                        refresh_positions = False
                    held = _stk_held_qty(positions, sym)
                    if held <= sized_qty:
                        logger.info(
                            "alloc trim skip %s — held=%s already at/under sized=%s",
                            sym,
                            held,
                            sized_qty,
                        )
                        continue
                    con_id = _stk_con_id(positions, sym)
                    if not con_id:
                        logger.info("alloc trim skip %s — no position conId", sym)
                        continue
                    logger.info(
                        "alloc trim %s qty=%s limit=%s conId=%s",
                        sym,
                        excess,
                        trim_px,
                        con_id,
                    )
                    act = {
                        "action": "limit_order",
                        "strategy": "limit_order",
                        "target_conId": con_id,
                        "params": {
                            "symbol": sym,
                            "action": "SELL",
                            "quantity": ticket.get("quantity"),
                            "limit_price": ticket.get("limit_price"),
                            "closing_position": True,
                            "conId": con_id,
                            "target_conId": con_id,
                        },
                        "rationale": str(ticket.get("reason") or "alloc_excess"),
                    }
                    exec_before = _sell_exec_count(connector, sym)
                    result = await execute_ticket(act, connector, world, snap)
                    executed = _send_succeeded(
                        result if isinstance(result, dict) else None
                    )
                    if not executed and _sell_exec_count(connector, sym) > exec_before:
                        executed = True
                    if executed:
                        _record_alloc_trim_sent(snap, sym, excess)
                        sent_bag = snap["alloc_trim_sent"]
                        refresh_positions = True
            except ImportError:
                logger.debug(
                    "execute_ticket not importable; trim_tickets left on snap"
                )
        except ImportError:
            logger.debug("alloc_trim not installed")
        except Exception:
            logger.debug("alloc trim failed", exc_info=True)

    spend: dict[str, Any] | None = None
    try:
        from abcxauto.look_ledger import session_spend

        spend = session_spend(today)
    except ImportError:
        logger.debug("look_ledger not installed")
    except Exception:
        logger.debug("session_spend failed", exc_info=True)

    try:
        from abcxauto.research_dossier import assemble_dossiers

        if isinstance(allocation, dict):
            bag = (
                snap.get("_research_bag")
                if isinstance(snap.get("_research_bag"), dict)
                else {}
            )
            research_web = (
                snap.get("research_web")
                if isinstance(snap.get("research_web"), dict)
                else {}
            )
            calendar = bag.get("calendar") if isinstance(bag.get("calendar"), dict) else {}
            odds = bag.get("odds") if isinstance(bag.get("odds"), dict) else {}
            web = research_web or (
                bag.get("web") if isinstance(bag.get("web"), dict) else {}
            )
            news: dict[str, Any] = {"items": list(snap.get("news_items") or [])}
            if isinstance(bag.get("news"), dict):
                news = bag["news"]
            dossiers = assemble_dossiers(
                allocation,
                scan_rows=scan_rows,
                calendar=calendar if isinstance(calendar, dict) else {},
                news=news,
                web=web if isinstance(web, dict) else {},
                odds=odds if isinstance(odds, dict) else {},
                spend=spend,
            )
            snap["dossiers"] = dossiers
    except ImportError:
        logger.debug("research_dossier not installed")
    except Exception:
        logger.debug("assemble_dossiers failed", exc_info=True)

    alloc_line = _short_alloc_wake(sized, ranked)
    research_line = _short_research_wake(snap.get("dossiers"))
    if alloc_line:
        snap["alloc_line"] = alloc_line
        day_bag["alloc_line"] = alloc_line
    if research_line:
        snap["research_line"] = research_line
        day_bag["research_line"] = research_line
    if isinstance(spend, dict):
        if spend.get("unknown"):
            snap["session_spend_unknown"] = True
            day_bag["session_spend_unknown"] = True
        elif spend.get("usd") is not None:
            try:
                usd = float(spend["usd"])
            except (TypeError, ValueError):
                usd = None
            if usd is not None and usd == usd:
                snap["session_spend_usd"] = usd
                day_bag["session_spend_usd"] = usd


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
    kind = str(ev.kind or "").strip().lower()
    if kind not in ("fill", "order_change"):
        from abcxauto.world_state import desk_fact_is_duplicate, worst_wake_fact

        try:
            from abcxauto.scorecard import compute_scorecard

            sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
            day_now = day_facts(world, sc)
        except Exception:
            day_now = day_facts(world, None)
        fact = worst_wake_fact(
            unprotected=list(getattr(world, "unprotected", None) or []),
            day=day_now,
            session=str(getattr(world, "session_status", "") or ""),
        )
        prev = _chat_last_desk_fact(None, chat)
        if desk_fact_is_duplicate(prev, fact):
            # Lead-fact identity unchanged. Do not stream, do not wipe cache.
            return False
    note_wake(ev)
    turn.interrupted = True
    # This look's IBKR screens did not change. Quotes/book refetch only when
    # the poke actually moved the book (fill / order_change / unprotected).
    scan_snap = _scan_snap_bag(snap)
    look_bag = _look_tool_bag(snap)
    if live_poke_clears_tool_cache(ev):
        # Fill / real order fill-cancel / unprotected: the book moved under us.
        turn.tool_cache.clear()
        # Do not begin_look. A book poke means positions/orders moved. It
        # does not make a two-second-old IBKR option print invented.
    from abcxauto.desktop.stream import format_stream_poke

    think_emit("tool", f"\n{format_stream_poke(ev.kind, ev.detail)}\n")
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
                _restore_look_tool_bag(snap, look_bag)
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
        from abcxauto.world_state import omit_duplicate_fact_lead

        to_append = omit_duplicate_fact_lead(_chat_last_desk_fact(None, chat), poke)
        if to_append:
            chat.append(developer(to_append))
        _remember_desk_fact(None, chat, poke)
    except Exception:
        logger.debug("live poke append failed", exc_info=True)
        return False
    return True


_BLOTTER_DAY_KEYS = (
    "nl",
    "ibkr_daily_pnl",
    "daily_pnl",
    "daily_pnl_pct",
    "open_upnl",
    "open_upnl_pct_of_nl",
    "names",
    "lots",
    "structures",
    "by_name",
    "open_lots",
    "mix",
    "capacity",
    "max_risk_per_trade_pct",
    "lot_lasts",
    "working_exits",
    "halt_trips_at_usd",
    "halt_trips_at_pct_of_nl",
    "ibkr_day_vs_halt",
    "ibkr_day_vs_halt_pct_of_nl",
    "clerk_halted",
    "sizing_floors",
    "portfolio_risk",
    "exposure",
    "capital_liquidity",
    "minutes_to_open",
    "countdown_to",
    "countdown_human",
    "tradable_now",
    "defined_risk_concentration",
    "stop_dist",
    "working_order_missing",
    "session_cap",
)


def _open_book_names(world: WorldState) -> list[str]:
    """Symbols on the live blotter — lots and working orders, not a scan tape."""
    out: list[str] = []
    for row in list(getattr(world, "positions", None) or []) + list(
        getattr(world, "open_orders", None) or []
    ):
        if not isinstance(row, dict):
            continue
        name = str(row.get("symbol") or "").upper().strip()
        if name and name not in out:
            out.append(name)
    return out


def _open_book_marks(world: WorldState) -> dict[str, float]:
    """IBKR lasts for open-book names only. Scan leftovers stay off the blotter."""
    qmap = dict(getattr(world, "ibkr_live_quotes", None) or {})
    marks: dict[str, float] = {}
    for name in _open_book_names(world):
        raw = qmap.get(name)
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if px > 0:
            marks[name] = px
    return marks


_BLOTTER_KEEP_NONE = frozenset(
    {"nl", "daily_pnl", "ibkr_daily_pnl", "open_upnl", "daily_pnl_pct"}
)


def _blotter_day(day: dict[str, Any] | None) -> dict[str, Any]:
    """Day facts that are the book. Score windows and playbooks stay off."""
    src = day if isinstance(day, dict) else {}
    out: dict[str, Any] = {}
    for key in _BLOTTER_DAY_KEYS:
        if key not in src:
            continue
        val = src[key]
        if val in ("", {}, []):
            continue
        if val is None and key not in _BLOTTER_KEEP_NONE:
            continue
        out[key] = val
    return out


def _book_facts(world: WorldState) -> dict[str, Any]:
    from abcxauto.world_state import (
        compact_position,
        compact_working_orders,
        open_upnl_of,
    )

    unreliable = bool((world.gates or {}).get("book_unreliable")) or bool(
        getattr(world, "book_unreliable", False)
    )
    facts: dict[str, Any] = {
        "source": "ibkr",
        "freshness": "live",
        "session": world.session_status,
        "flat": world.flat,
        "needs_protection": world.needs_protection,
        "unprotected": list(world.unprotected or []),
        "net_liquidation": world.net_liquidation,
        "daily_pnl": world.daily_pnl,
        "open_upnl": open_upnl_of(world.positions),
        "quote_source": "IBKR live",
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
    }
    cap = dict(world.capacity or {})
    if cap:
        facts["capacity"] = cap
    marks = _open_book_marks(world)
    if marks:
        facts["marks"] = marks
    sq = getattr(world, "stop_qty_fact", None)
    if isinstance(sq, dict) and sq:
        facts["stop_qty_fact"] = sq
    if unreliable:
        facts["book_unreliable"] = True
    return facts


def _mark_incomplete_book(
    out: dict[str, Any],
    world: WorldState,
    snap: dict[str, Any] | None,
) -> None:
    """Honest markers when the backing snap was incomplete or NL is unknown."""
    lost: list[str] = []
    src = None
    incomplete = False
    if isinstance(snap, dict):
        lost = [str(x) for x in (snap.get("snap_lost") or []) if str(x)]
        src = snap.get("nl_source")
        incomplete = bool(lost) or bool(snap.get("snap_incomplete")) or bool(
            snap.get("book_unreliable")
        )
    if incomplete:
        out["snap_incomplete"] = True
        if lost:
            out["snap_lost"] = lost
    if src:
        out["nl_source"] = src
    world_facts = out.get("world") if isinstance(out.get("world"), dict) else None
    day = out.get("day") if isinstance(out.get("day"), dict) else None
    if src and world_facts is not None:
        world_facts["nl_source"] = src
    if src and day is not None:
        day["nl_source"] = src
    nl = getattr(world, "net_liquidation", None)
    if nl is None:
        if world_facts is not None:
            world_facts["net_liquidation"] = None
            world_facts["nl_unavailable"] = True
        if day is not None:
            day["nl"] = None
            day["nl_unavailable"] = True
        acct_live = src == "account_summary"
        if not acct_live:
            if world_facts is not None:
                world_facts["cash_unavailable"] = True
            if day is not None:
                day["cash_unavailable"] = True
                cl = day.get("capital_liquidity")
                if isinstance(cl, dict) and cl.get("total_cash") in (0, 0.0, None):
                    day["capital_liquidity"] = dict(cl, total_cash=None)


def _book_payload(
    world: WorldState,
    tool_trace: list[str] | None = None,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Live blotter. Tape, notes, levers, and last look stay tools."""
    from abcxauto.world_state import day_facts

    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
    except Exception:
        sc = {}
    facts = _book_facts(world)
    day = _blotter_day(day_facts(world, sc))
    out: dict[str, Any] = {
        "day": day,
        "world": facts,
    }
    try:
        cash = None
        cap = day.get("capital_liquidity") if isinstance(day.get("capital_liquidity"), dict) else {}
        if isinstance(cap, dict) and cap.get("total_cash") is not None:
            cash = cap.get("total_cash")
        if cash is None:
            port = getattr(world, "portfolio_risk", None) or {}
            inner = port.get("capital_liquidity") if isinstance(port, dict) else {}
            if isinstance(inner, dict) and inner.get("total_cash") is not None:
                cash = inner.get("total_cash")
        from abcxauto.world_state import allocation_facts, allocation_line

        alloc = allocation_facts(
            list(getattr(world, "positions", None) or []),
            net_liq=getattr(world, "net_liquidation", None),
            total_cash=cash,
            quotes=getattr(world, "ibkr_live_quotes", None),
            orders=list(getattr(world, "open_orders", None) or []),
        )
        if alloc.get("lots") or alloc.get("cash_pct_nl") is not None:
            out["allocation"] = alloc
            line = allocation_line(alloc)
            if line:
                out["allocation_line"] = line
        from abcxauto.world_state import range_compare_line, to_high_line

        bag = snap if isinstance(snap, dict) else {}
        range_line = range_compare_line(bag.get("session_range"))
        if range_line:
            out["range_line"] = range_line
        high_line = to_high_line(bag.get("session_range"))
        if high_line:
            out["to_high_line"] = high_line
    except Exception:
        logger.debug("book allocation facts failed", exc_info=True)
    marks = facts.get("marks")
    if isinstance(marks, dict) and marks:
        out["marks"] = marks
    _mark_incomplete_book(out, world, snap)
    _ = tool_trace
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


def _should_bill_research_round(turn: BrainTurn, *, tool_calls: int = 0) -> bool:
    """False for a failed / empty / junk round — those are not a look."""
    if turn.failed or turn.ended or turn.parked:
        return False
    try:
        n = int(tool_calls or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return True
    return not _look_is_empty_or_question(turn)


def _bill_research_brief_round(
    turn: BrainTurn,
    *,
    session: str,
    snap: dict[str, Any] | None,
    tool_calls: int = 0,
) -> bool:
    """Count one billed research look. True when the card is now halted.

    Research-session only — RTH looks are not billed on the AH card.
    """
    if getattr(turn, "brief_billed", False):
        return bool(getattr(turn, "brief_loop_halted", False))
    if not _should_bill_research_round(turn, tool_calls=tool_calls):
        return False
    try:
        from abcxauto.desk_mode import is_research_session, is_rth_session
        from abcxauto.research_budget import note_brief_turn, resolve_research_card

        if not is_research_session(session) or is_rth_session(session):
            return False
        card, window = resolve_research_card(snap=snap)
        out = note_brief_turn(card, window, tool_calls=tool_calls)
        turn.brief_billed = bool(out.get("billed"))
        if out.get("brief_loop_halted"):
            # Card is full. The look already ran; do not sit the next one.
            turn.brief_loop_halted = True
            return bool(out.get("billed"))
    except Exception:
        logger.debug("research brief bill failed", exc_info=True)
    return False


async def grok_turn(
    g: GrokClient,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    wake: str,
    resume: bool = False,
    recover: bool = False,
) -> BrainTurn:
    """Call the model on one kept chat. send() is the only broker path.

    ``resume`` is optional so older grok_turn mocks keep working. A live
    chat is this look even when ``resume`` is false. Tool_calls execute
    and the model is called again with those results on this chat. Words
    with no tool_calls: stop calling the model. Chat kept. Do not call
    the model again because it spoke. A poke does not start a new
    messages list. A fresh BrainTurn still drops refused send tickets so
    they cannot be the next look's send target.     ``recover`` re-enters
    stream_round on the open chat — no wake re-intro, no new messages list.
    """
    try:
        from abcxauto.look_meter import look_meter_scope
    except Exception:
        logger.exception("look_meter import failed")
        return await _grok_turn_impl(
            g,
            connector=connector,
            world=world,
            snap=snap,
            wake=wake,
            turn=BrainTurn(),
            resume=resume,
            recover=recover,
        )

    model_id = str(getattr(g, "model", "") or "")
    with look_meter_scope(world=world, snap=snap, model=model_id) as meter:
        turn = await _grok_turn_impl(
            g,
            connector=connector,
            world=world,
            snap=snap,
            wake=wake,
            turn=BrainTurn(),
            resume=resume,
            recover=recover,
        )
        if meter is not None:
            try:
                meter.note_tools(list(turn.tool_trace or []))
            except Exception:
                logger.debug("look_meter tool_trace stamp failed", exc_info=True)
        return turn


def grok_turn_kwargs(
    fn: Any,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    wake: str,
    resume: bool = False,
    recover: bool = False,
) -> dict[str, Any]:
    """Keyword args for grok_turn. Omit resume/recover when the callee lacks them."""
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
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    if "resume" in params or var_kw:
        kwargs["resume"] = resume
    if "recover" in params or var_kw:
        kwargs["recover"] = recover
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
    from abcxauto.brain_tools import CANDLE_WAIT_S, CHAIN_WAIT_S, SCAN_S

    timeout = SEND_S if name in _MUTATING_TOOLS else TOOL_S
    if name == "option_chain":
        timeout = CHAIN_WAIT_S
    if name == "candles":
        timeout = CANDLE_WAIT_S
    if name == "scan":
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
    logger.info("tool start %s", name)
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
        # Do not await the cancelled task. A quote/news call that ignores
        # cancel used to sit here forever after the timeout, with the
        # window stuck on the name chips.
        droppable = name not in _MUTATING_TOOLS
        while True:
            if droppable and peek_interrupt() is not None:
                tool_task.cancel()
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
                raise asyncio.TimeoutError()
            done, _ = await asyncio.wait({tool_task}, timeout=min(0.25, remaining))
            if tool_task in done:
                exc = tool_task.exception()
                if exc is not None:
                    raise exc
                result = str(tool_task.result())
                try:
                    from abcxauto.desk_mode import (
                        RESEARCH_TOOLS,
                        note_research_tool,
                        write_research_brief,
                    )

                    if name in RESEARCH_TOOLS:
                        note_research_tool(snap, name, result, args=args)
                        sess = str(getattr(world, "session_status", "") or "")
                        write_research_brief(
                            session=sess, snap=snap, turn=turn, world=world
                        )
                except Exception:
                    logger.debug("research fact note failed", exc_info=True)
                return result
    except asyncio.TimeoutError:
        logger.warning("tool %s timed out after %.0fs", name, timeout)
        return json.dumps({"error": f"{name} timed out", "timeout_s": timeout})
    except Exception as exc:
        logger.exception("tool %s failed", name)
        return json.dumps({"error": f"{name} failed: {exc}"})


def _tool_call_args_text(tc: Any) -> str:
    """Raw args the model sent with this tool call. Empty ``{}`` is not worth keeping."""
    fn = getattr(tc, "function", None)
    raw = getattr(fn, "arguments", None) if fn is not None else None
    if isinstance(raw, dict):
        if not raw:
            return ""
        try:
            raw = json.dumps(raw, default=str)
        except (TypeError, ValueError):
            return ""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if text in ("", "{}", "null", "[]"):
        return ""
    return text


def _emit_paid_look(tc: Any, result: str) -> None:
    """Same string ``chat.append`` paid for, via emit() into the ET day file.

    Args when the tool call already has them. Glass/RAM still need Pro;
    the day file does not.
    """
    args_text = _tool_call_args_text(tc)
    if args_text:
        think_emit("tool", args_text if args_text.endswith("\n") else f"{args_text}\n")
    paid = str(result or "")
    if paid:
        think_emit("tool", paid if paid.endswith("\n") else f"{paid}\n")


# Fat candles/scan/web *error* blobs on chat.append can void the next
# sample (input=0 stop=void). Glass already slims for the window; bound
# what the model chat pays for while keeping a usable fact line.
TOOL_ERROR_CHAT_CHARS = 1_500
_BOUND_ERROR_TOOLS = frozenset({"candles", "scan", "web"})


def _bound_tool_chat_result(name: str, result: str) -> str:
    """Trim candles/scan/web errors for chat.append. Success paths unchanged."""
    tool = str(name or "").strip()
    raw = str(result or "")
    if tool not in _BOUND_ERROR_TOOLS:
        return raw
    data: Any = None
    try:
        data = json.loads(raw) if raw.strip() else None
    except (TypeError, json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        is_err = bool(data.get("error")) or data.get("ok") is False
        if not is_err:
            return raw
        slim: dict[str, Any] = {"_clipped": "error"}
        for key in (
            "symbol",
            "symbols",
            "resolution",
            "requested_resolution",
            "source",
            "ok",
            "last",
            "query",
            "url",
            "arena",
            "scan_code",
            "freshness",
        ):
            if key in data and data[key] is not None:
                slim[key] = data[key]
        err = (
            data.get("error")
            or data.get("hist_error")
            or data.get("rt_error")
            or "error"
        )
        slim["error"] = str(err).split("\n", 1)[0].strip()[:400] or "error"
        bars = data.get("bars")
        if isinstance(bars, list) and bars:
            last_bar = bars[-1]
            if isinstance(last_bar, dict):
                kept = _think_bar(last_bar)
                slim["last_bar"] = kept if kept is not None else {
                    k: last_bar[k]
                    for k in ("t", "o", "h", "l", "c", "v")
                    if k in last_bar
                }
        series = data.get("series")
        if "last_bar" not in slim and isinstance(series, list):
            for row in reversed(series):
                if not isinstance(row, dict):
                    continue
                row_bars = row.get("bars")
                if isinstance(row_bars, list) and row_bars and isinstance(
                    row_bars[-1], dict
                ):
                    kept = _think_bar(row_bars[-1])
                    if kept is not None:
                        slim["last_bar"] = kept
                    if row.get("symbol") and "symbol" not in slim:
                        slim["symbol"] = row.get("symbol")
                    if row.get("resolution") and "resolution" not in slim:
                        slim["resolution"] = row.get("resolution")
                    break
                if row.get("error") and slim.get("error") == "error":
                    slim["error"] = str(row.get("error")).split("\n", 1)[0][:400]
                    if row.get("symbol"):
                        slim["symbol"] = row.get("symbol")
                    break
        out = json.dumps(slim, default=str)
        if len(out) <= TOOL_ERROR_CHAT_CHARS:
            return out
        tiny = {
            "error": slim.get("error") or "error",
            "tool": tool,
            "_clipped": "error",
        }
        if slim.get("symbol") is not None:
            tiny["symbol"] = slim["symbol"]
        if slim.get("resolution") is not None:
            tiny["resolution"] = slim["resolution"]
        if slim.get("last_bar") is not None:
            tiny["last_bar"] = slim["last_bar"]
        elif slim.get("last") is not None:
            tiny["last"] = slim["last"]
        return json.dumps(tiny, default=str)
    if len(raw) <= TOOL_ERROR_CHAT_CHARS:
        return raw
    return json.dumps(
        {
            "error": raw.split("\n", 1)[0].strip()[:400] or "error",
            "tool": tool,
            "_clipped": "error",
        },
        default=str,
    )


def _append_tool_result(chat: Any, tc: Any, result: str) -> None:
    fn = getattr(tc, "function", None)
    name = str(getattr(fn, "name", None) or getattr(tc, "name", "") or "")
    paid = _bound_tool_chat_result(name, str(result or ""))
    try:
        chat.append(tool_result(paid, tool_call_id=getattr(tc, "id", None)))
    except TypeError:
        chat.append(tool_result(paid))
    _emit_paid_look(tc, paid)
    try:
        from abcxauto.look_meter import note_tool_result

        note_tool_result(name, paid)
    except Exception:
        logger.exception("look_meter note_tool_result failed")


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
        if name == "scan" and data.get("ok") is not False:
            stub = _scan_reuse_stub(cached=data)
            return json.dumps(stub, default=str)
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
    """Read tools in parallel; send stays serial and after facts.

    A book event mid-message defers the reads, never the writes. A read is
    stale the moment the book moves and Grok has to ask again anyway; the send
    carries geometry Grok already decided and cannot be reconstructed. Every
    tool_call_id still gets a result — a missing one makes the next round
    malformed.

    Returns True when a live poke is waiting for the think.
    """
    from abcxauto.park_clock import peek_interrupt

    parsed = []
    for tc in calls:
        try:
            parsed.append(_parse_tool_call(tc, world=world, snap=snap))
        except Exception:
            logger.exception("tool parse failed")
            fn = getattr(tc, "function", None)
            raw = getattr(fn, "arguments", None) or "{}"
            try:
                kept = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except (TypeError, json.JSONDecodeError, ValueError):
                kept = {}
            if not isinstance(kept, dict):
                kept = {}
            parsed.append((str(getattr(fn, "name", None) or "?"), kept, tc, TOOL_S))
    reads = [p for p in parsed if p[0] not in _MUTATING_TOOLS]
    writes = [p for p in parsed if p[0] in _MUTATING_TOOLS]
    logger.info("dispatch %s", ",".join(p[0] for p in parsed))

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
            gathered = await asyncio.gather(
                *[_one(item) for item in reads],
                return_exceptions=True,
            )
            for item, row in zip(reads, gathered):
                if isinstance(row, Exception):
                    logger.exception("parallel tool failed")
                    _append_tool_result(
                        chat, item[2], json.dumps({"error": f"{item[0]} failed: {row}"})
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
        try:
            from abcxauto.look_snapshot import begin_look

            begin_look(snap)
        except Exception:
            logger.debug("look snapshot reset on send failed", exc_info=True)
        _clear_look_tool_stash(chat)
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
    recover: bool = False,
) -> BrainTurn:
    turn = turn or BrainTurn()
    # Rejected clerk tickets must not ride to the next look.
    drop_refused_send_targets(turn)
    live_chat = getattr(g, "chat", None) if g is not None else None
    try:
        _begin_look_for_turn(
            snap, same_look=live_chat is not None, holder=live_chat or g
        )
    except Exception:
        logger.debug("look snapshot begin failed", exc_info=True)
    if g is None:
        turn.last_act = {}
        turn.last_result = {"status": "error", "note": "no_grok_client"}
        turn.failed = True
        return turn
    session = str(getattr(world, "session_status", "") or "")
    kill_mode = ""
    turn_cap = MAX_TOOL_STEPS
    in_flight = bool(
        snap.get("kill_entry_in_flight")
        or getattr(world, "kill_entry_in_flight", False)
    )
    try:
        from abcxauto.thin_rth_kill_look import kill_mode as _kill_mode

        kill_mode = _kill_mode(
            session,
            positions=list(getattr(world, "positions", None) or snap.get("positions") or []),
            open_lots=list(getattr(world, "open_lots", None) or []),
            in_flight=in_flight,
        )
        turn.kill_mode = kill_mode
    except Exception:
        logger.debug("kill-look mode stamp failed", exc_info=True)
    live_before = getattr(g, "chat", None)
    # Dollar F10 / model-cost fuse refuses new-risk sends; it must not skip
    # the look or set turn.loop_halted. Latch stays in session_caps.
    # A live chat is this look. A poke does not start a new messages list.
    resume = bool(resume) or live_before is not None
    recover = bool(recover) and live_before is not None
    if recover:
        # Re-enter stream_round on the paid chat. No wake re-intro.
        chat = live_before
        g._wake_appended = False
    else:
        try:
            chat = _open_wake(g, wake, session=session, resume=resume, snap=snap)
        except Exception as exc:
            logger.exception("chat start failed")
            turn.last_act = {}
            turn.last_result = {"status": "error", "note": f"chat_error: {exc}"}
            turn.failed = True
            turn.stream_error = str(exc)
            _finish_look_chat(g, turn, session=session)
            return turn
        appended = bool(getattr(g, "_wake_appended", False))
        lead = str(wake or "").splitlines()[0].strip() if wake else ""
        if appended and lead:
            think_emit("tool", f"{lead}\n")
        if live_before is not None and appended:
            # Desk-fact inject on a kept chat. Empty GROK after this is hung.
            turn.poked = True
            try:
                g._chat_had_work = True
            except Exception:
                logger.debug("chat work stamp failed", exc_info=True)
        work_resume = bool(getattr(g, "_work_resume", False))
        if work_resume:
            try:
                g._work_resume = False
            except Exception:
                logger.debug("work resume clear failed", exc_info=True)
        if resume and not appended:
            from abcxauto.park_clock import peek_interrupt

            if peek_interrupt() is not None:
                ok = await _inject_live_poke(
                    chat, connector=connector, world=world, snap=snap, turn=turn
                )
                if not ok:
                    # Duplicate lead fact — a look may end with no send.
                    turn.ended = True
                    _finish_look_chat(g, turn, session=session)
                    return turn
                turn.poked = True
                try:
                    g._chat_had_work = True
                except Exception:
                    logger.debug("chat work stamp failed", exc_info=True)
            elif work_resume:
                # Same lead, kept chat — call the model with no extra
                # developer text. A harness sentence becomes permanent
                # history under store_messages and the model mocks it.
                logger.info("work resume, same lead, calling the model")
            else:
                # Duplicate lead-fact identity. Do not start a fresh go-do-desk.
                turn.ended = True
                _finish_look_chat(g, turn, session=session)
                return turn
    billed_before = _model_usage_calls()
    ran_out = True
    abort_tries = 0
    empty_tries = 0
    while turn.steps < turn_cap:
        turn.steps += 1
        try:
            from abcxauto.park_clock import peek_interrupt

            if peek_interrupt() is not None:
                ok = await _inject_live_poke(
                    chat, connector=connector, world=world, snap=snap, turn=turn
                )
                if ok:
                    turn.poked = True
                    try:
                        g._chat_had_work = True
                    except Exception:
                        logger.debug("chat work stamp failed", exc_info=True)
                continue
            text, response, stop = await stream_round(chat)
        except Exception as exc:
            if (
                is_stream_abort_error(exc)
                and abort_tries < STREAM_ABORT_TRIES
            ):
                abort_tries += 1
                logger.warning(
                    "stream abort retry %s/%s same chat: %s",
                    abort_tries,
                    STREAM_ABORT_TRIES,
                    exc,
                )
                think_emit("tool", f"\n[stream retry: {exc}]\n")
                turn.stream_error = ""
                turn.failed = False
                await asyncio.sleep(STREAM_ABORT_BACKOFF_S)
                continue
            # A dead empty stream ends the look. A look that already spoke
            # or sent still keeps the stay-up chat.
            logger.exception("stream_round failed")
            think_emit("tool", f"\n[stream failed: {exc}]\n")
            turn.failed = True
            turn.stream_error = str(exc)
            ran_out = False
            break
        # Keep every spoken chunk, including a later empty / interrupt /
        # repeat-text stop. Junk is the whole look, not the last assistant turn.
        if text:
            if _look_text_is_junk(turn.text):
                turn.text = text.strip()
            else:
                turn.text = (turn.text + "\n" + text).strip()
        if stop == "interrupt":
            ok = await _inject_live_poke(
                chat, connector=connector, world=world, snap=snap, turn=turn
            )
            if ok:
                turn.poked = True
            continue
        if stop == "loop":
            ran_out = False
            break
        if response is not None:
            try:
                chat.append(response)
            except Exception:
                logger.debug("chat.append(response) failed", exc_info=True)
            else:
                _persist_response_id(response)
        calls = list(getattr(response, "tool_calls", None) or []) if response is not None else []
        # Paint the names before any tool work. A hang inside dispatch
        # used to leave the window on the say.
        called = []
        for tc in calls:
            fn = getattr(tc, "function", None)
            called.append(str(getattr(fn, "name", None) or "?"))
        if called:
            think_emit("tool", "\n" + " ".join(f"[{n}]" for n in called) + "\n")
        logger.info("grok round back tools=%s %s", len(calls), ",".join(called))
        if not calls:
            # Empty GROK after any completed tool is hung, not a checkpoint
            # and not a completed look. #147 keyed this on
            # _look_is_empty_or_question (False after send). #148 gated on
            # stop==empty (no think AND no say), so think-only after
            # option_quote / quote / book sat. #153 gated on this-round
            # tools, so a poke / fact inject on a kept chat sat idle.
            # Gate on THIS round: no say, no tool. [think] is not a
            # checkpoint. A [say] this round sits. stop==empty is not
            # required — wall-clock silent / stall / junk tip count.
            if stop == "think_only":
                turn.trailing_think_only = True
                turn.trailing_empty_grok = False
                turn.skip_identical_retry = True
                logger.info("think-only GROK — completed, no recover")
                ran_out = False
                break
            empty_after_work = _empty_grok_round_after_work(
                turn,
                text,
                stop,
                chat_had_work=bool(getattr(g, "_chat_had_work", False)),
                live_chat=live_before is not None,
                poked=bool(getattr(turn, "poked", False)),
            )
            # Zero-token void sample: stream returned no chunks (response is
            # None, finish_reason=-). After tools/send/poke, never stamp
            # skip_identical_retry — that sets _recover_gave_up and drops
            # the chat cold, wiping the paid tool trace. Spoken look: keep
            # the say and sit (no trailing_empty_grok). Tools-only empty:
            # trailing_empty_grok so pro_engine same-chat recovers once
            # (EMPTY_GROK_RECOVER_TRIES), then may sit/drop.
            look_spoke = not _look_text_is_junk(turn.text)
            void_stream = response is None and stop == "empty"
            if empty_after_work and look_spoke:
                turn.trailing_empty_grok = False
                turn.skip_identical_retry = False
                if void_stream:
                    logger.warning(
                        "void GROK after tools — keep spoken look, no chat wipe"
                    )
            elif empty_after_work:
                turn.trailing_empty_grok = True
                turn.skip_identical_retry = False
                logger.warning(
                    "empty GROK after tools/send/poke — same-chat recover, keep paid tools"
                )
            else:
                turn.trailing_empty_grok = False
            # Words (or empty) and no tools: stop calling the model. Chat
            # stays. Next call is fill / order_change / unprotected / poke
            # with this chat plus a fresh snap. Do not call again because it spoke.
            # AH-card billing is one debit at the end of this grok_turn.
            ran_out = False
            break
        turn.trailing_empty_grok = False
        turn.trailing_think_only = False
        interrupted = await _dispatch_tool_calls(
            calls,
            chat=chat,
            connector=connector,
            world=world,
            snap=snap,
            turn=turn,
        )
        if turn.tool_trace or turn.sends or turn.poked:
            try:
                g._chat_had_work = True
            except Exception:
                logger.debug("chat work stamp failed", exc_info=True)
        if interrupted:
            ok = await _inject_live_poke(
                chat, connector=connector, world=world, snap=snap, turn=turn
            )
            if ok:
                turn.poked = True
                try:
                    g._chat_had_work = True
                except Exception:
                    logger.debug("chat work stamp failed", exc_info=True)
    if ran_out:
        turn.tool_budget_hit = True
        think_emit("tool", "\n[think stopped: step ceiling]\n")
    if (
        not turn.ended
        and not turn.parked
        and not turn.failed
        and _look_is_empty_or_question(turn)
    ):
        # Idle in this chat. Do not stamp failed — that cold-restarts.
        logger.warning("look idle: empty or junk assistant text")
    _bill_spoken_look_if_unbilled(turn, before_calls=billed_before)
    _bill_research_brief_round(
        turn,
        session=session,
        snap=snap,
        tool_calls=len(turn.tool_trace),
    )
    try:
        from abcxauto.desk_mode import write_research_brief

        write_research_brief(
            session=session, snap=snap, turn=turn, world=world
        )
    except Exception:
        logger.debug("research brief write failed", exc_info=True)
    _stash_look_tool_bag(getattr(g, "chat", None), snap)
    _stash_look_tool_bag(g, snap)
    _finish_look_chat(g, turn, session=session)
    if not turn.sends:
        if str(turn.last_strat or "").lower() == "hold":
            turn.last_strat = ""
        if str((turn.last_act or {}).get("strategy") or "").lower() == "hold":
            turn.last_act = {}
            turn.last_result = {}
    return turn
