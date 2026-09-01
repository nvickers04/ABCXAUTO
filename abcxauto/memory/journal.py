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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = str(_REPO_ROOT / "journal.db")

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
    quote_reason TEXT
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

_UNFILLED_GRACE_S = 15.0


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc_iso(value: Any) -> Optional[str]:
    """Canonicalise a caller's timestamp to ``...Z`` UTC.

    Rows are compared and bucketed by day as plain strings, so an offset-bearing
    or bare-digit stamp from the broker layer has to be converted before it is
    stored, not after. Every writer here means UTC, so bare digits are labelled
    rather than shifted. An unparseable value is stored untouched — losing the
    operator's stamp is worse than keeping an odd one.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _row_ts(value: Any = None) -> str:
    """Canonical UTC stamp for a journal row. Unparseable caller text is kept."""
    return _utc_iso(value) or _utc_now_iso()


def _ts_bound(value: Any) -> str:
    """Canonical UTC bound for string compares against stored ``ts`` values."""
    return _utc_iso(value) or str(value)


def _et_calendar_date(value: Any = None) -> Optional[str]:
    """America/New_York calendar date. IBKR DailyPnL resets on this day."""
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text[:10] if len(text) >= 10 else None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return dt.astimezone(timezone.utc).date().isoformat()


def _et_day_utc_range(session_date: str) -> Optional[Tuple[str, str]]:
    """UTC [start, end) for an America/New_York calendar date ``YYYY-MM-DD``."""
    text = str(session_date or "").strip()
    if len(text) < 10:
        return None
    try:
        y, m, d = int(text[0:4]), int(text[5:7]), int(text[8:10])
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        start = datetime(y, m, d, 0, 0, 0, tzinfo=et)
        end = start + timedelta(days=1)
    except Exception:
        return None
    lo = _utc_iso(start)
    hi = _utc_iso(end)
    if not lo or not hi:
        return None
    return lo, hi


# ib_insync can hand over TWS UTC digits as local time. The fill then sits one
# US offset in the future — 4h EDT / 5h CDT in summer, up to 8h PT.
_FILL_TZ_OFFSETS_H = (4, 5, 6, 7, 8)
_FILL_TZ_SLACK = timedelta(minutes=20)
_DISPATCH_GRACE = timedelta(seconds=90)


def _parse_ts(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _align_fill_ts_to_dispatch(fill_ts: str, dispatch_ts: Optional[str]) -> str:
    """Keep a fill on the same UTC clock as the ticket that caused it.

    A +5h CDT shift of 20:13Z becomes 01:13Z the next UTC day — daily and
    session rows keyed on fill ts then belong to the wrong day. Only a
    whole-hour US offset near the dispatch is rewritten; a later real stop
    is left alone.
    """
    if not dispatch_ts:
        return fill_ts
    fill_dt = _parse_ts(fill_ts)
    disp_dt = _parse_ts(dispatch_ts)
    if fill_dt is None or disp_dt is None:
        return fill_ts
    delta = fill_dt - disp_dt
    if delta <= _FILL_TZ_SLACK:
        return fill_ts
    for hours in _FILL_TZ_OFFSETS_H:
        target = timedelta(hours=hours)
        if abs(delta - target) <= _FILL_TZ_SLACK:
            shifted = fill_dt - target
            if shifted + _DISPATCH_GRACE >= disp_dt:
                return _utc_iso(shifted) or fill_ts
    return fill_ts


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _account_float(account: dict, *keys: str) -> Optional[float]:
    """Extract a float from account dict, trying exact then case-insensitive keys."""
    for key in keys:
        if key in account and account[key] is not None:
            try:
                return float(account[key])
            except (TypeError, ValueError):
                continue
        lower = key.lower()
        if lower in account and account[lower] is not None:
            try:
                return float(account[lower])
            except (TypeError, ValueError):
                continue
        # Case-insensitive scan of all keys (e.g. NetLiquidation vs netliquidation).
        for ak, av in account.items():
            if str(ak).lower() == lower and av is not None:
                try:
                    return float(av)
                except (TypeError, ValueError):
                    break
    return None


# Order-id keys seen in broker/orders.py and options gateway result dicts.
_ORDER_ID_KEYS = (
    "order_id",
    "orderId",
    "bracket_order_id",
    "entry_order_id",
    "stop_order_id",
    "target_order_id",
)
_ORDER_IDS_LIST_KEYS = ("order_ids", "orderIds")


def _coerce_order_id(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sql_fill_dict(row: sqlite3.Row) -> dict:
    keys = set(row.keys())

    def _get(name: str, default: Any = None) -> Any:
        return row[name] if name in keys else default

    pnl = _get("realized_pnl")
    try:
        pnl_f = float(pnl) if pnl is not None else None
    except (TypeError, ValueError):
        pnl_f = None
    return {
        "ts": _get("ts"),
        "exec_id": _get("exec_id"),
        "order_id": _coerce_order_id(_get("order_id")),
        "symbol": str(_get("symbol") or "").upper(),
        "sec_type": _get("sec_type"),
        "side": _get("side"),
        "quantity": _get("quantity"),
        "price": _get("price"),
        "commission": _get("commission"),
        "realized_pnl": pnl_f,
        "ibkr_last": _get("ibkr_last"),
        "bid": _get("bid"),
        "ask": _get("ask"),
        "fill_label": _get("fill_label"),
        "quote_reason": _get("quote_reason"),
    }


def _collect_order_ids(obj: Any, out: set) -> None:
    """Recursively collect integer order ids from a dispatch result dict/list."""
    if isinstance(obj, dict):
        for key in _ORDER_ID_KEYS:
            if key in obj:
                oid = _coerce_order_id(obj.get(key))
                if oid is not None:
                    out.add(oid)
        for key in _ORDER_IDS_LIST_KEYS:
            raw = obj.get(key)
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    oid = _coerce_order_id(item)
                    if oid is not None:
                        out.add(oid)
            else:
                oid = _coerce_order_id(raw)
                if oid is not None:
                    out.add(oid)
        for key, value in obj.items():
            if key in ("send_marks", "marks_json"):
                continue
            if isinstance(value, (dict, list, tuple)):
                _collect_order_ids(value, out)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _collect_order_ids(item, out)


def _order_ids_from_result_json(raw: Any) -> set:
    if not raw:
        return set()
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    found: set = set()
    _collect_order_ids(parsed, found)
    return found


def _table_cols(conn: sqlite3.Connection, table: str) -> set:
    return {
        str(r[1])
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _ensure_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    have = _table_cols(conn, table)
    for name, decl in columns:
        if name in have:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _open_order_id_set(open_orders: Any) -> set:
    out: set = set()
    if not open_orders:
        return out
    if isinstance(open_orders, (set, frozenset)):
        items = open_orders
    elif isinstance(open_orders, (list, tuple)):
        items = open_orders
    else:
        items = [open_orders]
    for item in items:
        if isinstance(item, dict):
            oid = _coerce_order_id(
                item.get("order_id")
                if item.get("order_id") is not None
                else item.get("orderId")
            )
        else:
            oid = _coerce_order_id(item)
        if oid is not None:
            out.add(oid)
    return out


def _patch_dispatch_send_marks(
    conn: sqlite3.Connection, dispatch_id: Optional[int], marks: Any
) -> None:
    if dispatch_id is None or not isinstance(marks, dict):
        return
    row = conn.execute(
        "SELECT result_json FROM dispatches WHERE id = ?",
        (int(dispatch_id),),
    ).fetchone()
    if row is None:
        return
    blob: Any = {}
    raw = row["result_json"] if row["result_json"] is not None else None
    if raw:
        try:
            blob = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            blob = {"raw": raw}
    if not isinstance(blob, dict):
        blob = {"raw": blob}
    from abcxauto.send_marks import public_marks

    blob["send_marks"] = public_marks(marks)
    conn.execute(
        "UPDATE dispatches SET result_json = ? WHERE id = ?",
        (_json_dumps(blob), int(dispatch_id)),
    )


class TradeJournal:
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
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_send_marks_order_id "
                    "ON send_marks(order_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_send_marks_status "
                    "ON send_marks(status)"
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

    # ------------------------------------------------------------------
    # Writers (never raise into the caller)
    # ------------------------------------------------------------------

    def record_proposal(
        self,
        *,
        source: str = "",
        strategy: str = "",
        symbol: str = "",
        direction: str = "",
        quantity: Optional[float] = None,
        params: Any = None,
        validation_ok: Optional[bool] = None,
        validation_reason: str = "",
        ts: Optional[str] = None,
    ) -> Optional[int]:
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            params_json = _json_dumps(params) if params is not None else None
            ok_int = None if validation_ok is None else (1 if validation_ok else 0)
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO proposals (
                        ts, source, strategy, symbol, direction, quantity,
                        params_json, validation_ok, validation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        source,
                        strategy,
                        symbol,
                        direction,
                        quantity,
                        params_json,
                        ok_int,
                        validation_reason or None,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_proposal failed")
            return None

    def record_gate_decision(
        self,
        proposal_id: Optional[int],
        allowed: bool,
        reason: str = "",
        *,
        ts: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO gate_decisions (ts, proposal_id, allowed, reason)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        proposal_id,
                        1 if allowed else 0,
                        reason or None,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.record_gate_decision failed")

    def record_dispatch(
        self,
        proposal_id: Optional[int],
        ok: bool,
        result: Any = None,
        *,
        ts: Optional[str] = None,
    ) -> Optional[int]:
        if not self.enabled:
            return None
        try:
            self._ensure_schema()
            result_json = _json_dumps(result) if result is not None else None
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO dispatches (ts, proposal_id, ok, result_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        _row_ts(ts),
                        proposal_id,
                        1 if ok else 0,
                        result_json,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except Exception:
            logger.exception("journal.record_dispatch failed")
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
            logger.exception("journal.record_send_marks failed")
            return None

    def record_halt(
        self,
        reason: str,
        kind: str = "halt",
        *,
        ts: Optional[str] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO halts (ts, reason, kind)
                    VALUES (?, ?, ?)
                    """,
                    (_row_ts(ts), reason or None, kind or "halt"),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.record_halt failed")

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
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO fills (
                                ts, exec_id, order_id, symbol, sec_type, side,
                                quantity, price, commission, realized_pnl,
                                ibkr_last, bid, ask, sent_price,
                                signed_slippage, spread_paid, fill_label,
                                quote_reason
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def ingest_look(self, snap: Optional[dict] = None) -> dict:
        """Persist this look's book on the existing journal. Same writer as monitor.

        Snapshot + fills + missed-send resolve. Not a second ledger.
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
        return {"fills_inserted": int(inserted or 0), "sends_resolved": int(resolved or 0)}

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
            logger.exception("journal.record_decision failed")
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
            logger.exception("journal.set_working_thesis failed")

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
            logger.exception("journal.record_judgment failed")
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

    def recent_dispatches(self, limit: int = 50) -> List[dict]:
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT d.id, d.ts, d.proposal_id, d.ok, d.result_json,
                           sm.ibkr_last, sm.bid, sm.ask, sm.sent_price,
                           sm.fill_price, sm.signed_slippage, sm.spread_paid,
                           sm.fill_label, sm.status AS fill_status
                    FROM dispatches d
                    LEFT JOIN send_marks sm ON sm.dispatch_id = d.id
                    ORDER BY d.id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                item = dict(row)
                raw = item.get("result_json")
                if raw:
                    try:
                        item["result"] = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["result"] = raw
                else:
                    item["result"] = None
                out.append(item)
            return out
        except Exception:
            logger.exception("journal.recent_dispatches failed")
            return []

    def recent_proposals(self, limit: int = 20) -> List[dict]:
        """Recent proposals including validation failures (for agent learning)."""
        try:
            self._ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, ts, source, strategy, symbol, direction, quantity,
                           params_json, validation_ok, validation_reason
                    FROM proposals
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            out: List[dict] = []
            for row in rows:
                item = dict(row)
                raw = item.get("params_json")
                if raw:
                    try:
                        item["params"] = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["params"] = raw
                else:
                    item["params"] = None
                ok = item.get("validation_ok")
                item["validation_ok"] = None if ok is None else bool(ok)
                out.append(item)
            return out
        except Exception:
            logger.exception("journal.recent_proposals failed")
            return []

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
            logger.exception("journal.record_self_tune failed")
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
