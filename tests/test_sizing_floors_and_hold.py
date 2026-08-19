"""sizing_floors clerk flag, size_* % NL reasons, wake/day pct facts."""

from __future__ import annotations

import pytest

from abcxauto.config import (
    RISK_CONFIG_KEYS,
    clear_runtime_overrides,
    get_config,
    update_risk_config,
)
from abcxauto.proposals import validate_proposal
from abcxauto.risk_gates import reset_risk_gate, sizing_floors_active
from abcxauto.self_tune import apply_self_tune, floor_clamp_config_fields
from abcxauto.world_state import (
    WorldState,
    compact_position,
    day_facts,
    format_wake,
    pct_of_nl,
)
from tests.test_proposals import RATIONALE
from tests.test_risk_gates import FakeConnector, _bracket, _cfg


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()
    reset_risk_gate()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()
    reset_risk_gate()


def _world(**kwargs):
    base = dict(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="defensive",
        effective_posture="defensive",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        trade_plans=[],
        capacity={"open_count": 0, "max_open_positions": 15, "allows_new_risk": True},
        structure_lessons=[],
        structure_cooldown={},
        book={},
        pulse={},
        taken_at="",
        option_facts=[],
        fills=[],
        stop_qty_fact=None,
        book_reconciled=True,
    )
    base.update(kwargs)
    return WorldState(**base)


# ---------------------------------------------------------------------------
# sizing_floors
# ---------------------------------------------------------------------------


def test_sizing_floors_default_off_on_paper():
    cfg = get_config()
    assert cfg.trading_mode == "paper" or cfg.is_paper
    assert cfg.sizing_floors is False
    assert sizing_floors_active(cfg) is False


def test_sizing_floors_in_risk_config_keys_not_budget():
    assert "sizing_floors" in RISK_CONFIG_KEYS
    assert "trading_budget_usd" not in RISK_CONFIG_KEYS


def test_grok_cannot_self_tune_sizing_floors():
    out = apply_self_tune({"sizing_floors": True}, persist=False)
    rejected = out.get("rejected") or {}
    assert "sizing_floors" in rejected
    assert get_config().sizing_floors is False


def test_live_forces_sizing_floors_on():
    cfg = _cfg(trading_mode="live", sizing_floors=False, ibkr_port=7496)
    fixes = floor_clamp_config_fields(cfg)
    assert fixes.get("sizing_floors") is True
    assert "risk_posture" not in fixes
    assert sizing_floors_active(cfg) is True


def test_paper_can_flip_floors_both_ways(tmp_path, monkeypatch):
    path = tmp_path / "floors.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto.config import clear_risk_settings, load_risk_settings

    clear_risk_settings(path=path)
    load_risk_settings(path)
    assert sizing_floors_active() is False
    update_risk_config(sizing_floors=True, persist=True)
    assert sizing_floors_active() is True
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "sizing_floors" in text
    update_risk_config(sizing_floors=False, persist=True)
    assert sizing_floors_active() is False


@pytest.mark.asyncio
async def test_floors_off_skips_pct_but_blocks_unknown_option_and_short(monkeypatch):
    cfg = _cfg(
        sizing_floors=False,
        trading_mode="paper",
        risk_posture="balanced",
        max_position_pct=1.0,
        max_risk_per_trade_pct=0.5,
        daily_loss_limit_pct=1.0,
        max_peak_drawdown_pct=1.0,
        cash_only=True,
        max_open_positions=0,
        defined_risk_only=False,
    )
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    gate = reset_risk_gate()
    conn = FakeConnector(
        account={
            "netliquidation": 10_000.0,
            "dailypnl": -500.0,  # would trip daily-loss if floors on
            "TotalCashValue": 100.0,
        }
    )
    # Huge bracket would fail % floors — passes when OFF
    ok, reason = await gate.pre_trade_check(
        _bracket(qty=500, entry=100.0, stop=95.0, target=110.0), conn
    )
    assert ok is True, reason

    # Structural: still reject short stock
    ok, reason = await gate.pre_trade_check(
        _bracket(qty=1, entry=100.0, direction="SHORT"), conn
    )
    assert ok is False
    assert "short" in reason.lower() or "cash-only" in reason.lower()

    # Unknown option notional still fail-closed
    buy = validate_proposal(
        "buy_option",
        {
            "symbol": "SPY",
            "expiration": "20260718",
            "strike": 500.0,
            "right": "C",
            "quantity": 1,
            # no limit_price → unknown notional
        },
        RATIONALE,
    )
    ok, reason = await gate.pre_trade_check(buy, conn)
    assert ok is False
    assert reason == "size_unknown_notional"


