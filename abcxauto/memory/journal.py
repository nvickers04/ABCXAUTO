"""SQLite trade journal — durable record of proposals, gates, dispatches, halts,
snapshots, and fills.

Uses stdlib sqlite3 only. Journaling must never break trading: all public write/read
methods catch and log internally.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from abcxauto.memory.schema import JournalSchema, _FILL_MARK_COLS, _SCHEMA_SQL
from abcxauto.memory.trades import JournalTrades
from abcxauto.memory.fills import JournalFills
from abcxauto.memory.journal_support import (
    _DEFAULT_DB_PATH,
    _DISPATCH_GRACE,
    _FILL_TZ_OFFSETS_H,
    _FILL_TZ_SLACK,
    _ORDER_IDS_LIST_KEYS,
    _ORDER_ID_KEYS,
    _REPO_ROOT,
    _UNFILLED_GRACE_S,
    _account_float,
    _align_fill_ts_to_dispatch,
    _coerce_order_id,
    _collect_order_ids,
    _ensure_columns,
    _env_bool,
    _et_calendar_date,
    _et_day_utc_range,
    _json_dumps,
    _open_order_id_set,
    _order_ids_from_result_json,
    _parse_ts,
    _patch_dispatch_send_marks,
    _row_ts,
    _sql_fill_dict,
    _sqlite_locked,
    _table_cols,
    _ts_bound,
    _utc_iso,
    _utc_now_iso,
)

logger = logging.getLogger(__name__)


class TradeJournal(JournalSchema, JournalTrades, JournalFills):
    """Thread-safe SQLite trade journal (one connection per call, WAL mode)."""

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

    def equity_curve(self, limit: int = 500) -> List[Tuple[str, Optional[float]]]:
        """Most recent ``limit`` snapshots, oldest-first for charting."""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts, net_liquidation FROM (
                        SELECT ts, net_liquidation, id
                        FROM snapshots
                        ORDER BY id DESC
                        LIMIT ?
                    ) AS recent
                    ORDER BY id ASC
                    """,
                    (int(limit),),
                ).fetchall()
            return [(str(r["ts"]), r["net_liquidation"]) for r in rows]
        except Exception:
            logger.exception("journal.equity_curve failed")
            return []

    def account_performance(self) -> dict:
        """Latest NetLiq plus simple returns vs IBKR snapshots ~1w / 3m / 1y ago.

        A horizon is only populated when a snapshot exists at least that many
        calendar days before ``as_of`` (no oldest-snapshot fallback).

        Returns keys: net_liquidation, daily_pnl, ret_1w, ret_3m, ret_1y,
        as_of, history_start, history_days, source (``ibkr_nav`` | ``none``).
        Returns are fractions, or None when history is insufficient.
        """
        empty = {
            "net_liquidation": None,
            "daily_pnl": None,
            "ret_1w": None,
            "ret_3m": None,
            "ret_1y": None,
            "as_of": None,
            "history_start": None,
            "history_days": None,
            "source": "none",
        }
        try:
            self._ensure_schema()
            now = datetime.now(timezone.utc)
            with self._connect() as conn:
                latest = conn.execute(
                    """
                    SELECT ts, net_liquidation, daily_pnl FROM snapshots
                    WHERE net_liquidation IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
                if not latest or latest["net_liquidation"] is None:
                    return empty
                try:
                    net = float(latest["net_liquidation"])
                except (TypeError, ValueError):
                    return empty
                if net <= 0:
                    return empty
                daily = latest["daily_pnl"]
                try:
                    daily_f = float(daily) if daily is not None else None
                except (TypeError, ValueError):
                    daily_f = None
                latest_ts = str(latest["ts"] or "")
                snap_day = _et_calendar_date(latest_ts)
                today = _et_calendar_date(now)
                if daily_f is not None and (
                    not snap_day or not today or snap_day != today
                ):
                    # Yesterday's IBKR DailyPnL is not today's daily figure.
                    daily_f = None
                if daily_f is not None:
                    try:
                        marker = self.last_session_marker()
                    except Exception:
                        marker = None
                    if isinstance(marker, dict) and marker.get("ts"):
                        if _ts_bound(latest_ts) < _ts_bound(marker.get("ts")):
                            # Snapshot is from before this session — leftover.
                            daily_f = None

                as_of_dt = now
                if latest_ts:
                    try:
                        parsed = datetime.fromisoformat(
                            latest_ts.replace("Z", "+00:00")
                        )
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        as_of_dt = parsed
                    except ValueError:
                        as_of_dt = now

                oldest = conn.execute(
                    """
                    SELECT ts FROM snapshots
                    WHERE net_liquidation IS NOT NULL
                    ORDER BY id ASC LIMIT 1
                    """
                ).fetchone()
                history_start = str(oldest["ts"]) if oldest and oldest["ts"] else None
                history_days: Optional[int] = None
                if history_start:
                    try:
                        start_dt = datetime.fromisoformat(
                            history_start.replace("Z", "+00:00")
                        )
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=timezone.utc)
                        history_days = max(0, int((now - start_dt).total_seconds() // 86400))
                    except ValueError:
                        history_days = None

                def _baseline(days: int) -> float | None:
                    cutoff = _ts_bound(as_of_dt - timedelta(days=days))
                    row = conn.execute(
                        """
                        SELECT net_liquidation FROM snapshots
                        WHERE net_liquidation IS NOT NULL
                          AND net_liquidation > 0
                          AND ts <= ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (cutoff,),
                    ).fetchone()
                    if not row or row["net_liquidation"] is None:
                        return None
                    try:
                        base = float(row["net_liquidation"])
                    except (TypeError, ValueError):
                        return None
                    if base <= 0:
                        return None
                    return (net / base) - 1.0

                return {
                    "net_liquidation": net,
                    "daily_pnl": daily_f,
                    "ret_1w": _baseline(7),
                    "ret_3m": _baseline(90),
                    "ret_1y": _baseline(365),
                    "as_of": str(latest["ts"]),
                    "history_start": history_start,
                    "history_days": history_days,
                    "source": "ibkr_nav",
                }
        except Exception:
            logger.exception("journal.account_performance failed")
            return empty

    def closed_fill_pnls(self, limit: int = 200) -> List[float]:
        """Realized P&L on closing fills. Openers are usually 0 / missing."""
        try:
            self._ensure_schema()
            cap = max(8, min(500, int(limit or 200)))
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT realized_pnl FROM fills
                    WHERE realized_pnl IS NOT NULL
                      AND ABS(realized_pnl) > 1e-9
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (cap,),
                ).fetchall()
            out: List[float] = []
            for row in rows:
                try:
                    out.append(float(row["realized_pnl"]))
                except (TypeError, ValueError, KeyError):
                    continue
            return out
        except Exception:
            logger.exception("journal.closed_fill_pnls failed")
            return []

    def strategy_performance(self, since_day: Optional[str] = None) -> List[dict]:
        """Per-strategy realized P&L attribution from fills joined through dispatches.

        Join chain (best-effort, read-only):
          fills.order_id
            -> dispatches.result_json order ids
               (order_id / order_ids / bracket_order_id / entry_order_id /
                stop_order_id / target_order_id, including nested dicts)
            -> dispatches.proposal_id
            -> proposals.strategy

        Fills whose order_id matches no dispatch aggregate under strategy
        ``(unattributed)``.

        Limitations:
        - IBKR ``commissionReport.realizedPNL`` is typically present only on
          closing fills; opening fills often show 0 / missing realized P&L.
        - Attribution depends on order ids recorded in dispatch result_json;
          manual TWS orders and fills from other client sessions are usually
          unattributed.
        - Session ``ib.fills()`` history is incomplete across restarts; this
          method only sees fills already stored in the journal.
        - If multiple dispatches claim the same order id, the earliest
          dispatch (lowest id) wins.
        """
        try:
            self._ensure_schema()
            with self._connect() as conn:
                dispatch_rows = conn.execute(
                    """
                    SELECT d.id, d.proposal_id, d.result_json, p.strategy
                    FROM dispatches d
                    LEFT JOIN proposals p ON p.id = d.proposal_id
                    ORDER BY d.id ASC
                    """
                ).fetchall()
                if since_day:
                    like = f"{since_day}%"
                    fill_rows = conn.execute(
                        """
                        SELECT ts, order_id, commission, realized_pnl
                        FROM fills
                        WHERE ts LIKE ?
                        ORDER BY id ASC
                        """,
                        (like,),
                    ).fetchall()
                else:
                    fill_rows = conn.execute(
                        """
                        SELECT ts, order_id, commission, realized_pnl
                        FROM fills
                        ORDER BY id ASC
                        """
                    ).fetchall()

            order_to_strategy: dict = {}
            for row in dispatch_rows:
                strategy = (row["strategy"] or "").strip() or "(unknown)"
                for oid in _order_ids_from_result_json(row["result_json"]):
                    if oid not in order_to_strategy:
                        order_to_strategy[oid] = strategy

            buckets: dict = {}
            for row in fill_rows:
                oid = _coerce_order_id(row["order_id"])
                if oid is not None and oid in order_to_strategy:
                    strategy = order_to_strategy[oid]
                else:
                    strategy = "(unattributed)"
                bucket = buckets.setdefault(
                    strategy,
                    {
                        "strategy": strategy,
                        "n_fills": 0,
                        "realized_pnl_sum": 0.0,
                        "commissions_sum": 0.0,
                        "first_fill_ts": None,
                        "last_fill_ts": None,
                    },
                )
                bucket["n_fills"] += 1
                try:
                    if row["realized_pnl"] is not None:
                        bucket["realized_pnl_sum"] += float(row["realized_pnl"])
                except (TypeError, ValueError):
                    pass
                try:
                    if row["commission"] is not None:
                        bucket["commissions_sum"] += float(row["commission"])
                except (TypeError, ValueError):
                    pass
                ts = row["ts"]
                if ts:
                    if bucket["first_fill_ts"] is None or ts < bucket["first_fill_ts"]:
                        bucket["first_fill_ts"] = ts
                    if bucket["last_fill_ts"] is None or ts > bucket["last_fill_ts"]:
                        bucket["last_fill_ts"] = ts

            return sorted(buckets.values(), key=lambda b: b["strategy"])
        except Exception:
            logger.exception("journal.strategy_performance failed")
            return []

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
            logger.exception("journal.record_model_usage failed")
            return None

    def last_session_marker(self) -> Optional[dict]:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ts, model, net_liquidation FROM session_markers
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
            if not row:
                return None
            nl = row["net_liquidation"]
            return {
                "ts": str(row["ts"] or ""),
                "model": str(row["model"] or ""),
                "net_liquidation": float(nl) if nl is not None else None,
            }
        except Exception:
            logger.exception("journal.last_session_marker failed")
            return None

    def session_start_marker(self, session_date: str) -> Optional[dict]:
        """First ``session_markers`` row with usable NL on an ET calendar day."""
        return self._session_marker_on_et_day(session_date, require_nl=True)

    def _session_marker_on_et_day(
        self,
        session_date: str,
        *,
        require_nl: bool,
    ) -> Optional[dict]:
        bounds = _et_day_utc_range(session_date)
        if bounds is None:
            return None
        lo, hi = bounds
        extra = ""
        if require_nl:
            extra = "AND net_liquidation IS NOT NULL AND net_liquidation > 0"
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    f"""
                    SELECT id, ts, model, net_liquidation FROM session_markers
                    WHERE ts >= ? AND ts < ?
                      {extra}
                    ORDER BY id ASC LIMIT 1
                    """,
                    (lo, hi),
                ).fetchone()
            if not row:
                return None
            nl = row["net_liquidation"]
            return {
                "id": int(row["id"]),
                "ts": str(row["ts"] or ""),
                "model": str(row["model"] or ""),
                "net_liquidation": float(nl) if nl is not None else None,
                "session_date": session_date,
            }
        except Exception:
            logger.exception("journal.session_marker_on_et_day failed")
            return None

    def ensure_model_session(
        self,
        model: str,
        *,
        net_liquidation: Optional[float] = None,
        ts: Optional[str] = None,
    ) -> Optional[dict]:
        """Stamp a session when the model changes (or on first real NetLiq).

        A boot call with no book print must not persist ``net_liquidation=None``.
        Wait for a snapshot NL, or skip the stamp.

        Calendar-session start NL is ``ensure_session_start_nl`` — same model
        plus a leftover marker from another ET day must still write today's
        ``session_markers`` row. Read ``last`` after that write so the
        same-model early return cannot skip a new calendar session.
        """
        name = str(model or "").strip()
        if not name or not self.enabled:
            return self.last_session_marker()
        nl: Optional[float] = None
        if net_liquidation is not None:
            try:
                nl = float(net_liquidation)
            except (TypeError, ValueError):
                nl = None
        if nl is not None:
            try:
                self.ensure_session_start_nl(nl, ts=ts, model=name)
            except Exception:
                logger.exception("journal.ensure_model_session session-start NL failed")
        last = self.last_session_marker()
        same = bool(last and str(last.get("model") or "") == name)
        if same and last is not None and last.get("net_liquidation") is not None:
            self._pending_session = None
            return last
        if nl is None:
            self._pending_session = (name, ts)
            return last if same else None
        self._pending_session = None
        try:
            self._ensure_schema()
            if same and last is not None and last.get("net_liquidation") is None:
                with self._connect() as conn:
                    conn.execute(
                        """
                        UPDATE session_markers
                        SET net_liquidation = ?
                        WHERE id = (
                            SELECT id FROM session_markers ORDER BY id DESC LIMIT 1
                        )
                          AND net_liquidation IS NULL
                        """,
                        (nl,),
                    )
                    conn.commit()
                return {
                    "ts": str(last.get("ts") or ""),
                    "model": name,
                    "net_liquidation": nl,
                }
            stamp = _row_ts(ts)
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO session_markers (ts, model, net_liquidation)
                    VALUES (?, ?, ?)
                    """,
                    (stamp, name, nl),
                )
                conn.commit()
            return {
                "ts": stamp,
                "model": name,
                "net_liquidation": nl,
            }
        except Exception:
            logger.exception("journal.ensure_model_session failed")
            return last

    def first_nl_on_et_day(
        self, session_date: str
    ) -> tuple[Optional[float], Optional[str]]:
        """First usable NetLiq snapshot on an ET calendar day. (None, None) if none."""
        bounds = _et_day_utc_range(session_date)
        if bounds is None:
            return None, None
        lo, hi = bounds
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ts, net_liquidation FROM snapshots
                    WHERE net_liquidation IS NOT NULL
                      AND net_liquidation > 0
                      AND ts >= ? AND ts < ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (lo, hi),
                ).fetchone()
            if not row or row["net_liquidation"] is None:
                return None, None
            return float(row["net_liquidation"]), str(row["ts"] or "") or None
        except Exception:
            logger.exception("journal.first_nl_on_et_day failed")
            return None, None

    def ensure_session_start_nl(
        self,
        net_liquidation: Any,
        *,
        ts: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[dict]:
        """Insert today's first usable NL into ``session_markers``. One row per ET day.

        #145 wrote a snapshot when the ET day had none, then returned if any
        snap already existed. ``ingest_look`` always ``record_snapshot`` first,
        so that branch never reached ``session_markers``. Same-model
        ``ensure_model_session`` then returned the leftover marker (last live
        row 2026-08-26) and skipped the insert. Today's start NL never landed.

        Subsequent looks the same calendar session must not clobber the start
        NL. A first-of-day snapshot is still seeded when the day has none so
        nav windows keep a path.
        """
        if not self.enabled:
            return None
        try:
            nl = float(net_liquidation)
        except (TypeError, ValueError):
            return None
        if nl != nl or nl <= 0:
            return None
        stamp = _row_ts(ts)
        day = _et_calendar_date(stamp)
        if not day:
            return None
        existing = self.session_start_marker(day)
        if existing is not None:
            return existing
        name = str(model or "").strip()
        if not name:
            try:
                from abcxauto.config import get_config

                name = str(getattr(get_config(), "model", "") or "").strip()
            except Exception:
                name = ""
        bounds = _et_day_utc_range(day)
        if bounds is None:
            return None
        lo, hi = bounds
        seeded: Optional[dict] = None
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, ts, model, net_liquidation FROM session_markers
                    WHERE ts >= ? AND ts < ?
                      AND net_liquidation IS NOT NULL
                      AND net_liquidation > 0
                    ORDER BY id ASC LIMIT 1
                    """,
                    (lo, hi),
                ).fetchone()
                if row is not None:
                    got = row["net_liquidation"]
                    return {
                        "id": int(row["id"]),
                        "ts": str(row["ts"] or ""),
                        "model": str(row["model"] or ""),
                        "net_liquidation": float(got) if got is not None else None,
                        "session_date": day,
                    }
                hollow = conn.execute(
                    """
                    SELECT id, ts, model FROM session_markers
                    WHERE ts >= ? AND ts < ?
                      AND (net_liquidation IS NULL OR net_liquidation <= 0)
                    ORDER BY id ASC LIMIT 1
                    """,
                    (lo, hi),
                ).fetchone()
                if hollow is not None:
                    conn.execute(
                        """
                        UPDATE session_markers
                        SET net_liquidation = ?
                        WHERE id = ?
                          AND (net_liquidation IS NULL OR net_liquidation <= 0)
                        """,
                        (nl, int(hollow["id"])),
                    )
                    conn.commit()
                    seeded = {
                        "id": int(hollow["id"]),
                        "ts": str(hollow["ts"] or ""),
                        "model": str(hollow["model"] or name),
                        "net_liquidation": nl,
                        "session_date": day,
                    }
                else:
                    conn.execute(
                        """
                        INSERT INTO session_markers (ts, model, net_liquidation)
                        VALUES (?, ?, ?)
                        """,
                        (stamp, name, nl),
                    )
                    conn.commit()
                    seeded = {
                        "ts": stamp,
                        "model": name,
                        "net_liquidation": nl,
                        "session_date": day,
                    }
        except Exception:
            logger.exception("journal.ensure_session_start_nl session_markers failed")
            return None
        try:
            existing_nl, _existing_ts = self.first_nl_on_et_day(day)
            if existing_nl is None:
                self.record_snapshot(
                    account={"NetLiquidation": nl},
                    ts=stamp,
                )
        except Exception:
            logger.exception("journal.ensure_session_start_nl snapshot seed failed")
        return seeded

    def closed_fill_stats_since(self, since_iso: str) -> dict:
        """Closed-fill stats for tickets this desk dispatched.

        Fills whose order_id is missing or not in a dispatch result are
        leftover TWS / other-client rows — they cannot be tied back to a
        ticket, so they are not this run's ledger.
        """
        empty = {"n": 0, "wins": 0, "sum": 0.0}
        try:
            self._ensure_schema()
            placed = self.dispatched_order_ids()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT order_id, realized_pnl FROM fills
                    WHERE realized_pnl IS NOT NULL
                      AND ABS(realized_pnl) > 1e-9
                      AND ts >= ?
                    """,
                    (_ts_bound(since_iso),),
                ).fetchall()
            n = 0
            wins = 0
            total = 0.0
            for row in rows:
                oid = _coerce_order_id(row["order_id"])
                if oid is None or oid not in placed:
                    continue
                try:
                    pnl = float(row["realized_pnl"])
                except (TypeError, ValueError, KeyError):
                    continue
                n += 1
                total += pnl
                if pnl > 0:
                    wins += 1
            return {"n": n, "wins": wins, "sum": total}
        except Exception:
            logger.exception("journal.closed_fill_stats_since failed")
            return empty

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

    def nav_at_or_before(self, before_iso: str) -> tuple[Optional[float], Optional[str]]:
        """Latest NetLiq at or before ``before_iso``. (None, None) if none."""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ts, net_liquidation FROM snapshots
                    WHERE net_liquidation IS NOT NULL
                      AND net_liquidation > 0
                      AND ts <= ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (_ts_bound(before_iso),),
                ).fetchone()
            if not row or row["net_liquidation"] is None:
                return None, None
            return float(row["net_liquidation"]), str(row["ts"] or "") or None
        except Exception:
            logger.exception("journal.nav_at_or_before failed")
            return None, None

    def nav_at_or_after(self, after_iso: str) -> tuple[Optional[float], Optional[str]]:
        """Earliest NetLiq at or after ``after_iso``. (None, None) if none.

        This is the first observation in a session. ``nav_at_or_before``
        would reach into leftover snapshots from the previous run.
        """
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ts, net_liquidation FROM snapshots
                    WHERE net_liquidation IS NOT NULL
                      AND net_liquidation > 0
                      AND ts >= ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (_ts_bound(after_iso),),
                ).fetchone()
            if not row or row["net_liquidation"] is None:
                return None, None
            return float(row["net_liquidation"]), str(row["ts"] or "") or None
        except Exception:
            logger.exception("journal.nav_at_or_after failed")
            return None, None

    def snapshot_count_since(self, since_iso: str) -> int:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM snapshots WHERE ts >= ?",
                    (_ts_bound(since_iso),),
                ).fetchone()
            return int((row["n"] if row else 0) or 0)
        except Exception:
            logger.exception("journal.snapshot_count_since failed")
            return 0

    def nav_path_since(self, since_iso: str) -> list:
        """NL prints at or after ``since_iso``, oldest first. Empty on miss."""
        out: list = []
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT ts, net_liquidation FROM snapshots
                    WHERE net_liquidation IS NOT NULL
                      AND net_liquidation > 0
                      AND ts >= ?
                    ORDER BY id ASC
                    """,
                    (_ts_bound(since_iso),),
                ).fetchall()
            for row in rows:
                try:
                    out.append((str(row["ts"] or ""), float(row["net_liquidation"])))
                except (TypeError, ValueError, KeyError):
                    continue
            return out
        except Exception:
            logger.exception("journal.nav_path_since failed")
            return []

    def commissions_since(self, since_iso: str) -> float:
        """Sum of abs(commission) on dispatched fills since ``since_iso``.

        Orphan TWS / other-client fills are skipped — same join as
        ``closed_fill_stats_since``.
        """
        try:
            self._ensure_schema()
            placed = self.dispatched_order_ids()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT order_id, commission FROM fills
                    WHERE commission IS NOT NULL
                      AND ts >= ?
                    """,
                    (_ts_bound(since_iso),),
                ).fetchall()
            total = 0.0
            for row in rows:
                oid = _coerce_order_id(row["order_id"])
                if oid is None or oid not in placed:
                    continue
                try:
                    total += abs(float(row["commission"]))
                except (TypeError, ValueError, KeyError):
                    continue
            return total
        except Exception:
            logger.exception("journal.commissions_since failed")
            return 0.0

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

    def first_snapshot(self) -> tuple[Optional[float], Optional[str]]:
        """Oldest NetLiq and its ts."""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT ts, net_liquidation FROM snapshots
                    WHERE net_liquidation IS NOT NULL AND net_liquidation > 0
                    ORDER BY id ASC LIMIT 1
                    """
                ).fetchone()
            if not row or row["net_liquidation"] is None:
                return None, None
            return float(row["net_liquidation"]), str(row["ts"] or "") or None
        except Exception:
            logger.exception("journal.first_snapshot failed")
            return None, None

    def startup_cash(self) -> Optional[float]:
        """First recorded NetLiq — book P&L start and return-% denominator."""
        nl, _ts = self.first_snapshot()
        return nl

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


_journal: Optional[TradeJournal] = None
_journal_lock = threading.Lock()


def get_journal() -> TradeJournal:
    """Module-level singleton accessor (thread-safe lazy init)."""
    global _journal
    with _journal_lock:
        if _journal is None:
            _journal = TradeJournal()
        return _journal


def reset_journal(
    path: Optional[str] = None,
    *,
    enabled: Optional[bool] = None,
) -> TradeJournal:
    """Replace the singleton (for tests)."""
    global _journal
    with _journal_lock:
        kwargs: dict = {}
        if path is not None:
            kwargs["path"] = path
        if enabled is not None:
            kwargs["enabled"] = enabled
        _journal = TradeJournal(**kwargs)
        return _journal
