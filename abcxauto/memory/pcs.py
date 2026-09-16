"""Journal PCS — kill sessions and fill-lambda events.

Moved from journal.py. Method bodies are unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from abcxauto.memory.journal_support import (
    _coerce_order_id,
    _json_dumps,
    _row_ts,
)

logger = logging.getLogger("abcxauto.memory.journal")


class JournalPcs:
    """PCS kill-scorecard rows and fill-lambda event log."""


    def record_pcs_kill_session(
        self,
        payload: Any = None,
        *,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        """Persist one PCS Arm v0 kill scorecard session row (QA logging only)."""
        if not self.enabled:
            return None
        try:
            from abcxauto.pcs_kill_scorecard import normalize_session_row

            row = normalize_session_row(payload or {})
            self._ensure_schema()
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO pcs_kill_sessions (
                        ts, session_id, session_date, valid, send, send_credited, row_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        ts=excluded.ts,
                        session_date=excluded.session_date,
                        valid=excluded.valid,
                        send=excluded.send,
                        send_credited=excluded.send_credited,
                        row_json=excluded.row_json
                    """,
                    (
                        _row_ts(ts),
                        str(row.get("session_id") or ""),
                        str(row.get("session_date") or ""),
                        1 if row.get("valid") else 0,
                        row.get("send"),
                        1 if row.get("send_credited") else 0,
                        _json_dumps(row),
                    ),
                )
                conn.commit()
                # lastrowid is 0 on conflict-update; re-read id
                rid = int(cur.lastrowid or 0)
                if rid <= 0:
                    got = conn.execute(
                        "SELECT id FROM pcs_kill_sessions WHERE session_id = ?",
                        (str(row.get("session_id") or ""),),
                    ).fetchone()
                    rid = int(got[0]) if got else 0
                return rid or None
        except Exception:
            logger.exception("journal.record_pcs_kill_session failed")
            return None

    def list_pcs_kill_sessions(
        self,
        *,
        limit: int = 20,
        session_date: Optional[str] = None,
        ascending: bool = True,
    ) -> List[dict]:
        """Return normalized PCS kill session rows (oldest-first by default)."""
        try:
            self._ensure_schema()
            lim = max(1, int(limit))
            with self._connect() as conn:
                if session_date:
                    sql = """
                        SELECT id, ts, session_id, session_date, valid, send,
                               send_credited, row_json
                        FROM pcs_kill_sessions
                        WHERE session_date = ?
                        ORDER BY id ASC
                        LIMIT ?
                    """
                    rows = conn.execute(sql, (str(session_date), lim)).fetchall()
                else:
                    sql = """
                        SELECT id, ts, session_id, session_date, valid, send,
                               send_credited, row_json
                        FROM pcs_kill_sessions
                        ORDER BY id ASC
                        LIMIT ?
                    """
                    rows = conn.execute(sql, (lim,)).fetchall()
            out: List[dict] = []
            for raw in rows:
                item = dict(raw)
                blob = item.pop("row_json", None)
                try:
                    parsed = json.loads(blob) if blob else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                if not isinstance(parsed, dict):
                    parsed = {}
                parsed["journal_id"] = item.get("id")
                parsed["ts"] = item.get("ts")
                out.append(parsed)
            if not ascending:
                out.reverse()
            return out
        except Exception:
            logger.exception("journal.list_pcs_kill_sessions failed")
            return []

    def pcs_kill_window(
        self,
        *,
        limit: int = 20,
        session_date: Optional[str] = None,
        qty0_streak_limit: int = 5,
    ) -> dict:
        """Window aggregates + PASS/FAIL/ABORT over recorded PCS kill rows."""
        try:
            from abcxauto.pcs_kill_scorecard import window_aggregates

            rows = self.list_pcs_kill_sessions(
                limit=limit, session_date=session_date, ascending=True
            )
            return window_aggregates(
                rows, qty0_streak_limit=qty0_streak_limit, normalize=False
            )
        except Exception:
            logger.exception("journal.pcs_kill_window failed")
            return {
                "n": 0,
                "n_valid": 0,
                "sum_NL": 0.0,
                "sum_conservative_pnl": 0.0,
                "sum_model_cost": 0.0,
                "net_conservative": 0.0,
                "send_rate": 0.0,
                "send_credited": 0,
                "mean_lambda": None,
                "mean_λ": None,
                "max_dd_pct": None,
                "qty0_streak_max": 0,
                "qty0_streak_limit": int(qty0_streak_limit),
                "f10_breach_count": 0,
                "invalid_rows": 0,
                "mean_session_score": None,
                "verdict": "FAIL",
                "abort_fuse": "none",
                "cancel_all_invoked": False,
                "working_orders_after_abort": None,
                "rows": [],
            }

    def record_pcs_event(self, payload: Any = None, *, ts: Optional[str] = None) -> Optional[int]:
        """Append one PCS fill-λ event. Never raises. Dedupe via dedupe_key."""
        if not self.enabled or not isinstance(payload, dict):
            return None
        event = str(payload.get("event") or "").strip()
        if not event:
            return None
        try:
            self._ensure_schema()
            ev = 1 if payload.get("evidence_valid") else (
                0 if "evidence_valid" in payload else None
            )
            inc = 1 if payload.get("include_in_pnl_mean") else (
                0 if "include_in_pnl_mean" in payload else None
            )
            oid = _coerce_order_id(payload.get("order_id"))
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO pcs_fill_events (
                        ts, event, lifecycle_id, dedupe_key, order_id, exec_id,
                        symbol, strategy, card, side, quote_source,
                        bid, ask, mid, half_spread, last, fill_price,
                        credit, debit, lambda_declared, lambda_implied,
                        lambda_stress, c_score, d_score, c_score_stress,
                        d_score_stress, manage_rule, evidence_valid,
                        include_in_pnl_mean, payload_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        _row_ts(ts or payload.get("ts")),
                        event,
                        str(payload.get("lifecycle_id") or "") or None,
                        str(payload.get("dedupe_key") or "") or None,
                        oid,
                        None if payload.get("exec_id") in (None, "") else str(payload.get("exec_id")),
                        str(payload.get("symbol") or "").upper()[:12] or None,
                        str(payload.get("strategy") or "")[:60] or None,
                        str(payload.get("card") or "")[:120] or None,
                        payload.get("side"),
                        payload.get("quote_source"),
                        payload.get("bid"),
                        payload.get("ask"),
                        payload.get("mid"),
                        payload.get("half_spread"),
                        payload.get("last"),
                        payload.get("fill_price"),
                        payload.get("credit"),
                        payload.get("debit"),
                        payload.get("lambda_declared"),
                        payload.get("lambda_implied"),
                        payload.get("lambda_stress"),
                        payload.get("c_score"),
                        payload.get("d_score"),
                        payload.get("c_score_stress"),
                        payload.get("d_score_stress"),
                        payload.get("manage_rule"),
                        ev,
                        inc,
                        _json_dumps(payload),
                    ),
                )
                conn.commit()
                rid = int(cur.lastrowid or 0)
                return rid or None
        except Exception:
            logger.exception("journal.record_pcs_event failed")
            return None

    def _pcs_row(self, raw: Any) -> dict:
        item = dict(raw)
        blob = item.pop("payload_json", None)
        parsed: dict = {}
        if blob:
            try:
                loaded = json.loads(blob) if isinstance(blob, str) else blob
            except (TypeError, ValueError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                parsed = loaded
        parsed.update({k: v for k, v in item.items() if v is not None})
        if item.get("evidence_valid") is not None:
            parsed["evidence_valid"] = bool(item.get("evidence_valid"))
        if item.get("include_in_pnl_mean") is not None:
            parsed["include_in_pnl_mean"] = bool(item.get("include_in_pnl_mean"))
        return parsed

    def pcs_events(
        self,
        *,
        lifecycle_id: Optional[str] = None,
        event: Optional[str] = None,
        limit: int = 200,
    ) -> List[dict]:
        """Newest-first PCS fill-λ events."""
        try:
            self._ensure_schema()
            lim = max(1, int(limit))
            clauses = []
            args: list = []
            if lifecycle_id:
                clauses.append("lifecycle_id = ?")
                args.append(str(lifecycle_id))
            if event:
                clauses.append("event = ?")
                args.append(str(event))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM pcs_fill_events
                    {where}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (*args, lim),
                ).fetchall()
            return [self._pcs_row(r) for r in rows]
        except Exception:
            logger.exception("journal.pcs_events failed")
            return []

    def pcs_open_lifecycle_id(self, geometry_key: str) -> Optional[str]:
        """Open (no lifecycle_end) lifecycle for this PCS geometry."""
        want = str(geometry_key or "").strip()
        if not want:
            return None
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT lifecycle_id, event, payload_json
                    FROM pcs_fill_events
                    WHERE lifecycle_id IS NOT NULL
                    ORDER BY id ASC
                    """
                ).fetchall()
            ended: set = set()
            found: Optional[str] = None
            for raw in rows:
                lid = str(raw["lifecycle_id"] or "")
                if not lid:
                    continue
                if str(raw["event"] or "") == "pcs_lifecycle_end":
                    ended.add(lid)
                    if found == lid:
                        found = None
                    continue
                geo = ""
                blob = raw["payload_json"]
                try:
                    parsed = json.loads(blob) if blob else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                if isinstance(parsed, dict):
                    geo = str(parsed.get("geometry_key") or "")
                if geo == want and lid not in ended:
                    found = lid
            return found
        except Exception:
            logger.exception("journal.pcs_open_lifecycle_id failed")
            return None

    def pcs_open_lifecycles(self) -> List[dict]:
        """Latest row per lifecycle that has not recorded pcs_lifecycle_end."""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM pcs_fill_events
                    WHERE lifecycle_id IS NOT NULL
                    ORDER BY id ASC
                    """
                ).fetchall()
            ended: set = set()
            latest: dict = {}
            for raw in rows:
                row = self._pcs_row(raw)
                lid = str(row.get("lifecycle_id") or "")
                if not lid:
                    continue
                if str(row.get("event") or "") == "pcs_lifecycle_end":
                    ended.add(lid)
                    latest.pop(lid, None)
                    continue
                if lid in ended:
                    continue
                latest[lid] = row
            return list(latest.values())
        except Exception:
            logger.exception("journal.pcs_open_lifecycles failed")
            return []
