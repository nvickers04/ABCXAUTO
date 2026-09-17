"""Paper peak-DD ceiling 40 / live 25. self_tune stays tighten-only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.config import (
    clamp_risk_knobs,
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
    update_risk_config,
)
from abcxauto.risk_gates import reset_risk_gate
from abcxauto.self_tune import (
    RISK_FLOOR,
    apply_self_tune,
    clamp_risk_to_floor,
    floor_clamp_config_fields,
    levers_snapshot,
    risk_floor_bounds,
)
from tests.test_risk_gates import FakeConnector, _bracket, _cfg, _market_order_exit


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()
    reset_risk_gate()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()
    reset_risk_gate()


def _paper_ns(**extra):
    base = dict(trading_mode="paper", ibkr_port=7497, is_paper=True)
    base.update(extra)
    return SimpleNamespace(**base)


def _live_ns(**extra):
    base = dict(trading_mode="live", ibkr_port=4002, is_paper=True)
    base.update(extra)
    return SimpleNamespace(**base)


def test_other_walkaway_ceilings_stay_25():
    assert RISK_FLOOR["daily_loss_limit_pct"] == (0.5, 25.0)
    assert RISK_FLOOR["max_position_pct"] == (5.0, 25.0)
    assert RISK_FLOOR["max_risk_per_trade_pct"] == (0.25, 25.0)
    assert RISK_FLOOR["max_symbol_concentration_pct"] == (5.0, 25.0)
    assert RISK_FLOOR["max_option_premium_pct"] == (1.0, 25.0)
    assert RISK_FLOOR["max_arena_concentration_pct"] == (5.0, 25.0)
    for key, (lo, hi) in RISK_FLOOR.items():
        if key == "max_peak_drawdown_pct":
            assert (lo, hi) == (2.0, 40.0)
        else:
            assert hi == 25.0


def test_peak_dd_bounds_are_paper_40_live_25():
    assert risk_floor_bounds("max_peak_drawdown_pct", _paper_ns()) == (2.0, 40.0)
    assert risk_floor_bounds("max_peak_drawdown_pct", _live_ns()) == (2.0, 25.0)
    assert risk_floor_bounds("daily_loss_limit_pct", _paper_ns()) == (0.5, 25.0)
    assert risk_floor_bounds("daily_loss_limit_pct", _live_ns()) == (0.5, 25.0)


def test_paper_request_40_is_accepted_and_reported():
    v, note = clamp_risk_to_floor("max_peak_drawdown_pct", 40.0, cfg=_paper_ns())
    assert v == 40.0
    assert note is None
    applied, notes = clamp_risk_knobs({"max_peak_drawdown_pct": 40.0})
    assert applied["max_peak_drawdown_pct"] == 40.0
    assert "max_peak_drawdown_pct" not in notes


def test_paper_41_clamps_to_40_and_reports():
    v, note = clamp_risk_to_floor("max_peak_drawdown_pct", 41.0, cfg=_paper_ns())
    assert v == 40.0
    assert note == {"raw": 41.0, "clamped": 40.0}
    v, note = clamp_risk_to_floor("max_peak_drawdown_pct", 99.0, cfg=_paper_ns())
    assert v == 40.0
    assert note is not None


def test_paper_below_floor_clamps_up_and_reports():
    v, note = clamp_risk_to_floor("max_peak_drawdown_pct", 1.0, cfg=_paper_ns())
    assert v == 2.0
    assert note == {"raw": 1.0, "clamped": 2.0}


def test_live_request_40_clamps_to_25_and_reports():
    v, note = clamp_risk_to_floor("max_peak_drawdown_pct", 40.0, cfg=_live_ns())
    assert v == 25.0
    assert note == {"raw": 40.0, "clamped": 25.0}


def test_live_clamp_risk_knobs_reports_40_to_25(monkeypatch):
    live_cfg = _cfg(trading_mode="live")
    monkeypatch.setattr("abcxauto.config.get_config", lambda: live_cfg)
    applied, notes = clamp_risk_knobs({"max_peak_drawdown_pct": 40.0})
    assert applied["max_peak_drawdown_pct"] == 25.0
    assert notes["max_peak_drawdown_pct"]["raw"] == 40.0
    assert notes["max_peak_drawdown_pct"]["clamped"] == 25.0


def test_paper_get_config_keeps_operator_40(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(max_peak_drawdown_pct=40.0, persist=True)
    get_config.cache_clear()
    assert get_config().max_peak_drawdown_pct == 40.0


def test_live_floor_clamp_reports_40_down_to_25():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=25.0,
        max_peak_drawdown_pct=40.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=0,
        risk_gates_enabled=True,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="live",
        ibkr_port=4002,
        is_paper=True,
        sizing_floors=True,
    )
    fixes = floor_clamp_config_fields(cfg)
    assert fixes["max_peak_drawdown_pct"] == 25.0


def test_paper_floor_clamp_keeps_40():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=25.0,
        max_peak_drawdown_pct=40.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=0,
        risk_gates_enabled=True,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        sizing_floors=False,
    )
    fixes = floor_clamp_config_fields(cfg)
    assert "max_peak_drawdown_pct" not in fixes


def test_self_tune_cannot_raise_peak_dd_on_paper(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(max_peak_drawdown_pct=12.0, persist=True)
    out = apply_self_tune({"max_peak_drawdown_pct": 40.0}, persist=True)
    assert "max_peak_drawdown_pct" in (out.get("rejected") or {})
    assert "max_peak_drawdown_pct" not in (out.get("applied") or {})
    assert get_config().max_peak_drawdown_pct == 12.0
    from_default = apply_self_tune({"max_peak_drawdown_pct": 25.0}, persist=True)
    assert "max_peak_drawdown_pct" in (from_default.get("rejected") or {})
    assert get_config().max_peak_drawdown_pct == 12.0


def test_self_tune_cannot_raise_peak_dd_on_live(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(max_peak_drawdown_pct=12.0, persist=True)
    monkeypatch.setattr("abcxauto.self_tune.live_desk", lambda cfg=None: True)
    out = apply_self_tune({"max_peak_drawdown_pct": 40.0}, persist=True)
    assert "max_peak_drawdown_pct" in (out.get("rejected") or {})
    assert "max_peak_drawdown_pct" not in (out.get("applied") or {})
    assert get_config().max_peak_drawdown_pct == 12.0


def test_self_tune_can_tighten_peak_dd(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(max_peak_drawdown_pct=40.0, persist=True)
    assert get_config().max_peak_drawdown_pct == 40.0
    out = apply_self_tune({"max_peak_drawdown_pct": 15.0}, persist=True)
    assert out["status"] == "ok"
    assert (out.get("applied") or {}).get("max_peak_drawdown_pct") == 15.0
    assert get_config().max_peak_drawdown_pct == 15.0
    monkeypatch.setattr("abcxauto.self_tune.live_desk", lambda cfg=None: True)
    live_out = apply_self_tune({"max_peak_drawdown_pct": 8.0}, persist=True)
    assert live_out["status"] == "ok"
    assert get_config().max_peak_drawdown_pct == 8.0


def test_defined_risk_and_cash_only_stay_locked():
    out = apply_self_tune(
        {"defined_risk_only": False, "cash_only": False},
        persist=False,
    )
    assert get_config().defined_risk_only is True
    assert get_config().cash_only is True
    rejected = out.get("rejected") or {}
    assert "defined_risk_only" in rejected
    assert "cash_only" in rejected


def test_levers_snapshot_peak_dd_is_mode_aware():
    paper = levers_snapshot(_paper_ns(max_peak_drawdown_pct=12.0))
    assert paper["max_peak_drawdown_pct"]["min"] == 2.0
    assert paper["max_peak_drawdown_pct"]["max"] == 40.0
    live = levers_snapshot(_live_ns(max_peak_drawdown_pct=12.0))
    assert live["max_peak_drawdown_pct"]["max"] == 25.0


@pytest.mark.asyncio
async def test_peak_dd_rejects_entries_never_halts_and_self_clears(monkeypatch):
    cfg = _cfg(
        max_peak_drawdown_pct=8.0,
        max_position_pct=0,
        daily_loss_limit_pct=0,
        max_open_positions=0,
        cash_only=False,
        sizing_floors=True,
    )
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    gate = reset_risk_gate()
    gate.update_equity(100_000.0)
    conn = FakeConnector(account={"netliquidation": 90_000.0, "dailypnl": 0.0})
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "drawdown" in reason.lower()
    assert gate.is_halted is False
    conn.account = {"netliquidation": 93_000.0, "dailypnl": 0.0}
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is True, reason
    assert gate.is_halted is False


@pytest.mark.asyncio
async def test_peak_dd_bypasses_exits(monkeypatch):
    cfg = _cfg(
        max_peak_drawdown_pct=8.0,
        max_position_pct=0,
        daily_loss_limit_pct=0,
        max_open_positions=0,
        cash_only=False,
        sizing_floors=True,
    )
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    gate = reset_risk_gate()
    gate.update_equity(100_000.0)
    conn = FakeConnector(account={"netliquidation": 90_000.0, "dailypnl": 0.0})
    ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "drawdown" in reason.lower()
    ok, reason = await gate.pre_trade_check(_market_order_exit(), conn)
    assert ok is True
    assert "bypass" in reason
    assert gate.is_halted is False


@pytest.mark.asyncio
async def test_auto_panic_reads_daily_loss_not_peak_dd(monkeypatch):
    import inspect

    from abcxauto.monitor import PortfolioMonitor

    body = inspect.getsource(PortfolioMonitor._maybe_auto_panic)
    assert "daily_loss_limit_pct" in body
    assert "max_peak_drawdown_pct" not in body

    gate = reset_risk_gate()
    injections = []

    class Session:
        def emit(self, *_a, **_k):
            pass

        async def inject(self, message, source=""):
            injections.append(message)

    cfg = _cfg(
        auto_panic_on_breach=True,
        daily_loss_limit_pct=25.0,
        max_peak_drawdown_pct=2.0,
        monitor_poll_s=30,
        monitor_review_s=300,
    )
    monkeypatch.setattr("abcxauto.monitor.get_config", lambda: cfg)
    conn = FakeConnector(
        account={"netliquidation": 50_000.0, "dailypnl": 0.0},
        positions=[{"symbol": "AAPL", "quantity": 10, "sec_type": "STK"}],
    )
    mon = PortfolioMonitor(Session(), conn)
    mon.cfg = cfg
    snap = {
        "account": conn.account,
        "protection": {"unprotected_symbols": [], "positions": conn.positions},
    }
    await mon._maybe_auto_panic(snap)
    assert conn.flatten_calls == 0
    assert gate.is_halted is False
    assert not injections
