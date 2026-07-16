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
    realized_pnl REAL
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
"""


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
        for value in obj.values():
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
                        ts or _utc_now_iso(),
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
                        ts or _utc_now_iso(),
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
    ) -> None:
        if not self.enabled:
            return
        try:
            self._ensure_schema()
            result_json = _json_dumps(result) if result is not None else None
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO dispatches (ts, proposal_id, ok, result_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        ts or _utc_now_iso(),
                        proposal_id,
                        1 if ok else 0,
                        result_json,
                    ),
                )
                conn.commit()
        except Exception:
            logger.exception("journal.record_dispatch failed")

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
                    (ts or _utc_now_iso(), reason or None, kind or "halt"),
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
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO snapshots (
                        ts, net_liquidation, daily_pnl, total_cash,
                        positions_json, open_orders_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts or _utc_now_iso(),
                        net_liq,
                        daily_pnl,
                        total_cash,
                        _json_dumps(positions or []),
                        _json_dumps(open_orders or []),
                    ),
                )
                conn.commit()
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
                for fill in fills or []:
                    if not isinstance(fill, dict):
                        continue
                    exec_id = fill.get("exec_id")
                    if exec_id is None or str(exec_id).strip() == "":
                        continue
                    try:
                        cur = conn.execute(
                            """
                            INSERT OR IGNORE INTO fills (
                                ts, exec_id, order_id, symbol, sec_type, side,
                                quantity, price, commission, realized_pnl
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                fill.get("ts") or _utc_now_iso(),
                                str(exec_id),
                                _coerce_order_id(fill.get("order_id")),
                                fill.get("symbol"),
                                fill.get("sec_type"),
                                fill.get("side"),
                                fill.get("quantity"),
                                fill.get("price"),
                                fill.get("commission"),
                                fill.get("realized_pnl"),
                            ),
                        )
                        inserted += int(cur.rowcount or 0)
                    except Exception:
                        logger.exception(
                            "journal.record_fills row failed exec_id=%s", exec_id
                        )
                conn.commit()
            return inserted
        except Exception:
            logger.exception("journal.record_fills failed")
            return 0

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
        """Record a cycle decision (including hold / blocked outcomes)."""
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
                        ts or _utc_now_iso(),
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
                    (ts or _utc_now_iso(), body[:2000]),
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
        """Persist a Judge-stage record for cross-cycle thesis continuity."""
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
                        ts or _utc_now_iso(),
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
                    SELECT id, ts, proposal_id, ok, result_json
                    FROM dispatches
                    ORDER BY id DESC
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
                    cutoff = (now - timedelta(days=days)).isoformat()
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
