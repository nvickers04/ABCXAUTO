"""Decisions, judgments, working thesis, and strategy diversity."""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from abcxauto.memory.journal.util import _json_dumps, _row_ts

logger = logging.getLogger(__name__)


class LessonsMixin:
    """Decisions, judgments, working thesis, and strategy diversity."""

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
            logger.exception("journal.record_decision failed table=decisions")
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
            logger.exception(
                "journal.set_working_thesis failed table=working_thesis"
            )

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
            logger.exception("journal.record_judgment failed table=judgments")
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
