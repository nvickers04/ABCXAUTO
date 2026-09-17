"""Named play cards — the thesis record tickets bind to.

Sibling of notes, not an extension: notes are 160-char observations
(fact|event|invalidate). A card is a labeled play with screen, structured
evidence, direction, and an invalidate condition. Tickets name it via
``card=``; expired or invalidated rows are inert.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from abcxauto.memory.journal_support import (
    _json_dumps,
    _parse_ts,
    _row_ts,
)

logger = logging.getLogger("abcxauto.memory.journal")

MAX_LIVE = 8
MAX_LABEL = 32
MAX_SCREEN = 40
MAX_SCAN_CODE = 40
MAX_DIRECTION = 16
MAX_EXPECTATION = 80
MAX_INVALIDATE = 80
MAX_EVIDENCE_ITEMS = 6
MAX_FACT_KEYS = 6
MAX_FACT_VALUE = 48
MAX_TOOL_NAME = 24
RETENTION_DAYS = 14
MAX_POINTER_LABELS = 3
MAX_POINTER_LABEL_LEN = 16
MAX_POINTER_CHARS = 80
MAX_POINTER_TOKENS = 25

SOURCE_GROK = "grok"
SOURCE_IMPORT = "import"
SOURCES = frozenset({SOURCE_GROK, SOURCE_IMPORT})

_SLUG_RE = re.compile(r"[^a-z0-9_]+")

CARDS_DDL = """
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    ts TEXT NOT NULL,
    screen TEXT,
    scan_code TEXT,
    evidence_json TEXT,
    direction TEXT,
    expectation TEXT,
    invalidate TEXT,
    expires_at TEXT NOT NULL,
    invalidated_at TEXT,
    source TEXT NOT NULL,
    rev INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_cards_expires_at ON cards(expires_at);
