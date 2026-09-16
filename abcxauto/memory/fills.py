"""Journal fills — snapshots, prints, send-mark linkage, look/poll ingest.

Moved from journal.py. Method bodies are unchanged, including ingest_poll.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, List, Optional

from abcxauto.memory.journal_support import (
    _UNFILLED_GRACE_S,
    _account_float,
    _align_fill_ts_to_dispatch,
    _coerce_order_id,
    _fill_multiplier,
    _json_dumps,
    _open_order_id_set,
    _order_ids_from_result_json,
    _parse_ts,
    _patch_dispatch_send_marks,
    _row_ts,
    _sql_fill_dict,
)

logger = logging.getLogger("abcxauto.memory.journal")


class JournalFills:
    """Fill ingest, missed-send resolve, and fill readers."""


    def record_snapshot(
        self,
        account: Optional[dict] = None,
        positions: Optional[list] = None,
        open_orders: Optional[list] = None,
        *,
        ts: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            account = account or {}
            net_liq = _account_float(account, "netliquidation", "NetLiquidation")
            daily_pnl = _account_float(account, "dailypnl", "DailyPnL")
            total_cash = _account_float(
                account, "totalcashvalue", "TotalCashValue", "total_cash", "TotalCash"
            )
            stamp = _row_ts(ts)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO snapshots (
                        ts, net_liquidation, daily_pnl, total_cash,
                        positions_json, open_orders_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stamp,
                        net_liq,
                        daily_pnl,
                        total_cash,
                        _json_dumps(positions or []),
                        _json_dumps(open_orders or []),
                    ),
                )
                conn.commit()
            if net_liq is not None and self._pending_session:
                model, pending_ts = self._pending_session
                self._pending_session = None
                try:
                    self.ensure_model_session(
                        model, net_liquidation=net_liq, ts=pending_ts or stamp
                    )
                except Exception:
                    logger.exception("journal.record_snapshot session stamp failed")
        except Exception:
            logger.exception("journal.record_snapshot failed")

    def record_fills(self, fills: Optional[list] = None) -> int:
        """Idempotent insert of fill dicts (UNIQUE exec_id). Returns rows inserted."""
        if not self.enabled:
            return 0
        try:
            self._ensure_schema()
            inserted = 0
            with self._connect() as conn:
                anchors: dict = {}
                for row in conn.execute(
                    "SELECT ts, result_json FROM dispatches WHERE result_json IS NOT NULL"
                ).fetchall():
                    dts = str(row["ts"] or "")
                    if not dts:
                        continue
                    for oid in _order_ids_from_result_json(row["result_json"]):
                        prev = anchors.get(oid)
                        if prev is None or dts < prev:
                            anchors[oid] = dts
                mark_by_oid: dict = {}
                try:
                    for mrow in conn.execute(
                        "SELECT send_mark_id, order_id FROM send_mark_orders"
                    ).fetchall():
                        oid_key = _coerce_order_id(mrow["order_id"])
                        if oid_key is not None:
                            mark_by_oid[oid_key] = int(mrow["send_mark_id"])
                except sqlite3.OperationalError:
                    mark_by_oid = {}
                mark_rows: dict = {}
                if mark_by_oid:
                    ids = sorted(set(mark_by_oid.values()))
                    qmarks = ",".join("?" * len(ids))
                    for mrow in conn.execute(
                        f"SELECT * FROM send_marks WHERE id IN ({qmarks})",
                        ids,
                    ).fetchall():
                        mark_rows[int(mrow["id"])] = dict(mrow)
                for fill in fills or []:
                    if not isinstance(fill, dict):
                        continue
                    exec_id = fill.get("exec_id")
                    if exec_id is None or str(exec_id).strip() == "":
                        continue
                    try:
                        oid = _coerce_order_id(fill.get("order_id"))
                        fill_ts = _align_fill_ts_to_dispatch(
                            _row_ts(fill.get("ts")),
                            anchors.get(oid) if oid is not None else None,
                        )
                        raw_mark = (
                            mark_rows.get(mark_by_oid.get(oid))
                            if oid is not None
                            else None
                        )
                        primary = (
                            _coerce_order_id(raw_mark.get("order_id"))
                            if isinstance(raw_mark, dict)
                            else None
                        )
                        # Stop/target oids share the entry send_mark. Do not
                        # stamp that dispatch NBBO onto the closer.
                        stamp_mark = (
                            raw_mark
                            if isinstance(raw_mark, dict)
                            and (primary is None or oid == primary)
                            else None
                        )
                        fill_marks = self._fill_mark_values(stamp_mark, fill)
                        if fill_marks.get("sent_price") is None and fill.get("sent_price") is not None:
                            fill_marks["sent_price"] = fill.get("sent_price")
                        if fill_marks.get("signed_slippage") is None and fill.get("signed_slippage") is not None:
                            fill_marks["signed_slippage"] = fill.get("signed_slippage")
                        if fill_marks.get("ibkr_last") is None:
                            fill_marks["ibkr_last"] = fill.get("ibkr_last") or fill.get("last")
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO fills (
                                ts, exec_id, order_id, symbol, sec_type, side,
                                quantity, price, commission, realized_pnl,
                                ibkr_last, bid, ask, sent_price,
                                signed_slippage, spread_paid, fill_label,
                                quote_reason, multiplier
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                fill_ts,
                                str(exec_id),
                                oid,
                                fill.get("symbol"),
                                fill.get("sec_type"),
                                fill.get("side"),
                                fill.get("quantity"),
                                fill.get("price"),
                                fill.get("commission"),
                                fill.get("realized_pnl"),
                                fill_marks.get("ibkr_last"),
                                fill_marks.get("bid"),
                                fill_marks.get("ask"),
                                fill_marks.get("sent_price"),
                                fill_marks.get("signed_slippage"),
                                fill_marks.get("spread_paid"),
                                fill_marks.get("fill_label"),
                                fill_marks.get("quote_reason"),
                                _fill_multiplier(fill),
                            ),
                        )
                        inserted += int(cur.rowcount or 0)
                        if (
                            int(cur.rowcount or 0)
                            and isinstance(raw_mark, dict)
                            and oid is not None
                        ):
                            self._apply_fill_to_send_mark_locked(
                                conn, raw_mark, fill, oid
                            )
                            refreshed = conn.execute(
                                "SELECT * FROM send_marks WHERE id = ?",
                                (int(raw_mark["id"]),),
                            ).fetchone()
                            if refreshed is not None:
                                mark_rows[int(raw_mark["id"])] = dict(refreshed)
                    except Exception:
                        logger.exception(
                            "journal.record_fills row failed exec_id=%s", exec_id
                        )
                conn.commit()
            return inserted
        except Exception:
            logger.exception("journal.record_fills failed")
            return 0

    def _fill_mark_values(self, mark: Optional[dict], fill: dict) -> dict:
        """Fill-time IBKR bid/ask, else this send's NBBO. Never invent a mid.

        Parent-bracket quotes stay off closers. A last-only or mid-only
        print is not bid/ask — those stay null with ``quote_reason``.
        """
        from abcxauto.send_marks import (
            QUOTE_REASON_IBKR_LIVE,
            QUOTE_REASON_INCOMPLETE,
            QUOTE_REASON_NO_QUOTE,
            QUOTE_REASON_SEND_NBBO,
            apply_fill_to_marks,
            compute_marks,
            finite_px,
            public_marks,
            quote_reason_of,
        )

        empty = {
            key: None
            for key in (
                "ibkr_last",
                "bid",
                "ask",
                "sent_price",
                "signed_slippage",
                "spread_paid",
                "fill_label",
            )
        }
        empty["quote_reason"] = fill.get("quote_reason") or QUOTE_REASON_NO_QUOTE
        fill_bid = finite_px(fill.get("bid"))
        fill_ask = finite_px(fill.get("ask"))
        if fill_bid is not None and fill_ask is not None:
            pub = public_marks(
                compute_marks(
                    {
                        "last": fill.get("ibkr_last") or fill.get("last"),
                        "bid": fill_bid,
                        "ask": fill_ask,
                    },
                    sent_price=fill.get("sent_price"),
                    fill_price=fill.get("price"),
                    side=fill.get("side"),
                    status="filled" if fill.get("price") is not None else "",
                )
            )
            pub["quote_reason"] = (
                fill.get("quote_reason") or QUOTE_REASON_IBKR_LIVE
            )
            return pub
        if isinstance(mark, dict):
            filled = apply_fill_to_marks(
                mark,
                fill_price=fill.get("price"),
                side=fill.get("side") or mark.get("side"),
            )
            pub = public_marks(filled)
            if finite_px(pub.get("bid")) is not None and finite_px(pub.get("ask")) is not None:
                pub["quote_reason"] = QUOTE_REASON_SEND_NBBO
                return pub
        if fill_bid is not None or fill_ask is not None:
            empty["bid"] = fill_bid
            empty["ask"] = fill_ask
            empty["ibkr_last"] = finite_px(fill.get("ibkr_last") or fill.get("last"))
            empty["quote_reason"] = fill.get("quote_reason") or QUOTE_REASON_INCOMPLETE
            return empty
        given = str(fill.get("quote_reason") or "").strip()
        empty["quote_reason"] = given or quote_reason_of(
            bid=fill.get("bid"), ask=fill.get("ask")
        )
        if empty["quote_reason"] == QUOTE_REASON_IBKR_LIVE:
            empty["quote_reason"] = QUOTE_REASON_NO_QUOTE
        return empty

    def _apply_fill_to_send_mark_locked(
        self,
        conn: sqlite3.Connection,
        mark: dict,
        fill: dict,
        oid: int,
    ) -> None:
        """Update the send row when its primary (entry) order fills."""
        from abcxauto.send_marks import apply_fill_to_marks

        primary = _coerce_order_id(mark.get("order_id"))
        if primary is not None and oid != primary:
            return
        if str(mark.get("status") or "") == "filled" and mark.get("fill_price") is not None:
            # Keep the first print; partials still land on the fills table.
            if primary is not None:
                return
        filled = apply_fill_to_marks(
            mark, fill_price=fill.get("price"), side=fill.get("side") or mark.get("side")
        )
        conn.execute(
            """
            UPDATE send_marks SET
                fill_price = ?, signed_slippage = ?, spread_paid = ?,
                fill_label = ?, status = ?, marks_json = ?
            WHERE id = ?
            """,
            (
                filled.get("fill_price"),
                filled.get("signed_slippage"),
                filled.get("spread_paid"),
                filled.get("fill_label"),
                filled.get("status"),
                _json_dumps(filled),
                int(mark["id"]),
            ),
        )
        _patch_dispatch_send_marks(conn, mark.get("dispatch_id"), filled)

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
            logger.exception("journal.resolve_unfilled_sends failed")
            return 0

    def ingest_poll(self, snap: Optional[dict] = None) -> dict:
        """Persist fills and missed sends from a monitor poll. No snapshot row.

        P&L truth cannot wait on a look. Snapshot rows are look-only.
        """
        bag = snap if isinstance(snap, dict) else {}
        open_orders = (
            bag.get("open_orders") if isinstance(bag.get("open_orders"), list) else []
        )
        fills = bag.get("fills") if isinstance(bag.get("fills"), list) else []
        taken = bag.get("taken_at")
        ts = taken if isinstance(taken, str) and taken.strip() else None
        inserted = self.record_fills(fills)
        resolved = 0
        try:
            resolved = self.resolve_unfilled_sends(open_orders, ts=ts)
        except Exception:
            logger.exception("journal.ingest_poll resolve failed")
        return {"fills_inserted": int(inserted or 0), "sends_resolved": int(resolved or 0)}

    def ingest_look(self, snap: Optional[dict] = None) -> dict:
        """Persist this look's book. Snapshot row is look-only.

        Fills and missed-send resolve also run here (idempotent with ``ingest_poll``).
        """
        bag = snap if isinstance(snap, dict) else {}
        account = bag.get("account") if isinstance(bag.get("account"), dict) else {}
        positions = bag.get("positions") if isinstance(bag.get("positions"), list) else []
        open_orders = (
            bag.get("open_orders") if isinstance(bag.get("open_orders"), list) else []
        )
        fills = bag.get("fills") if isinstance(bag.get("fills"), list) else []
        taken = bag.get("taken_at")
        ts = taken if isinstance(taken, str) and taken.strip() else None
        self.record_snapshot(account, positions, open_orders, ts=ts)
        nl = _account_float(account, "netliquidation", "NetLiquidation")
        if nl is not None:
            try:
                self.ensure_session_start_nl(nl, ts=ts)
            except Exception:
                logger.exception("journal.ingest_look session-start NL failed")
        inserted = self.record_fills(fills)
        resolved = 0
        try:
            resolved = self.resolve_unfilled_sends(open_orders, ts=ts)
        except Exception:
            logger.exception("journal.ingest_look resolve failed")
        try:
            from abcxauto.pcs_fill_lambda import ingest_pcs_from_look

            ingest_pcs_from_look(self, bag)
        except Exception:
            logger.debug("journal.ingest_look pcs fill-λ failed", exc_info=True)
        return {"fills_inserted": int(inserted or 0), "sends_resolved": int(resolved or 0)}

    def dispatched_order_ids(self, limit: int = 4000) -> set:
        """Order ids the clerk actually placed, from every dispatch result.

        The complement is the signal ``strategy_performance`` already buckets as
        ``(unattributed)``: a fill whose order id is not here came from a manual
        TWS order, another client session, or the panic/halt flatten path —
        never from a ticket this desk dispatched.
        """
        out: set = set()
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT result_json FROM dispatches
                    WHERE result_json IS NOT NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            for row in rows:
                out |= _order_ids_from_result_json(row["result_json"])
        except Exception:
            logger.exception("journal.dispatched_order_ids failed")
        return out

    def closing_fills(self, limit: int = 2000) -> List[dict]:
        """Fills that carry realized P&L — the ones that closed something.

        Openers report 0 / missing realized P&L, so a non-zero value is the
        marker of an exit. Symbol and ts let a caller line an exit up with the
        entry it closed without re-deriving the fills join. ``realized_pnl``
        is the raw IBKR print; commissions stay on the row for the caller to
        net.
        """
        out: List[dict] = []
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts, exec_id, order_id, symbol, sec_type, side,
                           quantity, price, commission, realized_pnl,
                           ibkr_last, bid, ask, fill_label, quote_reason
                    FROM fills
                    WHERE realized_pnl IS NOT NULL AND ABS(realized_pnl) > 1e-9
                    ORDER BY ts ASC, id ASC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            for row in rows:
                try:
                    item = _sql_fill_dict(row)
                except Exception:
                    continue
                if item.get("realized_pnl") is None:
                    continue
                out.append(item)
        except Exception:
            logger.exception("journal.closing_fills failed")
        return out

    def listed_fills(self, limit: int = 4000) -> List[dict]:
        """Every stored fill with price, side, fee, and quote sides when present.

        Openers and closers both land here so a conservative round-trip can
        mark debit-at-ask / credit-at-bid without a second join.
        """
        out: List[dict] = []
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts, exec_id, order_id, symbol, sec_type, side,
                           quantity, price, commission, realized_pnl,
                           ibkr_last, bid, ask, fill_label, quote_reason
                    FROM fills
                    ORDER BY ts ASC, id ASC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            for row in rows:
                try:
                    out.append(_sql_fill_dict(row))
                except Exception:
                    continue
        except Exception:
            logger.exception("journal.listed_fills failed")
        return out

    def realized_by_order_id(self, limit: int = 2000) -> dict:
        """order_id -> summed realized P&L net of commissions.

        IBKR ``commissionReport.realizedPNL`` is the raw close print. Fees
        belong in the same number a caller reads as P&L. Opening fills with
        a commission and no realized still subtract.
        """
        out: dict = {}
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT order_id, realized_pnl, commission
                    FROM fills
                    WHERE order_id IS NOT NULL
                      AND (realized_pnl IS NOT NULL OR commission IS NOT NULL)
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            for row in rows:
                oid = _coerce_order_id(row["order_id"])
                if oid is None:
                    continue
                pnl = 0.0
                try:
                    if row["realized_pnl"] is not None:
                        pnl = float(row["realized_pnl"])
                except (TypeError, ValueError):
                    pnl = 0.0
                fee = 0.0
                try:
                    if row["commission"] is not None:
                        fee = abs(float(row["commission"]))
                except (TypeError, ValueError):
                    fee = 0.0
                out[oid] = out.get(oid, 0.0) + pnl - fee
        except Exception:
            logger.exception("journal.realized_by_order_id failed")
        return out
