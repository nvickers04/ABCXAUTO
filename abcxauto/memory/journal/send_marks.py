"""Send previews and dispatch-time NBBO vs later fill."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, List, Optional

from abcxauto.memory.journal.util import (
    _UNFILLED_GRACE_S,
    _coerce_order_id,
    _json_dumps,
    _open_order_id_set,
    _order_ids_from_result_json,
    _parse_ts,
    _patch_dispatch_send_marks,
    _row_ts,
)

logger = logging.getLogger(__name__)


class SendMarksMixin:
    """Send previews and dispatch-time NBBO vs later fill."""

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
            logger.exception("journal.record_send_preview failed table=send_previews")
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
            logger.exception(
                "journal.mark_preview_token_used failed table=send_previews"
            )

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
            logger.exception(
                "journal.record_send_marks failed table=send_marks "
                "proposal_id=%s order_id=%s",
                proposal_id,
                marks.get("order_id") if isinstance(marks, dict) else None,
            )
            return None
    def resolve_unfilled_sends(
        self,
        open_orders: Any = None,
        *,
        grace_s: float = _UNFILLED_GRACE_S,
        ts: Optional[str] = None,
    ) -> int:
        """Once a working order is gone with no fill, stamp the send row missed."""
        if not self.enabled:
            return 0
        try:
            from abcxauto.send_marks import mark_missed

            self._ensure_schema()
            open_ids = _open_order_id_set(open_orders)
            now = _parse_ts(ts) or datetime.now(timezone.utc)
            try:
                grace = float(grace_s)
            except (TypeError, ValueError):
                grace = _UNFILLED_GRACE_S
            resolved = 0
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM send_marks
                    WHERE status = 'working' OR (status IS NULL AND fill_price IS NULL)
                    """
                ).fetchall()
                for row in rows:
                    mark = dict(row)
                    mark_id = int(mark["id"])
                    oids: set = set()
                    primary = _coerce_order_id(mark.get("order_id"))
                    if primary is not None:
                        oids.add(primary)
                    for link in conn.execute(
                        "SELECT order_id FROM send_mark_orders WHERE send_mark_id = ?",
                        (mark_id,),
                    ).fetchall():
                        oid = _coerce_order_id(link["order_id"])
                        if oid is not None:
                            oids.add(oid)
                    if not oids:
                        continue
                    live = oids & open_ids
                    if live:
                        if not mark.get("seen_working"):
                            conn.execute(
                                "UPDATE send_marks SET seen_working = 1 WHERE id = ?",
                                (mark_id,),
                            )
                        continue
                    fill_row = conn.execute(
                        """
                        SELECT price, side FROM fills
                        WHERE order_id IN ({})
                        ORDER BY id ASC LIMIT 1
                        """.format(",".join("?" * len(oids))),
                        tuple(sorted(oids)),
                    ).fetchone()
                    if fill_row is not None and fill_row["price"] is not None:
                        self._apply_fill_to_send_mark_locked(
                            conn,
                            mark,
                            {
                                "price": fill_row["price"],
                                "side": fill_row["side"],
                            },
                            primary if primary is not None else next(iter(oids)),
                        )
                        resolved += 1
                        continue
                    seen = bool(mark.get("seen_working"))
                    age = 0.0
                    born = _parse_ts(mark.get("ts"))
                    if born is not None:
                        age = (now - born).total_seconds()
                    if not seen and age < grace:
                        continue
                    missed = mark_missed(mark)
                    conn.execute(
                        """
                        UPDATE send_marks SET
                            fill_price = NULL, signed_slippage = NULL, spread_paid = NULL,
                            fill_label = ?, status = ?, marks_json = ?
                        WHERE id = ?
                        """,
                        (
                            missed.get("fill_label"),
                            missed.get("status"),
                            _json_dumps(missed),
                            mark_id,
                        ),
                    )
                    _patch_dispatch_send_marks(conn, mark.get("dispatch_id"), missed)
                    resolved += 1
                conn.commit()
            return resolved
        except Exception:
            logger.exception(
                "journal.resolve_unfilled_sends failed table=send_marks"
            )
            return 0
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
