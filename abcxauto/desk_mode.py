"""RTH thin sender vs research (premarket / AH / closed).

Same session clock as the desk (``regular`` / ``premarket`` / ``postmarket`` /
``closed``). Research has no broker send path. The research brief is COLOR
for RTH, never a trigger. Do not grow SYSTEM_PROMPT from here.
"""

from __future__ import annotations

import json
import logging
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

EXPECTANCY_CAP = 10
FACT_CAP = 24
SYMBOL_CAP = 16
BRIEF_STALE_S = 18 * 3600.0
WEB_TIMEOUT_S = 8.0
WEB_MAX_BYTES = 200_000
WEB_TEXT_CAP = 2_000

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_BRIEF_PATH = _REPO / "data" / "state" / "research_brief.json"
_SNAP_BAG = "_research_bag"
_ET = ZoneInfo("America/New_York")

_CATALYST_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "earnings",
        (
            "earnings",
            "eps",
            "guidance",
            "outlook",
            "quarterly",
            "revenue",
            "beat estimates",
            "misses estimates",
        ),
    ),
    (
        "m_and_a",
        ("acquire", "acquisition", "merger", "buyout", "takeover", "deal to buy"),
    ),
    (
        "regulatory",
        ("sec ", "doj", "ftc", "fda", "antitrust", "investigation", "probe"),
    ),
    (
        "announcement",
        ("announces", "announced", "press release", "launches", "launching"),
    ),
    (
        "gap_risk",
        (
            "after hours",
            "after-hours",
            "premarket",
            "pre-market",
            "halt",
            "gap up",
            "gap down",
        ),
    ),
)

