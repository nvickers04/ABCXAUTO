"""Disconnect halt auto-resumes only after reconnect + complete book."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from abcxauto.broker.connector import IBKRConnector
from abcxauto.memory import get_journal
from abcxauto.risk_gates import halt_state_path, reset_risk_gate
from abcxauto.self_tune import apply_self_tune
from tests.test_risk_gates import FakeConnector, _bracket, _cfg, _market_order_exit


OLD_REASON = "broker disconnected >120s"
OLD_TS = datetime.now(timezone.utc) - timedelta(days=21)


def _seed_journal_halt(kind: str, reason: str, *, ts: datetime | None = None) -> None:
    when = ts or OLD_TS
    stamp = when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    get_journal().record_halt(reason, kind, ts=stamp)
    path = halt_state_path()
    if path.is_file():
        path.unlink()


def _journal_kinds() -> list[tuple[str, str]]:
    path = get_journal().path
    with sqlite3.connect(str(path)) as conn:
        rows = conn.execute("SELECT kind, reason FROM halts ORDER BY id").fetchall()
    return [(str(k), str(r or "")) for k, r in rows]


def _connector(*, connected: bool = True, refresh_ok: bool = True) -> IBKRConnector:
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._connected = connected
    conn._ibkr_data_stale = True
    conn._disconnect_cause = "tws_restart"
    conn._reconnect_requested = True
    conn._heartbeat_failures = 2
    conn._reconnect_attempt = 3
    conn._disconnect_since = 1.0
    conn._disconnect_halt_fired = True
    conn._last_heartbeat_ok = 0.0
    conn._pending_resubscribe = set()
    conn._quote_cache = {}
    conn.net_liquidation = 100_000.0 if refresh_ok else 0.0
    ib = MagicMock()
    ib.isConnected = lambda: connected
    conn.ib = ib

    async def _refresh() -> bool:
        return bool(refresh_ok)

    conn._refresh_book_after_data_loss = _refresh
    return conn


def test_stale_disconnect_restore_is_loud_even_when_it_stays(caplog):
    _seed_journal_halt("disconnect", OLD_REASON)
    with caplog.at_level(logging.CRITICAL, logger="abcxauto.risk_gates"):
        gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    assert gate.halt_kind == "disconnect"
    assert OLD_REASON in gate.halt_reason
    text = caplog.text
    assert "RISK GATE RESTORED" in text
    assert "disconnect" in text
    assert OLD_REASON in text
    assert "21-day" in text


@pytest.mark.asyncio
async def test_stale_disconnect_clears_on_reconnect_and_complete_book(caplog):
    _seed_journal_halt("disconnect", OLD_REASON)
    with caplog.at_level(logging.WARNING, logger="abcxauto.risk_gates"):
        gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    assert gate.halt_kind == "disconnect"

    conn = _connector(connected=True, refresh_ok=True)
    with caplog.at_level(logging.WARNING, logger="abcxauto.risk_gates"):
        await conn._after_connect_restore()

    assert gate.is_halted is False
    assert gate.halt_kind == ""
    kinds = _journal_kinds()
    assert kinds[-1][0] == "resume"
    text = caplog.text
    assert "disconnect" in text
    assert OLD_REASON in text
    assert "21-day" in text
    assert "AUTO-RESUMED" in text or "auto-resume" in text.lower()


@pytest.mark.asyncio
async def test_stale_disconnect_stays_when_book_incomplete():
    _seed_journal_halt("disconnect", OLD_REASON)
    gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    conn = _connector(connected=True, refresh_ok=False)
    await conn._after_connect_restore()
    assert gate.is_halted is True
    assert gate.halt_kind == "disconnect"
    assert all(kind != "resume" for kind, _ in _journal_kinds())


@pytest.mark.asyncio
async def test_stale_disconnect_stays_when_broker_disconnected():
    _seed_journal_halt("disconnect", OLD_REASON)
    gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    conn = _connector(connected=False, refresh_ok=True)
    await conn._after_connect_restore()
    assert gate.is_halted is True
    assert gate.halt_kind == "disconnect"
    assert all(kind != "resume" for kind, _ in _journal_kinds())


@pytest.mark.asyncio
async def test_daily_loss_survives_restart_and_reconnect_complete_book():
    """daily_loss must latch across restart; reconnect + book must not clear it."""
    _seed_journal_halt("daily_loss", "daily_loss -2.5", ts=datetime.now(timezone.utc))
    gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    assert gate.halt_kind == "daily_loss"
    conn = _connector(connected=True, refresh_ok=True)
    await conn._after_connect_restore()
    assert gate.is_halted is True
    assert gate.halt_kind == "daily_loss"
    assert all(kind != "resume" for kind, _ in _journal_kinds())


@pytest.mark.asyncio
async def test_manual_halt_stays_until_operator_resume():
    _seed_journal_halt("halt", "manual halt from console")
    gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    assert gate.halt_kind == "halt"
    conn = _connector(connected=True, refresh_ok=True)
    await conn._after_connect_restore()
    assert gate.is_halted is True
    assert gate.halt_kind == "halt"
    gate.resume()
    assert gate.is_halted is False
    assert _journal_kinds()[-1][0] == "resume"


@pytest.mark.asyncio
async def test_auto_panic_and_unknown_kinds_do_not_auto_resume():
    for kind, reason in (
        ("auto_panic", "AUTO-PANIC"),
        ("max_peak_drawdown", "peak drawdown"),
        ("breaker", "mystery breaker"),
    ):
        _seed_journal_halt(kind, reason)
        gate = reset_risk_gate(restore=True)
        assert gate.is_halted is True
        assert gate.halt_kind == kind
        await _connector(connected=True, refresh_ok=True)._after_connect_restore()
        assert gate.is_halted is True
        assert gate.halt_kind == kind


def test_self_tune_cannot_trigger_disconnect_auto_resume():
    _seed_journal_halt("disconnect", OLD_REASON)
    gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    out = apply_self_tune(
        {
            "resume": True,
            "clear_halt": True,
            "halt": False,
            "maybe_resume_disconnect": True,
            "scan_fetch_cap": 4,
        },
        persist=False,
        rationale="try to talk past the halt",
    )
    assert gate.is_halted is True
    assert gate.halt_kind == "disconnect"
    assert "resume" not in (out.get("applied") or {})
    assert all(kind != "resume" for kind, _ in _journal_kinds())


@pytest.mark.asyncio
async def test_disconnect_halt_still_blocks_new_risk_allows_exits(monkeypatch):
    cfg = _cfg(defined_risk_only=False, cash_only=False, daily_loss_limit_pct=25.0)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    _seed_journal_halt("disconnect", OLD_REASON)
    gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    conn = FakeConnector()
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "halted" in reason.lower() or "disconnected" in reason.lower()
    ok, reason = await gate.pre_trade_check(_market_order_exit(), conn)
    assert ok is True
    assert "bypass" in reason


@pytest.mark.asyncio
async def test_complete_snap_can_clear_stale_disconnect_incomplete_cannot():
    from abcxauto.agent_loop import _reconcile_protection_after_snap

    _seed_journal_halt("disconnect", OLD_REASON)
    gate = reset_risk_gate(restore=True)
    assert gate.is_halted is True
    fake = SimpleNamespace(connected=True)
    await _reconcile_protection_after_snap(
        fake, {"book_unreliable": True, "positions": [], "open_orders": []}
    )
    assert gate.is_halted is True
    await _reconcile_protection_after_snap(
        fake, {"book_unreliable": False, "positions": [], "open_orders": []}
    )
    assert gate.is_halted is False
    assert _journal_kinds()[-1][0] == "resume"


def test_resume_already_journals_a_row():
    gate = reset_risk_gate()
    gate.halt(OLD_REASON, kind="disconnect")
    gate.resume()
    kinds = _journal_kinds()
    assert ("disconnect", OLD_REASON) in kinds
    assert kinds[-1][0] == "resume"
