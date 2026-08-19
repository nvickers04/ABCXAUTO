"""Grok owns the book via tools. The shell is facts + send clerk.

One wake = one streamed Grok turn with tools. Tickets go through
``execute_ticket`` → ``send_action``. IBKR tools are live. MDA scan is
daily-bar structure; candles are IBKR hist or the live 5s stream (error if both miss); news is
~15 min delayed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from xai_sdk.chat import developer, system, tool, tool_result, user

from abcxauto.llm import GrokClient, build_system_prompt
from abcxauto.opportunity_scan import (
    fetch_scan_metrics,
    mda_bar_freshness,
    mda_last_kind,
    normalize_tickers,
)
from abcxauto.order_examples import format_order_examples, ticket_strategy_names
from abcxauto.think_stream import emit as think_emit
from abcxauto.tools import run_readonly_tool
from abcxauto.tool_args import (
    CANDLE_CAP,
    CHAIN_CAP,
    OPTION_QUOTE_CAP,
    fallback_quote_symbols,
    normalize_tool_call,
    option_quote_specs,
    strip_ambiguous_last,
)
from abcxauto.world_state import WorldState

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 24
_MUTATING_TOOLS = frozenset({"send", "self_tune", "write_lab_playbook"})
STREAM_CHUNK_S = 8.0
STREAM_IDLE_LIMIT = 6
STREAM_LOOP_UNIT = 12
STREAM_LOOP_COPIES = 6
STREAM_LOOP_SENTENCE_COPIES = 3
TOOL_S = 20.0
SEND_S = 45.0
CHAIN_S = 60.0
CANDLE_S = 35.0
_QUOTE_SCHEMA = {"type": "string", "description": "Ticker, e.g. AAPL"}
_SYMBOLS_SCHEMA = {"type": "array", "items": {"type": "string"}}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


AGENT_TOOLS = [
    tool(
        name="book",
        description="Live IBKR book: positions, working orders, protection, tape, scorecard.",
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
        description="MDA headlines (~15 min delayed). Context only, not live last.",
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
        description="MDA daily-bar tape metrics. mda_last is daily close, not a 15m last.",
        parameters=_schema({"symbols": _SYMBOLS_SCHEMA}, ["symbols"]),
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
    tool(
        name="send",
        description=(
            "One IBKR ticket per call. Call send again this turn for another ticket. "
            "strategy name + fields match ORDER EXAMPLES. Knobs are self_tune, not a ticket. "
            "Hard risk is code."
        ),
        parameters=_schema(
            {
                "strategy": {
                    "type": "string",
                    "enum": ticket_strategy_names(),
                    "description": "Ticket name from ORDER EXAMPLES.",
                },
                "symbol": _QUOTE_SCHEMA,
                "quantity": {"type": "number"},
                "size_pct_nl": {
                    "type": "number",
                    "description": "Optional size as % of NetLiq next to quantity. Qty stays on the wire.",
                },
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
                "rationale": {"type": "string"},
            },
            ["strategy"],
        ),
    ),
    tool(
        name="self_tune",
        description=(
            "Retune knobs now. Floor cannot be weakened. Not a ticket — send is the book."
        ),
        parameters=_schema(
            {
                "max_risk_per_trade_pct": {"type": "number"},
                "daily_loss_limit_pct": {"type": "number"},
                "max_position_pct": {"type": "number"},
                "max_peak_drawdown_pct": {"type": "number"},
                "max_option_premium_pct": {"type": "number"},
                "max_open_positions": {"type": "integer"},
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
            "Paper only: replace your notes with whatever next-look-you should have. "
            "Optional mode / ready_to_promote. Book, quote, and gates are other tools."
        ),
        parameters=_schema(
            {
                "instructions": {
                    "type": "string",
                    "description": "Your notebook. Replaces the previous notes.",
                },
                "mode": {"type": "string", "description": "explore or exploit"},
                "ready_to_promote": {"type": "boolean"},
            },
            [],
        ),
    ),
    tool(
        name="set_wake",
        description=(
            "Next look. Always set a clock; a fill, order change, or material "
            "mark move can come sooner. Empty wake_if = any book event. "
            "If you skip this, the clerk looks again soon. "
            "Unprotected stock still interrupts."
        ),
        parameters=_schema(
            {
                "wake_in_s": {"type": "number"},
                "wake_at": {"type": "string"},
                "wake_if": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            [],
        ),
    ),
]


def brain_system_prompt() -> str:
    from abcxauto.agent_loop import AWARENESS_HEART

    return (
        build_system_prompt()
        + AWARENESS_HEART
        + "\n"
        + format_order_examples()
        + "\nsend is the only way to change the book."
    )


@dataclass
class BrainTurn:
    text: str = ""
    sends: list[dict[str, Any]] = field(default_factory=list)
    last_act: dict[str, Any] = field(default_factory=dict)
    last_result: dict[str, Any] = field(default_factory=dict)
    last_strat: str = "hold"
    tool_trace: list[str] = field(default_factory=list)
    lab_playbook: dict[str, Any] | None = None
    tool_budget_hit: bool = False


def _clip(data: Any, max_chars: int = 24_000) -> str:
    text = json.dumps(data, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + "... [truncated]"
    return text


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
            resp, ch = await asyncio.wait_for(anext(agen), timeout=STREAM_CHUNK_S)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            idle += 1
            think_emit("say", "…")
            if idle >= STREAM_IDLE_LIMIT:
                think_emit("say", "\n[stream stalled]\n")
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
            think_emit("say", "\n[stream loop]\n")
            reason = "loop"
            break
    try:
        fr = ""
        if last_ch is not None:
            choices = list(getattr(last_ch, "choices", None) or [])
            raw_fr = getattr(choices[0], "finish_reason", None) if choices else None
            fr = str(getattr(raw_fr, "name", None) or raw_fr or "")
        if "LENGTH" in fr.upper() or "MAX_TOKEN" in fr.upper():
            think_emit("say", "\n[truncated: max_tokens]\n")
    except Exception:
        logger.debug("finish_reason probe failed", exc_info=True)
    think_emit("stage_end", stage)
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


# Boot / alarm / operator: new chat. Fill / order_change / book_move: same episode.
EPISODE_KINDS = frozenset({"fill", "order_change", "book_move", "unprotected"})
EPISODE_MAX = 8


def _reset_chat(g: GrokClient) -> None:
    g.chat = None
    g._wake_n = 0


def _new_chat(g: GrokClient) -> Any:
    create_kw: dict[str, Any] = {
        "model": g.model,
        "messages": [system(brain_system_prompt())],
        "tools": list(AGENT_TOOLS),
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


def _ensure_chat(g: GrokClient, *, kind: str = "") -> Any:
    chat = getattr(g, "chat", None)
    n = int(getattr(g, "_wake_n", 0) or 0)
    if kind in EPISODE_KINDS and chat is not None and 0 < n < EPISODE_MAX:
        g._wake_n = n + 1
        return chat
    return _new_chat(g)


def _open_wake(g: GrokClient, wake: str, *, reset: bool = False) -> Any:
    if reset:
        _reset_chat(g)
    kind = ""
    try:
        from abcxauto.wake_bus import last_wake

        ev = last_wake()
        if ev is not None:
            kind = str(ev.kind or "")
    except Exception:
        kind = ""
    chat = _ensure_chat(g, kind=kind)
    chat.append(developer(wake))
    return chat


def _book_facts(world: WorldState) -> dict[str, Any]:
    from abcxauto.world_state import (
        COMBO_FACT,
        compact_position,
        compact_working_orders,
        open_upnl_of,
    )

    return {
        "cycle": world.cycle,
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
        "news": [
            f"[{n.get('symbol')}] {n.get('headline')}"
            for n in (world.news_items or [])[:8]
            if n.get("headline")
        ],
        "trade_plan": world.trade_plan,
        "book_unreliable": bool((world.gates or {}).get("book_unreliable")),
        "structure_cooldown": dict(getattr(world, "structure_cooldown", None) or {}),
    }


def _book_payload(world: WorldState) -> dict[str, Any]:
    from abcxauto.config import get_config
    from abcxauto.lab_playbook import playbook_glance
    from abcxauto.self_tune import levers_snapshot
    from abcxauto.world_state import day_facts

    cfg = get_config()
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
    except Exception:
        sc = {}
    facts = _book_facts(world)
    return {
        "day": day_facts(world, sc),
        "world": facts,
        "ibkr_live_quotes": dict(world.ibkr_live_quotes or {}),
        "score_windows": {
            "fastest_beating": (sc or {}).get("fastest_beating"),
            "best_pace": (sc or {}).get("best_pace"),
            "windows": (sc or {}).get("windows") or {},
        },
        "levers": levers_snapshot(cfg),
        "playbook": playbook_glance(sc),
        "path": _path_block(world, cfg),
    }


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


def _stash_live(world: WorldState, snap: dict[str, Any], data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        return
    rows = data.get("quotes")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                _stash_live(world, snap, row)
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
    snap["ibkr_live_symbol"] = sym
    snap["ibkr_live_last"] = px
    live = getattr(world, "ibkr_live_quotes", None)
    if not isinstance(live, dict):
        world.ibkr_live_quotes = {}
        live = world.ibkr_live_quotes
    live[sym] = px
    world.ibkr_live_symbol = sym
    world.ibkr_live_last = px


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
    from abcxauto.marketdata.client import get_marketdata_client

    client = get_marketdata_client()
    flag = getattr(client, "is_configured", False)
    if not (flag() if callable(flag) else flag):
        return []
    rows: list[dict[str, Any]] = []
    for sym in symbols[:8]:
        try:
            batch = list(await client.get_stock_news(sym, countback=per_symbol) or [])
        except Exception:
            logger.exception("news failed for %s", sym)
            batch = []
        rows.extend(batch)
    return rows


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
        payload = _book_payload(world)
        payload["sends_this_turn"] = len(turn.sends)
        world_facts = payload.get("world")
        if isinstance(world_facts, dict):
            world_facts["sends_this_turn"] = len(turn.sends)
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
        return _clip(st)
    if name == "quote":
        raw = await run_readonly_tool("quote", args, connector)
        try:
            data = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
            _stash_live(world, snap, data)
        return raw if isinstance(raw, str) else _clip(raw)
    if name == "fills":
        fn = getattr(connector, "get_fills", None) or getattr(connector, "get_recent_executions", None)
        if not callable(fn):
            return json.dumps({"error": "IBKR fills unavailable", "source": "ibkr"})
        rows = await fn()
        return _clip({"source": "ibkr", "freshness": "live", "fills": list(rows or [])[:40]})
    if name == "news":
        from abcxauto.news_feed import fetch_agent_news

        asked = normalize_tickers(args.get("symbols"))
        if asked:
            items = await _mda_news(asked)
        else:
            items = await fetch_agent_news(world.positions or snap.get("positions") or [])
        world.news_items = list(items)
        snap["news_items"] = list(items)
        return _clip({
            "source": "mda",
            "freshness": "delayed_15m",
            "use": "context_not_live_last",
            "items": items[:24],
        })
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
        from abcxauto.opportunity_scan import merge_tape, tape_symbols

        syms = normalize_tickers(args.get("symbols"))
        extra = await fetch_scan_metrics(syms) if syms else []
        if extra:
            ideas = merge_tape(list(world.opportunities or []), extra)
            world.opportunities = ideas
            world.scan_fetched = tape_symbols(extra)
            snap["opportunities"] = ideas
        tape = [strip_ambiguous_last(r) if isinstance(r, dict) else r for r in (extra or [])]
        return _clip({
            "source": "mda",
            "freshness": "delayed_daily",
            "bar": "D",
            "mda_last_is": "daily_bar_close",
            "use": "daily_structure_not_live_last",
            "symbols": syms,
            "tape": tape,
        })
    if name == "candles":
        from abcxauto.broker.bars import ibkr_bar_freshness
        from abcxauto.marketdata.client import get_marketdata_client

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
        res = str(args.get("resolution") or "D").strip() or "D"
        client = get_marketdata_client()
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

        async def _mda_candles(sym: str) -> dict[str, Any]:
            bars = await client.get_stock_candles(sym, resolution=res, countback=countback)
            return {
                "symbol": sym,
                "bars": list(bars or [])[-bar_cap:],
                "source": "mda",
                "freshness": mda_bar_freshness(res),
            }

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
            if callable(hist) and not warm:
                try:
                    raw = await hist(sym, resolution=res, countback=countback)
                except Exception as exc:
                    raw = {"error": str(exc), "source": "ibkr", "symbol": sym}
                if isinstance(raw, dict) and raw.get("bars"):
                    out = dict(raw)
                    out["bars"] = list(out.get("bars") or [])[-bar_cap:]
                    out.setdefault("source", "ibkr")
                    out.setdefault("freshness", ibkr_bar_freshness(res))
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
                    return out
                rt_err = str((raw or {}).get("error") or "no IBKR realtime bars")
            if ibkr_path:
                logger.info(
                    "candles %s hist=%s rt=%s path=ibkr_error",
                    sym,
                    hist_err or "n/a",
                    rt_err or "n/a",
                )
                err = {
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
            return await _mda_candles(sym)

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
            if src == "mda":
                kinds.add("mda")
            elif fresh == "ibkr_rt_5s":
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
        elif kinds == {"mda"}:
            source, freshness, use, out_res = (
                "mda",
                mda_bar_freshness(res),
                "backtest_or_context_not_live_last",
                res,
            )
        elif kinds <= {"hist", "rt"} and kinds:
            source, freshness, use, out_res = "ibkr", "ibkr_hist_or_rt", "prefer_hist_then_5s", res
        else:
            source, freshness, use, out_res = "mixed", "ibkr_or_mda", "prefer_ibkr_bars", res
        payload: dict[str, Any] = {
            "resolution": out_res,
            "source": source,
            "freshness": freshness,
            "use": use,
        }
        if out_res != res:
            payload["requested_resolution"] = res
        if source == "mda":
            payload["mda_last_is"] = mda_last_kind(res)
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
            return _clip(payload)
        payload["series"] = series
        return _clip(payload)
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
        return _clip({
            "source": "ibkr_live+mda_greeks",
            "freshness": "ibkr_live; greeks_delayed_15m",
            "use": "ibkr_live_for_decisions; mda_greeks_delayed",
            "facts": facts,
        })
    if name == "send":
        act = {
            "action": str(args.get("strategy") or args.get("action") or "").strip(),
            "strategy": str(args.get("strategy") or args.get("action") or "").strip(),
            "params": args.get("params") if isinstance(args.get("params"), dict) else {},
            "rationale": str(args.get("rationale") or ""),
        }
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
        return _clip(playbook_payload(args.get("revision"), full=bool(full)))
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
    if name == "set_wake":
        from abcxauto.wake_bus import set_wake

        ifs = args.get("wake_if")
        alarm = set_wake(
            wake_in_s=args.get("wake_in_s"),
            wake_at=args.get("wake_at"),
            wake_if=ifs,
            flat=getattr(world, "flat", None),
            session=str(getattr(world, "session_status") or ""),
        )
        return _clip({
            "status": "ok",
            "wake_at": alarm.wake_at,
            "wake_if": list(alarm.wake_if),
        })
    return json.dumps({"error": f"unknown tool {name}"})


async def grok_turn(
    g: GrokClient,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    wake: str,
) -> BrainTurn:
    """One Grok tool loop. send() is the only broker path."""
    return await _grok_turn_impl(
        g, connector=connector, world=world, snap=snap, wake=wake, turn=BrainTurn()
    )


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
    think_emit("say", f"\n[{name}]\n")
    turn.tool_trace.append(name)
    try:
        return await asyncio.wait_for(
            _run_tool(
                name, args, connector=connector, world=world, snap=snap, turn=turn
            ),
            timeout=timeout,
        )
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


async def _dispatch_tool_calls(
    calls: list[Any],
    *,
    chat: Any,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    turn: BrainTurn,
) -> None:
    """Read tools in parallel; send / playbook stay serial and after facts."""
    parsed = [_parse_tool_call(tc, world=world, snap=snap) for tc in calls]
    reads = [p for p in parsed if p[0] not in _MUTATING_TOOLS]
    writes = [p for p in parsed if p[0] in _MUTATING_TOOLS]

    async def _one(item: tuple[str, dict[str, Any], Any, float]) -> tuple[Any, str]:
        name, args, tc, timeout = item
        result = await _invoke_named_tool(
            name,
            args,
            timeout,
            connector=connector,
            world=world,
            snap=snap,
            turn=turn,
        )
        return tc, result

    if reads:
        rows = await asyncio.gather(*[_one(p) for p in reads], return_exceptions=True)
        for item, row in zip(reads, rows):
            if isinstance(row, Exception):
                logger.exception("parallel tool failed")
                _append_tool_result(
                    chat, item[2], json.dumps({"error": f"{item[0]} failed: {row}"})
                )
            else:
                _append_tool_result(chat, row[0], row[1])

    for item in writes:
        tc, result = await _one(item)
        _append_tool_result(chat, tc, result)


async def _grok_turn_impl(
    g: GrokClient,
    *,
    connector: Any,
    world: WorldState,
    snap: dict[str, Any],
    wake: str,
    turn: BrainTurn | None = None,
) -> BrainTurn:
    turn = turn or BrainTurn()
    if g is None:
        turn.last_act = {"action": "hold", "strategy": "hold", "rationale": "no_grok_client"}
        turn.last_result = {"status": "hold", "note": "no_grok_client"}
        return turn
    try:
        chat = _open_wake(g, wake)
    except Exception:
        logger.exception("chat start failed; reset once")
        try:
            chat = _open_wake(g, wake, reset=True)
        except Exception as exc:
            turn.last_act = {"action": "hold", "strategy": "hold", "rationale": f"chat_error: {exc}"}
            turn.last_result = {"status": "hold", "note": f"chat_error: {exc}"}
            return turn
    exhausted = True
    stream_resets = 0
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            text, response, stop = await stream_round(chat)
        except Exception as exc:
            logger.exception("stream_round failed")
            if stream_resets >= 1:
                think_emit("say", f"\n[stream failed: {exc}]\n")
                break
            stream_resets += 1
            think_emit("say", "\n[stream reset]\n")
            try:
                chat = _open_wake(g, wake, reset=True)
            except Exception:
                break
            continue
        if stop == "loop":
            _reset_chat(g)
            exhausted = False
            break
        if text:
            turn.text = (turn.text + "\n" + text).strip()
        if response is not None:
            try:
                chat.append(response)
            except Exception:
                logger.debug("chat.append(response) failed", exc_info=True)
        calls = list(getattr(response, "tool_calls", None) or []) if response is not None else []
        if not calls:
            exhausted = False
            break
        await _dispatch_tool_calls(
            calls,
            chat=chat,
            connector=connector,
            world=world,
            snap=snap,
            turn=turn,
        )
    if exhausted:
        turn.tool_budget_hit = True
        think_emit("say", "\n[tool budget exhausted]\n")
    _reset_chat(g)
    if not turn.sends:
        turn.last_act = turn.last_act or {
            "action": "hold",
            "strategy": "hold",
            "rationale": (turn.text or "no send")[:400],
        }
        turn.last_strat = "hold"
        turn.last_result = {"status": "hold", "strategy": "hold"}
    return turn
