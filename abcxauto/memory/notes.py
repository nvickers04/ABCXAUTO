"""Durable notes in journal.db. Fetch-only; never wake/book/status/prompt bodies.

Park / chat-reset leave this table alone.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from abcxauto.memory.journal_support import (
    _et_calendar_date,
    _et_day_utc_range,
    _json_dumps,
    _parse_ts,
    _row_ts,
    _utc_now_iso,
)

logger = logging.getLogger("abcxauto.memory.journal")

KIND_FACT = "fact"
KIND_EVENT = "event"
KIND_INVALIDATE = "invalidate"
KINDS = frozenset({KIND_FACT, KIND_EVENT, KIND_INVALIDATE})

SOURCE_GROK = "grok"
SOURCE_GATE = "gate"
SOURCE_HALT = "halt"
SOURCE_FILL = "fill"
SOURCE_IMPORT = "import"
SOURCE_BROKER = "broker"
SOURCES = frozenset(
    {
        SOURCE_GROK,
        SOURCE_GATE,
        SOURCE_HALT,
        SOURCE_FILL,
        SOURCE_IMPORT,
        SOURCE_BROKER,
    }
)

# Farm / connectivity chatter — log only, never a durable note.
_QUIET_IBKR_CODES = frozenset({2104, 2106, 2108, 2158})

MAX_LIVE = 32
MAX_BODY = 160
MAX_TAGS = 8
RETENTION_DAYS = 14
MAX_POINTER_TAGS = 4

NOTES_DDL = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    tags_json TEXT,
    body TEXT NOT NULL,
    evidence TEXT,
    invalidate TEXT,
    expires_at TEXT NOT NULL,
    source TEXT NOT NULL,
    rev INTEGER NOT NULL DEFAULT 1,
    invalidated_at TEXT,
    reason_code TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_expires_at ON notes(expires_at);
CREATE INDEX IF NOT EXISTS idx_notes_source_reason_ts
    ON notes(source, reason_code, ts);
"""

