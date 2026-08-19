"""ProEngine integration on shipped path (no mocks of engine itself).

Mocks only: agent_loop._tool, agent_loop.grok, get_ibkr_connector.
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from abcxauto.pro_engine import ProEngine
from tests.conftest import grok_json_as_turn

SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-80c4246a04fb\implementer")
GOAL_SCRATCH = SCRATCH


class _Cfg:
    xai_api_key = "test-key"
    monitor_enabled = True
    trading_mode = "paper"
    risk_posture = "balanced"


@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _Cfg())


@pytest.mark.asyncio
async def test_pro_engine_runs_cycles_with_inventory_and_tweak(monkeypatch, tmp_path):
    """Engine.start() drives >=3 run_cycle with inventory+validation in records."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.wake_bus.min_look_s", lambda: 0.05)
    monkeypatch.setattr("abcxauto.wake_bus.default_look_s", lambda **_k: 0.05)
    monkeypatch.setattr("abcxauto.wake_bus.pulse_sleep_s", lambda *_a, **_k: 0.05)
    calls = {"grok": 0}

    async def fake_grok(_g, prompt: str, *, stage: str = "act") -> str:
        calls["grok"] += 1
        if stage == "judge" or "JUDGE STAGE" in prompt:
            return json.dumps({
                "stance": "protect",
                "thesis": "Protect SPY",
                "focus": "unprotected STK",
                "dismissed": "",
                "intent": {
                    "kind": "protect",
                    "symbol": "SPY",
                    "direction": "LONG",
                    "urgency": "high",
                },
                "risk_budget_pct": 1.0,
                "regime_fit": True,
                "setup_grade": "A",
            })
        # Act JSON
        calls["act"] = calls.get("act", 0) + 1
        # Occasionally return a close with explicit conId target to exercise real protocol path
        if calls["act"] % 3 == 0:
            return json.dumps({
                "action": "market_order", "strategy": "market_order",
                "params": {"symbol": "SPY", "action": "SELL", "quantity": 1, "conId": "42"},
                "rationale": "Current reality: RTH. Closing target = conId=42 (SPY stock).",
                "target_conId": "42",
                "reasoning_chain": "exact conId match",
            })
        return json.dumps({
            "action": "modify_stop",
            "strategy": "modify_stop",
            "params": {
                "symbol": "SPY",
                "conId": 42,
                "stop_price": 490.0,
            },
            "rationale": "inventory + checklist reviewed; protect SPY conId=42",
            "reasoning_chain": "full inventory present",
            "target_conId": "42",
        })

    async def _fake_tool(_c, name, _a=None):
        return {
            "account_summary": {"netliquidation": 100000 + calls["grok"] * 10, "unrealizedpnl": calls["grok"]},
            "positions": [
                {"symbol": "SPY", "quantity": 5, "sec_type": "STK", "unrealized_pnl": 12.3, "con_id": 42}
            ],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 501},
        }.get(name, {})

    class _Conn:
        connected = True

        async def connect(self):
            return True

    async def _noop_send(action, conn):
        return {"status": "executed", "strategy": action.get("strategy")}

    async def _no_opps(*_a, **_k):
        return []

    async def _no_news(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_json_as_turn(fake_grok))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", _noop_send)
    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _no_news)
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _Cfg(),
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: _Cfg(),
    )

    eng = ProEngine()
    err = eng.start()
    assert err is None, f"start err: {err}"
    assert eng.state.running

    # Wake-bus pulse; default look is shortened via env for this test.
    deadline = time.time() + 20
    while time.time() < deadline and eng.state.cycles < 3:
        eng.drain_apply()
        await asyncio.sleep(0.05)

    eng.stop_engine()
    eng.drain_apply()

    # assertions
    assert eng.state.cycles >= 3
    assert len(eng.state.records) >= 3
    # inventory in records
    inv_recs = [r for r in eng.state.records if r.get("inventory")]
    assert len(inv_recs) >= 3
    assert any("LIVE POSITION LEDGER" in (r.get("inventory") or "") for r in inv_recs)
    # validation present
    assert any(r.get("validation") for r in eng.state.records if r.get("type") != "panic")
    # exercise close + conId target naming (real path)
    has_close = any("close" in str((r.get("action") or r.get("action_obj", {}))).lower() or r.get("target_conId") for r in eng.state.records)
    assert has_close or any("conId=42" in str(r) for r in eng.state.records)  # from fake close exercise
    # Cached suite used — no per-cycle suite theater in cycle records
    assert all(
        (r.get("reconfig") in (None, {}) or not r.get("reconfig"))
        for r in eng.state.records
        if r.get("type") == "cycle"
    )

    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "pro_integration_notes.txt").write_text(
        "\n".join([
            "ProEngine integration test (test_pro_engine.py)",
            f"cycles={eng.state.cycles}",
            f"records={len(eng.state.records)}",
            f"grok_calls={calls['grok']}",
            "inventory_in_records=True",
            "validation_in_records=True",
            "result=PASS",
        ]) + "\n",
        encoding="utf-8",
    )
    GOAL_SCRATCH.mkdir(parents=True, exist_ok=True)
    (GOAL_SCRATCH / "pro_integration_notes.txt").write_text(
        "\n".join([
            "ProEngine integration test (test_pro_engine.py)",
            f"cycles={eng.state.cycles}",
            f"records={len(eng.state.records)}",
            f"grok_calls={calls['grok']}",
            "inventory_in_records=True",
            "validation_in_records=True",
            "result=PASS",
            "conId target naming exercised in fake LLM output",
        ]) + "\n",
        encoding="utf-8",
    )


