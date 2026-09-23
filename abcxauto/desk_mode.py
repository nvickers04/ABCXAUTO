"""RTH thin sender vs research (premarket / AH / closed).

Same session clock as the desk (``regular`` / ``premarket`` / ``postmarket`` /
``closed``). Research has no broker send path. RTH may use news/scan/web as
COLOR; ``research_brief`` is this-look gathered color plus a prior-session stub, never a trigger.
Do not grow SYSTEM_PROMPT from here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

RTH_SESSION = "regular"
RESEARCH_SESSIONS = frozenset({"premarket", "postmarket", "closed"})
KNOWN_SESSIONS = frozenset({RTH_SESSION}) | RESEARCH_SESSIONS

REASON_RESEARCH_NO_SEND = "research_no_send"
REASON_RESEARCH_THIN = "research_thin"
_RESEARCH_THIN_NOTE = (
    "research_thin: this look has no dossier for the name"
)
_OCC_TICKET = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")
_OPTION_READ_STRATS = frozenset({
    "vertical_spread",
    "iron_condor",
    "iron_butterfly",
    "straddle",
    "strangle",
    "butterfly",
    "calendar_spread",
    "diagonal_spread",
    "cash_secured_put",
    "covered_call",
})
_NEWS_WEB_ONLY_STRATS = frozenset({"bracket", "market_bracket", "oca"})

RESEARCH_TOOLS = frozenset({
    "news",
    "scan",
    "option_facts",
    "odds",
    "candles",
    "web",
})
# Broker entry names. Research must not run these.
BROKER_ENTRY_TOOLS = frozenset({"send"})

FACT_CAP = 24
SYMBOL_CAP = 16
COLOR_SYMBOL_CAP = 8
BRIEF_STALE_S = 18 * 3600.0
WEB_TIMEOUT_S = 8.0
WEB_MAX_BYTES = 200_000
WEB_TEXT_CAP = 2_000
WEB_SEARCH_CAP = 5
WEB_SEARCH_TIMEOUT_S = 30.0
# Color search. The book model (grok-4.7, xhigh) does not finish a web
# tool turn inside a short wait.
WEB_SEARCH_MODEL = "grok-4-1-fast-non-reasoning"
THIS_LOOK_NEED = (
    "this look has no gathered color — scan|news|candles|web|odds|recall"
)
_NOTHING_GATHERED = "nothing was gathered this look"

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_BRIEF_PATH = _REPO / "data" / "state" / "research_brief.json"
_SNAP_BAG = "_research_bag"
_ET = ZoneInfo("America/New_York")
_PRIOR_SESSION_KEYS = (
    "as_of",
    "session",
    "mode",
    "symbols",
    "facts",
    "uncertainties",
)
_MID_GATHER = re.compile(
    r"^(pulling|checking|gathering|fetching|looking up)\b",
    re.I,
)
# Capability leftovers only — not a trading lecture.
_RESEARCH_OPEN: tuple[tuple[str, str], ...] = (
    ("scan", "scan: arena|scan_code|symbols[]"),
    ("news", "news: symbols[]"),
    ("candles", "candles: symbol+resolution"),
    ("web", "web: url"),
    ("odds", "odds: query|symbols[]"),
    ("recall", "recall: notes|cards"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    clock = dt or _utc_now()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.isoformat()


def desk_session(session: str = "") -> str:
    """Normalize to the desk's session labels. Empty/unknown uses park_clock.

    Not a second clock: labeled snaps keep their label; blanks fill the same
    way stay-up already does. Unresolved blanks fail closed to ``closed``.
    """
    sess = str(session or "").strip().lower()
    if sess == "unknown":
        sess = ""
    if sess in KNOWN_SESSIONS:
        return sess
    try:
        from abcxauto.park_clock import resolve_stay_up_session

        filled = str(resolve_stay_up_session(sess) or "").strip().lower()
    except Exception:
        filled = ""
    if filled in KNOWN_SESSIONS:
        return filled
    return "closed"


def is_rth_session(session: str = "") -> bool:
    return desk_session(session) == RTH_SESSION


def is_research_session(session: str = "") -> bool:
    return desk_session(session) != RTH_SESSION


def research_keep_looking(session: str = "") -> bool:
    """Mill is dead; after words-only wait for fill / order_change / unprotected / book event / poke."""
    _ = session
    return False


def _snap_is_known_flat_no_manage(snap: dict | None) -> bool:
    """True only when the snap evidentially has no lots and no working orders.

    Missing book keys fail closed — unknown is not flat. ``book_is_flat``
    keeps fill-lag from counting as a clean book.
    """
    if not isinstance(snap, dict):
        return False
    if "positions" not in snap or "open_orders" not in snap:
        return False
    lots = snap.get("open_lots")
    if lots is None:
        world = snap.get("world") if isinstance(snap.get("world"), dict) else {}
        day = snap.get("day") if isinstance(snap.get("day"), dict) else {}
        lots = world.get("open_lots") or day.get("open_lots") or []
    if any(str(x).strip() for x in (lots or [])):
        return False
    try:
        from abcxauto.world_state import book_is_flat

        return bool(
            book_is_flat(
                list(snap.get("positions") or []),
                list(snap.get("open_orders") or []),
                list(snap.get("fills") or []),
            )
        )
    except Exception:
        return False


def _desk_fact_is_wom(desk_fact: Any = "") -> bool:
    """Unchanged working_order_missing still waits fill / order_change."""
    try:
        from abcxauto.world_state import parse_desk_fact

        parsed = parse_desk_fact(desk_fact)
    except Exception:
        return False
    return bool(parsed) and str(parsed.get("kind") or "") == "working_order_missing"


def rth_flat_keep_looking(
    session: str = "",
    snap: dict | None = None,
    desk_fact: str = "",
) -> bool:
    """Words-only paper RTH waits for a real event. Never a pulse mill.

    LOOK.md: words only -> stop calling the model. Wait for fill /
    order_change / unprotected / operator poke / a lead fact that
    actually changed. An unchanged flat book is not a new fact.
    """
    _ = session, snap, desk_fact
    return False



# Spoken CLOSE/EXIT on an open lot is not a finished RTH look when send never
# ran. A named ORDER EXAMPLES ticket with zero send is the same class even
# when the book is flat. Hold / no-ticket / will-not-cut speech is finished —
# those names need a real ORDER EXAMPLES structure *without* a stand-down to
# re-enter. Detection is code (say + positions + sends==0 + tool_trace), not
# a prompt sermon. Close/exit+lots stays its own path; ticket names widen it.
_CLOSE_OR_EXIT_RE = re.compile(
    r"\b(?:close|closing|closed|exit|exiting|exited)\b",
    re.IGNORECASE,
)
_MARKET_CLOSE_NOISE_RE = re.compile(
    r"\b(?:until(?:\s+the)?|market|session|after[- ]hours?|rth)\s+close\b"
    r"|\bclose\s+of\s+(?:rth|regular|session|market)\b",
    re.IGNORECASE,
)
_CLOSE_THE_BOOK_RE = re.compile(
    r"\b(?:close|closing|exit|exiting)\s+"
    r"(?:the\s+)?(?:lot|lots|position|positions|book|both|all)\b",
    re.IGNORECASE,
)


def _qty_open(row: dict[str, Any]) -> bool:
    try:
        qty = float(row.get("quantity", row.get("position", 0)) or 0)
    except (TypeError, ValueError):
        return False
    return abs(qty) >= 1e-9


def _live_positions(positions: list[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in positions or []:
        if isinstance(row, dict) and _qty_open(row):
            out.append(row)
    return out


def _lot_name_tokens(
    positions: list[Any] | None,
    open_lots: list[Any] | None,
) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        tok = str(raw or "").strip()
        if not tok:
            return
        key = tok.upper()
        if key in seen:
            return
        seen.add(key)
        tokens.append(tok)

    for row in _live_positions(positions):
        add(row.get("symbol"))
    for lab in open_lots or []:
        head = str(lab or "").strip().split()
        if head:
            add(head[0])
    return tokens


def _had_send_tool(sends: int, tool_trace: list[Any] | None) -> bool:
    if int(sends or 0) > 0:
        return True
    for raw in tool_trace or []:
        name = str(raw or "").strip().split()[0].lower()
        if name == "send":
            return True
    return False


def _had_tool_or_send(sends: int, tool_trace: list[Any] | None) -> bool:
    """True when this look called any tool or send. Mill must not fire."""
    if _had_send_tool(sends, tool_trace):
        return True
    return any(str(raw or "").strip() for raw in (tool_trace or []))


def spoken_close_without_send(
    text: str = "",
    *,
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
    sends: int = 0,
    tool_trace: list[Any] | None = None,
) -> bool:
    """True when the say names CLOSE/EXIT on an open lot and send never ran.

    Market-close chatter ("until the close") is not a close decision.
    A send tool call — filled or clerk-blocked — is a send.
    """
    if _had_send_tool(sends, tool_trace):
        return False
    blob = str(text or "")
    if not blob.strip():
        return False
    lots = [str(x).strip() for x in (open_lots or []) if str(x).strip()]
    live = _live_positions(positions)
    if not live and not lots:
        return False
    cleaned = _MARKET_CLOSE_NOISE_RE.sub(" ", blob)
    if not _CLOSE_OR_EXIT_RE.search(cleaned):
        return False
    tokens = _lot_name_tokens(live, lots)
    if not tokens:
        return False
    for tok in tokens:
        if re.search(rf"\b{re.escape(tok)}\b", blob, re.IGNORECASE):
            return True
    if re.search(r"\b(?:CLOSE|EXIT)\b", blob):
        return True
    return bool(_CLOSE_THE_BOOK_RE.search(cleaned))


def _look_payload_parts(
    payload: dict[str, Any] | None,
) -> tuple[str, int, list[Any], list[Any], list[Any]]:
    """Unpack stay-up look payload: say, sends, positions, open_lots, tool_trace."""
    row = payload if isinstance(payload, dict) else {}
    try:
        sends = int(row.get("sends") or 0)
    except (TypeError, ValueError):
        sends = 0
    text = str(row.get("rationale") or row.get("text") or "")
    positions = list(row.get("positions") or [])
    open_lots = list(row.get("open_lots") or [])
    ws = row.get("world_state")
    if isinstance(ws, dict):
        if not open_lots:
            open_lots = list(ws.get("open_lots") or [])
        if not positions:
            positions = list(ws.get("positions") or [])
    return text, sends, positions, open_lots, list(row.get("tool_trace") or [])


def look_spoken_close_without_send(payload: dict[str, Any] | None) -> bool:
    """``_rearm_after_think`` payload: rationale/say + positions + sends."""
    text, sends, positions, open_lots, trace = _look_payload_parts(payload)
    return spoken_close_without_send(
        text,
        positions=positions,
        open_lots=open_lots,
        sends=sends,
        tool_trace=trace,
    )


def inventory_wake_fact(
    positions: list[Any] | None = None,
    *,
    open_lots: list[Any] | None = None,
) -> str:
    """Open lots as a wake lead. Not a fill / order_change / unprotected poke."""
    lots = [str(x).strip() for x in (open_lots or []) if str(x).strip()]
    if not lots:
        try:
            from abcxauto.world_state import lot_labels

            lots = lot_labels(list(positions or []))
        except Exception:
            lots = []
    if not lots:
        for row in _live_positions(positions):
            sym = str(row.get("symbol") or "").strip()
            if sym:
                lots.append(sym)
    if not lots:
        return ""
    return "open_lots=" + ",".join(lots) + "."


# Wake lead when a look named a sendable ticket and never called send.
# Not a fill / order_change / unprotected poke. Desk fact only — not an
# operator command (do not say SEND-THE-TICKET; models invent hold fills).
TICKET_WAKE_FACT = "Named order spoken; send did not run."

# RTH synthesize/decide mill: zero tools and zero send is not a finished look.
# Same-chat re-enter with a tool-or-send wake. After SYNTHESIZE_MILL_TRIES
# consecutive mill turns, drop the chat and continue cold (empty/junk spirit).
# Not a fill / order_change / unprotected poke. Not a "let me synthesize" sermon.
MILL_WAKE_FACT = "TOOL-OR-SEND this look. Do not synthesize."
SYNTHESIZE_MILL_TRIES = 2
_SYNTHESIZE_MILL_RE = re.compile(
    r"\bsynthesi[sz]e\b"
    r"|\btrading\s+plans?\b"
    r"|\bthe\s+picture\b"
    r"|\b(?:let\s+me|time\s+to|need\s+to|going\s+to)\s+decide\b"
    r"|\bdecide\s+(?:whether|on|what|how)\b"
    r"|\bgather(?:ing)?\b"
    r"|\b(?:keep\s+)?(?:scanning|browsing)\b"
    r"|\bspin(?:ning)?\b"
    r"|\bone\s+more\s+(?:scan|pass)\b"
    r"|\bdecide(?:d)?\s+without\s+(?:a\s+)?send\b",
    re.IGNORECASE,
)

# Single-token ORDER EXAMPLES names that also appear as ordinary English.
# Left out of the distinctive matcher so "relative to SPY" is not a ticket.
_AMBIGUOUS_TICKET_STRATS = frozenset({
    "oca",
    "adaptive",
    "relative",
    "vwap",
    "twap",
    "iceberg",
    "collar",
    "straddle",
    "strangle",
    "butterfly",
    "midprice",
})
_TICKET_ALIASES = (
    r"stk\s+bracket",
    r"stock\s+bracket",
    r"put\s+spread",
    r"call\s+spread",
    r"iron\s+fly",
    r"cash[-\s]secured\s+put",
    r"covered\s+call",
    r"protective\s+put",
)
_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
_SYM_INTENT_RE = re.compile(
    r"\b(?:buy|sell)\s+(?:the\s+|a\s+|an\s+)?([A-Z]{2,5})\b"
    r"|\b([A-Z]{2,5})\s+(?:to\s+)?(?:buy|sell)\b"
    r"|\b(?:long|short)\s+([A-Z]{2,5})\b"
    r"|\b([A-Z]{2,5})\s+(?:long|short)\b",
    re.IGNORECASE,
)
_SYM_OPTION_RE = re.compile(
    r"\b([A-Z]{2,5})\s+(?:\d+(?:\.\d+)?\s+)?(puts?|calls?)\b"
    r"|\b(puts?|calls?)\s+(?:spread\s+)?(?:on\s+)?([A-Z]{2,5})\b",
    re.IGNORECASE,
)
# Hold / pass / no-trade / no-ticket / will-not-cut conclusions are finished
# looks, not unpaid sends. "bracket stop / target" on an already-held lot
# must not arm wake — even when the essay names other tickers.
_STAND_DOWN_OPEN_RE = re.compile(
    r"\bno\s+ticket\b"
    r"|\bno[-\s]trade\b"
    r"|\b(?:standing|stand)\s+down\b"
    r"|\bpass(?:ing)?\s+on\b"
    r"|\bi(?:'ll|\s+will)\s+pass\b"
    r"|\bpass\s*[.!]"
    r"|\b(?:i\s+)?will\s+not\s+cut\b"
    r"|\b(?:i\s+)?won'?t\s+cut\b"
    r"|\b(?:conclusion|decision|verdict)\s*:?\s*hold(?:ing)?\b"
    r"|\b(?:i(?:'ll|\s+will)\s+)?(?:just\s+)?hold(?:ing)?\b"
    r"|\bhold(?:ing)?\s+(?:the\s+)?"
    r"(?:[A-Z]{2,5}|lot|lots|position|positions|shares|name)\b",
    re.IGNORECASE,
)
_RESERVED_TICKERS = frozenset({
    "AH", "ALL", "AND", "AT", "BAG", "BE", "BOTH", "BUY", "CALL", "CALLS",
    "CASH", "CHAT", "CLOSE", "COVERED", "CSP", "DOWN", "ETF", "EXIT", "FILL",
    "FLAT", "FLY", "FOK", "FOR", "FROM", "GTD", "IBKR", "ICEBERG", "IF",
    "INTO", "IOC", "IRON", "IT", "KILL", "LIMIT", "LMT", "LOC", "LONG",
    "LOOK", "LOO", "LOT", "LOTS", "MARKET", "MDA", "ME", "MKT", "MOC",
    "MOO", "MY", "NO", "NONE", "NOT", "OCA", "OF", "ON", "OPEN", "OPT",
    "OPTION", "OPTIONS", "OR", "ORDER", "PM", "POSITION", "POSITIONS",
    "PRICE", "PROTECTIVE", "PUT", "PUTS", "QTY", "RATIO", "RELATIVE",
    "ROLL", "RTH", "SAME", "SCAN", "SECURED", "SELL", "SEND", "SHORT",
    "SIZE", "SNAP", "SO", "SPREAD", "STILL", "STK", "STOP", "STP",
    "STRADDLE", "STRANGLE", "TARGET", "THAN", "THAT", "THE", "THEN",
    "THIS", "TICKET", "TO", "TRAILING", "TWAP", "UNDER", "UNPAID", "UP",
    "USD", "VS", "VWAP", "WATCHING", "WE", "WILL", "WITH", "YES",
})
_ticket_structure_re: re.Pattern[str] | None = None


def _is_spoken_ticker(tok: str) -> bool:
    key = str(tok or "").strip().upper()
    if len(key) < 2 or len(key) > 5:
        return False
    if key in _RESERVED_TICKERS:
        return False
    if not re.fullmatch(r"[A-Z][A-Z0-9]{1,4}", key):
        return False
    return True


def _spoken_tickers(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for m in _TICKER_RE.finditer(str(text or "").upper()):
        tok = m.group(1)
        if not _is_spoken_ticker(tok) or tok in seen:
            continue
        seen.add(tok)
        found.append(tok)
    return found


def _ticket_structure_pattern() -> re.Pattern[str]:
    global _ticket_structure_re
    if _ticket_structure_re is not None:
        return _ticket_structure_re
    from abcxauto.order_examples import ticket_strategy_names

    parts: list[str] = []
    for name in ticket_strategy_names():
        token = str(name or "").strip()
        if not token or token.lower() in _AMBIGUOUS_TICKET_STRATS:
            continue
        parts.append(re.escape(token))
        spaced = token.replace("_", " ")
        if spaced != token:
            parts.append(re.escape(spaced).replace(r"\ ", r"[\s_]+"))
    parts.extend(_TICKET_ALIASES)
    parts.sort(key=len, reverse=True)
    _ticket_structure_re = re.compile(
        r"\b(?:%s)\b" % "|".join(parts) if parts else r"(?!)",
        re.IGNORECASE,
    )
    return _ticket_structure_re


def _captured_ticker(match: re.Match[str]) -> str:
    for g in match.groups():
        if g and _is_spoken_ticker(g):
            return str(g).upper()
    return ""


def _book_ticker_set(
    positions: list[Any] | None,
    open_lots: list[Any] | None,
) -> set[str]:
    """Tickers already on the book (position symbols + open-lot labels)."""
    held: set[str] = set()
    for row in positions or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("symbol") or "").strip().upper()
        if _is_spoken_ticker(key):
            held.add(key)
    for tok in _lot_name_tokens(positions, open_lots):
        key = str(tok or "").strip().upper()
        if _is_spoken_ticker(key):
            held.add(key)
    return held


def _stand_down_open_decision(text: str) -> bool:
    """Hold / pass / no-trade / no-ticket / will-not-cut — not an unpaid order."""
    return bool(_STAND_DOWN_OPEN_RE.search(str(text or "")))


def spoken_ticket_without_send(
    text: str = "",
    *,
    positions: list[Any] | None = None,
    open_lots: list[Any] | None = None,
    sends: int = 0,
    tool_trace: list[Any] | None = None,
) -> bool:
    """True when the say names a concrete ORDER EXAMPLES ticket and send never ran.

    Flat book is enough — open lots are not required. Describing existing
    lots is not an unpaid ticket: if every spoken intent ticker is already
    on the book, only a real ORDER EXAMPLES structure re-enters.
    A hold / pass / no-trade conclusion does not arm on stop/target/bracket
    prose or other tickers named for comparison. An explicit buy/sell/short
    (or option) of a name not already held still arms even when the say
    also stands down. CLOSE/EXIT+lots stays on ``spoken_close_without_send``.
    A send tool call — filled or clerk-blocked — is a send. Illegal STK
    still has to attempt send.
    """
    if _had_send_tool(sends, tool_trace):
        return False
    blob = str(text or "")
    if not blob.strip():
        return False
    if not _spoken_tickers(blob):
        return False
    held = _book_ticker_set(positions, open_lots)
    # Explicit new-name intent survives a hold/pass essay in the same say.
    for m in _SYM_INTENT_RE.finditer(blob):
        tok = _captured_ticker(m)
        if tok and tok not in held:
            return True
    for m in _SYM_OPTION_RE.finditer(blob):
        tok = _captured_ticker(m)
        if tok and tok not in held:
            return True
    if _stand_down_open_decision(blob):
        return False
    if _ticket_structure_pattern().search(blob):
        return True
    return False


def look_spoken_ticket_without_send(payload: dict[str, Any] | None) -> bool:
    """``_rearm_after_think`` payload: rationale/say + positions + sends + tool_trace."""
    text, sends, positions, lots, trace = _look_payload_parts(payload)
    return spoken_ticket_without_send(
        text,
        positions=positions,
        open_lots=lots,
        sends=sends,
        tool_trace=trace,
    )


def look_unpaid_ticket(payload: dict[str, Any] | None) -> bool:
    """CLOSE/EXIT on lots, or a named ticket, with zero send. Widen, not replace."""
    return look_spoken_close_without_send(payload) or look_spoken_ticket_without_send(
        payload
    )


def ticket_wake_fact() -> str:
    """Lead the next same-chat wake. Not a fill / order_change / unprotected poke."""
    return TICKET_WAKE_FACT


def spoken_synthesize_mill(
    text: str = "",
    *,
    sends: int = 0,
    tool_trace: list[Any] | None = None,
) -> bool:
    """True when the say is a synthesize/decide mill and this look used no tool or send.

    Tight language: synthesize / trading plan / the picture / let-me-decide.
    A tool call or send is not a mill. Named unpaid tickets stay on
    ``look_unpaid_ticket``. Bare "decided to wait" is not this matcher.
    """
    if _had_tool_or_send(sends, tool_trace):
        return False
    blob = str(text or "")
    if not blob.strip():
        return False
    return bool(_SYNTHESIZE_MILL_RE.search(blob))


def look_synthesize_mill(payload: dict[str, Any] | None) -> bool:
    """``_rearm_after_think`` payload: mill language + zero tools + zero send."""
    text, sends, _positions, _lots, trace = _look_payload_parts(payload)
    return spoken_synthesize_mill(text, sends=sends, tool_trace=trace)


def mill_wake_fact() -> str:
    """Lead the next same-chat wake after a synthesize mill. Forces tool or send."""
    return MILL_WAKE_FACT


def desk_mode(session: str = "") -> str:
    return "rth" if is_rth_session(session) else "research"


def _model_token(raw: Any) -> str:
    return str(raw or "").strip()


def session_model(session: str = "", cfg: Any = None) -> str:
    """RTH uses ``model_rth`` when set; research uses ``model_research`` when set.

    Unset session models fall back to the current ``model`` knob so a single-model
    desk does not break.
    """
    if cfg is None:
        from abcxauto.config import get_config

        cfg = get_config()
    from abcxauto.config import DEFAULT_MODEL

    base = _model_token(getattr(cfg, "model", None)) or DEFAULT_MODEL
    if is_rth_session(session):
        token = _model_token(getattr(cfg, "model_rth", None)) or base
        try:
            from abcxauto.thin_rth_kill_look import rth_model_no_xhigh

            return rth_model_no_xhigh(token)
        except Exception:
            return token
    return _model_token(getattr(cfg, "model_research", None)) or base


def _params_map(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        return {}
    return dict(raw)


def session_model_params(session: str = "", cfg: Any = None) -> dict[str, Any]:
    """RTH/research ``model_params_*`` when set; else shared ``model_params``.

    Invalid maps fail-closed to ``{}``. RTH thin strips xhigh effort so
    params cannot undo ``rth_model_no_xhigh`` / F10.
    """
    if cfg is None:
        from abcxauto.config import get_config

        cfg = get_config()
    shared = _params_map(getattr(cfg, "model_params", None))
    if is_rth_session(session):
        chosen = _params_map(getattr(cfg, "model_params_rth", None)) or shared
        try:
            from abcxauto.thin_rth_kill_look import rth_params_no_xhigh

            return rth_params_no_xhigh(chosen)
        except Exception:
            return {}
    return _params_map(getattr(cfg, "model_params_research", None)) or shared


def research_send_block(*, session: str = "") -> dict[str, Any]:
    sess = desk_session(session)
    return {
        "status": "blocked",
        "note": (
            f"research mode ({sess}) — no broker send; "
            "AH/PM look writes the brief only"
        ),
        "reason_code": REASON_RESEARCH_NO_SEND,
        "strategy": "blocked",
        "desk_mode": "research",
        "session": sess,
    }


def research_brief_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_RESEARCH_BRIEF_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_BRIEF_PATH


def load_research_brief() -> dict[str, Any]:
    p = research_brief_path()
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _snap_research_tools(snap: dict[str, Any] | None) -> list[str]:
    """Tools already stamped on the look snap (even when the bag note missed)."""
    blob = snap if isinstance(snap, dict) else {}
    tools: list[str] = []
    store = blob.get("session_range")
    if isinstance(store, dict):
        for row in store.values():
            if isinstance(row, dict) and _bar_structure_row(row):
                tools.append("candles")
                break
            if isinstance(row, dict) and _finite_px(row.get("last")):
                tools.append("candles")
                break
    src = str(blob.get("candle_source") or "").strip().lower()
    if src and src not in ("", "ibkr_miss", "none") and "candles" not in tools:
        tools.append("candles")
    items = blob.get("news_items")
    if isinstance(items, list) and any(isinstance(it, dict) for it in items):
        tools.append("news")
    hits = blob.get("scan_hits") if isinstance(blob.get("scan_hits"), dict) else {}
    rows = hits.get("rows") if isinstance(hits, dict) else None
    if isinstance(rows, list) and rows:
        tools.append("scan")
    elif blob.get("scan_fetched"):
        tools.append("scan")
    if _web_ok(blob):
        tools.append("web")
    facts = blob.get("option_facts")
    if isinstance(facts, dict):
        facts = facts.get("facts")
    if isinstance(facts, list) and facts:
        tools.append("option_facts")
    return tools


def this_look_research(
    snap: dict[str, Any] | None,
    world: Any = None,
) -> dict[str, Any]:
    """Color gathered on this look. Capability leftovers, not a lecture."""
    del world
    bag = _bag(snap)
    facts = [row for row in (bag.get("facts") or []) if isinstance(row, dict)]
    tools: list[str] = []
    for row in facts:
        src = str(row.get("source") or "").strip()
        if src and src not in tools:
            tools.append(src)
    for src in _snap_research_tools(snap):
        if src not in tools:
            tools.append(src)
    used = set(tools)
    return {
        "symbols": list(bag.get("symbols") or [])[:SYMBOL_CAP],
        "facts": list(bag.get("facts") or [])[:FACT_CAP],
        "uncertainties": list(bag.get("uncertainties") or [])[:12],
        "tools": tools,
        "open": [line for name, line in _RESEARCH_OPEN if name not in used],
    }


def _inline_dossier_blocks_new_risk(
    dossier: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Same gate as research_dossier.dossier_blocks_new_risk when that module is absent.

    Unknown / unavailable / missing earnings do not refuse. Only a known
    ``earnings_in`` inside 2 sessions refuses.
    """
    if not isinstance(dossier, dict) or not dossier:
        return True, "no_dossier"
    earnings_in = dossier.get("earnings_in")
    if isinstance(earnings_in, int) and not isinstance(earnings_in, bool) and earnings_in <= 2:
        return True, "earnings_window"
    return False, ""