@pytest.mark.asyncio
async def test_floors_on_size_reasons_are_pct_of_nl(monkeypatch):
    cfg = _cfg(
        sizing_floors=True,
        trading_mode="paper",
        risk_posture="balanced",
        max_position_pct=50.0,
        max_risk_per_trade_pct=0.5,
        daily_loss_limit_pct=0,
        max_peak_drawdown_pct=0,
        max_option_premium_pct=25.0,
        cash_only=True,
        max_open_positions=0,
        defined_risk_only=False,
    )
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    gate = reset_risk_gate()
    conn = FakeConnector(
        account={
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 100_000.0,
        }
    )
    # risk = 200 * 5 = 1000 → 1.0% > 0.5%; notional 20k = 20% < max_position 50%
    ok, reason = await gate.pre_trade_check(
        _bracket(qty=200, entry=100.0, stop=95.0, target=110.0), conn
    )
    assert ok is False
    assert reason.startswith("size_risk_per_trade ")
    assert ">" in reason

    # option debit: limit*100*qty vs max_risk
    opt = validate_proposal(
        "buy_option",
        {
            "symbol": "SPY",
            "expiration": "20260718",
            "strike": 500.0,
            "right": "C",
            "quantity": 10,
            "limit_price": 2.0,  # 2000 = 2% NL > 0.5%
        },
        RATIONALE,
    )
    ok, reason = await gate.pre_trade_check(opt, conn)
    assert ok is False
    assert "size_risk_per_trade" in reason or "size_max_position" in reason


# ---------------------------------------------------------------------------
# send enum — hold is not a ticket
# ---------------------------------------------------------------------------


def _send_strategy_enum(tools) -> list[str]:
    import json

    for t in tools:
        fn = getattr(t, "function", None)
        name = getattr(fn, "name", None) if fn is not None else getattr(t, "name", None)
        if name != "send":
            continue
        params = getattr(fn, "parameters", None) if fn is not None else None
        if isinstance(params, str):
            params = json.loads(params)
        props = (params or {}).get("properties") or {}
        return list((props.get("strategy") or {}).get("enum") or [])
    return []


def test_send_enum_never_includes_hold():
    from abcxauto.brain import agent_tools, brain_system_prompt
    from abcxauto.order_examples import ticket_strategy_names

    enum = _send_strategy_enum(agent_tools())
    assert "hold" not in enum
    assert "market_bracket" in enum
    assert "hold" not in ticket_strategy_names()
    lines = [ln.strip() for ln in brain_system_prompt().splitlines()]
    assert not any(ln.startswith("hold:") for ln in lines)


# ---------------------------------------------------------------------------
# % of NL facts (scorecard covered by #13 — do not retest format_scorecard here)
# ---------------------------------------------------------------------------


def test_pct_of_nl_helper():
    assert pct_of_nl(50, 1000) == 5.0
    assert pct_of_nl(None, 1000) is None


def test_day_facts_include_pct_of_nl():
    world = _world(net_liquidation=10_000.0, daily_pnl=-100.0)
    world.positions = [
        {
            "symbol": "AAPL",
            "quantity": 10,
            "unrealizedPNL": -50.0,
            "marketValue": 1500.0,
            "market_price": 150.0,
            "secType": "STK",
        }
    ]
    day = day_facts(
        world,
        {
            "startup_cash": 10_000.0,
            "edge_usd": -200.0,
            "model_cost_usd": 5.0,
            "book_pnl": -100.0,
            "beating_model": False,
        },
    )
    assert day["daily_pnl"] == -100.0
    assert day["daily_pnl_pct_of_nl"] == -1.0
    assert day["open_upnl"] == -50.0
    assert day["open_upnl_pct_of_nl"] == -0.5
    assert day["edge_usd"] == -200.0
    assert day["edge_pct_of_nl"] == -2.0
    assert day["model_cost_usd"] == 5.0
    assert day["model_cost_pct_of_nl"] == 0.05
    assert "max_risk_per_trade_pct" in day
    assert day["max_risk_per_trade_pct"] == day["risk_per_trade_pct"]


