"""Journal trades — proposals, gates, dispatches, marks, notebook rows.

Moved from journal.py. Method bodies are unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from abcxauto.memory.journal_support import (
    _coerce_order_id,
    _json_dumps,
    _order_ids_from_result_json,
    _patch_dispatch_send_marks,
    _row_ts,
    _split_gate_stage,
)

logger = logging.getLogger("abcxauto.memory.journal")


class JournalTrades:
    """Proposal, gate, dispatch, send-mark, and notebook writers/readers."""

    # ------------------------------------------------------------------
    # Writers (never raise into the caller)
    # ------------------------------------------------------------------

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
            logger.exception("journal.record_proposal failed")
            return None

    def record_gate_decision(
        self,
        proposal_id: Optional[int],
        allowed: bool,
        reason: str = "",
        *,
        ts: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            stage_val = str(stage or "").strip()
            if not stage_val:
                stage_val, _note = _split_gate_stage(reason)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO gate_decisions (ts, proposal_id, allowed, reason, stage)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        proposal_id,
                        1 if allowed else 0,
                        reason or None,
                        stage_val or None,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.record_gate_decision failed")

    def record_send_preview(
        self,
        *,
        preview_id: str = "",
        preview_hash: str = "",
        strategy: str = "",
        symbol: str = "",
        max_loss: Any = None,
        would_refuse: Any = None,
        verdict: str = "",
        token_used: bool = False,
        source: str = "",
        portfolio_max_loss_usd: Any = None,
        portfolio_cap_usd: Any = None,
        portfolio_usd_refused: Any = None,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        """KEEP-3 preview row. Never raises."""
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            pid = str(preview_id or "").strip()
            if not pid:
                return None
            refuse_json = _json_dumps(would_refuse) if would_refuse is not None else None
            usd_refused = None
            if portfolio_usd_refused is not None:
                usd_refused = 1 if portfolio_usd_refused else 0
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO send_previews (
                        ts, preview_id, preview_hash, strategy, symbol,
                        max_loss, would_refuse_json, verdict, token_used,
                        source, portfolio_max_loss_usd, portfolio_cap_usd,
                        portfolio_usd_refused
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        pid,
                        str(preview_hash or ""),
                        strategy,
                        symbol,
                        max_loss,
                        refuse_json,
                        verdict or None,
                        1 if token_used else 0,
                        source or None,
                        portfolio_max_loss_usd,
                        portfolio_cap_usd,
                        usd_refused,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_send_preview failed")
            return None

    def mark_preview_token_used(
        self,
        preview_id: str,
        *,
        ts: Optional[str] = None,
    ) -> None:
        """Stamp token_used on a preview row. Never raises."""
        if not self.enabled:
            return
        pid = str(preview_id or "").strip()
        if not pid:
            return
        try:
            self._ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE send_previews
                    SET token_used = 1, used_ts = ?
                    WHERE preview_id = ?
                    """,
                    (_row_ts(ts), pid),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.mark_preview_token_used failed")

    def get_send_preview(self, preview_id: str) -> Optional[dict]:
        """Latest send_previews row for ``preview_id``, or None."""
        pid = str(preview_id or "").strip()
        if not pid or not self.enabled:
            return None
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT preview_id, preview_hash, strategy, symbol,
                           max_loss, would_refuse_json, verdict,
                           token_used, used_ts, source, ts,
                           portfolio_max_loss_usd, portfolio_cap_usd,
                           portfolio_usd_refused
                    FROM send_previews
                    WHERE preview_id = ?
                    """,
                    (pid,),
                ).fetchone()
            if row is None:
                return None
            item = dict(row)
            raw = item.get("would_refuse_json")
            if raw:
                try:
                    item["would_refuse"] = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["would_refuse"] = raw
            else:
                item["would_refuse"] = []
            item["token_used"] = bool(item.get("token_used"))
            if item.get("portfolio_usd_refused") is not None:
                item["portfolio_usd_refused"] = bool(item.get("portfolio_usd_refused"))
            return item
        except Exception:
            logger.exception("journal.get_send_preview failed")
            return None

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
            logger.exception("journal.record_dispatch failed")
            return None

    def record_send_marks(
        self,
        *,
        proposal_id: Optional[int] = None,
        dispatch_id: Optional[int] = None,
        marks: Any = None,
        result: Any = None,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        """Persist dispatch-time NBBO vs later fill. Never raises."""
        if not self.enabled or not isinstance(marks, dict):
            return None
        try:
            from abcxauto.send_marks import public_marks

            self._ensure_schema()
            oids = sorted(
                _order_ids_from_result_json(
                    _json_dumps(result) if result is not None else None
                )
            )
            primary = marks.get("order_id")
            if primary is not None:
                try:
                    primary = int(primary)
                except (TypeError, ValueError):
                    primary = None
            if primary is None and oids:
                primary = oids[0]
            stamp = _row_ts(ts)
            pub = public_marks(marks)
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO send_marks (
                        ts, proposal_id, dispatch_id, order_id, order_ids_json,
                        symbol, strategy, card, side,
                        ibkr_last, bid, ask, mid, sent_price, fill_price,
                        signed_slippage, spread_paid, fill_label, status,
                        seen_working, marks_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stamp,
                        proposal_id,
                        dispatch_id,
                        primary,
                        _json_dumps(oids),
                        str(marks.get("symbol") or "")[:12] or None,
                        str(marks.get("strategy") or "")[:60] or None,
                        str(marks.get("card") or "")[:120] or None,
                        marks.get("side"),
                        pub.get("ibkr_last"),
                        pub.get("bid"),
                        pub.get("ask"),
                        marks.get("mid"),
                        pub.get("sent_price"),
                        pub.get("fill_price"),
                        pub.get("signed_slippage"),
                        pub.get("spread_paid"),
                        pub.get("fill_label"),
                        marks.get("status"),
                        1 if marks.get("seen_working") else 0,
                        _json_dumps(marks),
                    ),
                )
                mark_id = int(cur.lastrowid)
                for oid in oids:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO send_mark_orders (order_id, send_mark_id)
                        VALUES (?, ?)
                        """,
                        (int(oid), mark_id),
                    )
                if primary is not None and primary not in oids:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO send_mark_orders (order_id, send_mark_id)
                        VALUES (?, ?)
                        """,
                        (int(primary), mark_id),
                    )
                _patch_dispatch_send_marks(conn, dispatch_id, marks)
                conn.commit()
                return mark_id
        except Exception:
            logger.exception("journal.record_send_marks failed")
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
            logger.exception("journal.record_halt failed")

    def recent_send_marks(self, limit: int = 50) -> List[dict]:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts, proposal_id, dispatch_id, order_id, symbol,
                           strategy, card, side, ibkr_last, bid, ask, mid,
                           sent_price, fill_price, signed_slippage, spread_paid,
                           fill_label, status, seen_working
                    FROM send_marks
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("journal.recent_send_marks failed")
            return []

    def send_marks_by_order_id(self, limit: int = 4000) -> dict:
        """Primary send order_id -> dispatch NBBO. Bracket children are omitted.

        Conservative reprice is fill vs this send's quote, not the parent
        bracket's NBBO stamped onto a stop.
        """
        out: dict = {}
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT order_id, ibkr_last, bid, ask, mid, sent_price,
                           fill_price, fill_label, side, status
                    FROM send_marks
                    WHERE order_id IS NOT NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            for row in rows:
                oid = _coerce_order_id(row["order_id"])
                if oid is None or oid in out:
                    continue
                out[oid] = dict(row)
        except Exception:
            logger.exception("journal.send_marks_by_order_id failed")
        return out

    def record_decision(
        self,
        cycle: Any = None,
        action: str = "",
        strategy: str = "",
        rationale: str = "",
        portfolio_snapshot: Any = None,
        outcome: Any = None,
        *,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        """Record a look decision (including hold / blocked outcomes)."""
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            cycle_int: Optional[int] = None
            if cycle is not None:
                try:
                    cycle_int = int(cycle)
                except (TypeError, ValueError):
                    cycle_int = None
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO decisions (
                        ts, cycle, action, strategy, rationale,
                        portfolio_json, outcome_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        cycle_int,
                        action or None,
                        strategy or None,
                        (rationale or None),
                        (
                            _json_dumps(portfolio_snapshot)
                            if portfolio_snapshot is not None
                            else None
                        ),
                        _json_dumps(outcome) if outcome is not None else None,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_decision failed")
            return None

    def set_working_thesis(self, text: str, *, ts: Optional[str] = None) -> None:
        """Upsert the single working thesis row (id=1)."""
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            body = (text or "").strip()
            if not body:
                return
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO working_thesis (id, ts, text)
                    VALUES (1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, text = excluded.text
                    """,
                    (_row_ts(ts), body[:2000]),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.set_working_thesis failed")

    def record_judgment(
        self,
        *,
        cycle: Any = None,
        stance: str = "",
        thesis: str = "",
        focus: str = "",
        dismissed: str = "",
        intent: Any = None,
        judgment: Any = None,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        """Persist a Judge-stage record for thesis continuity across looks."""
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            cycle_int: Optional[int] = None
            if cycle is not None:
                try:
                    cycle_int = int(cycle)
                except (TypeError, ValueError):
                    cycle_int = None
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO judgments (
                        ts, cycle, stance, thesis, focus, dismissed,
                        intent_json, judgment_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        cycle_int,
                        stance or None,
                        (thesis or None),
                        (focus or None),
                        (dismissed or None),
                        _json_dumps(intent) if intent is not None else None,
                        _json_dumps(judgment) if judgment is not None else None,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_judgment failed")
            return None

    # ------------------------------------------------------------------
    # Readers (never raise into the caller)
    # ------------------------------------------------------------------

    def recent_judgments(self, limit: int = 8) -> List[dict]:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts, cycle, stance, thesis, focus, dismissed,
                           intent_json, judgment_json
                    FROM judgments
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                item = dict(row)
                for key, dest in (
                    ("intent_json", "intent"),
                    ("judgment_json", "judgment"),
                ):
                    raw = item.pop(key, None)
                    if raw:
                        try:
                            item[dest] = json.loads(raw)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            item[dest] = raw
                    else:
                        item[dest] = None
                out.append(item)
            return out
        except Exception:
            logger.exception("journal.recent_judgments failed")
            return []

    def recent_decisions(self, limit: int = 8) -> List[dict]:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts, cycle, action, strategy, rationale,
                           portfolio_json, outcome_json
                    FROM decisions
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                item = dict(row)
                for key, dest in (
                    ("portfolio_json", "portfolio_snapshot"),
                    ("outcome_json", "outcome"),
                ):
                    raw = item.pop(key, None)
                    if raw:
                        try:
                            item[dest] = json.loads(raw)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            item[dest] = raw
                    else:
                        item[dest] = None
                out.append(item)
            return out
        except Exception:
            logger.exception("journal.recent_decisions failed")
            return []

    def strategy_diversity(self, limit: int = 40) -> dict:
        """Observe-only KPI: distinct strategies in recent decisions.

        Does not influence the agent loop — Phase 5C-style operator metric.
        """
        skip = frozenset({"", "blocked", "skipped", "hold", "—", "-"})
        decisions = self.recent_decisions(limit=max(1, int(limit)))
        seen: list[str] = []
        for d in decisions:
            strat = str(d.get("strategy") or d.get("action") or "").strip().lower()
            if not strat or strat in skip:
                continue
            if strat not in seen:
                seen.append(strat)
        return {
            "n_decisions": len(decisions),
            "n_distinct": len(seen),
            "strategies": seen,
            "limit": int(limit),
        }

    def get_working_thesis(self) -> str:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT text FROM working_thesis WHERE id = 1"
                ).fetchone()
            if not row:
                return ""
            return str(row["text"] or "")
        except Exception:
            logger.exception("journal.get_working_thesis failed")
            return ""

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

    def record_self_tune(
        self,
        *,
        applied: Any = None,
        clamped: Any = None,
        rejected: Any = None,
        rationale: str = "",
        ts: Optional[str] = None,
    ) -> Optional[int]:
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO self_tunes (ts, applied_json, clamped_json, rejected_json, rationale)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        _json_dumps(applied or {}),
                        _json_dumps(clamped or {}),
                        _json_dumps(rejected or {}),
                        (rationale or "")[:500],
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_self_tune failed")
            return None

    def recent_self_tunes(self, limit: int = 8) -> List[dict]:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts, applied_json, clamped_json, rejected_json, rationale
                    FROM self_tunes
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                item = dict(row)
                for key, dest in (
                    ("applied_json", "applied"),
                    ("clamped_json", "clamped"),
                    ("rejected_json", "rejected"),
                ):
                    raw = item.pop(key, None)
                    if raw:
                        try:
                            item[dest] = json.loads(raw)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            item[dest] = raw
                    else:
                        item[dest] = {}
                out.append(item)
            return out
        except Exception:
            logger.exception("journal.recent_self_tunes failed")
            return []
