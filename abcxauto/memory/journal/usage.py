"""Model token/cost rows."""

from __future__ import annotations

import logging
from typing import Optional

from abcxauto.memory.journal.util import _row_ts, _ts_bound

logger = logging.getLogger(__name__)


class UsageMixin:
    """Model token/cost rows."""

    def record_model_usage(
        self,
        *,
        stage: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        cost_usd: float = 0.0,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        if not self.enabled:
            return None
        inn = int(input_tokens or 0)
        out = int(output_tokens or 0)
        cached = int(cached_tokens or 0)
        if inn <= 0 and out <= 0 and cached <= 0:
            # Empty token row with a leftover $0.18 is not cost truth.
            return None
        try:
            self._ensure_schema()
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO model_usage (
                        ts, stage, model, input_tokens, output_tokens, cached_tokens, cost_usd
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        stage or None,
                        (model or None),
                        inn,
                        out,
                        cached,
                        float(cost_usd or 0.0),
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_model_usage failed table=model_usage")
            return None
    def model_usage_since(self, since_iso: str) -> dict:
        """Model usage with ts >= since_iso. Empty dict-shaped totals on miss."""
        empty = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": 0.0,
        }
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                           COALESCE(SUM(cost_usd), 0) AS cost_usd
                    FROM model_usage
                    WHERE ts >= ?
                    """,
                    (_ts_bound(since_iso),),
                ).fetchone()
            if not row:
                return empty
            return {
                "calls": int(row["calls"] or 0),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cached_tokens": int(row["cached_tokens"] or 0),
                "cost_usd": float(row["cost_usd"] or 0.0),
            }
        except Exception:
            logger.exception("journal.model_usage_since failed")
            return empty
    def model_usage_totals(self) -> dict:
        empty = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_usd": 0.0,
        }
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                           COALESCE(SUM(cost_usd), 0) AS cost_usd
                    FROM model_usage
                    """
                ).fetchone()
            if not row:
                return empty
            return {
                "calls": int(row["calls"] or 0),
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cached_tokens": int(row["cached_tokens"] or 0),
                "cost_usd": float(row["cost_usd"] or 0.0),
            }
        except Exception:
            logger.exception("journal.model_usage_totals failed")
            return empty
