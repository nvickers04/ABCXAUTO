"""Shared journal helpers — timestamps, JSON, order-id scrape, schema ALTERs."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# journal/util.py sits one level deeper than the old memory/journal.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB_PATH = str(_REPO_ROOT / "journal.db")

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


def _coerce_con_id(value: Any) -> Optional[int]:
    """IBKR ``conId``; 0 is the unset sentinel, not a contract."""
    cid = _coerce_order_id(value)
    if cid is None or cid == 0:
        return None
    return cid


def _attr_first(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    getter = obj.get if isinstance(obj, dict) else lambda k, default=None: getattr(obj, k, default)
    for name in names:
        try:
            val = getter(name)
        except Exception:
            val = None
        if val not in (None, ""):
            return val
    return None


def _opt_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _fill_identity_values(fill: dict) -> dict:
    """Contract identity from a fill dict and optional ``Fill.contract``."""
    contract = fill.get("contract")
    con_id = _attr_first(fill, "con_id", "conId", "contract_id")
    if con_id is None:
        con_id = _attr_first(contract, "conId", "con_id", "contract_id")
    local = _attr_first(fill, "local_symbol", "localSymbol")
    if local is None:
        local = _attr_first(contract, "localSymbol", "local_symbol")
    strike = _attr_first(fill, "strike")
    if strike is None:
        strike = _attr_first(contract, "strike")
    right = _attr_first(fill, "right")
    if right is None:
        right = _attr_first(contract, "right")
    expiry = _attr_first(fill, "expiry", "expiration", "lastTradeDateOrContractMonth")
    if expiry is None:
        expiry = _attr_first(
            contract, "lastTradeDateOrContractMonth", "expiry", "expiration"
        )
    strike_f: Optional[float] = None
    if strike is not None:
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            strike_f = None
    return {
        "con_id": _coerce_con_id(con_id),
        "local_symbol": _opt_text(local),
        "strike": strike_f,
        "right": _opt_text(right),
        "expiry": _opt_text(expiry),
    }


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
        "con_id": _coerce_con_id(_get("con_id")),
        "local_symbol": _opt_text(_get("local_symbol")),
        "strike": _get("strike"),
        "right": _opt_text(_get("right")),
        "expiry": _opt_text(_get("expiry")),
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
