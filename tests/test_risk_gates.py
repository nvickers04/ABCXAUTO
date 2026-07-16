"""RiskGate pre-trade checks: halt, sizing, limits, fail-closed, disabled knobs."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from abcxauto.config import Config, get_config
from abcxauto.proposals import validate_proposal
from abcxauto.risk_gates import (
    estimate_bracket_risk_dollars,
    estimate_notional,
    is_exit_or_management,
    reset_risk_gate,
)
from tests.test_proposals import RATIONALE, VALID_PAYLOADS


def _cfg(**overrides) -> Config:
    base = get_config()
    # Explicit salvage/capital knobs for unit tests (production defaults are off).
    defaults = {
        "cash_only": False,
        "max_peak_drawdown_pct": 0.0,
        "max_option_premium_pct": 0.0,
        "max_risk_per_trade_pct": 0.0,
        "defined_risk_only": False,
        "min_reward_risk": 0.0,
    }
    data = {**base.__dict__, **defaults, **overrides}
    return Config(**data)


class FakeConnector:
    def __init__(self, account=None, positions=None, account_error=None):
        self.account = account if account is not None else {
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 100_000.0,
        }
        self.positions = positions if positions is not None else []
        self.account_error = account_error
        self.flatten_calls = 0

    async def get_account_summary(self):
        if self.account_error is not None:
            if isinstance(self.account_error, Exception):
                raise self.account_error
            return {"error": self.account_error}
        return self.account

    async def get_positions(self):
        return self.positions

    async def flatten_all(self):
        self.flatten_calls += 1
        return {"success": True, "positions_closed": len(self.positions)}


@pytest.fixture
def gate(monkeypatch):
    g = reset_risk_gate()
    cfg = _cfg(
        risk_gates_enabled=True,
        daily_loss_limit_pct=2.0,
        max_position_pct=10.0,
        max_open_positions=5,
        max_daily_trades=20,
        auto_panic_on_breach=True,
    )
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    return g


def _bracket(qty=10, entry=100.0, stop=None, target=None, direction="LONG", symbol="NVDA"):
    stop = stop if stop is not None else (entry * 0.95 if direction == "LONG" else entry * 1.05)
    target = target if target is not None else (entry * 1.10 if direction == "LONG" else entry * 0.90)
    return validate_proposal(
        "bracket",
        {
            "symbol": symbol,
            "quantity": qty,
            "direction": direction,
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
        },
        RATIONALE,
    )


def _market_order_exit():
    return validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)


def _oca():
    return validate_proposal("oca", VALID_PAYLOADS["oca"], RATIONALE)


@pytest.mark.asyncio
async def test_halt_blocks_entries_not_exits(gate):
    gate.halt("manual kill")
    conn = FakeConnector()

    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "halted" in reason.lower() or "manual kill" in reason

    ok, reason = await gate.pre_trade_check(_market_order_exit(), conn)
    assert ok is True
    assert "bypass" in reason

    ok, reason = await gate.pre_trade_check(_oca(), conn)
    assert ok is True

    close_opt = validate_proposal("close_option", VALID_PAYLOADS["close_option"], RATIONALE)
    ok, _ = await gate.pre_trade_check(close_opt, conn)
    assert ok is True

    gate.resume()
    ok, _ = await gate.pre_trade_check(_bracket(), conn)
    assert ok is True


@pytest.mark.asyncio
async def test_daily_loss_breach_trips_halt(gate):
    # 2% of 100k = 2000; daily pnl -2500 trips
    conn = FakeConnector(account={"netliquidation": 100_000.0, "dailypnl": -2500.0})
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "circuit breaker" in reason.lower() or "daily loss" in reason.lower()
    assert gate.is_halted is True

    # Subsequent entry still blocked via latch even if PnL recovers in account
    conn.account = {"netliquidation": 100_000.0, "dailypnl": 0.0}
    ok, _ = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False


@pytest.mark.asyncio
async def test_position_sizing_rejection(gate):
    # 10% of 100k = 10k; 200 shares * 100 = 20k > 10k
    conn = FakeConnector()
    ok, reason = await gate.pre_trade_check(_bracket(qty=200, entry=100.0), conn)
    assert ok is False
    assert "position size" in reason.lower() or "exceeds max" in reason.lower()

    ok, _ = await gate.pre_trade_check(_bracket(qty=10, entry=100.0), conn)
    assert ok is True


@pytest.mark.asyncio
async def test_max_open_positions_rejection(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(max_open_positions=2, max_position_pct=0, daily_loss_limit_pct=0),
    )
    positions = [
        {"symbol": "AAPL", "quantity": 10, "sec_type": "STK"},
        {"symbol": "MSFT", "quantity": 5, "sec_type": "STK"},
    ]
    conn = FakeConnector(positions=positions)
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "max open positions" in reason.lower()

    conn.positions = [{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}]
    ok, _ = await gate.pre_trade_check(_bracket(), conn)
    assert ok is True


@pytest.mark.asyncio
async def test_max_daily_trades_rejection(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            max_daily_trades=2,
            max_position_pct=0,
            daily_loss_limit_pct=0,
            max_open_positions=0,
        ),
    )
    conn = FakeConnector()
    gate.record_entry()
    gate.record_entry()
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "max daily trades" in reason.lower()


@pytest.mark.asyncio
async def test_fail_closed_on_missing_account_data(gate):
    conn = FakeConnector(account_error="Not connected")
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "fail-closed" in reason.lower()

    conn2 = FakeConnector(account={"dailypnl": 0.0})  # missing NL
    ok, reason = await gate.pre_trade_check(_bracket(), conn2)
    assert ok is False
    assert "fail-closed" in reason.lower()

    conn3 = FakeConnector(account_error=RuntimeError("timeout"))
    ok, reason = await gate.pre_trade_check(_bracket(), conn3)
    assert ok is False
    assert "fail-closed" in reason.lower()


@pytest.mark.asyncio
async def test_missing_dailypnl_treated_as_flat(gate):
    """IBKR often omits DailyPnL early session — do not block entries."""
    conn = FakeConnector(account={"netliquidation": 100_000.0, "TotalCashValue": 100_000.0})
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is True, reason


@pytest.mark.asyncio
async def test_disabled_knobs_skip_rules(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            daily_loss_limit_pct=0,
            max_position_pct=0,
            max_open_positions=0,
            max_daily_trades=0,
            cash_only=False,
            max_peak_drawdown_pct=0,
            max_option_premium_pct=0,
            max_risk_per_trade_pct=0,
        ),
    )
    # Would trip every rule if enabled
    positions = [{"symbol": f"S{i}", "quantity": 1, "sec_type": "STK"} for i in range(20)]
    conn = FakeConnector(
        account={"netliquidation": 100_000.0, "dailypnl": -50_000.0},
        positions=positions,
    )
    for _ in range(50):
        gate.record_entry()

    ok, reason = await gate.pre_trade_check(_bracket(qty=10_000, entry=100.0), conn)
    assert ok is True, reason


@pytest.mark.asyncio
async def test_executor_records_entry_on_success(monkeypatch):
    from abcxauto.executor import execute_proposal

    gate = reset_risk_gate()
    cfg = _cfg(
        risk_gates_enabled=True,
        max_position_pct=50.0,
        daily_loss_limit_pct=0,
        max_open_positions=0,
        max_daily_trades=20,
    )
    monkeypatch.setattr("abcxauto.executor.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)

    class GW(FakeConnector):
        async def place_bracket_order(self, **kwargs):
            return {"success": True, "order_id": 1}

    proposal = _bracket()
    result = await execute_proposal(proposal, GW())
    assert result["success"] is True
    assert gate.daily_trade_count() == 1


@pytest.mark.asyncio
async def test_executor_rejects_with_error_dict(monkeypatch):
    from abcxauto.executor import execute_proposal

    gate = reset_risk_gate()
    gate.halt("test halt")
    cfg = _cfg(risk_gates_enabled=True)
    monkeypatch.setattr("abcxauto.executor.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)

    class GW(FakeConnector):
        async def place_bracket_order(self, **kwargs):
            return {"success": True}

    result = await execute_proposal(_bracket(), GW())
    assert "error" in result
    assert result.get("status") == "rejected"


@pytest.mark.asyncio
async def test_monitor_auto_panic_once(monkeypatch):
    from abcxauto.monitor import PortfolioMonitor

    gate = reset_risk_gate()
    injections = []

    class Session:
        def emit(self, *_a, **_k):
            pass

        async def inject(self, message, source=""):
            injections.append(message)

    cfg = _cfg(
        auto_panic_on_breach=True,
        daily_loss_limit_pct=2.0,
        monitor_poll_s=30,
        monitor_review_s=300,
    )
    monkeypatch.setattr("abcxauto.monitor.get_config", lambda: cfg)

    conn = FakeConnector(
        account={"netliquidation": 100_000.0, "dailypnl": -3000.0},
        positions=[{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}],
    )
    mon = PortfolioMonitor(Session(), conn)
    mon.cfg = cfg

    snap = {
        "account": conn.account,
        "protection": {"unprotected_symbols": [], "positions": conn.positions},
    }
    await mon._maybe_auto_panic(snap)
    assert gate.is_halted is True
    assert conn.flatten_calls == 1
    assert any("AUTO-PANIC" in m for m in injections)

    # Second tick must not flatten again (halt latch guard)
    await mon._maybe_auto_panic(snap)
    assert conn.flatten_calls == 1


def test_estimate_notional_bracket(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: _cfg(),
    )
    bracket = _bracket(qty=10, entry=50.0)
    assert estimate_notional(bracket) == 500.0

    assert is_exit_or_management(_market_order_exit()) is True
    assert is_exit_or_management(_bracket()) is False
    assert is_exit_or_management(_oca()) is True


def _market_bracket(qty=10, stop=95.0, target=110.0, direction="LONG", price_hint=None):
    params = {
        "symbol": "NVDA",
        "quantity": qty,
        "direction": direction,
        "stop_price": stop,
        "target_price": target,
    }
    if price_hint is not None:
        params["price_hint"] = price_hint
    return validate_proposal("market_bracket", params, RATIONALE)


def test_market_bracket_conservative_sizing_without_hint(monkeypatch):
    """Without price_hint: notional = max(stop,target)*qty; risk = |target-stop|*qty."""
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())
    mb = _market_bracket(qty=10, stop=95.0, target=110.0)
    # Conservative notional uses max(95, 110) = 110, NOT midpoint 102.5
    assert estimate_notional(mb) == pytest.approx(110.0 * 10)
    # Risk assumes fill at target extreme → full |110-95| = 15 per share
    assert estimate_bracket_risk_dollars(mb) == pytest.approx(10 * 15.0)


def test_market_bracket_sizing_prefers_price_hint(monkeypatch):
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())
    mb = _market_bracket(qty=10, stop=95.0, target=110.0, price_hint=100.0)
    assert estimate_notional(mb) == pytest.approx(100.0 * 10)
    assert estimate_bracket_risk_dollars(mb) == pytest.approx(10 * abs(100.0 - 95.0))


def test_kind_aware_auto_reset_daily_loss_only(gate):
    yesterday = date.today() - timedelta(days=1)

    # daily_loss from a prior day auto-clears
    gate.halt("daily loss breach", kind="daily_loss")
    gate._halt_date = yesterday
    assert gate.is_halted is False
    assert gate.halt_reason == ""
    assert gate.halt_kind == ""

    # disconnect persists across midnight
    gate.halt("broker disconnected >120s", kind="disconnect")
    gate._halt_date = yesterday
    assert gate.is_halted is True
    assert gate.halt_kind == "disconnect"

    gate.resume()

    # auto_panic persists
    gate.halt("AUTO-PANIC", kind="auto_panic")
    gate._halt_date = yesterday
    assert gate.is_halted is True
    assert gate.halt_kind == "auto_panic"

    gate.resume()

    # default/manual halt persists
    gate.halt("manual kill", kind="halt")
    gate._halt_date = yesterday
    assert gate.is_halted is True
    assert gate.halt_kind == "halt"


@pytest.mark.asyncio
async def test_daily_loss_breaker_uses_daily_loss_kind(gate):
    conn = FakeConnector(account={"netliquidation": 100_000.0, "dailypnl": -2500.0})
    ok, _ = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert gate.is_halted is True
    assert gate.halt_kind == "daily_loss"


# ---------------------------------------------------------------------------
# Sprint 2 salvage gates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cash_only_rejects_short_bracket(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            cash_only=True,
            max_position_pct=0,
            daily_loss_limit_pct=0,
            max_open_positions=0,
            max_daily_trades=0,
        ),
    )
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())
    conn = FakeConnector(account={
        "netliquidation": 100_000.0, "dailypnl": 0.0, "TotalCashValue": 50_000.0,
    })
    ok, reason = await gate.pre_trade_check(
        _bracket(direction="SHORT", entry=100.0, stop=105.0, target=90.0), conn
    )
    assert ok is False
    assert "cash-only" in reason.lower() or "short" in reason.lower()


@pytest.mark.asyncio
async def test_cash_only_rejects_notional_over_cash(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            cash_only=True,
            max_position_pct=0,
            daily_loss_limit_pct=0,
            max_open_positions=0,
            max_daily_trades=0,
        ),
    )
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())
    # 200 * 100 = 20k notional > 5k cash
    conn = FakeConnector(account={
        "netliquidation": 100_000.0, "dailypnl": 0.0, "TotalCashValue": 5_000.0,
    })
    ok, reason = await gate.pre_trade_check(_bracket(qty=200, entry=100.0), conn)
    assert ok is False
    assert "cash" in reason.lower()

    conn.account["TotalCashValue"] = 50_000.0
    ok, _ = await gate.pre_trade_check(_bracket(qty=200, entry=100.0), conn)
    assert ok is True


@pytest.mark.asyncio
async def test_peak_drawdown_rejects_and_self_clears(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            max_peak_drawdown_pct=8.0,
            max_position_pct=0,
            daily_loss_limit_pct=0,
            max_open_positions=0,
            max_daily_trades=0,
            cash_only=False,
        ),
    )
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())

    # Establish peak at 100k
    gate.update_equity(100_000.0)
    assert gate.peak_equity == 100_000.0

    # 10% drawdown > 8% floor
    conn = FakeConnector(account={"netliquidation": 90_000.0, "dailypnl": 0.0})
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "drawdown" in reason.lower()
    assert gate.is_halted is False  # must NOT trip permanent halt

    # Recovery above floor (floor = 92k) — self-clears
    conn.account = {"netliquidation": 93_000.0, "dailypnl": 0.0}
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is True, reason
    assert gate.is_halted is False


@pytest.mark.asyncio
async def test_risk_per_trade_cap(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            max_risk_per_trade_pct=1.0,
            max_position_pct=0,
            daily_loss_limit_pct=0,
            max_open_positions=0,
            max_daily_trades=0,
            cash_only=False,
        ),
    )
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())
    conn = FakeConnector(account={"netliquidation": 100_000.0, "dailypnl": 0.0})

    # risk = 100 * |100-95| = 500; 1% of 100k = 1000 — ok
    ok, _ = await gate.pre_trade_check(_bracket(qty=100, entry=100.0, stop=95.0, target=110.0), conn)
    assert ok is True

    # risk = 300 * 5 = 1500 > 1000
    ok, reason = await gate.pre_trade_check(
        _bracket(qty=300, entry=100.0, stop=95.0, target=110.0), conn
    )
    assert ok is False
    assert "risk-per-trade" in reason.lower() or "risk" in reason.lower()


@pytest.mark.asyncio
async def test_salvage_knobs_disabled(gate, monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            cash_only=False,
            max_peak_drawdown_pct=0,
            max_option_premium_pct=0,
            max_risk_per_trade_pct=0,
            max_position_pct=0,
            daily_loss_limit_pct=0,
            max_open_positions=0,
            max_daily_trades=0,
        ),
    )
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: _cfg())
    gate.update_equity(100_000.0)
    conn = FakeConnector(
        account={
            "netliquidation": 50_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 100.0,
        },
        positions=[
            {"symbol": "SPY", "quantity": 1, "sec_type": "OPT", "market_value": 40_000.0},
        ],
    )
    # Huge size / underwater equity — would fail if knobs were on
    ok, reason = await gate.pre_trade_check(
        _bracket(qty=500, entry=100.0, stop=95.0, target=110.0, direction="LONG"),
        conn,
    )
    assert ok is True, reason