_UP_HINTS = (
    "beat",
    "beats",
    "raise",
    "raises",
    "raised",
    "surge",
    "jumps",
    "soars",
    "upgrade",
    "buyout",
)
_DOWN_HINTS = (
    "miss",
    "misses",
    "cut",
    "cuts",
    "slashes",
    "plunge",
    "drops",
    "downgrade",
    "investigation",
    "halt",
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
    """True when a finished research look must re-enter, not wait for a book poke.

    Research has no sends, so fill / order_change never arrive. A looking
    premarket session keeps looking (news / scan / web, overwrite the brief)
    until RTH roll, operator stop, or an overnight park that still applies.
    RTH spoken-no-tool still waits for a real poke. Closed / postmarket stay
    parked. Blank labels do not clock-fill into a mill — the host passes the
    resolved snap label.
    """
    raw = str(session or "").strip().lower()
    if raw in ("", "unknown"):
        return False
    sess = desk_session(raw)
    if sess == RTH_SESSION or sess not in RESEARCH_SESSIONS:
        return False
    try:
        from abcxauto.park_clock import honor_park

        if honor_park(session=sess):
            return False
    except Exception:
        return False
    return True


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
    base = _model_token(getattr(cfg, "model", None)) or "grok-4.6"
    if is_rth_session(session):
        return _model_token(getattr(cfg, "model_rth", None)) or base
    return _model_token(getattr(cfg, "model_research", None)) or base


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


def _fact_line(source: str, payload: dict[str, Any], args: dict[str, Any] | None) -> str:
    src = str(source or "").strip() or "tool"
    if src == "news":
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        heads = []
        for it in items[:4]:
            if not isinstance(it, dict):
                continue
            hl = str(it.get("headline") or "").strip()
            sym = str(it.get("symbol") or "").strip()
            if hl:
                heads.append(f"{sym + ': ' if sym else ''}{hl}"[:160])
        if heads:
            return "; ".join(heads)
        return str(payload.get("error") or payload.get("note") or "news fetched")[:200]
    if src == "scan":
        rows = payload.get("rows") or payload.get("hits") or []
        n = len(rows) if isinstance(rows, list) else 0
        deepest = ""
        if isinstance(rows, list):
            best = None
            best_mag = -1.0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                gap = row.get("open_gap_pct")
                if gap is None:
                    gap = row.get("gap_pct")
                try:
                    mag = abs(float(gap))
                except (TypeError, ValueError):
                    continue
                if mag > best_mag:
                    best_mag = mag
                    best = row
            if best is not None:
                deepest = (
                    f" deepest={best.get('symbol')} {best_mag:g}%"
                )
        return f"hits={n}{deepest} src={payload.get('source') or 'scan'}"
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
        bars = payload.get("bars") if isinstance(payload.get("bars"), list) else []
        n = len(bars)
        return (
            f"{payload.get('symbol') or 'bars'} n={n} "
            f"src={payload.get('source') or 'candles'} "
            f"use={payload.get('use') or ''}"
        ).strip()
    if src == "web":
        title = str(payload.get("title") or "").strip()
        url = str(payload.get("url") or (args or {}).get("url") or "").strip()
        err = str(payload.get("error") or "").strip()
        if err:
            return f"web error {err}"[:200]
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


def _blob_text(payload: dict[str, Any]) -> str:
    bits = [
        str(payload.get("headline") or ""),
        str(payload.get("title") or ""),
        str(payload.get("text") or ""),
        str(payload.get("why") or ""),
        str(payload.get("note") or ""),
    ]
    return " ".join(bits).lower()


def _catalyst_kind(text: str) -> str:
    blob = str(text or "").lower()
    for kind, hints in _CATALYST_KINDS:
        if any(h in blob for h in hints):
            return kind
    return ""


def _direction_bias(text: str) -> str:
    blob = str(text or "").lower()
    up = any(h in blob for h in _UP_HINTS)
    down = any(h in blob for h in _DOWN_HINTS)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return ""


def _gap_of(row: dict[str, Any]) -> float | None:
    for key in ("open_gap_pct", "gap_pct"):
        if row.get(key) is None:
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            continue
    return None


def _expectancy_from_news(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        hl = str(it.get("headline") or it.get("title") or "").strip()
        if not hl:
            continue
        kind = _catalyst_kind(hl) or "announcement"
        sym = str(it.get("symbol") or "").upper().strip()
        pub = str(it.get("publisher") or it.get("source") or "news").strip()
        out.append(
            {
                "symbol": sym,
                "catalyst": kind,
                "why": hl[:180],
                "source": f"news/{pub}" if pub else "news",
                "direction": _direction_bias(hl),
                "uncertainty": "MDA delayed ~15m",
                "invalidate": "open prints inside prior range / headline reversed",
            }
        )
    return out


def _expectancy_from_scan(rows: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        gap = _gap_of(row)
        if gap is None:
            continue
        mag = abs(gap)
        if mag < 1.5:
            continue
        ranked.append((mag, row))
    ranked.sort(key=lambda item: item[0], reverse=True)
    for mag, row in ranked[:EXPECTANCY_CAP]:
        gap = _gap_of(row) or 0.0
        sym = str(row.get("symbol") or "").upper().strip()
        why = f"open_gap={gap:g}%"
        out.append(
            {
                "symbol": sym,
                "catalyst": "gap_risk",
                "why": why,
                "source": "scan",
                "direction": "up" if gap > 0 else "down",
                "uncertainty": "gap is tape color until RTH trade",
                "invalidate": "gap fills on the open / no follow-through",
            }
        )
    return out


def _expectancy_from_web(payload: dict[str, Any]) -> list[dict[str, Any]]:
    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "").strip()
    blob = f"{title} {text}"
    kind = _catalyst_kind(blob)
    if not kind and not title:
        return []
    return [
        {
            "symbol": str(payload.get("symbol") or "").upper().strip(),
            "catalyst": kind or "announcement",
            "why": (title or text)[:180],
            "source": "web",
            "direction": _direction_bias(blob),
            "uncertainty": "public page snippet, not a live trigger",
            "invalidate": "source corrected / move already in the overnight print",
        }
    ]


def _expectancy_from_odds(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get("events") or payload.get("markets") or payload.get("rows") or []
    if not isinstance(events, list):
        return []
    out: list[dict[str, Any]] = []
    for ev in events[:6]:
        if not isinstance(ev, dict):
            continue
        title = str(ev.get("title") or ev.get("question") or ev.get("name") or "").strip()
        if not title:
            continue
        kind = _catalyst_kind(title) or "announcement"
        out.append(
            {
                "symbol": str(ev.get("symbol") or "").upper().strip(),
                "catalyst": kind,
                "why": title[:180],
                "source": "odds",
                "direction": "",
                "uncertainty": "implied prob, not send geometry",
                "invalidate": "odds reprice / event resolves the other way",
            }
        )
    return out


def build_expectancy(
    *,
    snap: dict[str, Any] | None = None,
    world: Any = None,
) -> list[dict[str, Any]]:
    """Short ranked AH/PM catalyst list. Mechanical COLOR, not a ranker product."""
    blob = snap if isinstance(snap, dict) else {}
    rows: list[dict[str, Any]] = []
    news = blob.get("news_items")
    if not isinstance(news, list):
        news = getattr(world, "news_items", None) if world is not None else None
    if isinstance(news, list):
        rows.extend(_expectancy_from_news(news))
    hits = blob.get("scan_hits") if isinstance(blob.get("scan_hits"), dict) else {}
    scan_rows = hits.get("rows") or hits.get("hits") or []
    if isinstance(scan_rows, list):
        rows.extend(_expectancy_from_scan(scan_rows))
    nested_news = hits.get("news") if isinstance(hits.get("news"), list) else []
    if nested_news:
        rows.extend(_expectancy_from_news(nested_news))
    web = blob.get("research_web") if isinstance(blob.get("research_web"), dict) else {}
    if web:
        rows.extend(_expectancy_from_web(web))
    odds = blob.get("odds") if isinstance(blob.get("odds"), dict) else {}
    if odds:
        rows.extend(_expectancy_from_odds(odds))

    ranked: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    catalyst_rank = {
        "earnings": 0,
        "m_and_a": 1,
        "regulatory": 2,
        "gap_risk": 3,
        "announcement": 4,
    }
    rows.sort(
        key=lambda r: (
            catalyst_rank.get(str(r.get("catalyst") or ""), 9),
            0 if r.get("symbol") else 1,
        )
    )
    for row in rows:
        key = (
            str(row.get("symbol") or ""),
            str(row.get("catalyst") or ""),
            str(row.get("why") or "")[:80],
        )
        if key in seen:
            continue
        seen.add(key)
        ranked.append(row)
        if len(ranked) >= EXPECTANCY_CAP:
            break
    return ranked


def write_research_brief(
    *,
    session: str = "",
    snap: dict[str, Any] | None = None,
    turn: Any = None,
    world: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Overwrite ``data/state/research_brief.json``. No order tickets."""
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
    expectancy = build_expectancy(snap=snap, world=world)
    for row in expectancy:
        _add_symbol(bag, row.get("symbol"))
    uns = list(bag.get("uncertainties") or [])
    if not expectancy:
        note = "no AH/PM catalyst expectancy this look — news/scan/web returned none"
        if note not in uns:
            uns.append(note)
    payload = {
        "as_of": _iso(now),
        "session": sess,
        "mode": "research",
        "symbols": list(bag.get("symbols") or [])[:SYMBOL_CAP],
        "facts": list(bag.get("facts") or [])[:FACT_CAP],
        "uncertainties": uns[:12],
        "expectancy": expectancy,
        "tickets": [],
        "tool_trace": list(getattr(turn, "tool_trace", None) or [])[:24],
    }
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
    if not full:
        n = len(brief.get("expectancy") or [])
        return (
            "prior_session_research=on_disk "
            f"expectancy={n} (color, never a live trigger)."
        )
    bits = [
        "prior_session_research(color, not a live trigger):",
    ]
    as_of = str(brief.get("as_of") or "")
    sess = str(brief.get("session") or "")
    if as_of or sess:
        bits.append(f"as_of={as_of} session={sess}.")
    for row in (brief.get("expectancy") or [])[:EXPECTANCY_CAP]:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "?").strip() or "?"
        why = str(row.get("why") or "").strip()
        src = str(row.get("source") or "").strip()
        cat = str(row.get("catalyst") or "").strip()
        bias = str(row.get("direction") or "").strip()
        inv = str(row.get("invalidate") or "").strip()
        piece = f"{sym} {cat} {why} src={src}".strip()
        if bias:
            piece += f" bias={bias}"
        if inv:
            piece += f" invalidate={inv}"
        bits.append(piece[:180] + ".")
    if len(bits) == 1 or (len(bits) == 2 and bits[1].startswith("as_of=")):
        bits.append("expectancy=none.")
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
        f"desk_mode=research send=blocked({REASON_RESEARCH_NO_SEND}) "
        f"session={sess}."
    )


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = str(tag or "").lower()
        if name in ("script", "style", "noscript"):
            self._skip += 1
            return
        if name == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        name = str(tag or "").lower()
        if name in ("script", "style", "noscript") and self._skip:
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


async def fetch_public_page(url: str) -> dict[str, Any]:
    """Thin research-only GET. Title + short text. Not a crawler."""
    try:
        target = _public_http_url(url)
    except ValueError as exc:
        return {"error": str(exc), "url": str(url or ""), "source": "web"}
    try:
        import httpx
    except Exception as exc:
        return {"error": f"httpx unavailable: {exc}", "url": target, "source": "web"}
    try:
        async with httpx.AsyncClient(timeout=WEB_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(
                target,
                headers={"User-Agent": "ABCXAUTO-research/1.0"},
            )
    except Exception as exc:
        return {"error": f"fetch failed: {exc}", "url": target, "source": "web"}
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
    title = " ".join(parser.title_parts).strip()
    text = " ".join(parser.body_parts).strip()
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
    return {
        "url": str(resp.url) if getattr(resp, "url", None) else target,
        "status": int(getattr(resp, "status_code", 0) or 0),
        "title": title[:200],
        "text": text[:WEB_TEXT_CAP],
        "source": "web",
        "use": "research_expectancy_not_send",
    }