CREATE INDEX IF NOT EXISTS idx_cards_label ON cards(label);
CREATE TABLE IF NOT EXISTS card_links (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    card_id TEXT,
    card_label TEXT NOT NULL,
    proposal_id INTEGER,
    dispatch_id INTEGER,
    exec_id TEXT,
    fill_id INTEGER,
    order_id INTEGER,
    symbol TEXT,
    strategy TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_links_label ON card_links(card_label);
CREATE INDEX IF NOT EXISTS idx_card_links_exec ON card_links(exec_id);
CREATE INDEX IF NOT EXISTS idx_card_links_proposal ON card_links(proposal_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_card_links_exec_unique
    ON card_links(exec_id) WHERE exec_id IS NOT NULL;
"""


def _now(now: datetime | None = None) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


def normalize_card_label(raw: Any) -> str:
    text = _SLUG_RE.sub("-", str(raw or "").strip().lower()).strip("-")
    return text[:MAX_LABEL]


def card_label_of(params: Any = None, extra: Any = None) -> str:
    raw = None
    if isinstance(params, dict):
        raw = params.get("card")
    elif params is not None:
        raw = getattr(params, "card", None)
    if raw in (None, "") and extra is not None:
        raw = extra
    return normalize_card_label(raw)


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
        return "inert"
    if _expired(row, now):
        return "inert"
    return "live"


def _parse_evidence(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data[:MAX_EVIDENCE_ITEMS]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")[:MAX_TOOL_NAME]
        facts_in = item.get("facts")
        facts: dict[str, Any] = {}
        if isinstance(facts_in, dict):
            for i, (k, v) in enumerate(facts_in.items()):
                if i >= MAX_FACT_KEYS:
                    break
                key = str(k)[:MAX_TOOL_NAME]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    facts[key] = v
                else:
                    facts[key] = str(v)[:MAX_FACT_VALUE]
        if tool or facts:
            out.append({"tool": tool, "facts": facts})
    return out


def _evidence_error(raw: Any) -> str:
    if raw in (None, "", [], {}):
        return ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return "bad_evidence"
    if not isinstance(raw, list):
        return "bad_evidence"
    if len(raw) > MAX_EVIDENCE_ITEMS:
        return "evidence_too_long"
    for item in raw:
        if not isinstance(item, dict):
            return "bad_evidence"
        facts = item.get("facts")
        if facts in (None, ""):
            continue
        if not isinstance(facts, dict):
            return "bad_evidence"
        if len(facts) > MAX_FACT_KEYS:
            return "evidence_too_long"
        for v in facts.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                continue
            if len(str(v)) > MAX_FACT_VALUE:
                return "evidence_too_long"
    return ""


def _public(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    out = {
        "id": row.get("id"),
        "label": row.get("label") or row.get("id"),
        "ts": row.get("ts"),
        "screen": row.get("screen") or "",
        "scan_code": row.get("scan_code") or "",
        "evidence": _parse_evidence(row.get("evidence_json")),
        "direction": row.get("direction") or "",
        "expectation": row.get("expectation") or "",
        "invalidate": row.get("invalidate") or "",
        "expires_at": row.get("expires_at"),
        "source": row.get("source"),
        "rev": int(row.get("rev") or 1),
        "status": _status(row, now),
    }
    if row.get("invalidated_at"):
        out["invalidated_at"] = row.get("invalidated_at")
    return out


def _pointer_of(rows: list[dict[str, Any]], now: datetime) -> str:
    if not rows:
        return ""
    labels: list[str] = []
    ages: list[int] = []
    for row in rows:
        lab = str(row.get("label") or row.get("id") or "")[:MAX_POINTER_LABEL_LEN]
        if lab and lab not in labels and len(labels) < MAX_POINTER_LABELS:
            labels.append(lab)
        age = _age_days(row.get("ts"), now)
        if age is not None:
            ages.append(age)
    if ages:
        lo, hi = min(ages), max(ages)
        age_s = f"{lo}d..{hi}d" if lo != hi else f"{lo}d"
    else:
        age_s = "0d"
    bit = f"cards={len(rows)}"
    if labels:
        bit += " " + ",".join(labels)
    bit += f" age={age_s}"
    if len(bit) > MAX_POINTER_CHARS:
        bit = f"cards={len(rows)} age={age_s}"
    return bit[:MAX_POINTER_CHARS]


def _cap_error(name: str, value: str, cap: int) -> str:
    if len(value) > cap:
        return f"{name}_too_long"
    return ""


class JournalCards:
    """Named play cards and ticket/fill linkage. Never a send refuse."""

    def cards_pointer(self, *, now: datetime | None = None) -> str:
        if not getattr(self, "enabled", False):
            return ""
        clock = _now(now)
        try:
            self._ensure_schema()
            rows = self._cards_live_rows(clock)
        except Exception:
            logger.debug("cards pointer read failed", exc_info=True)
            return ""
        return _pointer_of(rows, clock)

    def list_cards(self, *, now: datetime | None = None) -> dict[str, Any]:
        clock = _now(now)
        if not getattr(self, "enabled", False):
            return {"pointer": "", "ids": [], "labels": [], "n": 0}
        try:
            self._ensure_schema()
            rows = self._cards_live_rows(clock)
        except Exception:
            logger.exception("journal.list_cards failed")
            return {"pointer": "", "ids": [], "labels": [], "n": 0, "error": "read_failed"}
        ids = [str(r.get("id") or "") for r in rows if r.get("id")]
        labels = [str(r.get("label") or r.get("id") or "") for r in rows if r.get("id")]
        return {
            "pointer": _pointer_of(rows, clock),
            "ids": ids,
            "labels": labels,
            "n": len(ids),
        }

    def get_cards(
        self,
        *,
        ids: list[str] | None = None,
        labels: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clock = _now(now)
        if not getattr(self, "enabled", False):
            return {"cards": []}
        want_ids = {normalize_card_label(x) for x in (ids or []) if str(x or "").strip()}
        want_labels = {normalize_card_label(x) for x in (labels or []) if str(x or "").strip()}
        if not want_ids and not want_labels:
            listed = self.list_cards(now=clock)
            return {"cards": [], **listed}
        try:
            self._ensure_schema()
            rows = self._cards_all_rows()
        except Exception:
            logger.exception("journal.get_cards failed")
            return {"cards": [], "error": "read_failed"}
        out: list[dict[str, Any]] = []
        for row in rows:
            cid = normalize_card_label(row.get("id"))
            lab = normalize_card_label(row.get("label") or row.get("id"))
            if want_ids and cid in want_ids:
                out.append(_public(row, clock))
                continue
            if want_ids:
                continue
            if want_labels and lab in want_labels:
                out.append(_public(row, clock))
        return {"cards": out, "n": len(out)}

    def get_card(self, id: str = "", *, now: datetime | None = None) -> dict[str, Any] | None:
        nid = normalize_card_label(id)
        if not nid:
            return None
        got = self.get_cards(ids=[nid], now=now)
        rows = got.get("cards") or []
        return rows[0] if rows else None

    def resolve_card(self, label: str = "", *, now: datetime | None = None) -> dict[str, Any]:
        """Look up a named play. Never invents. Inert/missing are not live."""
        clock = _now(now)
        lab = normalize_card_label(label)
        if not lab:
            return {"ok": False, "status": "missing", "invented": False}
        if not getattr(self, "enabled", False):
            return {"ok": False, "status": "missing", "invented": False, "label": lab}
        try:
            self._ensure_schema()
            row = self._cards_one(lab)
        except Exception:
            logger.debug("resolve_card failed", exc_info=True)
            return {"ok": False, "status": "missing", "invented": False, "label": lab}
        if row is None:
            return {"ok": False, "status": "missing", "invented": False, "label": lab}
        pub = _public(row, clock)
        live = pub["status"] == "live"
        return {
            "ok": live,
            "status": pub["status"] if live else "inert",
            "invented": False,
            "card": pub,
            "id": pub["id"],
            "label": pub["label"],
        }

    def write_card(
        self,
        *,
        label: str = "",
        id: str = "",
        screen: str = "",
        scan_code: str = "",
        evidence: Any = None,
        direction: str = "",
        expectation: str = "",
        invalidate: str = "",
        source: str = SOURCE_GROK,
        expires_at: str = "",
        ts: Optional[str] = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clock = _now(now)
        lab = normalize_card_label(label or id)
        if not lab:
            return {"ok": False, "error": "label_required"}
        src = str(source or SOURCE_GROK).strip().lower()
        if src not in SOURCES:
            return {"ok": False, "error": "bad_source", "source": src}
        if not getattr(self, "enabled", False):
            return {"ok": False, "error": "journal_disabled"}
        ev_err = _evidence_error(evidence)
        if ev_err:
            return {"ok": False, "error": ev_err}
        screen_s = str(screen or "").strip()
        scan_s = str(scan_code or "").strip()
        dir_s = str(direction or "").strip().lower()
        exp_s = str(expectation or "").strip()
        inv_s = str(invalidate or "").strip()
        for name, value, cap in (
            ("label", lab, MAX_LABEL),
            ("screen", screen_s, MAX_SCREEN),
            ("scan_code", scan_s, MAX_SCAN_CODE),
            ("direction", dir_s, MAX_DIRECTION),
            ("expectation", exp_s, MAX_EXPECTATION),
            ("invalidate", inv_s, MAX_INVALIDATE),
        ):
            err = _cap_error(name, value, cap)
            if err:
                return {"ok": False, "error": err, "max": cap}
        ev = _parse_evidence(evidence)
        nid = normalize_card_label(id) or lab
        stamp = _row_ts(ts if ts is not None else clock)
        exp_at = str(expires_at or "").strip() or _row_ts(
            clock + timedelta(days=RETENTION_DAYS)
        )
        try:
            self._ensure_schema()
            with self._connect() as conn:
                prev = conn.execute(
                    "SELECT rev FROM cards WHERE id = ?", (nid,)
                ).fetchone()
                if prev is not None:
                    rev = int(prev["rev"] or 1) + 1
                    conn.execute(
                        """
                        UPDATE cards SET ts=?, label=?, screen=?, scan_code=?,
                            evidence_json=?, direction=?, expectation=?,
                            invalidate=?, expires_at=?, source=?, rev=?,
                            invalidated_at=NULL
                        WHERE id=?
                        """,
                        (
                            stamp,
                            lab,
                            screen_s or None,
                            scan_s or None,
                            _json_dumps(ev) if ev else None,
                            dir_s or None,
                            exp_s or None,
                            inv_s or None,
                            exp_at,
                            src,
                            rev,
                            nid,
                        ),
                    )
                else:
                    rev = 1
                    conn.execute(
                        """
                        INSERT INTO cards (
                            id, label, ts, screen, scan_code, evidence_json,
                            direction, expectation, invalidate, expires_at,
                            source, rev, invalidated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            nid,
                            lab,
                            stamp,
                            screen_s or None,
                            scan_s or None,
                            _json_dumps(ev) if ev else None,
                            dir_s or None,
                            exp_s or None,
                            inv_s or None,
                            exp_at,
                            src,
                            rev,
                        ),
                    )
                self._evict_cards_over_cap_locked(conn, clock)
                conn.commit()
            row = self._cards_one(nid)
        except Exception:
            logger.exception("journal.write_card failed")
            return {"ok": False, "error": "write_failed"}
        if not row:
            return {"ok": False, "error": "write_failed"}
        return {"ok": True, "card": _public(row, clock)}

    def invalidate_card(
        self,
        id: str = "",
        *,
        evidence: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        clock = _now(now)
        nid = normalize_card_label(id)
        if not nid:
            return {"ok": False, "error": "id_required"}
        if not getattr(self, "enabled", False):
            return {"ok": False, "error": "journal_disabled"}
        try:
            self._ensure_schema()
            with self._connect() as conn:
                prev = conn.execute(
                    "SELECT * FROM cards WHERE id = ?", (nid,)
                ).fetchone()
                if prev is None:
                    return {"ok": False, "error": "not_found", "id": nid}
                row = dict(prev)
                extra = str(evidence or "").strip()
                inv = str(row.get("invalidate") or "")
                if extra:
                    inv = f"{inv} | {extra}".strip(" |") if inv else extra
                    inv = inv[:MAX_INVALIDATE]
                rev = int(row.get("rev") or 1) + 1
                conn.execute(
                    """
                    UPDATE cards SET invalidated_at=?, invalidate=?, rev=?
                    WHERE id=?
                    """,
                    (_row_ts(clock), inv or None, rev, nid),
                )
                conn.commit()
            row = self._cards_one(nid)
        except Exception:
            logger.exception("journal.invalidate_card failed")
            return {"ok": False, "error": "write_failed"}
        if not row:
            return {"ok": False, "error": "write_failed"}
        return {"ok": True, "card": _public(row, clock)}

    def link_card(
        self,
        *,
        card_label: str = "",
        proposal_id: Any = None,
        dispatch_id: Any = None,
        exec_id: str = "",
        fill_id: Any = None,
        order_id: Any = None,
        symbol: str = "",
        strategy: str = "",
        ts: Optional[str] = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist ticket/fill → label. Missing record is missing, not invented."""
        lab = normalize_card_label(card_label)
        if not lab:
            return {"ok": False, "error": "nameless", "invented": False}
        if not getattr(self, "enabled", False):
            return {"ok": False, "error": "journal_disabled", "invented": False}
        clock = _now(now)
        resolved = self.resolve_card(lab, now=clock)
        status = str(resolved.get("status") or "missing")
        card_id = resolved.get("id") if status in ("live", "inert") else None
        live = status == "live"
        try:
            pid = int(proposal_id) if proposal_id not in (None, "") else None
        except (TypeError, ValueError):
            pid = None
        try:
            did = int(dispatch_id) if dispatch_id not in (None, "") else None
        except (TypeError, ValueError):
            did = None
        try:
            fid = int(fill_id) if fill_id not in (None, "") else None
        except (TypeError, ValueError):
            fid = None
        try:
            oid = int(order_id) if order_id not in (None, "") else None
        except (TypeError, ValueError):
            oid = None
        eid = str(exec_id or "").strip() or None
        stamp = _row_ts(ts if ts is not None else clock)
        try:
            self._ensure_schema()
            with self._connect() as conn:
                if eid:
                    prev = conn.execute(
                        "SELECT id FROM card_links WHERE exec_id = ?", (eid,)
                    ).fetchone()
                    if prev is not None:
                        conn.execute(
                            """
                            UPDATE card_links SET ts=?, card_id=?, card_label=?,
                                proposal_id=COALESCE(?, proposal_id),
                                dispatch_id=COALESCE(?, dispatch_id),
                                fill_id=COALESCE(?, fill_id),
                                order_id=COALESCE(?, order_id),
                                symbol=COALESCE(?, symbol),
                                strategy=COALESCE(?, strategy),
                                resolved=?, status=?
                            WHERE exec_id=?
                            """,
                            (
                                stamp,
                                card_id,
                                lab,
                                pid,
                                did,
                                fid,
                                oid,
                                str(symbol or "").upper() or None,
                                str(strategy or "") or None,
                                1 if live else 0,
                                status,
                                eid,
                            ),
                        )
                        conn.commit()
                        return {
                            "ok": True,
                            "status": status,
                            "card_label": lab,
                            "card_id": card_id,
                            "resolved": live,
                            "invented": False,
                            "updated": True,
                        }
                conn.execute(
                    """
                    INSERT INTO card_links (
                        ts, card_id, card_label, proposal_id, dispatch_id,
                        exec_id, fill_id, order_id, symbol, strategy,
                        resolved, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stamp,
                        card_id,
                        lab,
                        pid,
                        did,
                        eid,
                        fid,
                        oid,
                        str(symbol or "").upper() or None,
                        str(strategy or "") or None,
                        1 if live else 0,
                        status,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.link_card failed")
            return {"ok": False, "error": "write_failed", "invented": False}
        return {
            "ok": True,
            "status": status,
            "card_label": lab,
            "card_id": card_id,
            "resolved": live,
            "invented": False,
        }

    def pnl_by_card(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Realized P&L grouped by ticket card label. Scorecard reads this."""
        if not getattr(self, "enabled", False):
            return []
        clock = _now(now)
        try:
            self._ensure_schema()
            with self._connect() as conn:
                fills = [
                    dict(r)
                    for r in conn.execute(
                        """
                        SELECT id, ts, exec_id, order_id, symbol, realized_pnl
                        FROM fills
                        """
                    ).fetchall()
                ]
                links = [
                    dict(r)
                    for r in conn.execute("SELECT * FROM card_links").fetchall()
                ]
                marks: list[dict[str, Any]] = []
                try:
                    marks = [
                        dict(r)
                        for r in conn.execute(
                            """
                            SELECT sm.card, smo.order_id
                            FROM send_marks sm
                            JOIN send_mark_orders smo ON smo.send_mark_id = sm.id
                            """
                        ).fetchall()
                    ]
                except Exception:
                    marks = []
        except Exception:
            logger.exception("journal.pnl_by_card failed")
            return []
        lo = str(since or "").strip()
        hi = str(until or "").strip()

        def _in_window(ts: Any) -> bool:
            text = str(ts or "")
            if lo and text < lo:
                return False
            if hi and text >= hi:
                return False
            return True

        by_exec: dict[str, str] = {}
        link_status: dict[str, str] = {}
        link_id: dict[str, str] = {}
        n_links: dict[str, int] = {}
        for link in links:
            lab = normalize_card_label(link.get("card_label"))
            if not lab:
                continue
            n_links[lab] = n_links.get(lab, 0) + 1
            if link.get("status"):
                link_status.setdefault(lab, str(link.get("status")))
            if link.get("card_id"):
                link_id.setdefault(lab, str(link.get("card_id")))
            eid = str(link.get("exec_id") or "").strip()
            if eid:
                by_exec[eid] = lab
        mark_by_oid: dict[int, str] = {}
        for mark in marks:
            lab = normalize_card_label(mark.get("card"))
            if not lab:
                continue
            try:
                oid = int(mark.get("order_id"))
            except (TypeError, ValueError):
                continue
            mark_by_oid[oid] = lab
        grouped: dict[str, dict[str, Any]] = {}
        for fill in fills:
            if not _in_window(fill.get("ts")):
                continue
            eid = str(fill.get("exec_id") or "").strip()
            lab = by_exec.get(eid, "")
            if not lab:
                try:
                    oid = int(fill.get("order_id"))
                except (TypeError, ValueError):
                    oid = None
                if oid is not None:
                    lab = mark_by_oid.get(oid, "")
            if not lab:
                continue
            row = grouped.setdefault(
                lab,
                {
                    "card_label": lab,
                    "card_id": link_id.get(lab),
                    "status": "missing",
                    "n_fills": 0,
                    "realized_pnl": 0.0,
                    "n_links": n_links.get(lab, 0),
                },
            )
            row["n_fills"] += 1
            try:
                pnl = float(fill.get("realized_pnl") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            row["realized_pnl"] = float(row["realized_pnl"]) + pnl
        for lab, row in grouped.items():
            resolved = self.resolve_card(lab, now=clock)
            row["status"] = str(resolved.get("status") or link_status.get(lab) or "missing")
            if resolved.get("id"):
                row["card_id"] = resolved.get("id")
            row["realized_pnl"] = round(float(row["realized_pnl"]), 4)
        return sorted(grouped.values(), key=lambda r: r["card_label"])

    def _cards_all_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cards ORDER BY ts ASC").fetchall()
        return [dict(r) for r in rows]

    def _cards_live_rows(self, now: datetime) -> list[dict[str, Any]]:
        return [r for r in self._cards_all_rows() if _live(r, now)]

    def _cards_one(self, nid: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cards WHERE id = ?", (nid,)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM cards WHERE label = ?", (nid,)
                ).fetchone()
        return dict(row) if row is not None else None

    def _evict_cards_over_cap_locked(self, conn: Any, now: datetime) -> None:
        rows = [dict(r) for r in conn.execute("SELECT * FROM cards").fetchall()]
        live = [r for r in rows if _live(r, now)]
        live.sort(key=lambda r: str(r.get("ts") or ""))
        extra = len(live) - MAX_LIVE
        if extra <= 0:
            return
        stamp = _row_ts(now)
        for row in live[:extra]:
            conn.execute(
                "UPDATE cards SET expires_at=? WHERE id=?",
                (stamp, row.get("id")),
            )


def cards_wake_bit(*, now: datetime | None = None) -> str:
    """Counts/labels/ages only. Empty when nothing live. Never evidence."""
    try:
        from abcxauto.memory import get_journal

        return get_journal().cards_pointer(now=now)
    except Exception:
        logger.debug("cards wake pointer failed", exc_info=True)
        return ""


def memory_wake_bit(*, now: datetime | None = None) -> str:
    """Notes pointer plus cards pointer. world_state already calls notes_wake_bit."""
    try:
        from abcxauto.memory.notes import notes_wake_bit

        return notes_wake_bit(now=now)
    except Exception:
        return cards_wake_bit(now=now)


def pnl_by_card(
    *,
    since: str | None = None,
    until: str | None = None,
    journal: Any = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Scorecard entry point: realized P&L grouped by card label."""
    j = journal
    if j is None:
        try:
            from abcxauto.memory import get_journal

            j = get_journal()
        except Exception:
            return []
    fn = getattr(j, "pnl_by_card", None)
    if not callable(fn):
        return []
    return list(fn(since=since, until=until, now=now) or [])