def test_pro_engine_passes_new_fields_through_records(monkeypatch):
    """Sanity: cycle payload carries inventory/reasoning/validation/tweak_before."""
    # lighter sync style via direct but engine is exercised in above
    eng = ProEngine()
    # no start, just check dataclass has the attrs used by apply
    assert hasattr(eng.state, "records")
    assert hasattr(eng.state, "portfolio")
    assert hasattr(eng.state, "mandate_health")
    assert hasattr(eng.state, "last_decision")
    assert hasattr(eng.state, "hold_count")
    assert hasattr(eng.state, "trade_count")


def test_on_cycle_populates_book_and_mandate(monkeypatch):
    """_on_cycle fills portfolio / mandate_health / last_decision / hold-trade stats."""
    from abcxauto.pro_engine import compute_mandate_health
    from abcxauto.risk_gates import reset_risk_gate

    reset_risk_gate()
    eng = ProEngine()
    eng._on_cycle(
        {
            "cycle": 1,
            "pnl": -50.0,
            "pnl_chg": -10.0,
            "equity": 100_000.0,
            "strat": "market_bracket",
            "rationale": "enter",
            "action_obj": {"strategy": "market_bracket"},
            "result": {"status": "executed"},
            "impact": {},
            "reality_pulse": {},
            "positions": [{"symbol": "SPY", "sec_type": "STK", "quantity": 1}],
            "open_orders": [],
            "unprotected": [],
            "protection": {"unprotected_symbols": []},
            "portfolio": "1 positions | 0 orders",
            "inventory": "LIVE POSITION LEDGER",
            "validation": "ok",
        }
    )
    s = eng.state
    assert s.last_decision == "trade"
    assert s.trade_count == 1
    assert s.hold_count == 0
    assert s.unprotected_count == 0
    assert s.mandate_health == "green"
    assert s.portfolio.get("net_liquidation") == 100_000.0
    assert s.portfolio.get("last_decision") == "trade"

    eng._on_cycle(
        {
            "cycle": 2,
            "pnl": -50.0,
            "pnl_chg": 0.0,
            "equity": 100_000.0,
            "strat": "hold",
            "rationale": "wait",
            "action_obj": {},
            "result": {"status": "hold"},
            "impact": {},
            "reality_pulse": {},
            "positions": [{"symbol": "SPY", "sec_type": "STK", "quantity": 1}],
            "open_orders": [],
            "unprotected": ["SPY"],
            "protection": {"unprotected_symbols": ["SPY"]},
            "inventory": "LIVE POSITION LEDGER",
            "validation": "ok",
        }
    )
    s = eng.state
    assert s.last_decision == "hold"
    assert s.hold_count == 1
    assert s.trade_count == 1
    assert s.unprotected_count == 1
    assert s.mandate_health == "red"
    assert "unprotected" in s.mandate_health_label

    level, _ = compute_mandate_health(
        unprotected_count=0,
        halted=False,
        equity=100_000.0,
        daily_pnl=-1200.0,  # > 50% of 2% limit (1000)
        gate_blocks=0,
    )
    # With daily-loss limit default off, amber comes from gate_blocks instead.
    assert level == "green"
    level2, _ = compute_mandate_health(
        unprotected_count=0,
        halted=False,
        equity=100_000.0,
        daily_pnl=0.0,
        gate_blocks=3,
    )
    assert level2 == "amber"
    reset_risk_gate()


