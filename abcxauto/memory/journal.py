"""SQLite trade journal — durable record of proposals, gates, dispatches, halts,
snapshots, and fills.

Uses stdlib sqlite3 only. Journaling must never break trading: all public write/read
methods catch and log internally.

Table work lives in sibling modules. This file is the facade callers already
import (``TradeJournal``, ``get_journal``, helpers).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from abcxauto.memory.analytics import JournalAnalytics
from abcxauto.memory.fills import JournalFills
from abcxauto.memory.journal_support import (  # noqa: F401 — facade re-export
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
    _fill_multiplier,
    _json_dumps,
    _open_order_id_set,
    _order_ids_from_result_json,
    _parse_ts,
    _patch_dispatch_send_marks,
    _row_ts,
    _sql_fill_dict,
    _split_gate_stage,
    _sqlite_locked,
    _table_cols,
    _ts_bound,
    _utc_iso,
    _utc_now_iso,
)
from abcxauto.memory.notes import JournalNotes
from abcxauto.memory.pcs import JournalPcs
from abcxauto.memory.schema import JournalSchema, _FILL_MARK_COLS, _SCHEMA_SQL
from abcxauto.memory.trades import JournalTrades

logger = logging.getLogger(__name__)


class TradeJournal(JournalSchema, JournalTrades, JournalFills, JournalAnalytics, JournalPcs, JournalNotes):
    """Thread-safe SQLite trade journal (one connection per call, WAL mode)."""


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