def _dossier_for_symbol(snap: dict[str, Any], sym: str) -> dict[str, Any] | None:
    store = snap.get("dossiers")
    if not isinstance(store, dict):
        return None
    row = store.get(sym)
    if row is None:
        for key, val in store.items():
            if str(key).upper().strip() == sym:
                row = val
                break
    if isinstance(row, dict) and row:
        return row
    return None


def new_risk_research_error(
    symbol: str,
    snap: dict[str, Any] | None,
    *,
    strat: str = "",
) -> str:
    """Empty = may go. Non-empty = this look has no usable dossier for the ticket name.

    Missing dossiers are filled from this-look research (news / web / candles /
    priced scan hit) before the earnings gate runs. No research still refuses.
    Exits skip this in the caller.
    """
    del strat
    blob = snap if isinstance(snap, dict) else {}
    name = _ticket_research_name(symbol)
    if not name:
        return ""
    dossier = _dossier_for_symbol(blob, name)
    if dossier is None:
        try:
            from abcxauto.research_dossier import ensure_this_look_dossier
        except ImportError:
            ensure_this_look_dossier = None  # type: ignore[assignment]
        if ensure_this_look_dossier is not None:
            dossier = ensure_this_look_dossier(blob, name)
        if dossier is None:
            return _RESEARCH_THIN_NOTE
    try:
        from abcxauto.research_dossier import dossier_blocks_new_risk
    except ImportError:
        blocked, reason = _inline_dossier_blocks_new_risk(dossier)
    else:
        blocked, reason = dossier_blocks_new_risk(dossier)
    if not blocked:
        return ""
    code = str(reason or "no_dossier").strip() or "no_dossier"
    if code == "no_dossier":
        return _RESEARCH_THIN_NOTE
    return f"research_thin: {code}"


