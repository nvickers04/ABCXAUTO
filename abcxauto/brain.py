"""Grok owns the book via tools. The shell is facts + send gates.

Paper RTH / premarket stay-up continues the live chat across successful
looks. Overnight / after-close / park drop it. Empty-junk retries once
in the same chat, then sits.
Tickets go through ``execute_ticket`` → ``send_action``. IBKR tools are
live. scan() is one tape this look (merged hits + on_book); candles
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
from abcxauto.brain_tools import *  # noqa: F401,F403

logger = logging.getLogger(__name__)

_MUTATING_TOOLS = frozenset(
    {"send", "self_tune", "write_lab_playbook", "write_desk_lessons"}
)
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
    lab_playbook: dict[str, Any] | None = None
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

    def look_failed(self) -> bool:
        """True empty / lone '?' only. A real say or send/fill is not junk.

        A later empty assistant chunk, a leftover ``failed`` stamp, or a
        dead stream after a spoken/send look must not wipe the stay-up chat.
        """
        if self.parked or self.ended:
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


async def stream_round(
    chat: Any, *, stage: str = "grok", emit_stage: bool = True
) -> tuple[str, Any, str]:
    """Stream one model step. Returns (assistant text, response, stop_reason)."""
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
    if emit_stage:
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
    g._wake_appended = False
    g._last_desk_fact = ""


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


def _finish_look_chat(g: GrokClient, turn: BrainTurn, *, session: str) -> None:
    """Keep the live chat on paper stay-up, including empty / '?' idle.

    Park and overnight drop it so the next think is a cold start. A
    ``failed`` / dead-stream stamp on a real say or send/fill is not a
    drop. An ended look (duplicate lead fact) keeps the chat — a look
    may end with no send. Stay-up junk retries in this chat, then sits.
    """
    if turn.ended:
        return
    if turn.parked:
        _reset_chat(g)
        return
    try:
        from abcxauto.park_clock import paper_stay_up

        if paper_stay_up(_stay_up_session_label(session)):
            return
    except Exception:
        logger.debug("stay-up chat keep check failed", exc_info=True)
    if _look_is_empty_or_question(turn):
        _reset_chat(g)
        return
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
    g._wake_appended = False
    live = None if reset else getattr(g, "chat", None)
    if resume and live is not None:
        pending = False
        try:
            from abcxauto.park_clock import peek_interrupt

            pending = peek_interrupt() is not None
        except Exception:
            pending = False
        if not pending:
            from abcxauto.world_state import desk_fact_is_duplicate

            prev = _chat_last_desk_fact(g, live)
            if desk_fact_is_duplicate(prev, wake):
                # Same lead-fact identity (set / list / tick). A look may
                # end — do not append a fresh go-do-desk developer turn.
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
        from abcxauto.world_state import omit_duplicate_fact_lead

        to_append = omit_duplicate_fact_lead(_chat_last_desk_fact(None, chat), poke)
        if to_append:
            chat.append(developer(to_append))
        _remember_desk_fact(None, chat, poke)
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
        # First look may pull the flush trio (3 IBKR subs) under one call.
        timeout = SCAN_S * 3
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


def _append_tool_result(chat: Any, tc: Any, result: str) -> None:
    try:
        chat.append(tool_result(result, tool_call_id=getattr(tc, "id", None)))
    except TypeError:
        chat.append(tool_result(result))
    _emit_paid_look(tc, result)


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
    appended = bool(getattr(g, "_wake_appended", False))
    lead = str(wake or "").splitlines()[0].strip() if wake else ""
    if appended and lead:
        think_emit("tool", f"{lead}\n")
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
        else:
            # Duplicate lead-fact identity. Do not start a fresh go-do-desk.
            turn.ended = True
            _finish_look_chat(g, turn, session=session)
            return turn
    ran_out = True
    silent_round = False
    junk_retried = False
    while turn.steps < MAX_TOOL_STEPS:
        turn.steps += 1
        try:
            from abcxauto.park_clock import peek_interrupt

            if peek_interrupt() is not None:
                await _inject_live_poke(
                    chat, connector=connector, world=world, snap=snap, turn=turn
                )
                continue
            text, response, stop = await stream_round(
                chat, emit_stage=not silent_round
            )
            silent_round = False
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
            if _look_text_is_junk(turn.text):
                turn.text = text.strip()
            else:
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
            if (
                not junk_retried
                and not turn.parked
                and not turn.ended
                and _look_is_empty_or_question(turn)
            ):
                # Same chat, once. No new --- GROK --- re-intro.
                junk_retried = True
                silent_round = True
                continue
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
    if (
        not turn.ended
        and not turn.parked
        and not turn.failed
        and _look_is_empty_or_question(turn)
    ):
        # Idle in this chat. Do not stamp failed — that cold-restarts.
        logger.warning("look idle: empty or junk assistant text")
    _finish_look_chat(g, turn, session=session)
    if not turn.sends:
        if str(turn.last_strat or "").lower() == "hold":
            turn.last_strat = ""
        if str((turn.last_act or {}).get("strategy") or "").lower() == "hold":
            turn.last_act = {}
            turn.last_result = {}
    return turn
