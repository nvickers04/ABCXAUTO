"""Live-card notes used as hunt hints. Prose is not a send gate.

Hold / gap / tape / session / book sentences cannot invent a refuse.
``apply_hunt_send_sketch`` is a no-op — the notebook does not fill tickets.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from abcxauto.playbook.hub import hub as _hub
from abcxauto.playbook.schema import (
    card_key,
    notebook_text,
    walk_cards,
)

logger = logging.getLogger(__name__)

_HUNT_PREFIX = frozenset({
    "book",
    "status",
    "playbook",
    "write_lab_playbook",
    "write_desk_lessons",
    "self_tune",
    "set_wake",
})
_DEFAULT_HUNT_ORDER = ("scan", "news", "quote", "candles", "send")
_DEFAULT_MANAGE_ORDER = ("book", "fills", "quote", "candles")


def _tool_names(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= 16:
            break
    return out


def _next_in_order(order: list[str], done: list[str]) -> str:
    seen = {str(name) for name in done}
    for name in order:
        if name not in seen:
            return name
    return order[-1] if order else "book"


def _effective_tool_trace(
    this_look: list[str] | None,
    last_look: list[str] | None,
    *,
    managing: bool,
) -> list[str]:
    """This look's tools, or last look's hunt if this look has not started one.

    A completed send starts a new hunt when the book is flat. An open book
    uses the manage order and does not inherit last look's scan loop.
    """
    this = _tool_names(this_look)
    last = _tool_names(last_look)
    if any(name not in _HUNT_PREFIX for name in this):
        return this
    if managing or "send" in last:
        return this
    return last + this


def _screen_quoted(raw: Any) -> bool:
    """True when a scan already stamped IBKR last/bid/ask on its rows."""
    if raw is True:
        return True
    if isinstance(raw, (int, float)) and raw > 0:
        return True
    if not isinstance(raw, dict):
        return False
    try:
        if int(raw.get("quoted") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    hits = raw.get("scan_hits") if isinstance(raw.get("scan_hits"), dict) else raw
    if not isinstance(hits, dict):
        return False
    try:
        if int(hits.get("quoted") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    for row in hits.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if row.get("last") is not None or row.get("ibkr") or row.get("bid") is not None:
            return True
    return False


def _scan_carries_news(raw: Any) -> bool:
    """True when a scan already nested MDA headlines on the same hits."""
    if raw is True:
        return True
    if isinstance(raw, list):
        return any(
            isinstance(item, dict)
            and (item.get("headline") or item.get("title") or item.get("summary"))
            for item in raw
        )
    if not isinstance(raw, dict):
        return False
    items = raw.get("news")
    if isinstance(items, list) and items:
        return True
    for row in list(raw.get("hits") or []) + list(raw.get("rows") or []):
        if not isinstance(row, dict):
            continue
        if row.get("news"):
            return True
        mda = row.get("mda")
        if isinstance(mda, dict) and mda.get("news"):
            return True
    return False


_CARD_RISK_RE = re.compile(r"(?i)(?:dollar\s+)?risk\s*[≤<=]{1,2}\s*([\d.]+)\s*%")
_CARD_NOTIONAL_RE = re.compile(r"(?i)notional\s*[≤<=]{1,2}\s*([\d.]+)\s*%")
_CARD_MIN_GAP_RE = re.compile(r"(?:>=|≥)\s*([\d.]+)\s*%")
_CARD_MIN_PRICE_RE = re.compile(r"(?i)sub-?\s*\$?\s*([\d.]+)")
_CARD_TIGHT_SPREAD_RE = re.compile(r"(?i)tight.{0,24}spread|spread.{0,24}tight")
_CARD_REENTRY_RE = re.compile(r"(?i)do not re-enter|same session")
_CARD_NO_ADD_RE = re.compile(r"(?i)\bno add\b")
_CARD_ONE_NAME_RE = re.compile(r"(?i)\bone name\b")
_CARD_SKIP_SPY_RE = re.compile(
    r"(?i)\bSPY\b.{0,48}(?:same session|scrape)|(?:skip|no|not|never|do not).{0,24}\bSPY\b"
)
_CARD_SESSION_RE = re.compile(
    r"(?i)opening low|opening high|session low|session high|gap retrace"
)
_CARD_HOLD_OPEN_RE = re.compile(
    r"(?i)hold(?:s|ing)? above(?: the)? open(?!ing)"
)


def _send_facts_from_row(type_name: str, card: dict[str, Any]) -> dict[str, Any]:
    shape = str(card.get("shape") or "")
    upper = shape.upper()
    direction = (
        "LONG" if "LONG" in upper else ("SHORT" if "SHORT" in upper else None)
    )
    risk_m = _CARD_RISK_RE.search(shape)
    notional_m = _CARD_NOTIONAL_RE.search(shape)
    try:
        risk_pct = float(risk_m.group(1)) if risk_m else None
    except (TypeError, ValueError):
        risk_pct = None
    try:
        notional_pct = float(notional_m.group(1)) if notional_m else None
    except (TypeError, ValueError):
        notional_pct = None
    return {
        "type": type_name,
        "card": card.get("name"),
        "direction": direction,
        "risk_pct": risk_pct,
        "notional_pct": notional_pct,
    }


def live_card_send_facts(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> dict[str, Any]:
    """Type, card, direction, and size caps from a named card or the first live one."""
    named = str(card or "").strip().lower()
    for type_name, row in _walk_testing(book):
        if named and str(row.get("name") or "").strip().lower() != named:
            continue
        return _send_facts_from_row(type_name, row)
    return {}


def live_card_gap_floors(
    book: dict[str, Any] | None = None,
    *,
    deepest: float | None = None,
) -> list[dict[str, Any]]:
    """Each testing card's |gap| floor, and whether ``deepest`` clears it."""
    out: list[dict[str, Any]] = []
    for _type_name, row in _walk_testing(book):
        gap = _card_min_gap_pct(row)
        if gap is None:
            continue
        item: dict[str, Any] = {"card": row.get("name"), "min_gap_pct": gap}
        if deepest is not None:
            item["met"] = deepest + 1e-9 >= gap
        out.append(item)
    return out


