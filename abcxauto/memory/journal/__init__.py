"""SQLite trade journal — durable record of proposals, gates, dispatches, halts,
snapshots, and fills.

Uses stdlib sqlite3 only. Journaling must never break trading: all public write/read
methods catch and log internally.

Public surface is identical to the former ``memory/journal.py`` module: import
``TradeJournal``, ``get_journal``, ``reset_journal``, and the private helpers
tests/scripts already pin (``_et_calendar_date``, ``_order_ids_from_result_json``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from abcxauto.memory.journal.fills import FillsMixin
from abcxauto.memory.journal.lessons import LessonsMixin
from abcxauto.memory.journal.pcs import PcsMixin
from abcxauto.memory.journal.schema import (
    _FILL_IDENTITY_COLS,
    _FILL_MARK_COLS,
    _SCHEMA_SQL,
)
from abcxauto.memory.journal.self_tune import SelfTuneMixin
from abcxauto.memory.journal.send_marks import SendMarksMixin
from abcxauto.memory.journal.snapshots import SnapshotsMixin
from abcxauto.memory.journal.tickets import TicketsMixin
from abcxauto.memory.journal.usage import UsageMixin
from abcxauto.memory.journal.util import (
    _DEFAULT_DB_PATH,
    _UNFILLED_GRACE_S,
    _account_float,
    _align_fill_ts_to_dispatch,
    _coerce_con_id,
    _coerce_order_id,
    _fill_identity_values,
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
    _table_cols,
    _ts_bound,
    _utc_iso,
    _utc_now_iso,
)

logger = logging.getLogger(__name__)


class TradeJournal(
    FillsMixin,
    TicketsMixin,
    LessonsMixin,
    SnapshotsMixin,
    SendMarksMixin,
    UsageMixin,
    SelfTuneMixin,
    PcsMixin,
):
    """Thread-safe SQLite trade journal (one connection per call, WAL mode)."""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        enabled: Optional[bool] = None,
        timeout: float = 30.0,
    ) -> None:
        if path is None:
            path = os.environ.get("ABCXAUTO_JOURNAL_PATH", _DEFAULT_DB_PATH).strip() or _DEFAULT_DB_PATH
        if enabled is None:
            enabled = _env_bool("ABCXAUTO_JOURNAL_ENABLED", True)
        self.path = str(path)
        self.enabled = bool(enabled)
        self._timeout = float(timeout)
        self._init_lock = threading.Lock()
        self._initialized = False
        # (model, ts) waiting for a real NetLiq. Never persist NL=None.
        self._pending_session: Optional[tuple[str, Optional[str]]] = None
        if self.enabled:
            try:
                self._ensure_schema()
            except Exception:
                logger.exception("journal schema init failed path=%s", self.path)

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            parent = Path(self.path).parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA_SQL)
                cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(model_usage)").fetchall()
                }
                if "cached_tokens" not in cols:
                    conn.execute(
                        "ALTER TABLE model_usage ADD COLUMN cached_tokens INTEGER DEFAULT 0"
                    )
                if "model" not in cols:
                    conn.execute("ALTER TABLE model_usage ADD COLUMN model TEXT")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS session_markers (
                        id INTEGER PRIMARY KEY,
                        ts TEXT NOT NULL,
                        model TEXT,
                        net_liquidation REAL
                    )
                    """
                )
                if "fills" in {
                    str(r[0])
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }:
                    _ensure_columns(conn, "fills", _FILL_MARK_COLS)
                    _ensure_columns(conn, "fills", _FILL_IDENTITY_COLS)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_send_marks_order_id "
                    "ON send_marks(order_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_send_marks_status "
                    "ON send_marks(status)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pcs_kill_sessions (
                        id INTEGER PRIMARY KEY,
                        ts TEXT NOT NULL,
                        session_id TEXT NOT NULL UNIQUE,
                        session_date TEXT NOT NULL,
                        valid INTEGER NOT NULL,
                        send TEXT,
                        send_credited INTEGER NOT NULL DEFAULT 0,
                        row_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pcs_fill_events (
                        id INTEGER PRIMARY KEY,
                        ts TEXT NOT NULL,
                        event TEXT NOT NULL,
                        lifecycle_id TEXT,
                        dedupe_key TEXT UNIQUE,
                        order_id INTEGER,
                        exec_id TEXT,
                        symbol TEXT,
                        strategy TEXT,
                        card TEXT,
                        side TEXT,
                        quote_source TEXT,
                        bid REAL,
                        ask REAL,
                        mid REAL,
                        half_spread REAL,
                        last REAL,
                        fill_price REAL,
                        credit REAL,
                        debit REAL,
                        lambda_declared REAL,
                        lambda_implied REAL,
                        lambda_stress REAL,
                        c_score REAL,
                        d_score REAL,
                        c_score_stress REAL,
                        d_score_stress REAL,
                        manage_rule TEXT,
                        evidence_valid INTEGER,
                        include_in_pnl_mean INTEGER,
                        payload_json TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pcs_kill_sessions_date "
                    "ON pcs_kill_sessions(session_date)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pcs_fill_events_lifecycle "
                    "ON pcs_fill_events(lifecycle_id, event)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS send_previews (
                        id INTEGER PRIMARY KEY,
                        ts TEXT NOT NULL,
                        preview_id TEXT NOT NULL UNIQUE,
                        preview_hash TEXT NOT NULL,
                        strategy TEXT,
                        symbol TEXT,
                        max_loss REAL,
                        would_refuse_json TEXT,
                        verdict TEXT,
                        token_used INTEGER NOT NULL DEFAULT 0,
                        used_ts TEXT,
                        source TEXT,
                        portfolio_max_loss_usd REAL,
                        portfolio_cap_usd REAL,
                        portfolio_usd_refused INTEGER
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_send_previews_hash "
                    "ON send_previews(preview_hash)"
                )
                _ensure_columns(
                    conn,
                    "proposals",
                    (
                        ("preview_id", "TEXT"),
                        ("preview_hash", "TEXT"),
                        ("would_refuse_json", "TEXT"),
                        ("token_used", "INTEGER"),
                        ("portfolio_max_loss_usd", "REAL"),
                        ("portfolio_cap_usd", "REAL"),
                        ("portfolio_usd_refused", "INTEGER"),
                    ),
                )
                _ensure_columns(
                    conn,
                    "send_previews",
                    (
                        ("portfolio_max_loss_usd", "REAL"),
                        ("portfolio_cap_usd", "REAL"),
                        ("portfolio_usd_refused", "INTEGER"),
                    ),
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.commit()
            self._initialized = True

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=self._timeout)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

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


__all__ = [
    "TradeJournal",
    "get_journal",
    "reset_journal",
]
