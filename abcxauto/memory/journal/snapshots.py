"""Net-liq snapshots, session markers, and account-return windows."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from abcxauto.memory.journal.util import (
    _account_float,
    _et_calendar_date,
    _et_day_utc_range,
    _json_dumps,
    _row_ts,
    _ts_bound,
)

logger = logging.getLogger(__name__)


class SnapshotsMixin:
    """Net-liq snapshots, session markers, and account-return windows."""

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
                    logger.exception(
                        "journal.record_snapshot session stamp failed "
                        "table=session_markers"
                    )
        except Exception:
            logger.exception("journal.record_snapshot failed table=snapshots")
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
                logger.exception(
                    "journal.ensure_model_session session-start NL failed "
                    "table=session_markers"
                )
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
            logger.exception(
                "journal.ensure_model_session failed table=session_markers"
            )
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
            logger.exception(
                "journal.ensure_session_start_nl session_markers failed "
                "table=session_markers"
            )
            return None
        try:
            existing_nl, _existing_ts = self.first_nl_on_et_day(day)
            if existing_nl is None:
                self.record_snapshot(
                    account={"NetLiquidation": nl},
                    ts=stamp,
                )
        except Exception:
            logger.exception(
                "journal.ensure_session_start_nl snapshot seed failed table=snapshots"
            )
        return seeded
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
