"""Proposals, gate decisions, dispatches, halts, and daily counts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from abcxauto.memory.journal.util import _json_dumps, _row_ts

logger = logging.getLogger(__name__)


class TicketsMixin:
    """Proposals, gate decisions, dispatches, halts, and daily counts."""

    def record_proposal(
        self,
        *,
        source: str = "",
        strategy: str = "",
        symbol: str = "",
        direction: str = "",
        quantity: Optional[float] = None,
        params: Any = None,
        validation_ok: Optional[bool] = None,
        validation_reason: str = "",
        ts: Optional[str] = None,
    ) -> Optional[int]:
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            params_json = _json_dumps(params) if params is not None else None
            ok_int = None if validation_ok is None else (1 if validation_ok else 0)
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO proposals (
                        ts, source, strategy, symbol, direction, quantity,
                        params_json, validation_ok, validation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        source,
                        strategy,
                        symbol,
                        direction,
                        quantity,
                        params_json,
                        ok_int,
                        validation_reason or None,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_proposal failed table=proposals")
            return None

    def record_gate_decision(
        self,
        proposal_id: Optional[int],
        allowed: bool,
        reason: str = "",
        *,
        ts: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO gate_decisions (ts, proposal_id, allowed, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        proposal_id,
                        1 if allowed else 0,
                        reason or None,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception(
                "journal.record_gate_decision failed table=gate_decisions "
                "proposal_id=%s",
                proposal_id,
            )
    def record_dispatch(
        self,
        proposal_id: Optional[int],
        ok: bool,
        result: Any = None,
        *,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            result_json = _json_dumps(result) if result is not None else None
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO dispatches (ts, proposal_id, ok, result_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        proposal_id,
                        1 if ok else 0,
                        result_json,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception(
                "journal.record_dispatch failed table=dispatches proposal_id=%s",
                proposal_id,
            )
            return None
    def record_halt(
        self,
        reason: str,
        kind: str = "halt",
        *,
        ts: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO halts (ts, reason, kind)
                    VALUES (?, ?, ?)
                    """,
                    (_row_ts(ts), reason or None, kind or "halt"),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.record_halt failed table=halts")
    def recent_dispatches(self, limit: int = 50) -> List[dict]:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT d.id, d.ts, d.proposal_id, d.ok, d.result_json,
                           sm.ibkr_last, sm.bid, sm.ask, sm.sent_price,
                           sm.fill_price, sm.signed_slippage, sm.spread_paid,
                           sm.fill_label, sm.status AS fill_status
                    FROM dispatches d
                    LEFT JOIN send_marks sm ON sm.dispatch_id = d.id
                    ORDER BY d.id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                item = dict(row)
                raw = item.get("result_json")
                if raw:
                    try:
                        item["result"] = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["result"] = raw
                else:
                    item["result"] = None
                out.append(item)
            return out
        except Exception:
            logger.exception("journal.recent_dispatches failed")
            return []

    def recent_proposals(self, limit: int = 20) -> List[dict]:
        """Recent proposals including validation failures (for agent learning)."""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts, source, strategy, symbol, direction, quantity,
                           params_json, validation_ok, validation_reason
                    FROM proposals
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                item = dict(row)
                raw = item.get("params_json")
                if raw:
                    try:
                        item["params"] = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["params"] = raw
                else:
                    item["params"] = None
                ok = item.get("validation_ok")
                item["validation_ok"] = None if ok is None else bool(ok)
                out.append(item)
            return out
        except Exception:
            logger.exception("journal.recent_proposals failed")
            return []

    def daily_summary(self, day: Optional[str] = None) -> dict:
        if day is None:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        empty = {
            "day": day,
            "proposals": 0,
            "allowed": 0,
            "rejected": 0,
            "validation_failed": 0,
            "dispatch_ok": 0,
            "dispatch_failed": 0,
            "halts": 0,
        }
        try:
            self._ensure_schema()
            # Match ISO date prefix (handles ...Z and +00:00 forms).
            like = f"{day}%"
            with self._connect() as conn:
                proposals = conn.execute(
                    "SELECT COUNT(*) FROM proposals WHERE ts LIKE ?", (like,)
                ).fetchone()[0]
                allowed = conn.execute(
                    "SELECT COUNT(*) FROM gate_decisions WHERE ts LIKE ? AND allowed = 1",
                    (like,),
                ).fetchone()[0]
                rejected = conn.execute(
                    "SELECT COUNT(*) FROM gate_decisions WHERE ts LIKE ? AND allowed = 0",
                    (like,),
                ).fetchone()[0]
                validation_failed = conn.execute(
                    "SELECT COUNT(*) FROM proposals WHERE ts LIKE ? AND validation_ok = 0",
                    (like,),
                ).fetchone()[0]
                dispatch_ok = conn.execute(
                    "SELECT COUNT(*) FROM dispatches WHERE ts LIKE ? AND ok = 1",
                    (like,),
                ).fetchone()[0]
                dispatch_failed = conn.execute(
                    "SELECT COUNT(*) FROM dispatches WHERE ts LIKE ? AND ok = 0",
                    (like,),
                ).fetchone()[0]
                halts = conn.execute(
                    "SELECT COUNT(*) FROM halts WHERE ts LIKE ?", (like,)
                ).fetchone()[0]
            return {
                "day": day,
                "proposals": int(proposals),
                "allowed": int(allowed),
                "rejected": int(rejected),
                "validation_failed": int(validation_failed),
                "dispatch_ok": int(dispatch_ok),
                "dispatch_failed": int(dispatch_failed),
                "halts": int(halts),
            }
        except Exception:
            logger.exception("journal.daily_summary failed")
            return empty
