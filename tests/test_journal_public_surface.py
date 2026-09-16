"""Pin the journal facade import surface after the table-domain split."""

from __future__ import annotations

import inspect

from abcxauto.memory import TradeJournal as MemoryTradeJournal
from abcxauto.memory import get_journal as memory_get_journal
from abcxauto.memory import reset_journal as memory_reset_journal
from abcxauto.memory import journal as journal_mod
from abcxauto.memory import journal_support as support_mod
from abcxauto.memory.journal import (
    TradeJournal,
    _et_calendar_date,
    _order_ids_from_result_json,
    get_journal,
    reset_journal,
)
from abcxauto.memory.schema import JournalSchema, _FILL_MARK_COLS, _SCHEMA_SQL


# Names scripts and tests already import from journal.py.
_FACADE_HELPERS = (
    "_DEFAULT_DB_PATH",
    "_DISPATCH_GRACE",
    "_FILL_MARK_COLS",
    "_FILL_TZ_OFFSETS_H",
    "_FILL_TZ_SLACK",
    "_ORDER_IDS_LIST_KEYS",
    "_ORDER_ID_KEYS",
    "_REPO_ROOT",
    "_SCHEMA_SQL",
    "_UNFILLED_GRACE_S",
    "_account_float",
    "_align_fill_ts_to_dispatch",
    "_coerce_order_id",
    "_collect_order_ids",
    "_ensure_columns",
    "_env_bool",
    "_et_calendar_date",
    "_et_day_utc_range",
    "_json_dumps",
    "_open_order_id_set",
    "_order_ids_from_result_json",
    "_parse_ts",
    "_patch_dispatch_send_marks",
    "_row_ts",
    "_sql_fill_dict",
    "_sqlite_locked",
    "_table_cols",
    "_ts_bound",
    "_utc_iso",
    "_utc_now_iso",
)

# Public TradeJournal methods plus the private helpers other modules call.
_JOURNAL_METHODS = frozenset(
    {
        "account_performance",
        "closed_fill_pnls",
        "closed_fill_stats_since",
        "closing_fills",
        "commissions_since",
        "daily_summary",
        "dispatched_order_ids",
        "ensure_model_session",
        "ensure_session_start_nl",
        "equity_curve",
        "first_nl_on_et_day",
        "first_snapshot",
        "get_send_preview",
        "get_working_thesis",
        "ingest_look",
        "ingest_poll",
        "last_session_marker",
        "list_pcs_kill_sessions",
        "listed_fills",
        "mark_preview_token_used",
        "model_usage_since",
        "model_usage_totals",
        "nav_at_or_after",
        "nav_at_or_before",
        "nav_path_since",
        "pcs_events",
        "pcs_kill_window",
        "pcs_open_lifecycle_id",
        "pcs_open_lifecycles",
        "realized_by_order_id",
        "recent_decisions",
        "recent_dispatches",
        "recent_judgments",
        "recent_proposals",
        "recent_self_tunes",
        "recent_send_marks",
        "record_decision",
        "record_dispatch",
        "record_fills",
        "record_gate_decision",
        "record_halt",
        "record_judgment",
        "record_model_usage",
        "record_pcs_event",
        "record_pcs_kill_session",
        "record_proposal",
        "record_self_tune",
        "record_send_marks",
        "record_send_preview",
        "record_snapshot",
        "resolve_unfilled_sends",
        "send_marks_by_order_id",
        "session_start_marker",
        "set_working_thesis",
        "snapshot_count_since",
        "startup_cash",
        "strategy_diversity",
        "strategy_performance",
    }
)


def test_memory_package_reexports_journal_facade():
    assert MemoryTradeJournal is TradeJournal
    assert memory_get_journal is get_journal
    assert memory_reset_journal is reset_journal


def test_journal_module_keeps_singleton_and_helpers():
    assert journal_mod.TradeJournal is TradeJournal
    assert journal_mod.get_journal is get_journal
    assert journal_mod.reset_journal is reset_journal
    assert hasattr(journal_mod, "_journal")
    assert hasattr(journal_mod, "_journal_lock")
    assert journal_mod._SCHEMA_SQL is _SCHEMA_SQL
    assert journal_mod._FILL_MARK_COLS is _FILL_MARK_COLS
    assert callable(_et_calendar_date)
    assert callable(_order_ids_from_result_json)
    for name in _FACADE_HELPERS:
        assert hasattr(journal_mod, name), name


def test_facade_helpers_are_the_support_and_schema_objects():
    for name in _FACADE_HELPERS:
        if name in ("_SCHEMA_SQL", "_FILL_MARK_COLS"):
            continue
        assert getattr(journal_mod, name) is getattr(support_mod, name), name
    assert issubclass(TradeJournal, JournalSchema)


def test_trade_journal_keeps_public_methods():
    found = {
        name
        for name, obj in inspect.getmembers(TradeJournal, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    # inspect.isfunction misses mixin methods bound from parents on some impls;
    # dir + getattr covers the facade.
    found |= {
        name
        for name in dir(TradeJournal)
        if not name.startswith("_") and callable(getattr(TradeJournal, name, None))
    }
    missing = _JOURNAL_METHODS - found
    assert missing == set(), missing
    extra = found - _JOURNAL_METHODS - {"enabled"}
    # enabled is an instance attr, not a method. Ignore non-methods.
    extras = {name for name in extra if inspect.isfunction(getattr(TradeJournal, name, None))
              or inspect.ismethoddescriptor(getattr(TradeJournal, name, None))}
    # Mixins may expose nothing beyond the pin. Fail if a public method appears
    # that the facade test does not know about — that is an API change.
    unknown = set()
    for name in found:
        if name in _JOURNAL_METHODS:
            continue
        obj = getattr(TradeJournal, name, None)
        if inspect.isfunction(obj) or inspect.ismethod(obj):
            unknown.add(name)
    assert unknown == set(), unknown
    assert callable(TradeJournal.ingest_poll)
    assert callable(TradeJournal._connect)
    assert callable(TradeJournal._ensure_schema)
    assert callable(TradeJournal._open)