_IMPERATIVE_HEAD = re.compile(
    r"^\s*(always|never|do not|don't|do\s+not|you must|ban)\b",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_SNAKE_RE = re.compile(r"\b([a-z][a-z0-9_]{2,40})\b")


def notes_wake_bit(*, now: datetime | None = None) -> str:
    """Notes counts/tags/ages plus live cards. Empty when nothing live. Never a body."""
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        notes = journal.notes_pointer(now=now)
    except Exception:
        logger.debug("notes wake pointer failed", exc_info=True)
        return ""
    try:
        from abcxauto.memory.cards import cards_wake_bit

        cards = cards_wake_bit(now=now)
    except Exception:
        logger.debug("cards wake pointer failed", exc_info=True)
        cards = ""
    return " ".join(x for x in (notes, cards) if x)


def lecture_error(body: str) -> str:
    """Empty if the body is an observation; else a reject reason."""
    text = str(body or "").strip()
    if not text:
        return "empty_body"
    if _IMPERATIVE_HEAD.match(text):
        return "imperative_body"
    return ""


def _now(now: datetime | None = None) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


def _slug(raw: Any, fallback: str) -> str:
    text = _SLUG_RE.sub("-", str(raw or "").strip().lower()).strip("-")
    if not text:
        text = fallback
    return text[:56]


def _tags_of(raw: Any) -> list[str]:
    if isinstance(raw, str):
        bits = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        bits = [str(p).strip() for p in raw]
    else:
        bits = []
    out: list[str] = []
    for bit in bits:
        tag = _slug(bit, "")
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def _parse_tags(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return _tags_of(raw)
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return _tags_of(raw)
    return _tags_of(data)


def _age_days(ts: Any, now: datetime) -> int | None:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    sec = (now - dt).total_seconds()
    if sec < 0:
        return 0
    return int(sec // 86400)


def _expired(row: dict[str, Any], now: datetime) -> bool:
    exp = _parse_ts(row.get("expires_at"))
    if exp is None:
        return False
    return exp <= now


def _live(row: dict[str, Any], now: datetime) -> bool:
    if str(row.get("invalidated_at") or "").strip():
        return False
    return not _expired(row, now)


def _status(row: dict[str, Any], now: datetime) -> str:
    if str(row.get("invalidated_at") or "").strip():
        return "invalidated"
    if _expired(row, now):
        return "expired"
    return "live"


def _public(row: dict[str, Any], now: datetime, *, body: bool) -> dict[str, Any]:
    out = {
        "id": row.get("id"),
        "ts": row.get("ts"),
        "kind": row.get("kind"),
        "symbol": row.get("symbol") or "",
        "tags": _parse_tags(row.get("tags_json")),
        "evidence": row.get("evidence") or "",
        "invalidate": row.get("invalidate") or "",
        "expires_at": row.get("expires_at"),
        "source": row.get("source"),
        "rev": int(row.get("rev") or 1),
        "status": _status(row, now),
    }
    if row.get("reason_code"):
        out["reason_code"] = row.get("reason_code")
    if row.get("invalidated_at"):
        out["invalidated_at"] = row.get("invalidated_at")
    if body:
        out["body"] = row.get("body") or ""
    return out


def _pointer_of(rows: list[dict[str, Any]], now: datetime) -> str:
    if not rows:
        return ""
    tags: list[str] = []
    ages: list[int] = []
    for row in rows:
        for tag in _parse_tags(row.get("tags_json")):
            if tag not in tags:
                tags.append(tag)
        age = _age_days(row.get("ts"), now)
        if age is not None:
            ages.append(age)
    tag_s = ",".join(tags[:MAX_POINTER_TAGS])
    if ages:
        lo, hi = min(ages), max(ages)
        age_s = f"{lo}d..{hi}d" if lo != hi else f"{lo}d"
    else:
        age_s = "0d"
    bit = f"notes={len(rows)}"
    if tag_s:
        bit += f" tags={tag_s}"
    bit += f" age={age_s}"
    return bit


def _reason_code_of(reason: str, stage: str = "") -> str:
    text = str(reason or "")
    hits = _SNAKE_RE.findall(text.lower())
    for token in hits:
        if "_" in token:
            return token[:40]
    if hits:
        return hits[0][:40]
    staged = _slug(stage, "")
    return staged or "gate"


class JournalNotes:
    """Durable notes table. Fetch-only; never send geometry."""

    def notes_pointer(self, *, now: datetime | None = None) -> str:
        if not getattr(self, "enabled", False):
            return ""
        clock = _now(now)
        try:
            self._ensure_schema()
            rows = self._notes_live_rows(clock)
        except Exception:
            logger.debug("notes pointer read failed", exc_info=True)
            return ""
        return _pointer_of(rows, clock)

    def list_notes(self, *, now: datetime | None = None) -> dict[str, Any]:
        clock = _now(now)
        if not getattr(self, "enabled", False):
            return {"pointer": "", "ids": [], "n": 0}
        try:
            self._ensure_schema()
            rows = self._notes_live_rows(clock)
        except Exception:
            logger.exception("journal.list_notes failed")
            return {"pointer": "", "ids": [], "n": 0, "error": "read_failed"}
        ids = [str(r.get("id") or "") for r in rows if r.get("id")]
        return {
            "pointer": _pointer_of(rows, clock),
            "ids": ids,
            "n": len(ids),
        }

    def get_notes(
        self,
        *,
        ids: list[str] | None = None,
        tags: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clock = _now(now)
        if not getattr(self, "enabled", False):
            return {"notes": []}
        want_ids = [_slug(x, "") for x in (ids or []) if str(x or "").strip()]
        want_tags = set(_tags_of(tags or []))
        if not want_ids and not want_tags:
            listed = self.list_notes(now=clock)
            return {"notes": [], **listed}
        try:
            self._ensure_schema()
            rows = self._notes_all_rows()
        except Exception:
            logger.exception("journal.get_notes failed")
            return {"notes": [], "error": "read_failed"}
        out: list[dict[str, Any]] = []
        for row in rows:
            nid = str(row.get("id") or "")
            row_tags = set(_parse_tags(row.get("tags_json")))
            if want_ids and nid in want_ids:
                out.append(_public(row, clock, body=True))
                continue
            if want_ids:
                continue
            if want_tags and want_tags & row_tags:
                out.append(_public(row, clock, body=True))
        return {"notes": out, "n": len(out)}

    def write_note(
        self,
        *,
        body: str = "",
        id: str = "",
        kind: str = KIND_FACT,
        symbol: str = "",
        tags: Any = None,
        evidence: str = "",
        invalidate: str = "",
        source: str = SOURCE_GROK,
        reason_code: str = "",
        expires_at: str = "",
        ts: Optional[str] = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clock = _now(now)
        text = str(body or "").strip()
        why = lecture_error(text)
        if why == "empty_body":
            return {"ok": False, "error": why}
        if why == "imperative_body":
            return {
                "ok": False,
                "error": why,
                "note": "observation with evidence, not a law",
            }
        clipped = False
        if len(text) > MAX_BODY:
            text = text[:MAX_BODY]
            clipped = True
        kind_s = str(kind or KIND_FACT).strip().lower()
        if kind_s == "rule":
            return {"ok": False, "error": "kind_rule_forbidden"}
        if kind_s not in KINDS:
            return {"ok": False, "error": "bad_kind", "kind": kind_s}
        src = str(source or SOURCE_GROK).strip().lower()
        if src not in SOURCES:
            return {"ok": False, "error": "bad_source", "source": src}
        if not getattr(self, "enabled", False):
            return {"ok": False, "error": "journal_disabled"}
        tag_list = _tags_of(tags)
        stamp = _row_ts(ts if ts is not None else clock)
        exp = str(expires_at or "").strip() or _row_ts(clock + timedelta(days=RETENTION_DAYS))
        nid = _slug(id, "") or _slug(
            "-".join([src, kind_s, (tag_list[0] if tag_list else ""), text[:24]]),
            f"n-{int(clock.timestamp())}",
        )
        sym = str(symbol or "").upper().strip() or None
        code = _slug(reason_code, "") or None
        try:
            self._ensure_schema()
            with self._connect() as conn:
                prev = conn.execute(
                    "SELECT rev FROM notes WHERE id = ?", (nid,)
                ).fetchone()
                if prev is not None:
                    rev = int(prev["rev"] or 1) + 1
                    conn.execute(
                        """
                        UPDATE notes SET ts=?, kind=?, symbol=?, tags_json=?,
                            body=?, evidence=?, invalidate=?, expires_at=?,
                            source=?, rev=?, invalidated_at=NULL, reason_code=?
                        WHERE id=?
                        """,
                        (
                            stamp,
                            kind_s,
                            sym,
                            _json_dumps(tag_list),
                            text,
                            str(evidence or "") or None,
                            str(invalidate or "") or None,
                            exp,
                            src,
                            rev,
                            code,
                            nid,
                        ),
                    )
                else:
                    rev = 1
                    conn.execute(
                        """
                        INSERT INTO notes (
                            id, ts, kind, symbol, tags_json, body, evidence,
                            invalidate, expires_at, source, rev, invalidated_at,
                            reason_code
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                        """,
                        (
                            nid,
                            stamp,
                            kind_s,
                            sym,
                            _json_dumps(tag_list),
                            text,
                            str(evidence or "") or None,
                            str(invalidate or "") or None,
                            exp,
                            src,
                            rev,
                            code,
                        ),
                    )
                self._evict_over_cap_locked(conn, clock)
                conn.commit()
            row = self._notes_one(nid)
        except Exception:
            logger.exception("journal.write_note failed")
            return {"ok": False, "error": "write_failed"}
        if not row:
            return {"ok": False, "error": "write_failed"}
        out: dict[str, Any] = {"ok": True, "note": _public(row, clock, body=True)}
        if clipped:
            out["clipped"] = True
        return out

    def invalidate_note(
        self,
        id: str = "",
        *,
        evidence: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clock = _now(now)
        nid = _slug(id, "")
        if not nid:
            return {"ok": False, "error": "id_required"}
        if not getattr(self, "enabled", False):
            return {"ok": False, "error": "journal_disabled"}
        try:
            self._ensure_schema()
            with self._connect() as conn:
                prev = conn.execute(
                    "SELECT * FROM notes WHERE id = ?", (nid,)
                ).fetchone()
                if prev is None:
                    return {"ok": False, "error": "not_found", "id": nid}
                row = dict(prev)
                extra = str(evidence or "").strip()
                inv = str(row.get("invalidate") or "")
                if extra:
                    inv = f"{inv} | {extra}".strip(" |") if inv else extra
                rev = int(row.get("rev") or 1) + 1
                conn.execute(
                    """
                    UPDATE notes SET invalidated_at=?, invalidate=?, rev=?
                    WHERE id=?
                    """,
                    (_row_ts(clock), inv or None, rev, nid),
                )
                conn.commit()
            row = self._notes_one(nid)
        except Exception:
            logger.exception("journal.invalidate_note failed")
            return {"ok": False, "error": "write_failed"}
        if not row:
            return {"ok": False, "error": "write_failed"}
        return {"ok": True, "note": _public(row, clock, body=True)}

    def record_code_note(
        self,
        *,
        source: str,
        reason_code: str,
        body: str,
        kind: str = KIND_EVENT,
        symbol: str = "",
        tags: Any = None,
        evidence: str = "",
        invalidate: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """One live row per source+reason_code per ET day. Never raises."""
        if not getattr(self, "enabled", False):
            return {"ok": False, "error": "journal_disabled"}
        clock = _now(now)
        src = str(source or "").strip().lower()
        if src not in SOURCES or src == SOURCE_GROK:
            return {"ok": False, "error": "bad_source"}
        code = _slug(reason_code, "event")
        day = _et_calendar_date(clock)
        bounds = _et_day_utc_range(day) if day else None
        nid = _slug(f"{src}-{code}-{day or clock.date().isoformat()}", f"{src}-{code}")
        try:
            self._ensure_schema()
            if bounds:
                lo, hi = bounds
                with self._connect() as conn:
                    hit = conn.execute(
                        """
                        SELECT id FROM notes
                        WHERE source=? AND reason_code=?
                          AND ts>=? AND ts<?
                        LIMIT 1
                        """,
                        (src, code, lo, hi),
                    ).fetchone()
                if hit is not None:
                    return {
                        "ok": True,
                        "capped": True,
                        "id": str(hit["id"]),
                        "reason_code": code,
                    }
            return self.write_note(
                id=nid,
                body=body,
                kind=kind,
                symbol=symbol,
                tags=tags,
                evidence=evidence,
                invalidate=invalidate,
                source=src,
                reason_code=code,
                now=clock,
            )
        except Exception:
            logger.debug("journal.record_code_note failed", exc_info=True)
            return {"ok": False, "error": "write_failed"}

    def _notes_all_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY ts ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def _notes_live_rows(self, now: datetime) -> list[dict[str, Any]]:
        return [r for r in self._notes_all_rows() if _live(r, now)]

    def _notes_one(self, nid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (nid,)
            ).fetchone()
        return dict(row) if row is not None else None

    def _evict_over_cap_locked(self, conn: Any, now: datetime) -> None:
        rows = [dict(r) for r in conn.execute("SELECT * FROM notes").fetchall()]
        live = [r for r in rows if _live(r, now)]
        live.sort(key=lambda r: str(r.get("ts") or ""))
        extra = len(live) - MAX_LIVE
        if extra <= 0:
            return
        stamp = _row_ts(now)
        for row in live[:extra]:
            conn.execute(
                "UPDATE notes SET expires_at=? WHERE id=?",
                (stamp, row.get("id")),
            )


def _recall_cards(journal: Any, op: str, blob: dict[str, Any]) -> dict[str, Any]:
    if op in ("", "list"):
        out = journal.list_cards()
        out["op"] = "list"
        out["store"] = "cards"
        out["send_geometry"] = False
        return out
    if op == "get":
        ids = blob.get("ids") or blob.get("id") or blob.get("label")
        if isinstance(ids, str):
            ids = [ids]
        labels = blob.get("labels") or blob.get("label")
        if isinstance(labels, str):
            labels = [labels]
        out = journal.get_cards(ids=list(ids or []), labels=list(labels or []) if labels else None)
        out["op"] = "get"
        out["store"] = "cards"
        out["send_geometry"] = False
        return out
    if op == "write":
        out = journal.write_card(
            label=str(blob.get("label") or blob.get("id") or ""),
            id=str(blob.get("id") or ""),
            screen=str(blob.get("screen") or ""),
            scan_code=str(blob.get("scan_code") or ""),
            evidence=blob.get("evidence"),
            direction=str(blob.get("direction") or ""),
            expectation=str(blob.get("expectation") or blob.get("body") or ""),
            invalidate=str(blob.get("invalidate") or ""),
            source=SOURCE_GROK,
        )
        out["op"] = "write"
        out["store"] = "cards"
        out["send_geometry"] = False
        return out
    if op == "invalidate":
        out = journal.invalidate_card(
            str(blob.get("id") or blob.get("label") or ""),
            evidence=str(blob.get("evidence") or blob.get("body") or ""),
        )
        out["op"] = "invalidate"
        out["store"] = "cards"
        out["send_geometry"] = False
        return out
    return {"ok": False, "error": f"unknown_op:{op}", "store": "cards"}


def recall_tool(args: dict[str, Any] | None) -> dict[str, Any]:
    """Grok tool surface. Never attaches to book/status/wake/prompt."""
    from abcxauto.memory import get_journal

    blob = args if isinstance(args, dict) else {}
    op = str(blob.get("op") or blob.get("action") or "list").strip().lower()
    store = str(blob.get("store") or "").strip().lower()
    journal = get_journal()
    if store == "cards":
        return _recall_cards(journal, op, blob)
    if op in ("", "list"):
        out = journal.list_notes()
        try:
            cards = journal.list_cards()
        except Exception:
            cards = {"pointer": "", "ids": [], "n": 0}
        out["op"] = "list"
        out["store"] = "notes"
        out["send_geometry"] = False
        out["cards_pointer"] = cards.get("pointer") or ""
        out["cards_n"] = int(cards.get("n") or 0)
        out["cards_ids"] = list(cards.get("ids") or [])
        return out
    if op == "get":
        ids = blob.get("ids") or blob.get("id")
        if isinstance(ids, str):
            ids = [ids]
        tags = blob.get("tags")
        out = journal.get_notes(ids=list(ids or []), tags=tags)
        out["op"] = "get"
        out["store"] = "notes"
        out["send_geometry"] = False
        return out
    if op == "write":
        out = journal.write_note(
            body=str(blob.get("body") or blob.get("line") or ""),
            id=str(blob.get("id") or ""),
            kind=str(blob.get("kind") or KIND_FACT),
            symbol=str(blob.get("symbol") or ""),
            tags=blob.get("tags"),
            evidence=str(blob.get("evidence") or ""),
            invalidate=str(blob.get("invalidate") or ""),
            source=SOURCE_GROK,
        )
        out["op"] = "write"
        out["store"] = "notes"
        out["send_geometry"] = False
        return out
    if op == "invalidate":
        out = journal.invalidate_note(
            str(blob.get("id") or ""),
            evidence=str(blob.get("evidence") or blob.get("body") or ""),
        )
        out["op"] = "invalidate"
        out["store"] = "notes"
        out["send_geometry"] = False
        return out
    return {"ok": False, "error": f"unknown_op:{op}", "store": "notes"}


def record_gate_note(reason: str = "", *, stage: str = "", symbol: str = "") -> None:
    try:
        from abcxauto.memory import get_journal

        code = _reason_code_of(reason, stage)
        body = f"gate refused {code}" + (f" {symbol}" if symbol else "")
        get_journal().record_code_note(
            source=SOURCE_GATE,
            reason_code=code,
            kind=KIND_EVENT,
            symbol=symbol,
            tags=["gate", code.split("_")[0]],
            body=body[:MAX_BODY],
            evidence=f"gate:{stage or code}",
            invalidate="gate allows a send; this reason_code is gone",
        )
    except Exception:
        logger.debug("gate note failed", exc_info=True)


def record_halt_note(reason: str = "", kind: str = "halt") -> None:
    try:
        from abcxauto.memory import get_journal

        code = _reason_code_of(reason, kind) or _slug(kind, "halt")
        why = str(reason or kind or "halt").strip()
        body = f"halt {kind}: {why}"[:MAX_BODY]
        get_journal().record_code_note(
            source=SOURCE_HALT,
            reason_code=code,
            kind=KIND_EVENT,
            tags=["halt", code],
            body=body,
            evidence=f"halt:{kind}",
            invalidate="halt cleared / reconnect",
        )
    except Exception:
        logger.debug("halt note failed", exc_info=True)


def record_fill_note(fill: dict[str, Any] | None) -> None:
    row = fill if isinstance(fill, dict) else {}
    sym = str(row.get("symbol") or "").upper().strip()
    side = str(row.get("side") or "").upper().strip()
    qty = row.get("quantity")
    px = row.get("price")
    bits = ["fill"]
    if sym:
        bits.append(sym)
    if side:
        bits.append(side)
    if qty is not None:
        bits.append(str(qty))
    if px is not None:
        bits.append(f"@{px}")
    body = " ".join(bits)[:MAX_BODY]
    code = f"fill-{sym}" if sym else "fill"
    try:
        from abcxauto.memory import get_journal

        get_journal().record_code_note(
            source=SOURCE_FILL,
            reason_code=code,
            kind=KIND_EVENT,
            symbol=sym,
            tags=["fill", sym.lower()] if sym else ["fill"],
            body=body,
            evidence=f"fills.exec_id={row.get('exec_id') or ''}",
            invalidate="position closed / opposite fill",
        )
    except Exception:
        logger.debug("fill note failed", exc_info=True)


def _broker_order_label(order_type: str = "") -> str:
    ot = str(order_type or "").strip().upper()
    if ot.startswith("STP") or ot in ("TRAIL", "TRAIL LIMIT", "TRAILLIMIT"):
        return "stop"
    if ot:
        return ot.lower()
    return "order"


def record_broker_order_note(
    *,
    symbol: str = "",
    order_id: Any = "",
    order_type: str = "",
    status: str = "",
    error_code: Any = "",
    detail: str = "",
) -> dict[str, Any]:
    """One durable line for cancel / IBKR order error. Fail soft; never raises.

    Example body: ``stop cancelled AVGO oid 23210 10326``.
    Quiet farm codes 2104/2106/2108/2158 write nothing.
    """
    try:
        code_raw = error_code
        try:
            code_i = int(code_raw) if code_raw not in (None, "") else None
        except (TypeError, ValueError):
            code_i = None
        if code_i is not None and code_i in _QUIET_IBKR_CODES:
            return {"ok": False, "error": "quiet_code"}

        sym = str(symbol or "").upper().strip()
        oid = str(order_id if order_id not in (None, "") else "").strip()
        code_s = str(code_i if code_i is not None else (code_raw or "")).strip()
        st = str(status or "").strip().lower()
        label = _broker_order_label(order_type)
        cancelled = st in ("cancelled", "canceled", "apicancelled")

        bits: list[str] = []
        if cancelled:
            bits.append(f"{label} cancelled")
        elif code_s:
            bits.append(f"IBKR {code_s}" if label == "order" else f"{label} IBKR {code_s}")
        else:
            bits.append(label)
        if sym:
            bits.append(sym)
        if oid:
            bits.append(f"oid {oid}")
        if code_s and cancelled:
            bits.append(code_s)
        body = " ".join(bits).strip()[:MAX_BODY]
        if not body:
            return {"ok": False, "error": "empty_body"}

        reason = ""
        if oid and code_s:
            reason = f"broker-{oid}-{code_s}"
        elif oid:
            reason = f"broker-cancel-{oid}"
        elif code_s:
            reason = f"broker-{code_s}"
        else:
            reason = f"broker-{label}-{sym or 'x'}"

        tags = ["broker"]
        if cancelled:
            tags.append("cancel")
        if code_s:
            tags.append(f"ibkr-{code_s}")
        if sym:
            tags.append(sym.lower())

        from abcxauto.memory import get_journal

        return get_journal().record_code_note(
            source=SOURCE_BROKER,
            reason_code=reason,
            kind=KIND_EVENT,
            symbol=sym,
            tags=tags,
            body=body,
            evidence=str(detail or "")[:MAX_BODY] or f"broker:{reason}",
            invalidate="order gone / new protect / flat book",
        )
    except Exception:
        logger.debug("broker order note failed", exc_info=True)
        return {"ok": False, "error": "write_failed"}


def record_book_health_notes(snap: dict[str, Any] | None) -> None:
    bag = snap if isinstance(snap, dict) else {}
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
    except Exception:
        return
    if bag.get("book_unreliable"):
        try:
            journal.record_code_note(
                source=SOURCE_GATE,
                reason_code="book_unreliable",
                kind=KIND_FACT,
                tags=["gate", "book", "nl"],
                body="book_unreliable=true; positions/orders/account incomplete",
                evidence="snap.book_unreliable",
                invalidate="book_unreliable=false",
            )
        except Exception:
            logger.debug("book_unreliable note failed", exc_info=True)
    nl = bag.get("net_liquidation")
    if nl is None:
        try:
            journal.record_code_note(
                source=SOURCE_GATE,
                reason_code="nl_unknown",
                kind=KIND_FACT,
                tags=["nl", "gate"],
                body="NL unknown; snap.net_liquidation is null",
                evidence="snap.net_liquidation=None",
                invalidate="NL>0 and book_unreliable=false",
            )
        except Exception:
            logger.debug("nl_unknown note failed", exc_info=True)