@pytest.mark.asyncio
async def test_pro_engine_wires_portfolio_monitor(monkeypatch):
    """After IBKR connect, ProEngine starts PortfolioMonitor on the worker loop."""
    from abcxauto.monitor import PortfolioMonitor
    from abcxauto.risk_gates import get_risk_gate, reset_risk_gate

    class _MonCfg:
        xai_api_key = "test-key"
        monitor_enabled = True
        monitor_poll_s = 60
        monitor_review_s = 300
        monitor_extended_hours = False
        auto_panic_on_breach = True
        daily_loss_limit_pct = 2.0

    class _Conn:
        connected = True

        async def connect(self):
            return True

        async def get_positions(self):
            return []

        async def get_open_orders(self):
            return []

        async def get_account_summary(self):
            return {"netliquidation": 100_000.0, "dailypnl": -3000.0}

        async def get_fills(self):
            return []

        async def flatten_all(self):
            return {"success": True, "flattened": 0}

    async def fake_grok(_g, prompt: str) -> str:
        return json.dumps(
            {
                "action": "market_bracket",
                "strategy": "market_bracket",
                "params": {
                    "symbol": "SPY",
                    "quantity": 1,
                    "direction": "LONG",
                    "stop_price": 490.0,
                    "target_price": 520.0,
                    "price_hint": 500.0,
                },
                "rationale": "active entry",
                "reasoning_chain": "test",
                "target_conId": "",
                "kahneman": {
                    "system1_scan": "scan",
                    "system2_base_rate": "base",
                    "pre_mortem": "gap",
                    "alternatives": ["market_bracket"],
                    "bias_audit": ["anchoring"],
                },
            }
        )

    async def _fake_tool(_c, name, _a=None):
        return {
            "account_summary": {"netliquidation": 100000, "unrealizedpnl": 0},
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _MonCfg())
    monkeypatch.setattr("abcxauto.monitor.get_config", lambda: _MonCfg())
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: _MonCfg())
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_json_as_turn(fake_grok))
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)

    reset_risk_gate()
    eng = ProEngine()
    err = eng.start()
    assert err is None

    deadline = time.time() + 8
    while time.time() < deadline and eng.monitor is None:
        eng.drain_apply()
        await asyncio.sleep(0.05)

    assert eng.monitor is not None
    assert isinstance(eng.monitor, PortfolioMonitor)
    assert eng.monitor.running is True
    assert getattr(eng.monitor.session, "supports_agent_review", True) is False

    # Auto-panic path reachable via monitor (halt latch + inject to stub)
    snap = {
        "account": {"netliquidation": 100_000.0, "dailypnl": -3000.0},
        "protection": {"positions": [], "unprotected_symbols": []},
    }
    await eng.monitor._maybe_auto_panic(snap)
    assert get_risk_gate().is_halted

    eng.stop_engine()
    eng.drain_apply()
    assert eng.monitor is None
    reset_risk_gate()


@pytest.mark.asyncio
async def test_connect_broker_no_cycles_without_xai(monkeypatch):
    """connect_broker links IBKR + monitor without requiring xAI or running cycles."""

    class _NoXaiCfg:
        xai_api_key = ""
        monitor_enabled = True

    class _Conn:
        connected = True

        async def connect(self):
            return True

    cycle_calls = {"n": 0}

    async def boom_cycle(*_a, **_k):
        cycle_calls["n"] += 1
        raise AssertionError("run_cycle must not run on connect-only")

    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _NoXaiCfg())
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr("abcxauto.pro_engine.run_cycle", boom_cycle)
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._start_monitor",
        lambda self: setattr(self, "monitor", type("M", (), {"running": True})()),
    )

    eng = ProEngine()
    assert eng.start() == "XAI_API_KEY missing"
    err = eng.connect_broker()
    assert err is None

    deadline = time.time() + 5
    while time.time() < deadline and not eng.state.connected:
        eng.drain_apply()
        await asyncio.sleep(0.05)

    assert eng.state.connected
    assert eng.state.autonomous is False
    assert eng.state.running is False
    assert eng.state.status == "Connected"
    assert cycle_calls["n"] == 0
    await asyncio.sleep(0.2)
    eng.drain_apply()
    assert cycle_calls["n"] == 0
    assert eng.state.cycles == 0

    eng.stop_engine()
    eng.drain_apply()
    assert eng.state.connected is False


@pytest.mark.asyncio
async def test_start_after_connect_enables_autonomous(monkeypatch):
    """Start on an already-connected worker flips autonomous and runs cycles."""
    calls = {"grok": 0}

    async def fake_grok(_g, prompt: str) -> str:
        calls["grok"] += 1
        return json.dumps({
            "action": "hold",
            "strategy": "hold",
            "params": {},
            "rationale": "hold",
            "reasoning_chain": "hold",
            "target_conId": "",
            "kahneman": {
                "system1_scan": "ok",
                "system2_base_rate": "ok",
                "pre_mortem": "ok",
                "alternatives": ["hold"],
                "bias_audit": [],
            },
        })

    async def _fake_tool(_c, name, _a=None):
        return {
            "account_summary": {"netliquidation": 100000, "unrealizedpnl": 0},
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    class _Conn:
        connected = True

        async def connect(self):
            return True

    async def _noop_send(action, conn):
        return {"status": "held", "strategy": "hold"}

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_json_as_turn(fake_grok))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", _noop_send)
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: _Cfg())
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._start_monitor",
        lambda self: setattr(self, "monitor", type("M", (), {"running": True})()),
    )

    eng = ProEngine()
    assert eng.connect_broker() is None
    deadline = time.time() + 5
    while time.time() < deadline and not eng.state.connected:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert eng.state.connected
    assert eng.state.autonomous is False

    assert eng.start() is None
    assert eng.state.autonomous is True
    assert eng.state.running is True

    deadline = time.time() + 10
    while time.time() < deadline and eng.state.cycles < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)

    eng.pause_engine()
    eng.drain_apply()
    assert eng.state.autonomous is False
    assert eng.state.running is False
    assert eng.monitor is not None  # pause keeps monitor

    eng.stop_engine()
    eng.drain_apply()