def _ticket_research_name(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if not raw:
        return ""
    compact = raw.replace(" ", "")
    m = _OCC_TICKET.match(compact)
    return m.group(1) if m else raw


def _bag_view(snap: dict[str, Any] | None) -> dict[str, Any]:
    blob = snap if isinstance(snap, dict) else {}
    bag = blob.get(_SNAP_BAG)
    if isinstance(bag, dict):
        return bag
    return {"facts": [], "symbols": [], "uncertainties": []}


def _text_starts_with_sym(text: str, sym: str) -> bool:
    raw = str(text or "").strip()
    if not raw or not sym:
        return False
    up = raw.upper()
    if not up.startswith(sym):
        return False
    if len(up) == len(sym):
        return True
    return not up[len(sym)].isalnum()


def _field_is_sym(row: dict[str, Any], sym: str) -> bool:
    for key in ("symbol", "ticker", "underlying"):
        raw = str(row.get(key) or "").upper().strip()
        if not raw:
            continue
        if raw == sym or _ticket_research_name(raw) == sym:
            return True
    return False


def _fact_names_sym(row: dict[str, Any], sym: str) -> bool:
    if _field_is_sym(row, sym):
        return True
    text = str(row.get("text") or "")
    if _text_starts_with_sym(text, sym):
        return True
    for part in text.split("|"):
        if _text_starts_with_sym(part.strip(), sym):
            return True
    return False


def _scan_stashed_last(row: dict[str, Any]) -> bool:
    if str(row.get("print") or "") == "live_open":
        return True
    return str(row.get("source") or "").strip().lower() == "scan"


def _finite_px(raw: Any) -> bool:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return False
    return math.isfinite(n)


def _bar_structure_row(row: dict[str, Any]) -> bool:
    if _scan_stashed_last(row):
        return False
    for key in ("open", "high", "low", "last"):
        if not _finite_px(row.get(key)):
            return False
    try:
        n = int(row.get("n"))
    except (TypeError, ValueError):
        return False
    return n >= 1


def _structure_this_look(snap: dict[str, Any], sym: str) -> bool:
    store = snap.get("session_range")
    if isinstance(store, dict):
        row = store.get(sym)
        if row is None:
            for key, val in store.items():
                if str(key).upper().strip() == sym:
                    row = val
                    break
        if isinstance(row, dict) and _bar_structure_row(row):
            return True
    for fact in _bag_view(snap).get("facts") or []:
        if not isinstance(fact, dict):
            continue
        if str(fact.get("source") or "") == "candles" and _fact_names_sym(fact, sym):
            return True
    return False


def _news_this_look(snap: dict[str, Any], sym: str) -> bool:
    items = snap.get("news_items")
    if not isinstance(items, list):
        return False
    for it in items:
        if not isinstance(it, dict):
            continue
        if _field_is_sym(it, sym):
            return True
        for key in ("headline", "title", "text"):
            if _text_starts_with_sym(str(it.get(key) or ""), sym):
                return True
    return False


def _web_ok(snap: dict[str, Any]) -> bool:
    page = snap.get("research_web")
    if not isinstance(page, dict):
        return False
    if str(page.get("error") or "").strip():
        return False
    return bool(
        str(page.get("url") or "").strip()
        and str(page.get("title") or "").strip()
        and str(page.get("text") or "").strip()
    )


def _option_read_strat(strat: str) -> bool:
    st = str(strat or "").strip().lower()
    return st in _OPTION_READ_STRATS or "option" in st


def _option_payload_ok(row: Any, sym: str) -> bool:
    if not isinstance(row, dict) or row.get("error"):
        return False
    if _field_is_sym(row, sym):
        return True
    return False


def _option_read_present(snap: dict[str, Any], sym: str) -> bool:
    facts = snap.get("option_facts")
    if isinstance(facts, dict):
        facts = facts.get("facts")
    if isinstance(facts, list):
        for row in facts:
            if _option_payload_ok(row, sym):
                return True
    chains = snap.get("option_chains")
    if isinstance(chains, dict):
        for key, row in chains.items():
            if str(key).upper().strip() == sym and isinstance(row, dict) and not row.get("error"):
                return True
            if _option_payload_ok(row, sym):
                return True
    chain = snap.get("option_chain")
    if _option_payload_ok(chain, sym):
        return True
    if (
        isinstance(chain, dict)
        and not chain.get("error")
        and not str(chain.get("symbol") or chain.get("underlying") or "").strip()
        and (chain.get("expirations") or chain.get("strikes") or chain.get("n_strikes"))
    ):
        return True
    for fact in _bag_view(snap).get("facts") or []:
        if not isinstance(fact, dict):
            continue
        src = str(fact.get("source") or "")
        if src in {"option_facts", "option_chain"} and _fact_names_sym(fact, sym):
            return True
    return False


def _read_this_look(snap: dict[str, Any], sym: str, strat: str) -> bool:
    if _news_this_look(snap, sym) or _web_ok(snap):
        return True
    st = str(strat or "").strip().lower()
    if st in _NEWS_WEB_ONLY_STRATS:
        return False
    return _option_read_strat(st) and _option_read_present(snap, sym)


def _prior_session_stub(
    brief: dict[str, Any],
    *,
    missing: bool,
    stale: bool,
) -> dict[str, Any]:
    if missing:
        return {}
    if stale:
        return {
            key: brief[key]
            for key in ("as_of", "session")
            if brief.get(key) not in (None, "")
        }
    out: dict[str, Any] = {}
    for key in _PRIOR_SESSION_KEYS:
        if key in brief:
            out[key] = brief[key]
    return out


def _brief_research_sources(brief: dict[str, Any] | None) -> set[str]:
    row = brief if isinstance(brief, dict) else {}
    out: set[str] = set()
    for fact in row.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        src = str(fact.get("source") or "").strip()
        if src in RESEARCH_TOOLS:
            out.add(src)
    return out


def _this_look_empty(this_look: dict[str, Any] | None) -> bool:
    row = this_look if isinstance(this_look, dict) else {}
    return not row.get("tools") and not row.get("facts")


def _merge_brief_research_into_look(
    this_look: dict[str, Any],
    brief: dict[str, Any],
) -> dict[str, Any]:
    """Fold research-tool facts from a mid-look disk write into this_look."""
    facts = [row for row in (this_look.get("facts") or []) if isinstance(row, dict)]
    tools = [str(t) for t in (this_look.get("tools") or []) if str(t).strip()]
    seen = {(str(row.get("source") or ""), str(row.get("text") or "")) for row in facts}
    for fact in brief.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        src = str(fact.get("source") or "").strip()
        if src not in RESEARCH_TOOLS:
            continue
        key = (src, str(fact.get("text") or ""))
        if key in seen:
            continue
        facts.append({"source": src, "text": str(fact.get("text") or "")})
        seen.add(key)
        if src not in tools:
            tools.append(src)
        if len(facts) >= FACT_CAP:
            break
    used = set(tools)
    out = dict(this_look)
    out["facts"] = facts[:FACT_CAP]
    out["tools"] = tools
    out["open"] = [line for name, line in _RESEARCH_OPEN if name not in used]
    if not out.get("symbols"):
        out["symbols"] = [
            str(s).upper().strip()
            for s in (brief.get("symbols") or [])
            if str(s).strip()
        ][:SYMBOL_CAP]
    return out


def research_brief_look_payload(
    brief: dict[str, Any] | None,
    *,
    snap: dict[str, Any] | None = None,
    world: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """This-look color plus a prior-session stub (wake-safe; may keep symbols)."""
    row = brief if isinstance(brief, dict) else {}
    missing = not bool(row)
    stale = True if missing else research_brief_stale(row, now=now)
    this_look = this_look_research(snap, world)
    brief_src = _brief_research_sources(row)
    look_src = set(this_look.get("tools") or [])
    # Mid-look write_research_brief overwrites the on-disk brief with this look.
    # Overlap means that file is this-look color, not a prior-session stub.
    if not _this_look_empty(this_look) and brief_src and (brief_src & look_src):
        this_look = _merge_brief_research_into_look(this_look, row)
        prior = _prior_session_stub(row, missing=False, stale=True)
    else:
        prior = _prior_session_stub(row, missing=missing, stale=stale)
    out: dict[str, Any] = {
        "use": "color, never a live trigger",
        "send_geometry": False,
        "this_look": this_look,
        "prior_session": prior,
        "missing": missing,
        "stale": stale,
    }
    if _this_look_empty(this_look):
        out["need"] = THIS_LOOK_NEED
    return out


def research_brief_tool_payload(
    brief: dict[str, Any] | None,
    *,
    snap: dict[str, Any] | None = None,
    world: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Tool result for research_brief: color only, never a symbol allowlist."""
    out = dict(
        research_brief_look_payload(brief, snap=snap, world=world, now=now)
    )
    out["use"] = "color, never a live trigger"
    out["send_geometry"] = False
    this_look = out.get("this_look")
    if isinstance(this_look, dict):
        trimmed = dict(this_look)
        trimmed.pop("symbols", None)
        out["this_look"] = trimmed
    prior = out.get("prior_session")
    if isinstance(prior, dict) and "symbols" in prior:
        trimmed_prior = dict(prior)
        trimmed_prior.pop("symbols", None)
        out["prior_session"] = trimmed_prior
    return out


def research_brief_stale(
    brief: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    row = brief if isinstance(brief, dict) else {}
    if not row:
        return True
    ts = _parse_iso(str(row.get("as_of") or row.get("ts") or ""))
    if ts is None:
        return True
    clock = now or _utc_now()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    age = (clock - ts).total_seconds()
    if age < 0:
        return False
    return age > BRIEF_STALE_S


def _parse_iso(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bag(snap: dict[str, Any] | None) -> dict[str, Any]:
    blob = snap if isinstance(snap, dict) else {}
    bag = blob.get(_SNAP_BAG)
    if not isinstance(bag, dict):
        bag = {"facts": [], "symbols": [], "uncertainties": []}
        if isinstance(snap, dict):
            snap[_SNAP_BAG] = bag
    bag.setdefault("facts", [])
    bag.setdefault("symbols", [])
    bag.setdefault("uncertainties", [])
    return bag


def _add_symbol(bag: dict[str, Any], raw: Any) -> None:
    sym = str(raw or "").upper().strip()
    if not sym or any(c.isspace() for c in sym) or len(sym) > 12:
        return
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", sym):
        return
    syms = bag.setdefault("symbols", [])
    if sym not in syms and len(syms) < SYMBOL_CAP:
        syms.append(sym)


def _walk_symbols(bag: dict[str, Any], payload: Any, *, depth: int = 0) -> None:
    if depth > 4 or payload is None:
        return
    if isinstance(payload, dict):
        for key in ("symbol", "ticker", "underlying"):
            _add_symbol(bag, payload.get(key))
        for key in ("symbols", "tickers"):
            val = payload.get(key)
            if isinstance(val, (list, tuple)):
                for item in val[:SYMBOL_CAP]:
                    _add_symbol(bag, item)
        for key in ("items", "rows", "hits", "facts", "quotes", "events", "series"):
            val = payload.get(key)
            if isinstance(val, list):
                for item in val[:24]:
                    _walk_symbols(bag, item, depth=depth + 1)
        return
    if isinstance(payload, list):
        for item in payload[:24]:
            _walk_symbols(bag, item, depth=depth + 1)


def _as_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except (TypeError, json.JSONDecodeError, ValueError):
                data = None
            if isinstance(data, dict):
                return data
            if isinstance(data, list):
                return {"rows": data}
        return {"text": text[:400]}
    return {"text": str(payload)[:400]}


def _news_fact_line(payload: dict[str, Any]) -> str:
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    heads: list[str] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        hl = str(it.get("headline") or "").strip()
        if not hl:
            continue
        sym = str(it.get("symbol") or "").strip().upper()
        key = sym or hl[:24]
        if key in seen:
            continue
        seen.add(key)
        heads.append(f"{sym + ': ' if sym else ''}{hl}"[:90])
        if len(heads) >= 6:
            break
    if heads:
        return "; ".join(heads)
    return str(payload.get("error") or payload.get("note") or "")[:200]


def _candle_bar_bit(row: dict[str, Any]) -> str:
    sym = str(row.get("symbol") or "").strip().upper()
    sess = row.get("session") if isinstance(row.get("session"), dict) else {}
    bars = row.get("bars") if isinstance(row.get("bars"), list) else []
    last = bars[-1] if bars and isinstance(bars[-1], dict) else {}
    px = sess.get("last")
    if px is None:
        px = last.get("c")
    if px is None:
        return ""
    bit = f"{sym} {px}" if sym else f"{px}"
    vs = sess.get("vs_open")
    if vs is not None:
        bit += f" vs_open={vs}"
    return bit


def _candle_fact_line(payload: dict[str, Any]) -> str:
    series = payload.get("series") if isinstance(payload.get("series"), list) else []
    bits = [_candle_bar_bit(row) for row in series[:8] if isinstance(row, dict)]
    bits = [b for b in bits if b]
    src = str(payload.get("source") or "ibkr")
    if bits:
        return f"{' | '.join(bits)} src={src}"
    # Single-symbol candles put session on the payload, not under series.
    top = _candle_bar_bit(
        {
            "symbol": payload.get("symbol"),
            "session": payload.get("session"),
            "bars": payload.get("bars"),
        }
    )
    if top:
        return f"{top} src={src}"
    bars = payload.get("bars") if isinstance(payload.get("bars"), list) else []
    if not bars:
        last = payload.get("last")
        sym = str(payload.get("symbol") or "").strip()
        if last is not None:
            return f"{(sym + ' ') if sym else ''}{last} src={src}".strip()
        return ""
    sym = str(payload.get("symbol") or "bars").strip()
    last = bars[-1] if isinstance(bars[-1], dict) else {}
    px = last.get("c")
    if px is None:
        return f"{sym} n={len(bars)} src={src} use={payload.get('use') or ''}".strip()
    return f"{sym} {px} n={len(bars)} src={src}".strip()


def _fact_line(source: str, payload: dict[str, Any], args: dict[str, Any] | None) -> str:
    src = str(source or "").strip() or "tool"
    if src == "news":
        return _news_fact_line(payload)
    if src == "scan":
        rows = payload.get("rows") or payload.get("hits") or []
        n = len(rows) if isinstance(rows, list) else 0
        deepest = ""
        named: list[str] = []
        skipped = 0
        if isinstance(rows, list):
            best = None
            best_mag = -1.0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                gap = row.get("open_gap_pct")
                if gap is None:
                    gap = row.get("gap%")
                try:
                    mag = abs(float(gap))
                except (TypeError, ValueError):
                    mag = None
                if mag is not None and mag > best_mag:
                    best_mag = mag
                    best = row
                skip = str(row.get("skip_class") or "").strip()
                if skip:
                    skipped += 1
                    continue
                if len(named) >= 5:
                    continue
                sym = str(row.get("symbol") or "").strip()
                if not sym:
                    continue
                bit = sym
                last = row.get("last")
                if last is not None:
                    bit += f" {last}"
                if mag is not None:
                    bit += f" gap={gap}"
                named.append(bit)
            if best is not None:
                deepest = f" deepest={best.get('symbol')} {best_mag:g}%"
        names = (" " + " ".join(named)) if named else ""
        skip_bit = f" skip={skipped}" if skipped else ""
        return f"hits={n}{deepest}{names}{skip_bit} src={payload.get('source') or 'scan'}"
    if src == "option_facts":
        facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
        n = len(facts) if isinstance(facts, list) else 0
        return f"open option legs n={n} src={payload.get('source') or 'option_facts'}"
    if src == "odds":
        events = payload.get("events") or payload.get("markets") or payload.get("rows") or []
        n = len(events) if isinstance(events, list) else 0
        q = str((args or {}).get("query") or payload.get("query") or "").strip()
        bit = f" query={q}" if q else ""
        return f"implied-prob events n={n}{bit} (not send geometry)"
    if src == "candles":
        return _candle_fact_line(payload)
    if src == "web":
        err = str(payload.get("error") or "").strip()
        if err:
            return f"web error {err}"[:200]
        hits = payload.get("results")
        if isinstance(hits, list) and hits:
            titles = []
            for row in hits[:3]:
                if not isinstance(row, dict):
                    continue
                bit = str(row.get("title") or "").strip()
                if bit:
                    titles.append(bit)
            q = str(payload.get("query") or (args or {}).get("query") or "").strip()
            head = f"web search {q}: " if q else "web search: "
            return (head + "; ".join(titles))[:220]
        title = str(payload.get("title") or "").strip()
        url = str(payload.get("url") or (args or {}).get("url") or "").strip()
        return f"{title or 'page'} {url}".strip()[:220]
    text = str(payload.get("text") or payload.get("note") or payload.get("error") or src)
    return text[:200]


def _uncertainty_for(source: str, payload: dict[str, Any]) -> str | None:
    src = str(source or "")
    if src == "news":
        return "MDA news delayed ~15m — time-sensitive prints may already be in the price"
    if src == "scan":
        return "scan hits are tape color, not a send trigger"
    if src == "option_facts":
        return "MDA greeks delayed if present — not send geometry"
    if src == "odds":
        return "Polymarket implied probs, not IBKR last / not send geometry"
    if src == "candles":
        fresh = str(payload.get("freshness") or "")
        if "miss" in fresh:
            return "candles missed IBKR hist and live 5s"
        return None
    if src == "web":
        where = str(payload.get("where") or "").strip().lower()
        if where in ("x", "both"):
            return "X posts and web snippets are color, not a live trigger"
        return "web fetch is a public page snippet, not a live trigger"
    return None


def note_research_tool(
    snap: dict[str, Any] | None,
    name: str,
    payload: Any,
    *,
    args: dict[str, Any] | None = None,
) -> None:
    """Record one research-ish tool result onto the look bag. No tickets."""
    src = str(name or "").strip()
    if src not in RESEARCH_TOOLS:
        return
    bag = _bag(snap)
    data = _as_dict(payload)
    _walk_symbols(bag, data)
    _walk_symbols(bag, args or {})
    line = _fact_line(src, data, args)
    facts = bag.setdefault("facts", [])
    row = {"source": src, "text": line}
    if line and row not in facts and len(facts) < FACT_CAP:
        facts.append(row)
    note = _uncertainty_for(src, data)
    if note:
        uns = bag.setdefault("uncertainties", [])
        if note not in uns:
            uns.append(note)


def _px(raw: Any) -> float | None:
    if isinstance(raw, dict):
        raw = raw.get("last") if raw.get("last") is not None else raw.get("mid")
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def _as_quote_map(snap: dict[str, Any] | None, world: Any) -> dict[str, float]:
    qmap: dict[str, float] = {}
    blobs: list[Any] = []
    lists: list[Any] = []
    if world is not None:
        blobs.append(getattr(world, "ibkr_live_quotes", None))
        lists.append(getattr(world, "quotes", None))
        book = getattr(world, "book", None)
        if isinstance(book, dict):
            lists.append(book.get("quotes"))
    if isinstance(snap, dict):
        blobs.append(snap.get("ibkr_live_quotes"))
        lists.append(snap.get("quotes"))
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for key, raw in blob.items():
            px = _px(raw)
            if px is None:
                continue
            name = str(key).upper().strip()
            if name:
                qmap[name] = px
    for rows in lists:
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("symbol") or "").upper().strip()
            px = _px(row)
            if name and px is not None:
                qmap[name] = px
    return qmap


def _dict_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _lot_rows(snap: dict[str, Any] | None, world: Any) -> list[dict[str, Any]]:
    if world is not None:
        rows = _dict_rows(getattr(world, "positions", None))
        if rows:
            return rows
    if isinstance(snap, dict):
        return _dict_rows(snap.get("positions"))
    return []


def _working_rows(snap: dict[str, Any] | None, world: Any) -> list[dict[str, Any]]:
    if world is not None:
        rows = _dict_rows(getattr(world, "working_orders", None))
        if rows:
            return rows
    if isinstance(snap, dict):
        return _dict_rows(snap.get("working_orders"))
    return []


def _mark_fact_rows(
    snap: dict[str, Any] | None,
    world: Any,
    bag: dict[str, Any],
) -> list[dict[str, str]]:
    from abcxauto.world_state import allocation_facts, allocation_line

    qmap = _as_quote_map(snap, world)
    lots = _lot_rows(snap, world)
    cash = None
    nl = None
    if world is not None:
        nl = getattr(world, "net_liquidation", None)
        port = getattr(world, "portfolio_risk", None)
        if isinstance(port, dict):
            cap = port.get("capital_liquidity") if isinstance(port.get("capital_liquidity"), dict) else {}
            cash = cap.get("total_cash")
    if cash is None and isinstance(snap, dict):
        cap = snap.get("capital_liquidity") if isinstance(snap.get("capital_liquidity"), dict) else {}
        cash = cap.get("total_cash")
        if nl is None:
            nl = snap.get("net_liquidation") or snap.get("nl")
    alloc = allocation_facts(
        lots,
        net_liq=nl,
        total_cash=cash,
        quotes=qmap,
        orders=_working_rows(snap, world),
    )
    for lot in alloc.get("lots") or []:
        if isinstance(lot, dict):
            _add_symbol(bag, lot.get("symbol"))
    held = {
        str(lot.get("symbol") or "").upper()
        for lot in (alloc.get("lots") or [])
        if isinstance(lot, dict) and lot.get("symbol")
    }
    rows: list[dict[str, str]] = []
    line = allocation_line(alloc)
    if line:
        rows.append({"source": "marks", "text": line})
    exits: list[str] = []
    for order in _working_rows(snap, world)[:12]:
        typ = str(order.get("type") or "").upper()
        role = str(order.get("role") or "").lower()
        if typ not in {"STP", "LMT"} and role != "exit":
            continue
        sym = str(order.get("symbol") or "").upper().strip()
        if not sym:
            continue
        _add_symbol(bag, sym)
        if typ == "STP" or order.get("stop") is not None:
            px = order.get("stop")
            if px is not None:
                exits.append(f"{sym} stp {px}")
                continue
        if typ == "LMT" or order.get("lmt") is not None:
            px = order.get("lmt")
            if px is not None:
                exits.append(f"{sym} tgt {px}")
    if exits:
        rows.append({"source": "marks", "text": "exits " + " / ".join(exits[:8])})
    tape: list[str] = []
    for name in ("SPY", "QQQ", "IWM", "VIX"):
        if name in qmap and name not in held:
            tape.append(f"{name} {qmap[name]}")
    if tape:
        rows.append({"source": "marks", "text": "tape " + " ".join(tape)})
    return rows


def _spoken_conclusion(turn: Any) -> str:
    text = str(getattr(turn, "text", None) or "").strip()
    if not text:
        act = getattr(turn, "last_act", None)
        if isinstance(act, dict):
            text = str(act.get("rationale") or "").strip()
    if not text:
        return ""
    paras = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    pick = paras[-1] if paras else text
    first = pick.splitlines()[0].strip() if pick else ""
    if _MID_GATHER.match(first):
        return ""
    if re.search(r"\b(pulling|gathering|fetching)\b", pick, re.I) and len(pick) < 280:
        return ""
    if pick.endswith("...") and len(pick) < 180:
        return ""
    return pick[:400]
def _marks_conclusion(marks: list[dict[str, str]], session: str) -> str:
    bits = [str(row.get("text") or "") for row in marks if row.get("text")]
    if not bits:
        return ""
    sess = str(session or "").strip()
    prefix = f"no spoken conclude session={sess}. " if sess else "no spoken conclude. "
    return (prefix + " ".join(bits))[:400]


def write_research_brief(
    *,
    session: str = "",
    snap: dict[str, Any] | None = None,
    turn: Any = None,
    world: Any = None,
    now: datetime | None = None,
    research_card_id: str = "",
    prove_window_id: str = "",
) -> dict[str, Any]:
    """Overwrite ``data/state/research_brief.json``. Gathered color only.

    RTH and research sessions write. COLOR, never a trigger.
    No expectancy, no tickets.
    """
    sess = desk_session(session)
    bag = _bag(snap)
    if world is not None:
        for pos in getattr(world, "positions", None) or []:
            if isinstance(pos, dict):
                _add_symbol(bag, pos.get("symbol"))
        for it in getattr(world, "news_items", None) or []:
            if isinstance(it, dict):
                _add_symbol(bag, it.get("symbol"))
    if isinstance(snap, dict):
        hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else {}
        for row in hits.get("rows") or []:
            if isinstance(row, dict):
                _add_symbol(bag, row.get("symbol"))
        for raw in snap.get("scan_fetched") or []:
            _add_symbol(bag, raw)
    marks = _mark_fact_rows(snap, world, bag)
    facts = (marks + list(bag.get("facts") or []))[:FACT_CAP]
    uns = list(bag.get("uncertainties") or [])
    if not facts:
        if _NOTHING_GATHERED not in uns:
            uns.append(_NOTHING_GATHERED)
    spoken = _spoken_conclusion(turn)
    conclusion = spoken or _marks_conclusion(marks, sess)
    payload = {
        "as_of": _iso(now),
        "session": sess,
        "mode": "research",
        "symbols": list(bag.get("symbols") or [])[:SYMBOL_CAP],
        "facts": facts,
        "uncertainties": uns[:12],
        "tool_trace": list(getattr(turn, "tool_trace", None) or [])[:24],
    }
    if conclusion:
        payload["conclusion"] = conclusion
    try:
        from abcxauto.research_budget import stamp_brief_lineage

        stamp_brief_lineage(
            payload,
            research_card_id=research_card_id,
            prove_window_id=prove_window_id,
            snap=snap if isinstance(snap, dict) else None,
            now=now,
        )
    except Exception:
        logger.debug("research brief lineage stamp failed", exc_info=True)
    p = research_brief_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        logger.debug("research_brief write failed", exc_info=True)
    return payload


def rth_research_color(
    *,
    full: bool = True,
    now: datetime | None = None,
) -> str:
    """Prior-session research as COLOR. Missing/stale is stated; RTH still runs."""
    brief = load_research_brief()
    if not brief:
        return (
            "prior_session_research=missing "
            "(color, never a live trigger)."
        )
    if research_brief_stale(brief, now=now):
        as_of = str(brief.get("as_of") or "")
        return (
            "prior_session_research=stale "
            f"as_of={as_of} (color, never a live trigger)."
        )
    facts = [row for row in (brief.get("facts") or []) if isinstance(row, dict)]
    symbols = [
        str(s).strip()
        for s in (brief.get("symbols") or [])
        if str(s).strip()
    ]
    if not full:
        age_bit = ""
        ts = _parse_iso(str(brief.get("as_of") or brief.get("ts") or ""))
        clock = now or _utc_now()
        if ts is not None:
            if clock.tzinfo is None:
                clock = clock.replace(tzinfo=timezone.utc)
            age = max(0, int((clock - ts).total_seconds() // 86400))
            age_bit = f" age={age}d"
        return (
            "prior_session_research=on_disk "
            f"facts={len(facts)} symbols={len(symbols)}{age_bit} "
            "(color, never a live trigger)."
        )
    bits = [
        "prior_session_research(color, not a live trigger):",
    ]
    as_of = str(brief.get("as_of") or "")
    sess = str(brief.get("session") or "")
    if as_of or sess:
        bits.append(f"as_of={as_of} session={sess}.")
    named = symbols[:COLOR_SYMBOL_CAP]
    if named:
        bits.append(" ".join(named) + ".")
    bits.append(f"facts={len(facts)}.")
    conclusion = str(brief.get("conclusion") or "").strip()
    if conclusion:
        bits.append(conclusion[:200])
    return " ".join(bits)


def desk_mode_wake_bit(session: str = "", *, rth_full: bool = True) -> str:
    """One wake fact. Not a strategy menu."""
    if is_rth_session(session):
        return (
            f"desk_mode=rth send=allowed(existing gates). "
            f"{rth_research_color(full=rth_full)}"
        )
    sess = desk_session(session)
    return (
        f"desk_mode=research send=allowed(existing gates) "
        f"session={sess}."
    )


_HTML_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "header", "nav", "footer", "aside"}
)
_PUBLISHED_META = frozenset(
    {"article:published_time", "og:article:published_time", "datepublished"}
)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self.og_title = ""
        self.published = ""
        self._in_title = False

    def _meta_map(self, attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(k or "").lower(): str(v or "") for k, v in attrs}

    def _ingest_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        ad = self._meta_map(attrs)
        key = (ad.get("property") or ad.get("name") or ad.get("itemprop") or "").strip().lower()
        content = (ad.get("content") or "").strip()
        if not content:
            return
        if key == "og:title" and not self.og_title:
            self.og_title = content
        if key in _PUBLISHED_META and not self.published:
            self.published = content

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = str(tag or "").lower()
        if name == "meta":
            self._ingest_meta(attrs)
            return
        if name == "time" and not self.published:
            ad = self._meta_map(attrs)
            prop = (ad.get("itemprop") or ad.get("property") or "").strip().lower()
            if prop == "datepublished":
                self.published = (ad.get("datetime") or ad.get("content") or "").strip()
        if name in _HTML_SKIP_TAGS:
            self._skip += 1
            return
        if name == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        name = str(tag or "").lower()
        if name in _HTML_SKIP_TAGS and self._skip:
            self._skip -= 1
            return
        if name == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data or "").strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        else:
            self.body_parts.append(text)


def _public_http_url(url: str) -> str:
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("web url must be http(s)")
    host = str(parsed.hostname or "").strip().lower()
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("web url host refused")
    if host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("web url host refused")
    return raw


WEB_USE = "color_not_live_trigger"


def _web_host(url: str) -> str:
    return str(urlparse(str(url or "")).hostname or "").strip().lower()


def _web_payload(**fields: Any) -> dict[str, Any]:
    row = dict(fields)
    row.setdefault("source", "web")
    row.setdefault("use", WEB_USE)
    row.setdefault("as_of", _iso())
    if not row.get("host"):
        host = _web_host(str(row.get("url") or ""))
        if host:
            row["host"] = host
    return row


def _search_where(raw: str) -> str:
    key = str(raw or "").strip().lower()
    if key in ("x", "twitter"):
        return "x"
    if key == "web":
        return "web"
    return "both"


def _search_handles(raw: Any) -> list[str]:
    if isinstance(raw, str):
        parts = re.split(r"[\s,]+", raw)
    elif isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = []
    out: list[str] = []
    for part in parts:
        name = str(part or "").strip().lstrip("@")
        if name and name not in out:
            out.append(name)
        if len(out) >= 5:
            break
    return out


_X_HANDLE_IN_URL = re.compile(
    r"(?:x|twitter)\.com/(?!i/|intent/|search)([A-Za-z0-9_]{1,15})(?:/|$)",
    re.I,
)
_MD_CITE_URL = re.compile(r"\[\[[^\]]+\]\]\((https?://[^)\s]+)\)")


def _x_host(host: str) -> bool:
    h = str(host or "").strip().lower()
    return h in ("x.com", "twitter.com") or h.endswith((".x.com", ".twitter.com"))


def _x_handle_from_url(url: str) -> str:
    """xai-sdk XCitation is url-only; pull @handle from /user/status paths when present."""
    m = _X_HANDLE_IN_URL.search(str(url or ""))
    return m.group(1) if m else ""


def _cite_has(cite: Any, field: str) -> bool:
    """Prefer protobuf HasField for oneof cites; fall back for SimpleNamespace tests."""
    has = getattr(cite, "HasField", None)
    if callable(has):
        try:
            return bool(has(field))
        except (ValueError, TypeError, AttributeError):
            return False
    return getattr(cite, field, None) is not None


def _cite_rows(resp: Any) -> list[dict[str, Any]]:
    """Titles and urls from an xAI agent-tools search response.

    xai-sdk 1.19 InlineCitation oneof is url-only (WebCitation.url / XCitation.url).
    Plain ``citations`` and markdown ``[[n]](url)`` in content are folded as backup.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        url = str(row.get("url") or "").strip()
        if not url or url in seen or len(rows) >= WEB_SEARCH_CAP:
            return
        try:
            _public_http_url(url)
        except ValueError:
            return
        seen.add(url)
        rows.append(row)

    def add_url(url: str, *, kind: str = "", title: str = "", handle: str = "") -> None:
        raw = str(url or "").strip()
        if not raw:
            return
        host = _web_host(raw)
        src = kind or ("x" if _x_host(host) else "web")
        hand = str(handle or "").strip().lstrip("@")
        if src == "x" and not hand:
            hand = _x_handle_from_url(raw)
        label = str(title or "").strip() or hand or raw
        row: dict[str, Any] = {"source": src, "url": raw, "title": label[:180]}
        if hand and src == "x":
            row["handle"] = hand
        add(row)

    for cite in list(getattr(resp, "inline_citations", None) or []):
        if _cite_has(cite, "x_citation"):
            x = getattr(cite, "x_citation", None)
            x_url = str(getattr(x, "url", "") or "").strip() if x is not None else ""
            handle = str(
                getattr(x, "username", None) or getattr(x, "handle", None) or ""
            ).strip().lstrip("@")
            title = str(
                getattr(x, "title", None) or getattr(x, "snippet", None) or ""
            ).strip()
            add_url(x_url, kind="x", title=title, handle=handle)
        elif _cite_has(cite, "web_citation"):
            web = getattr(cite, "web_citation", None)
            web_url = str(getattr(web, "url", "") or "").strip() if web is not None else ""
            title = str(getattr(web, "title", None) or "").strip()
            add_url(web_url, kind="web", title=title)
    # Prefer structured inline cites; still fold plain citation urls (deduped).
    for raw in list(getattr(resp, "citations", None) or []):
        add_url(str(raw or "").strip())
    # Markdown [[n]](url) in content when structured fields were empty.
    if len(rows) < WEB_SEARCH_CAP:
        text = str(getattr(resp, "content", "") or "")
        for m in _MD_CITE_URL.finditer(text):
            add_url(m.group(1))
            if len(rows) >= WEB_SEARCH_CAP:
                break
    return rows


async def _xai_search_sample(query: str, tools: list[Any]) -> Any:
    """One short xAI agent-tools search. Not the desk brain, and not xhigh."""
    import asyncio

    from xai_sdk import AsyncClient
    from xai_sdk.chat import user as xai_user

    from abcxauto.config import get_config

    cfg = get_config()
    if not getattr(cfg, "xai_api_key", ""):
        raise RuntimeError("XAI_API_KEY is not set")
    client = AsyncClient(api_key=cfg.xai_api_key, timeout=WEB_SEARCH_TIMEOUT_S)
    try:
        chat = client.chat.create(
            model=WEB_SEARCH_MODEL,
            messages=[
                xai_user(
                    "Quote the matching posts and pages. "
                    "Short bullets only: who, what they said, url. No trade advice.\n"
                    f"Query: {query}"
                )
            ],
            max_tokens=600,
            tools=list(tools or []),
            max_turns=2,
            reasoning_effort="none",
            include=["inline_citations"],
        )
        return await asyncio.wait_for(chat.sample(), timeout=WEB_SEARCH_TIMEOUT_S)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                done = close()
                if asyncio.iscoroutine(done):
                    await done
            except Exception:
                logger.debug("xai search client close failed", exc_info=True)


async def search_public(
    query: str,
    *,
    where: str = "both",
    handles: Any = None,
) -> dict[str, Any]:
    """Public web and/or X search. Color only. Not send geometry."""
    q = str(query or "").strip()
    which = _search_where(where)
    if not q:
        return _web_payload(error="web search needs a query", where=which)
    q = q[:240]
    names = _search_handles(handles)
    try:
        from xai_sdk.tools import web_search, x_search
    except Exception:
        return _web_payload(error="web search unavailable", query=q, where=which)
    tools: list[Any] = []
    if which in ("web", "both"):
        tools.append(web_search(user_location_country="US"))
    if which in ("x", "both"):
        x_kw: dict[str, Any] = {}
        if names:
            x_kw["allowed_x_handles"] = names
        tools.append(x_search(**x_kw))
    if not tools:
        return _web_payload(error="web search unavailable", query=q, where=which)
    try:
        resp = await _xai_search_sample(q, tools)
    except Exception as exc:
        logger.warning("web search failed: %s", type(exc).__name__)
        return _web_payload(error="web search unavailable", query=q, where=which)
    text = str(getattr(resp, "content", "") or "").strip()
    results = _cite_rows(resp)
    return _web_payload(
        query=q,
        where=which,
        text=text[:WEB_TEXT_CAP],
        results=results,
        n=len(results),
    )


async def fetch_public_page(url: str) -> dict[str, Any]:
    """Thin public GET. Title + short text. COLOR, not a live trigger. Not a crawler."""
    try:
        target = _public_http_url(url)
    except ValueError as exc:
        return _web_payload(error=str(exc), url=str(url or ""))
    try:
        import httpx
    except Exception as exc:
        return _web_payload(error=f"httpx unavailable: {exc}", url=target)
    try:
        async with httpx.AsyncClient(timeout=WEB_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(
                target,
                headers={"User-Agent": "ABCXAUTO-research/1.0"},
            )
    except Exception as exc:
        return _web_payload(error=f"fetch failed: {exc}", url=target)
    body = resp.content[:WEB_MAX_BYTES] if resp.content else b""
    try:
        html = body.decode(resp.encoding or "utf-8", errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.debug("web html parse failed", exc_info=True)
    title = str(parser.og_title or "").strip() or " ".join(parser.title_parts).strip()
    text = " ".join(parser.body_parts).strip()
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    published = str(parser.published or "").strip()
    fields: dict[str, Any] = {
        "url": str(resp.url) if getattr(resp, "url", None) else target,
        "status": int(getattr(resp, "status_code", 0) or 0),
        "title": title[:200],
        "text": text[:WEB_TEXT_CAP],
    }
    if published:
        fields["published"] = published
    return _web_payload(**fields)