def _is_live_hypothesis(card: Any) -> bool:
    """Grok's testing/working card. Virgin locked starters are catalog, not a hunt."""
    if not isinstance(card, dict) or not card.get("name"):
        return False
    if str(card.get("status") or "testing").strip().lower() == "retired":
        return False
    if card.get("locked") is True:
        return False
    return True


def live_hypothesis_keys(state: dict[str, Any] | None) -> set[tuple[str, str]]:
    """Filed testing/working unlocked cards. Locked starters do not count."""
    keys: set[tuple[str, str]] = set()
    for type_name, card in walk_cards(state if isinstance(state, dict) else {}):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        keys.add(card_key(type_name, card.get("name")))
    return keys


def live_hypothesis_count(state: dict[str, Any] | None = None) -> int:
    return len(live_hypothesis_keys(state))


def _is_open_notebook_card(card: Any) -> bool:
    """Non-retired card. Lock is seed identity, not hide-from-wake."""
    if not isinstance(card, dict) or not card.get("name"):
        return False
    if str(card.get("status") or "testing").strip().lower() == "retired":
        return False
    return True


def _testing_card(
    book: dict[str, Any] | None,
    card_name: Any = None,
) -> dict[str, Any] | None:
    """Named testing card, or the first live card when no name is given."""
    state = book if isinstance(book, dict) else _hub().load_lab()
    want = str(card_name or "").strip().lower()
    first: dict[str, Any] | None = None
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        if first is None:
            first = card
        if want and str(card.get("name") or "").strip().lower() == want:
            return card
    return None if want else first


