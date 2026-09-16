"""Journal CREATE TABLE script and additive column migrations."""

from __future__ import annotations

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
    reason TEXT
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
    con_id INTEGER,
    local_symbol TEXT,
    strike REAL,
    right TEXT,
    expiry TEXT
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

_FILL_IDENTITY_COLS = (
    ("con_id", "INTEGER"),
    ("local_symbol", "TEXT"),
    ("strike", "REAL"),
    ("right", "TEXT"),
    ("expiry", "TEXT"),
)