def test_format_wake_prints_dollars_and_pct():
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 1,
            "lots": 1,
            "nl": 10_000.0,
            "daily_pnl": -100.0,
            "daily_pnl_pct_of_nl": -1.0,
            "open_upnl": -50.0,
            "open_upnl_pct_of_nl": -0.5,
            "edge_usd": -200.0,
            "edge_pct_of_nl": -2.0,
            "model_cost_usd": 5.0,
            "model_cost_pct_of_nl": 0.05,
            "halt_trips_at_usd": -250.0,
            "halt_trips_at_pct_of_nl": -2.5,
            "nl_vs_start": -100.0,
            "beating_model": False,
            "risk_per_trade_pct": 25.0,
            "capacity": {"open_count": 1, "max_open_positions": 15},
            "open_lots": ["AAPL STK long 10"],
        },
    )
    assert "ibkrDay=-1.0% NL ($-100.0)" in text
    assert "openU=-0.5% NL ($-50.0)" in text
    assert "edgeVsModel=-2.0% NL ($-200.0)" in text
    assert "cost=0.05% NL ($5.0)" in text
    assert "haltAt=-2.5% NL ($-250.0)" in text
    assert "vsStart=" in text
    # Review leads with % NL, not dollars-first.
    assert text.index("ibkrDay=-1.0% NL") < text.index("$-100.0")
    # Ceiling knob — not working size. Older day dicts still fall back.
    assert "max_risk=25.0%" in text
    assert "risk/trade=" not in text


def test_compact_position_pct_nl():
    row = compact_position(
        {
            "symbol": "AAPL",
            "quantity": 10,
            "unrealizedPNL": -20.0,
            "marketValue": 1500.0,
            "market_price": 150.0,
            "stop_price": 145.0,
            "secType": "STK",
        },
        net_liq=10_000.0,
    )
    assert row["uPnL"] == -20.0
    assert row["uPnL_pct_nl"] == -0.2
    assert row["mv_pct_nl"] == 15.0
    assert row["risk_pct_nl"] == 0.5  # |150-145|*10 / 10000 * 100


def test_compact_position_opt_avg_usd_pct_nl():
    row = compact_position(
        {
            "symbol": "SPY",
            "quantity": 1,
            "secType": "OPT",
            "avgCost": 126.0,  # contract cash → avg_usd
            "market_price": 1.20,
            "unrealizedPNL": -6.0,
            "marketValue": 120.0,
            "strike": 500,
            "right": "C",
            "expiration": "20260718",
        },
        net_liq=10_000.0,
    )
    assert row.get("avg_usd") == 126.0
    assert row["avg_usd_pct_nl"] == 1.26
    assert row["uPnL_pct_nl"] == -0.06
    assert row["mv_pct_nl"] == 1.2


def test_size_pct_nl_hoisted_not_converted():
    from abcxauto.tool_args import hoist_send_params, normalize_tool_call

    name, args = normalize_tool_call(
        "send",
        {
            "strategy": "bracket",
            "symbol": "AAPL",
            "quantity": 7,
            "size_pct_nl": 1.5,
            "direction": "LONG",
            "entry_price": 100,
            "stop_price": 95,
            "target_price": 110,
        },
    )
    assert name == "send"
    assert args["params"]["quantity"] == 7
    assert args["params"]["size_pct_nl"] == 1.5
    # Hoist keeps annotation; does not invent shares from %.
    out = hoist_send_params({"strategy": "bracket", "size_pct_nl": 2.0, "quantity": 3})
    assert out["params"]["quantity"] == 3
    assert out["params"]["size_pct_nl"] == 2.0


def test_size_pct_nl_is_clerk_send_annotation():
    """size_pct_nl lives on send/tool_args — not brain AGENT_TOOLS schema."""
    from abcxauto.send import SEND_SIZE_PCT_NL
    from abcxauto.tool_args import SEND_SIZE_PCT_NL as TA_KEY
    from abcxauto import tool_args as ta

    assert SEND_SIZE_PCT_NL == "size_pct_nl"
    assert TA_KEY == "size_pct_nl"
    assert "size_pct_nl" in ta._SEND_HOIST

    from abcxauto.brain import AGENT_TOOLS

    send = None
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        name = getattr(fn, "name", None) if fn is not None else getattr(t, "name", None)
        if name == "send":
            send = t
            break
    assert send is not None
    assert "size_pct_nl" not in str(send)