def _card_min_gap_pct(card: dict[str, Any] | None) -> float | None:
    row = card if isinstance(card, dict) else {}
    match = _CARD_MIN_GAP_RE.search(str(row.get("when_on") or ""))
    if not match:
        return None
    try:
        val = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _walk_testing(
    book: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    state = book if isinstance(book, dict) else _hub().load_lab()
    out: list[tuple[str, dict[str, Any]]] = []
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        out.append((type_name, card))
    return out


def _tightest_matching_card(
    book: dict[str, Any] | None,
    mag: float | None,
    *,
    card_name: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """Named card if its floor is met, else the tightest testing card the gap clears."""
    want = str(card_name or "").strip().lower()
    hits: list[tuple[float, str, dict[str, Any]]] = []
    for type_name, card in _walk_testing(book):
        if want and str(card.get("name") or "").strip().lower() != want:
            continue
        floor = _card_min_gap_pct(card)
        if floor is not None and (mag is None or mag + 1e-9 < floor):
            continue
        hits.append((floor or 0.0, type_name, card))
    if not hits:
        return None
    hits.sort(key=lambda row: row[0], reverse=True)
    return hits[0][1], hits[0][2]


def live_card_min_gap_pct(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> float | None:
    """|gap| floor on a named card, else the loosest floor among testing cards.

    Scan paint uses the loosest floor so a 3% sibling can fire while a 6%
    card stays on the book. Send uses ``card=`` so the ticket's own floor binds.
    """
    state = book if isinstance(book, dict) else _hub().load_lab()
    named = str(card or "").strip()
    if named:
        return _card_min_gap_pct(_testing_card(state, named))
    floors: list[float] = []
    for type_name, row in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(row):
            continue
        gap = _card_min_gap_pct(row)
        if gap is not None:
            floors.append(gap)
    return min(floors) if floors else None


def _session_gap_mag(session: Any) -> float | None:
    if not isinstance(session, dict):
        return None
    raw = session.get("gap_pct", session.get("open_gap_pct"))
    if raw is None:
        return None
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return None


def live_card_needs_session(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    """True when the card's stop is the opening low / gap retrace."""
    row = _testing_card(book, card)
    if not row:
        return False
    text = " ".join(
        str(row.get(key) or "")
        for key in ("shape", "when_on", "invalidation", "thesis")
    )
    return bool(_CARD_SESSION_RE.search(text))


def live_card_needs_hold_above_open(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    """True when the card wants last above the RTH open, not only the low."""
    row = _testing_card(book, card)
    if row is not None:
        text = " ".join(
            str(row.get(key) or "")
            for key in ("when_on", "shape", "thesis")
        )
        return bool(_CARD_HOLD_OPEN_RE.search(text))
    return bool(_CARD_HOLD_OPEN_RE.search(_live_card_prose(book, ("when_on", "shape", "thesis"))))


def live_card_session_error(
    params: dict[str, Any] | None,
    session: Any = None,
    book: dict[str, Any] | None = None,
) -> str:
    """No-op. Card prose is a notebook, not a hold / gap / candles send gate."""
    return ""


def session_card_open_print_error(
    params: dict[str, Any] | None,
    session: Any = None,
    book: dict[str, Any] | None = None,
    *,
    market_session: str = "",
) -> str:
    """Refuse session-card new risk until today's RTH open print exists.

    Tape fact, not card prose. Hold / gap / candles / sitting on the low
    still cannot invent a refuse. ``live_card_session_error`` stays a no-op.
    """
    p = params if isinstance(params, dict) else {}
    try:
        if not live_card_needs_session(book, card=p.get("card")):
            return ""
    except Exception:
        return ""
    try:
        from abcxauto.structure_grade import session_usable
    except Exception:
        def session_usable(s: Any) -> bool:
            return isinstance(s, dict) and s.get("today") is True
    if session_usable(session):
        return ""
    sess = str(market_session or "").lower()
    prior_day = isinstance(session, dict) and session.get("today") is False
    if sess == "regular" and not prior_day:
        return ""
    return "session card needs today's opening print"


def _live_card_prose(
    book: dict[str, Any] | None,
    keys: tuple[str, ...],
    type_keys: tuple[str, ...] = (),
) -> str:
    state = book if isinstance(book, dict) else _hub().load_lab()
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        bits = [str(card.get(key) or "") for key in keys]
        stanza = types.get(type_name) if isinstance(types.get(type_name), dict) else {}
        bits.extend(str(stanza.get(key) or "") for key in type_keys)
        return " ".join(bits)
    return ""


def live_card_needs_tight_spread(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    row = _testing_card(book, card)
    if row is not None:
        text = " ".join(
            str(row.get(key) or "")
            for key in ("when_on", "thesis", "shape")
        )
        return bool(_CARD_TIGHT_SPREAD_RE.search(text))
    return bool(
        _CARD_TIGHT_SPREAD_RE.search(
            _live_card_prose(book, ("when_on", "thesis", "shape"))
        )
    )


def live_card_skips_spy(book: dict[str, Any] | None = None) -> bool:
    text = " ".join(
        [
            _live_card_prose(
                book,
                ("when_on", "shape", "invalidation", "thesis"),
                ("gotchas", "review"),
            ),
            notebook_text(book if isinstance(book, dict) else _hub().load_lab())[:800],
        ]
    )
    return bool(_CARD_SKIP_SPY_RE.search(text))


def _tape_symbols(quoted: Any) -> set[str]:
    names: set[str] = set()
    blobs: list[dict[str, Any]] = []
    if isinstance(quoted, dict):
        hits = quoted.get("scan_hits")
        if isinstance(hits, dict):
            blobs.append(hits)
        blobs.append(quoted)
    for blob in blobs:
        for row in blob.get("rows") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("symbol") or "").upper().strip()
            if name:
                names.add(name)
        for raw in blob.get("symbols") or []:
            name = str(raw or "").upper().strip()
            if name:
                names.add(name)
    return names


def _has_tape_blob(quoted: Any) -> bool:
    if not isinstance(quoted, dict):
        return False
    if "scan_hits" in quoted or "rows" in quoted or quoted.get("quoted") is not None:
        return True
    return False


def _explicit_empty_tape(quoted: Any) -> bool:
    """True only when a screen ran and produced no names. Missing tape is not that."""
    if not isinstance(quoted, dict):
        return False
    hits = quoted.get("scan_hits") if isinstance(quoted.get("scan_hits"), dict) else None
    blob = hits if hits is not None else quoted
    if "rows" not in blob and blob.get("quoted") is None:
        return False
    return _tape_empty(quoted)


def _tape_empty(quoted: Any) -> bool:
    if quoted is True:
        return False
    if isinstance(quoted, (int, float)) and quoted > 0:
        return False
    if not isinstance(quoted, dict):
        return True
    try:
        if int(quoted.get("quoted") or 0) > 0:
            return False
    except (TypeError, ValueError):
        pass
    hits = quoted.get("scan_hits") if isinstance(quoted.get("scan_hits"), dict) else {}
    try:
        if int(hits.get("quoted") or 0) > 0:
            return False
    except (TypeError, ValueError):
        pass
    return not _tape_symbols(quoted)


def live_card_needs_no_reentry(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    row = _testing_card(book, card)
    text = " ".join(
        [
            " ".join(
                str((row or {}).get(key) or "")
                for key in ("invalidation", "shape", "when_on", "review")
            ),
            _live_card_prose(
                book,
                ("review", "invalidation", "shape", "when_on"),
                ("review",),
            ),
        ]
    )
    return bool(_CARD_REENTRY_RE.search(text))


def _et_day_of(ts: str) -> str:
    raw = str(ts or "").strip()
    if not raw:
        return ""
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return raw[:10] if len(raw) >= 10 else ""


def card_sent_symbol_today(card: str, symbol: str) -> bool:
    """True when this card already opened new risk in ``symbol`` today (ET)."""
    name = str(card or "").strip().lower()
    want = str(symbol or "").upper().strip()
    if not want:
        return False
    try:
        from abcxauto.opportunity_scan import _et_calendar_day

        today = _et_calendar_day()
    except Exception:
        today = ""
    if not today:
        return False
    for row in _hub()._card_sends():
        if not row.get("new_risk"):
            continue
        if str(row.get("symbol") or "").upper() != want:
            continue
        if name and str(row.get("card") or "").strip().lower() != name:
            continue
        if _et_day_of(str(row.get("ts") or "")) == today:
            return True
    return False


def _scan_hit_row(snap: dict[str, Any] | None, symbol: str) -> dict[str, Any]:
    if not isinstance(snap, dict) or not symbol:
        return {}
    want = str(symbol).upper()
    blobs: list[dict[str, Any]] = []
    hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else None
    if hits:
        blobs.append(hits)
    blobs.append(snap)
    for blob in blobs:
        for row in blob.get("rows") or []:
            if isinstance(row, dict) and str(row.get("symbol") or "").upper() == want:
                return row
    return {}


def _positive_px(raw: Any) -> float | None:
    if isinstance(raw, dict):
        return _positive_px(raw.get("last")) or _positive_px(raw.get("mid"))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _row_ibkr_last(row: dict[str, Any] | None) -> float | None:
    if not isinstance(row, dict):
        return None
    ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else {}
    return _positive_px(ibkr.get("last")) or _positive_px(row.get("last"))


def ibkr_live_last(
    symbol: str,
    *,
    snap: dict[str, Any] | None = None,
    quoted: Any = None,
) -> float | None:
    """IBKR last for a name: quote map first, then this look's scan print."""
    want = str(symbol or "").upper()
    if not want:
        return None
    for blob in (snap, quoted):
        if not isinstance(blob, dict):
            continue
        qmap = blob.get("ibkr_live_quotes")
        if isinstance(qmap, dict):
            px = _positive_px(qmap.get(want))
            if px:
                return px
        px = _row_ibkr_last(_scan_hit_row(blob, want))
        if px:
            return px
    return None


def _open_stk_symbols(positions: Any) -> list[str]:
    out: list[str] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        sec = str(pos.get("sec_type") or pos.get("secType") or "STK").upper()
        if sec not in ("STK", "ETF"):
            continue
        try:
            qty = float(pos.get("quantity") or pos.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        name = str(pos.get("symbol") or "").upper().strip()
        if name and name not in out:
            out.append(name)
    return out


def live_card_book_error(
    params: dict[str, Any] | None,
    positions: Any = None,
    book: dict[str, Any] | None = None,
) -> str:
    """No-op. Card prose is a notebook, not a no-add / one-name send gate."""
    return ""


def _spread_width(session: Any, snap: dict[str, Any] | None, symbol: str) -> float | None:
    sources = []
    if isinstance(session, dict):
        sources.append(session)
    row = _scan_hit_row(snap, symbol)
    if row:
        sources.append(row)
        ibkr = row.get("ibkr")
        if isinstance(ibkr, dict):
            sources.append(ibkr)
    for src in sources:
        try:
            bid = float(src.get("bid"))
            ask = float(src.get("ask"))
        except (TypeError, ValueError):
            continue
        if bid > 0 and ask > bid:
            return ask - bid
    return None


def live_card_tape_error(
    params: dict[str, Any] | None,
    session: Any = None,
    snap: dict[str, Any] | None = None,
    book: dict[str, Any] | None = None,
) -> str:
    """No-op. Card prose is a notebook, not a price / spread / reentry send gate."""
    return ""


_CAP_SCAN_WORDS = {
    "mega": "mega_cap",
    "mega_cap": "mega_cap",
    "large": "large_cap",
    "large_cap": "large_cap",
    "mid": "mid_cap",
    "mid_cap": "mid_cap",
}


def _live_card_scan_line(book: dict[str, Any] | None = None) -> str:
    state = book if isinstance(book, dict) else _hub().load_lab()
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        return str(card.get("scan") or "")
    return ""


def live_card_scan_arenas(book: dict[str, Any] | None = None) -> list[str]:
    """Known arena names written on the first live card's scan line."""
    try:
        from abcxauto.universe import ARENA_CATALOG

        known = {str(key).lower() for key in ARENA_CATALOG}
    except Exception:
        known = {"most_active", "top_losers", "mega_cap", "large_cap"}
    out: list[str] = []
    for token in re.findall(r"[a-z][a-z0-9_]+", _live_card_scan_line(book).lower()):
        if token in known and token not in out:
            out.append(token)
    return out


def live_card_scan_screens(
    book: dict[str, Any] | None = None,
    *,
    scan: str | None = None,
) -> list[dict[str, str]]:
    """Written scan line as compose screens: universe × sort.

    ``most_active + top_losers; mega/large only`` is mega/large ranked by
    those sorts, not an unfiltered MOST_ACTIVE junk tape.
    """
    try:
        from abcxauto.universe import ARENA_CATALOG
    except Exception:
        ARENA_CATALOG = {}
    if scan is not None:
        text = scan
    else:
        lines: list[str] = []
        state = book if isinstance(book, dict) else _hub().load_lab()
        for type_name, card in walk_cards(state):
            if not type_name:
                continue
            if not _is_live_hypothesis(card):
                continue
            line = str(card.get("scan") or "").strip()
            if line and line not in lines:
                lines.append(line)
        text = " ".join(lines) if lines else _live_card_scan_line(book)
    if not text:
        return []
    caps: list[str] = []
    sorts: list[str] = []
    for token in re.findall(r"[a-z][a-z0-9_]+", text.lower()):
        cap = _CAP_SCAN_WORDS.get(token)
        if cap:
            if cap not in caps:
                caps.append(cap)
            continue
        meta = ARENA_CATALOG.get(token) or {}
        if meta.get("group") == "scans" and token not in sorts:
            sorts.append(token)
        elif meta.get("group") == "caps" and token not in caps:
            caps.append(token)
    screens: list[dict[str, str]] = []
    if caps and sorts:
        pairs = ((cap, sort) for cap in caps for sort in sorts)
    elif sorts:
        pairs = ((sort, sort) for sort in sorts)
    elif caps:
        pairs = ((cap, "") for cap in caps)
    else:
        pairs = ()
    for arena, sort in pairs:
        code = ""
        if sort:
            ibkr = (ARENA_CATALOG.get(sort) or {}).get("ibkr") or {}
            code = str(ibkr.get("scanCode") or "").strip().upper()
        if arena:
            row = {"arena": arena}
            if code:
                row["scan_code"] = code
            screens.append(row)
        if len(screens) >= 8:
            break
    return screens


def live_card_scan_constraints(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> dict[str, Any]:
    """Price floor, CORP-only, and cap floor written on a card's scan line."""
    named = str(card or "").strip()
    if named:
        row = _testing_card(book, named)
        line = str((row or {}).get("scan") or "")
    else:
        line = _live_card_scan_line(book)
    if not line:
        return {}
    out: dict[str, Any] = {}
    match = _CARD_MIN_PRICE_RE.search(line)
    if match:
        try:
            px = float(match.group(1))
            if px > 0:
                out["min_price"] = px
        except (TypeError, ValueError):
            pass
    if re.search(r"(?i)levered", line):
        out["skip_levered"] = True
    caps: list[str] = []
    for screen in live_card_scan_screens(book, scan=line):
        arena = str(screen.get("arena") or "")
        if arena in ("mega_cap", "large_cap", "mid_cap") and arena not in caps:
            caps.append(arena)
    if not caps:
        for token in re.findall(r"[a-z][a-z0-9_]+", line.lower()):
            cap = _CAP_SCAN_WORDS.get(token)
            if cap and cap not in caps:
                caps.append(cap)
    if caps:
        out["caps"] = caps
    return out


def apply_card_constraints_to_spec(
    spec: dict[str, Any] | None,
    book: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Overlay the written scan floors onto one IBKR spec. Never lowers a tighter floor."""
    applied: dict[str, Any] = {}
    if not spec:
        return spec, applied
    constraints = live_card_scan_constraints(book)
    if not constraints:
        return spec, applied
    out = dict(spec)
    min_px = constraints.get("min_price")
    if min_px is not None:
        try:
            cur = float(out.get("abovePrice") or 0)
        except (TypeError, ValueError):
            cur = 0.0
        out["abovePrice"] = max(cur, float(min_px))
        applied["card_min_price"] = float(min_px)
        applied["above_price"] = out["abovePrice"]
    if constraints.get("skip_levered"):
        out["stockTypeFilter"] = "CORP"
        applied["card_stock_type"] = "CORP"
    floors: list[float] = []
    try:
        from abcxauto.universe import ARENA_CATALOG
    except Exception:
        ARENA_CATALOG = {}
    for cap in constraints.get("caps") or []:
        ibkr = (ARENA_CATALOG.get(cap) or {}).get("ibkr") or {}
        raw = ibkr.get("marketCapAbove")
        if raw is None:
            continue
        try:
            floors.append(float(raw))
        except (TypeError, ValueError):
            continue
    if floors:
        floor = min(floors)
        try:
            cur = float(out.get("marketCapAbove") or 0)
        except (TypeError, ValueError):
            cur = 0.0
        out["marketCapAbove"] = max(cur, floor)
        applied["card_market_cap_above"] = floor
        applied["market_cap_above"] = out["marketCapAbove"]
    return out, applied


def drop_hits_off_card(
    rows: list[dict[str, Any]] | None,
    book: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop quoted names under the written sub-$ floor. Unknown last stays."""
    constraints = live_card_scan_constraints(book)
    min_px = constraints.get("min_price")
    src = [r for r in (rows or []) if isinstance(r, dict)]
    if not min_px:
        return src, []
    keep: list[dict[str, Any]] = []
    dropped: list[str] = []
    for row in src:
        last = row.get("last")
        if last is None:
            ibkr = row.get("ibkr")
            if isinstance(ibkr, dict):
                last = ibkr.get("last")
            elif ibkr is not None:
                last = ibkr
        if last is None:
            keep.append(row)
            continue
        try:
            if float(last) + 1e-9 < float(min_px):
                name = str(row.get("symbol") or "").upper()
                if name:
                    dropped.append(name)
                continue
        except (TypeError, ValueError):
            keep.append(row)
            continue
        keep.append(row)
    return keep, dropped


def scan_screen_key(arena: str = "", scan_code: str = "") -> str:
    name = str(arena or "").strip()
    code = str(scan_code or "").strip()
    if name and code:
        return f"{name}:{code}"
    return name or code


def _session_rows(session_range: Any) -> list[dict[str, Any]]:
    store = session_range if isinstance(session_range, dict) else {}
    if not store:
        return []
    try:
        from abcxauto.think_stream import _compact_session_range

        compact = _compact_session_range(store)
        if compact:
            store = compact
    except Exception:
        pass
    if store.get("ticket") or store.get("low") is not None or store.get("open") is not None:
        return [store]
    return [rng for rng in store.values() if isinstance(rng, dict)]


def _has_today_session(session_range: Any) -> bool:
    """True when this look already has a today RTH range to pin the stop."""
    return any(rng.get("today") is True for rng in _session_rows(session_range))


def _today_session_on_lows(session_range: Any) -> bool:
    """True when every today session is sitting on or through the opening low."""
    saw_today = False
    for rng in _session_rows(session_range):
        if rng.get("today") is not True:
            continue
        saw_today = True
        if rng.get("above_low") is not False:
            return False
    return saw_today


def _today_session_under_min_gap(session_range: Any, min_gap: float) -> bool:
    """True when every today gap we have is under the card's written floor."""
    saw_gap = False
    for rng in _session_rows(session_range):
        if rng.get("today") is not True:
            continue
        mag = _session_gap_mag(rng)
        if mag is None:
            continue
        saw_gap = True
        if mag + 1e-9 >= min_gap:
            return False
    return saw_gap


def session_target(session: Any, direction: str = "LONG") -> float | None:
    """30% retrace, or 50% if last already traded through 30. None if both are gone."""
    if not isinstance(session, dict):
        return None
    side = str(direction or "LONG").upper()
    last = session.get("last")
    try:
        last_f = float(last) if last not in (None, "") else None
    except (TypeError, ValueError):
        last_f = None
    for key in ("retrace_30", "retrace_50"):
        raw = session.get(key)
        if raw is None:
            continue
        try:
            tgt = float(raw)
        except (TypeError, ValueError):
            continue
        if last_f is None:
            return tgt
        if side == "SHORT":
            if tgt < last_f:
                return tgt
        elif tgt > last_f:
            return tgt
    return None


def hunt_send_sketch(
    session_range: Any,
    tape: Any = None,
    *,
    card: Any = None,
) -> dict[str, Any] | None:
    """Stamped ticket on today's session, if any. Clerk does not invent fields."""
    store = session_range if isinstance(session_range, dict) else {}
    if not store:
        return None
    try:
        from abcxauto.think_stream import _compact_session_range

        compact = _compact_session_range(store)
        if compact:
            store = compact
    except Exception:
        pass
    items: list[tuple[str, dict[str, Any]]] = []
    if store.get("ticket") or store.get("low") is not None or store.get("open") is not None:
        items.append(("", store))
    else:
        for key, rng in store.items():
            if isinstance(rng, dict):
                items.append((str(key), rng))

        def _gap_mag(item: tuple[str, dict[str, Any]]) -> float:
            try:
                raw = item[1].get("gap_pct", item[1].get("open_gap_pct"))
                return abs(float(raw or 0))
            except (TypeError, ValueError):
                return 0.0

        items.sort(key=_gap_mag, reverse=True)
    named = str(card or "").strip()
    if named:
        for type_name, row in _walk_testing():
            if str(row.get("name") or "").strip().lower() == named.lower():
                if type_name not in ("market_bracket", "bracket"):
                    return None
                break
    min_px = live_card_scan_constraints().get("min_price")
    no_reentry = live_card_needs_no_reentry()
    tight = live_card_needs_tight_spread()
    skip_spy = live_card_skips_spy()
    allowed = _tape_symbols(tape) if _has_tape_blob(tape) else set()
    scanned = _has_tape_blob(tape)
    for sym, rng in items:
        if rng.get("today") is not True:
            continue
        prior = rng.get("ticket") if isinstance(rng.get("ticket"), dict) else {}
        want_card = named or str(prior.get("card") or "").strip()
        mag = _session_gap_mag(rng)
        picked = _tightest_matching_card(None, mag, card_name=want_card or None)
        existing = _testing_card(None, want_card) if want_card else None
        if picked is None:
            if want_card and existing is not None:
                continue
            if not want_card and live_card_min_gap_pct() is not None:
                continue
            type_name = str(prior.get("strategy") or "")
            row = {"name": want_card or prior.get("card")}
        else:
            type_name, row = picked
        if type_name and type_name not in ("market_bracket", "bracket"):
            continue
        pick_name = str(row.get("name") or "")
        min_gap = _card_min_gap_pct(existing or row)
        if min_gap and (mag is None or mag + 1e-9 < min_gap):
            continue
        if min_px is not None and rng.get("last") is not None:
            try:
                if float(rng["last"]) + 1e-9 < float(min_px):
                    continue
            except (TypeError, ValueError):
                pass
        name = str(sym or prior.get("symbol") or "").upper()
        if skip_spy and name == "SPY":
            continue
        if scanned and name and name not in allowed:
            continue
        if no_reentry and name and card_sent_symbol_today(
            pick_name or str(prior.get("card") or ""), name
        ):
            continue
        if tight and rng.get("spread") is not None and rng.get("last") is not None:
            stop = prior.get("stop_price") if prior.get("stop_price") not in (None, "") else rng.get("low")
            if stop not in (None, ""):
                try:
                    if float(rng["spread"]) + 1e-9 >= abs(float(rng["last"]) - float(stop)):
                        continue
                except (TypeError, ValueError):
                    pass
        ticket = rng.get("ticket") if isinstance(rng.get("ticket"), dict) else {}
        hold_side = str(ticket.get("direction") or "LONG").upper()
        if rng.get("above_low") is False and hold_side != "SHORT":
            continue
        if (
            live_card_needs_hold_above_open(card=pick_name)
            and rng.get("above_open") is False
            and hold_side != "SHORT"
        ):
            continue
        tgt = session_target(rng, hold_side)
        if tgt is None and (
            rng.get("retrace_30") is not None or rng.get("retrace_50") is not None
        ):
            continue
        sketch = dict(ticket)
        # Session key names the row. Clerk does not invent the rest of the ticket.
        if name:
            sketch["symbol"] = name
        if sketch.get("symbol") and sketch.get("card"):
            return sketch
    return None


def apply_hunt_send_sketch(act: dict[str, Any], snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """No-op. Playbook is a notebook — clerk does not fill omitted send fields."""
    return None


def hunt_recipe_has(name: str, book: dict[str, Any] | None = None) -> bool:
    """Whether a live card's hunt order names this tool."""
    want = str(name or "").strip()
    if not want:
        return False
    for row in _hub().playbook_run_sheets(book, flat=True):
        if want in (row.get("tool_order") or []):
            return True
    return False



__all__ = [
    '_HUNT_PREFIX',
    '_DEFAULT_HUNT_ORDER',
    '_DEFAULT_MANAGE_ORDER',
    '_tool_names',
    '_next_in_order',
    '_effective_tool_trace',
    '_screen_quoted',
    '_scan_carries_news',
    '_CARD_RISK_RE',
    '_CARD_NOTIONAL_RE',
    '_CARD_MIN_GAP_RE',
    '_CARD_MIN_PRICE_RE',
    '_CARD_TIGHT_SPREAD_RE',
    '_CARD_REENTRY_RE',
    '_CARD_NO_ADD_RE',
    '_CARD_ONE_NAME_RE',
    '_CARD_SKIP_SPY_RE',
    '_CARD_SESSION_RE',
    '_CARD_HOLD_OPEN_RE',
    '_send_facts_from_row',
    'live_card_send_facts',
    'live_card_gap_floors',
    '_is_live_hypothesis',
    'live_hypothesis_keys',
    'live_hypothesis_count',
    '_is_open_notebook_card',
    '_testing_card',
    '_card_min_gap_pct',
    '_walk_testing',
    '_tightest_matching_card',
    'live_card_min_gap_pct',
    '_session_gap_mag',
    'live_card_needs_session',
    'live_card_needs_hold_above_open',
    'live_card_session_error',
    'session_card_open_print_error',
    '_live_card_prose',
    'live_card_needs_tight_spread',
    'live_card_skips_spy',
    '_tape_symbols',
    '_has_tape_blob',
    '_explicit_empty_tape',
    '_tape_empty',
    'live_card_needs_no_reentry',
    '_et_day_of',
    'card_sent_symbol_today',
    '_scan_hit_row',
    '_positive_px',
    '_row_ibkr_last',
    'ibkr_live_last',
    '_open_stk_symbols',
    'live_card_book_error',
    '_spread_width',
    'live_card_tape_error',
    '_CAP_SCAN_WORDS',
    '_live_card_scan_line',
    'live_card_scan_arenas',
    'live_card_scan_screens',
    'live_card_scan_constraints',
    'apply_card_constraints_to_spec',
    'drop_hits_off_card',
    'scan_screen_key',
    '_session_rows',
    '_has_today_session',
    '_today_session_on_lows',
    '_today_session_under_min_gap',
    'session_target',
    'hunt_send_sketch',
    'apply_hunt_send_sketch',
    'hunt_recipe_has',
]
