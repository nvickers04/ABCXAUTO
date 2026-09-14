"""self_tune application log."""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from abcxauto.memory.journal.util import _json_dumps, _row_ts

logger = logging.getLogger(__name__)


class SelfTuneMixin:
    """self_tune application log."""

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
            logger.exception("journal.record_self_tune failed table=self_tunes")
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
