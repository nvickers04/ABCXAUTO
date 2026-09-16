"""Journal schema, migrations, and the WAL connection.

Moved from journal.py. Schema SQL is unchanged.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from abcxauto.memory.journal_support import (
    _DEFAULT_DB_PATH,
    _ensure_columns,
    _env_bool,
    _sqlite_locked,
)

logger = logging.getLogger("abcxauto.memory.journal")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT,
    strategy TEXT,
    symbol TEXT,
    direction TEXT,
    quantity REAL,
    params_json TEXT,
    validation_ok INTEGER,
    validation_reason TEXT
);

CREATE TABLE IF NOT EXISTS gate_decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    proposal_id INTEGER REFERENCES proposals(id),
    allowed INTEGER,
    reason TEXT,
    stage TEXT
);

CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    proposal_id INTEGER REFERENCES proposals(id),
    ok INTEGER,
    result_json TEXT
);

CREATE TABLE IF NOT EXISTS halts (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    reason TEXT,
    kind TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    net_liquidation REAL,
    daily_pnl REAL,
    total_cash REAL,
    positions_json TEXT,
    open_orders_json TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    exec_id TEXT UNIQUE,
    order_id INTEGER,
    symbol TEXT,
    sec_type TEXT,
    side TEXT,
    quantity REAL,
    price REAL,
    commission REAL,
    realized_pnl REAL,
    ibkr_last REAL,
    bid REAL,
    ask REAL,
    sent_price REAL,
    signed_slippage REAL,
    spread_paid REAL,
    fill_label TEXT,
    quote_reason TEXT,
    multiplier REAL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    cycle INTEGER,
    action TEXT,
    strategy TEXT,
    rationale TEXT,
    portfolio_json TEXT,
    outcome_json TEXT
);

CREATE TABLE IF NOT EXISTS working_thesis (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    cycle INTEGER,
    stance TEXT,
    thesis TEXT,
    focus TEXT,
    dismissed TEXT,
    intent_json TEXT,
    judgment_json TEXT
);

CREATE TABLE IF NOT EXISTS model_usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    stage TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    cost_usd REAL
);

CREATE TABLE IF NOT EXISTS session_markers (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    model TEXT,
    net_liquidation REAL
);

CREATE TABLE IF NOT EXISTS self_tunes (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    applied_json TEXT,
    clamped_json TEXT,
    rejected_json TEXT,
    rationale TEXT
);

CREATE TABLE IF NOT EXISTS send_marks (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    proposal_id INTEGER REFERENCES proposals(id),
    dispatch_id INTEGER REFERENCES dispatches(id),
    order_id INTEGER,
    order_ids_json TEXT,
    symbol TEXT,
    strategy TEXT,
    card TEXT,
    side TEXT,
    ibkr_last REAL,
    bid REAL,
    ask REAL,
    mid REAL,
    sent_price REAL,
    fill_price REAL,
    signed_slippage REAL,
    spread_paid REAL,
    fill_label TEXT,
    status TEXT,
    seen_working INTEGER DEFAULT 0,
    marks_json TEXT
);

CREATE TABLE IF NOT EXISTS send_mark_orders (
    order_id INTEGER PRIMARY KEY,
    send_mark_id INTEGER NOT NULL REFERENCES send_marks(id)
);

CREATE TABLE IF NOT EXISTS pcs_kill_sessions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE,
    session_date TEXT NOT NULL,
    valid INTEGER NOT NULL,
    send TEXT,
    send_credited INTEGER NOT NULL DEFAULT 0,
    row_json TEXT NOT NULL
);

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
);

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
    source TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    tags_json TEXT,
    body TEXT NOT NULL,
    evidence TEXT,
    invalidate TEXT,
    expires_at TEXT NOT NULL,
    source TEXT NOT NULL,
    rev INTEGER NOT NULL DEFAULT 1,
    invalidated_at TEXT,
    reason_code TEXT
);
"""

_FILL_MARK_COLS = (
    ("ibkr_last", "REAL"),
    ("bid", "REAL"),
    ("ask", "REAL"),
    ("sent_price", "REAL"),
    ("signed_slippage", "REAL"),
    ("spread_paid", "REAL"),
    ("fill_label", "TEXT"),
    ("quote_reason", "TEXT"),
)


class JournalSchema:
    """Schema init plus one locked WAL connection per call."""

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
        self._io_lock = threading.RLock()
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
                _ensure_columns(conn, "gate_decisions", (("stage", "TEXT"),))
                _ensure_columns(conn, "fills", (("multiplier", "REAL"),))
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS notes (
                        id TEXT PRIMARY KEY,
                        ts TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        symbol TEXT,
                        tags_json TEXT,
                        body TEXT NOT NULL,
                        evidence TEXT,
                        invalidate TEXT,
                        expires_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        rev INTEGER NOT NULL DEFAULT 1,
                        invalidated_at TEXT,
                        reason_code TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_notes_expires_at ON notes(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_notes_source_reason_ts
                        ON notes(source, reason_code, ts);
                    """
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.commit()
            self._initialized = True

    def _open(self) -> sqlite3.Connection:
        """One connection: WAL + busy timeout. Retry open if the file is locked."""
        delay = 0.05
        last: Optional[BaseException] = None
        timeout_ms = max(0, int(self._timeout * 1000))
        for _ in range(8):
            try:
                conn = sqlite3.connect(self.path, timeout=self._timeout)
                conn.row_factory = sqlite3.Row
                conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    pass
                return conn
            except sqlite3.OperationalError as e:
                if not _sqlite_locked(e):
                    raise
                last = e
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
        if last is not None:
            raise last
        raise sqlite3.OperationalError("database is locked")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._io_lock:
            conn = self._open()
            try:
                yield conn
            finally:
                conn.close()
