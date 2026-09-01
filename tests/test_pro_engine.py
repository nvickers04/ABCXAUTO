"""ProEngine integration on shipped path (no mocks of engine itself).

Mocks only: agent_loop._tool, brain.grok_turn, get_ibkr_connector.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from abcxauto.pro_engine import ProEngine
from tests.conftest import grok_json_as_turn


class _Cfg:
    xai_api_key = "test-key"
    monitor_enabled = True
    trading_mode = "paper"
    risk_posture = "balanced"


@pytest.fixture(autouse=True)
def patch_config(monkeypatch):
    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _Cfg())


def _poke_book(eng, kind: str = "fill", detail: str = "SPY") -> None:
    """Wake a sitting stay-up worker from the pytest thread."""
    from abcxauto.park_clock import BookEvent, note_interrupt

    note_interrupt(BookEvent(kind, detail))
    ev = getattr(eng, "_wake_event", None)
    loop = getattr(eng, "_worker_loop", None)
    if loop is not None and ev is not None:
        loop.call_soon_threadsafe(ev.set)


@pytest.mark.asyncio
async def test_pro_engine_runs_cycles_with_inventory_and_tweak(monkeypatch, tmp_path):
    """Engine.start() drives >=3 looks with inventory+validation in records."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_DEFAULT_LOOK_S", "0.05")
    monkeypatch.setattr("abcxauto.park_clock.min_look_s", lambda: 0.05)
    monkeypatch.setattr("abcxauto.park_clock.default_look_s", lambda **_k: 0.05)
    monkeypatch.setattr("abcxauto.park_clock.pulse_sleep_s", lambda *_a, **_k: 0.05)
    # Multi-cycle pulse uses a real park clock. Paper RTH no longer seeds one.
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
            "market_hours": {"session": "premarket"},
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
    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_json_as_turn(fake_grok))
    monkeypatch.setattr(
        "abcxauto.pro_engine.GrokClient",
        lambda: SimpleNamespace(chat=object()),
    )
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

    # Stay-up sits after a look. Fill pokes drive the extra cycles.
    deadline = time.time() + 20
    while time.time() < deadline and eng.state.cycles < 3:
        eng.drain_apply()
        if eng.state.cycles >= 1 and str(eng.state.status or "") == "On":
            _poke_book(eng, "fill", "SPY")
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


def test_think_emit_source_does_not_number_the_think():
    from pathlib import Path
    from tests.conftest import assert_no_cycle_counter

    src = (Path(__file__).resolve().parents[1] / "abcxauto" / "pro_engine.py").read_text(
        encoding="utf-8"
    )
    assert "Cycle {n}" not in src
    assert "Cycle {" not in src
    assert_no_cycle_counter("boot — Grok.")


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


def test_on_cycle_failed_look_does_not_blank_last_turn(tmp_path):
    """Cycle apply still updates the engine; junk persist does not wipe last_turn."""
    from abcxauto import think_stream as ts

    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "strat": "",
        "rationale": "Flat. No ticket.",
        "tool_trace": ["book", "status", "playbook"],
        "world_state": {"flat": True, "net_liquidation": 35000},
    })
    before = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    brief_before = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))

    eng = ProEngine()
    eng._on_cycle(
        {
            "cycle": 2,
            "pnl": 0.0,
            "pnl_chg": 0.0,
            "equity": 35000.0,
            "strat": "",
            "rationale": "",
            "tool_trace": [],
            "_failed": True,
            "action_obj": {},
            "result": {},
            "impact": {},
            "reality_pulse": {},
            "positions": [],
            "open_orders": [],
            "unprotected": [],
            "protection": {"unprotected_symbols": []},
            "inventory": "",
            "validation": "ok",
            "world_state": {"flat": True, "net_liquidation": 35000},
        }
    )
    after = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    brief_after = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert after["rationale"] == "Flat. No ticket."
    assert after["tool_trace"] == ["book", "status", "playbook"]
    assert after["ts"] == before["ts"]
    assert brief_after["rationale"] == brief_before["rationale"]
    assert eng.state.equity == 35000.0
    assert eng.state.tool_trace == []


def test_on_cycle_park_and_finished_look_still_write_last_turn(tmp_path):
    from abcxauto import think_stream as ts

    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "strat": "",
        "rationale": "Flat. No ticket.",
        "tool_trace": ["book"],
        "world_state": {"flat": True, "net_liquidation": 35000},
    })
    eng = ProEngine()
    eng._on_cycle(
        {
            "cycle": 3,
            "pnl": 0.0,
            "equity": 35000.0,
            "strat": "",
            "rationale": "Gate off. Parking.",
            "tool_trace": ["playbook", "book", "scan", "set_wake"],
            "_parked": True,
            "_failed": False,
            "action_obj": {},
            "result": {},
            "impact": {},
            "reality_pulse": {},
            "positions": [],
            "open_orders": [],
            "unprotected": [],
            "protection": {"unprotected_symbols": []},
            "inventory": "",
            "validation": "ok",
            "world_state": {"flat": True, "net_liquidation": 35000},
        }
    )
    parked = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert "Parking" in parked["rationale"]
    assert parked["tool_trace"] == ["playbook", "book", "scan", "set_wake"]

    eng._on_cycle(
        {
            "cycle": 4,
            "pnl": 0.0,
            "equity": 35100.0,
            "strat": "",
            "rationale": "Watching IWM. No ticket.",
            "tool_trace": ["book", "scan"],
            "_failed": False,
            "action_obj": {},
            "result": {},
            "impact": {},
            "reality_pulse": {},
            "positions": [],
            "open_orders": [],
            "unprotected": [],
            "protection": {"unprotected_symbols": []},
            "inventory": "",
            "validation": "ok",
            "world_state": {"flat": True, "net_liquidation": 35100},
        }
    )
    done = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert done["rationale"] == "Watching IWM. No ticket."
    assert done["tool_trace"] == ["book", "scan"]


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
    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_json_as_turn(fake_grok))
    monkeypatch.setattr(
        "abcxauto.pro_engine.GrokClient",
        lambda: SimpleNamespace(chat=object()),
    )
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
    mine: dict = {}

    async def boom_think(engine, *_a, **_k):
        # _host_think is patched on the class: a worker thread another module
        # left running would otherwise fail this test. Count only our engine.
        if engine is not mine.get("eng"):
            return {"_parked": True, "cycle": 0}
        cycle_calls["n"] += 1
        raise AssertionError("the think must not run on connect-only")

    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _NoXaiCfg())
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr("abcxauto.pro_engine.ProEngine._host_think", boom_think)
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._start_monitor",
        lambda self: setattr(self, "monitor", type("M", (), {"running": True})()),
    )

    eng = ProEngine()
    mine["eng"] = eng
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
    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_json_as_turn(fake_grok))
    monkeypatch.setattr(
        "abcxauto.pro_engine.GrokClient",
        lambda: SimpleNamespace(chat=object()),
    )
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


class _AliveWorker:
    def is_alive(self) -> bool:
        return True


def test_start_on_live_worker_sets_wake_event():
    """Start on a sitting worker must poke `_wake_event`, not only `_resume_think`."""
    eng = ProEngine()
    eng.worker = _AliveWorker()
    eng._wake_event = asyncio.Event()
    assert not eng._wake_event.is_set()
    assert eng.start() is None
    assert eng._resume_think is True
    assert not eng.pause.is_set()
    assert eng._wake_event.is_set()
    assert eng.state.autonomous is True
    assert eng.state.running is True
    assert eng.state.status == "Thinking"


def test_pause_then_start_clears_pause_and_sets_wake():
    """Pause → Start must leave the sit wait, not paint Grok on mid-wait."""
    eng = ProEngine()
    eng.worker = _AliveWorker()
    eng._wake_event = asyncio.Event()
    eng.pause_engine()
    assert eng.pause.is_set()
    assert eng.state.autonomous is False
    assert eng.start() is None
    assert not eng.pause.is_set()
    assert eng._resume_think is True
    assert eng._wake_event.is_set()
    assert eng.state.autonomous is True
    assert eng.state.running is True


def _stay_up_snap(session: str) -> dict:
    return {
        "account": {"netliquidation": 100000, "unrealizedpnl": 0},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": session},
        "protection": {"unprotected_symbols": []},
        "reality_pulse": {"session": {"status": session}},
        "fills": [],
    }


def _wire_stay_up_engine(monkeypatch, *, session: str, think, paper: bool = True):
    class _Conn:
        connected = True

        async def connect(self):
            return True

    async def fake_snap(_c):
        return _stay_up_snap(session)

    async def _al(*_a, **_k):
        return {"legal_symbols": [], "source": "test"}

    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: paper),
    )
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr("abcxauto.pro_engine.snap", fake_snap)
    monkeypatch.setattr(
        "abcxauto.pro_engine.GrokClient",
        lambda: SimpleNamespace(chat=object()),
    )
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._start_monitor",
        lambda self: setattr(self, "monitor", type("M", (), {"running": True})()),
    )
    monkeypatch.setattr("abcxauto.universe.refresh_legal_set", _al)
    monkeypatch.setattr("abcxauto.pro_engine.ProEngine._host_think", think)
    # Every park is floored at min_look_s (30s, env-clamped at 5s), so a test
    # clock has to go under the floor directly or one look eats the deadline.
    monkeypatch.setattr("abcxauto.park_clock.min_look_s", lambda: 0.01)


def test_session_of_snap_reads_pulse_and_hours():
    eng = ProEngine()
    assert eng._session_of_snap(_stay_up_snap("regular")) == "regular"
    assert eng._session_of_snap({"market_hours": {"session": "premarket"}}) == "premarket"
    assert (
        eng._session_of_snap({"market_hours": {"session": {"status": "closed"}}})
        == "closed"
    )


def test_a_good_paper_look_stays_up_and_sits():
    """Paper RTH / premarket stay on this process. The runner does not self-schedule."""
    for sess in ("regular", "premarket"):
        eng = ProEngine()
        wait = eng._rearm_after_think({"_failed": False}, session=sess)
        assert eng._resume_think is False, sess
        assert eng._cold_next is False, sess
        assert wait == 0.0


def test_ended_duplicate_fact_does_not_rearm_a_fresh_desk():
    """After FACT, the next look is not a go-do-desk. Wait for a poke."""
    eng = ProEngine()
    wait = eng._rearm_after_think(
        {"_ended": True, "_failed": False, "rationale": "", "sends": 0},
        session="regular",
    )
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0


def test_rearm_empty_session_during_rth_still_sits(monkeypatch):
    """A blank snap label in RTH must not sit-wake or cold-restart."""
    monkeypatch.setattr(
        "abcxauto.park_clock.infer_session_before_open",
        lambda **_k: ("", None),
    )
    monkeypatch.setattr(
        "abcxauto.opportunity_scan.rth_now",
        lambda now=None: True,
    )
    eng = ProEngine()
    wait = eng._rearm_after_think(
        {"_failed": True, "rationale": "?"},
        session="",
    )
    assert eng._last_session == "regular"
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0


def test_a_good_look_closed_and_live_do_not_rearm(monkeypatch):
    eng = ProEngine()
    wait = eng._rearm_after_think({"_failed": False}, session="closed")
    assert eng._resume_think is False
    assert wait == 0.0
    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: False),
    )
    eng = ProEngine()
    wait = eng._rearm_after_think({"_failed": False}, session="regular")
    assert eng._resume_think is False
    assert wait == 0.0


def test_rearm_junk_look_idles_without_cold_next():
    """Empty / ? retries in-chat (brain). The runner sits. Never _cold_next."""
    eng = ProEngine()
    wait = eng._rearm_after_think({"_failed": True, "rationale": "?"}, session="regular")
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0
    wait = eng._rearm_after_think({"_failed": True, "rationale": ""}, session="premarket")
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0


def test_rearm_spoken_look_is_resume_not_cold():
    """A no-send look with a real say keeps stay-up on the same chat and sits."""
    eng = ProEngine()
    wait = eng._rearm_after_think(
        {
            "_failed": False,
            "rationale": "Standing down. Watching IWM. No ticket.",
            "sends": 0,
        },
        session="regular",
    )
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0
    # A mis-tagged spoken look must not wipe the chat either.
    wait = eng._rearm_after_think(
        {
            "_failed": True,
            "rationale": "Standing down. Watching IWM. No ticket.",
            "sends": 0,
        },
        session="regular",
    )
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0


def test_rearm_send_or_fill_look_is_resume_not_cold():
    """A send/fill keeps stay-up on the same chat even if rationale is empty."""
    eng = ProEngine()
    wait = eng._rearm_after_think(
        {
            "_failed": True,
            "rationale": "",
            "sends": 1,
        },
        session="regular",
    )
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0
    wait = eng._rearm_after_think(
        {
            "_failed": True,
            "_stream_error": "stream stalled",
            "rationale": "6384 was already gone. Iron fly 6834 is working.",
            "sends": 1,
        },
        session="regular",
    )
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0


@pytest.mark.asyncio
async def test_spoken_no_send_look_sits_without_cold(monkeypatch, tmp_path):
    """A spoken stay-up look keeps the chat and sits. Next look is a poke."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    resumes: list[bool] = []

    async def think(self, n, g, s, *, resume=False):
        resumes.append(resume)
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": True,
            "rationale": "Standing down. Watching IWM. No ticket.",
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(resumes) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(resumes) == 1
    assert resumes[0] is True
    assert eng._cold_next is False
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert eng._resume_think is False


def test_rearm_failed_look_idles_without_cold_next(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_STAY_UP_RETRY_S", "30")
    eng = ProEngine()
    wait = eng._rearm_after_think(
        {"_failed": True, "_stream_error": "stream stalled"},
        session="regular",
    )
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert wait == 0.0


def test_rearm_closed_and_live_do_not(monkeypatch):
    eng = ProEngine()
    wait = eng._rearm_after_think({"_failed": False}, session="closed")
    assert eng._resume_think is False
    assert wait == 0.0
    wait = eng._rearm_after_think({"_parked": True}, session="closed")
    assert eng._resume_think is False
    assert wait == 0.0
    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: False),
    )
    eng = ProEngine()
    wait = eng._rearm_after_think({"_failed": False}, session="regular")
    assert eng._resume_think is False
    assert wait == 0.0


@pytest.mark.asyncio
async def test_host_think_surfaces_question_failed(monkeypatch):
    from abcxauto.brain import BrainTurn

    async def grok_turn(*_a, **_k):
        return BrainTurn(text="?", tool_trace=["book", "status", "playbook"])

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_turn)
    eng = ProEngine()
    eng.conn = SimpleNamespace(connected=True)
    out = await eng._host_think(1, SimpleNamespace(chat=None), _stay_up_snap("regular"))
    assert out.get("_failed") is True
    assert not out.get("_parked")


@pytest.mark.asyncio
async def test_host_think_send_or_spoken_look_is_not_failed(monkeypatch):
    from abcxauto.brain import BrainTurn

    async def grok_turn(*_a, **_k):
        return BrainTurn(
            text="",
            sends=[
                {
                    "act": {"strategy": "iron_fly", "rationale": ""},
                    "result": {"status": "submitted", "success": True},
                    "strat": "iron_fly",
                }
            ],
            last_act={"strategy": "iron_fly", "rationale": ""},
            last_result={"status": "submitted", "success": True},
            last_strat="iron_fly",
        )

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_turn)
    eng = ProEngine()
    eng.conn = SimpleNamespace(connected=True)
    out = await eng._host_think(1, SimpleNamespace(chat=None), _stay_up_snap("regular"))
    assert out.get("_failed") is False
    assert out.get("sends") == 1


@pytest.mark.asyncio
async def test_host_think_keeps_a_real_say_with_trailing_question(monkeypatch):
    from abcxauto.brain import BrainTurn

    async def grok_turn(*_a, **_k):
        return BrainTurn(
            text="I'll inspect the book, status, and playbook first.\n?",
            tool_trace=["book", "status", "playbook"],
        )

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_turn)
    eng = ProEngine()
    eng.conn = SimpleNamespace(connected=True)
    out = await eng._host_think(1, SimpleNamespace(chat=None), _stay_up_snap("regular"))
    assert out.get("_failed") is False
    assert not out.get("_parked")


@pytest.mark.asyncio
async def test_host_think_park_keeps_the_look_for_last_turn(monkeypatch):
    """A stub {_parked, cycle} left desk_brief on 'retry scans' from the last kill."""
    from abcxauto.brain import BrainTurn

    async def grok_turn(*_a, **_k):
        return BrainTurn(
            text="Gate off. Parking.",
            tool_trace=["playbook", "book", "scan", "set_wake"],
            parked=True,
        )

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_turn)
    eng = ProEngine()
    eng.conn = SimpleNamespace(connected=True)
    out = await eng._host_think(1, SimpleNamespace(chat=None), _stay_up_snap("regular"))
    assert out.get("_parked") is True
    assert out.get("_failed") is False
    assert out.get("tool_trace") == ["playbook", "book", "scan", "set_wake"]
    assert "Gate off" in str(out.get("rationale") or "")
    assert "equity" in out
    assert "pnl" in out
    assert "scan_fetched" in out
    assert "news_items" in out


@pytest.mark.asyncio
async def test_host_think_resume_sends_book_facts_not_yield_resume(monkeypatch):
    from abcxauto.brain import BrainTurn

    got: dict[str, object] = {}

    async def grok_turn(*_a, **k):
        got["wake"] = str(k.get("wake") or "")
        got["resume"] = k.get("resume")
        return BrainTurn(text="looking")

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_turn)
    eng = ProEngine()
    eng.conn = SimpleNamespace(connected=True)
    await eng._host_think(
        2,
        SimpleNamespace(chat=object()),
        _stay_up_snap("regular"),
        resume=True,
    )
    wake = str(got.get("wake") or "")
    assert got.get("resume") is True
    assert wake != "yield resume."
    assert "yield resume" not in wake
    assert "session=" in wake
    assert "flat=" in wake


@pytest.mark.asyncio
async def test_finished_rth_look_does_not_write_a_sit_clock(monkeypatch, tmp_path):
    """Done criterion: a finished paper RTH look leaves no grok_wake.json."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    stamps: list[float] = []

    async def think(self, n, g, s, *, resume=False):
        stamps.append(time.monotonic())
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "looking",
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(stamps) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(stamps) == 1
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert not (tmp_path / "wake.json").exists()


@pytest.mark.asyncio
async def test_paper_regular_stay_up_looks_without_a_clock(monkeypatch, tmp_path):
    """The process stays up. A finished look sits. Clerk writes no sit clock."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    stamps: list[float] = []
    resumes: list[bool] = []

    async def think(self, n, g, s, *, resume=False):
        stamps.append(time.monotonic())
        resumes.append(resume)
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "looking",
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(stamps) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(stamps) == 1
    assert resumes == [True]
    assert eng._resume_think is False
    assert eng._cold_next is False
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert not (tmp_path / "wake.json").exists()


@pytest.mark.asyncio
async def test_start_on_sitting_worker_enters_a_look(monkeypatch, tmp_path):
    """Operator Start on a stay-up sit must host a look without waiting PULSE_S."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "looking",
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and calls["n"] < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    sit_deadline = time.time() + 3
    while time.time() < sit_deadline:
        eng.drain_apply()
        ev = getattr(eng, "_wake_event", None)
        if calls["n"] == 1 and ev is not None and ev._waiters:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("worker never sat on _wake_event")
    worker = eng.worker
    assert eng.start() is None
    # PULSE_S is 10s. A real Start poke must look well before that.
    deadline = time.time() + 2
    while time.time() < deadline and calls["n"] < 2:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert calls["n"] == 2
    assert worker is eng.worker
    eng.stop_engine()
    eng.drain_apply()


@pytest.mark.asyncio
async def test_pause_then_start_resumes_think_path(monkeypatch, tmp_path):
    """Pause → Start on a live worker must enter another look."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "looking",
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and calls["n"] < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    sit_deadline = time.time() + 3
    while time.time() < sit_deadline:
        eng.drain_apply()
        ev = getattr(eng, "_wake_event", None)
        if calls["n"] == 1 and ev is not None and ev._waiters:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("worker never sat on _wake_event")
    worker = eng.worker
    eng.pause_engine()
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert calls["n"] == 1
    assert eng.start() is None
    deadline = time.time() + 2
    while time.time() < deadline and calls["n"] < 2:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert calls["n"] == 2
    assert worker is eng.worker
    eng.stop_engine()
    eng.drain_apply()


@pytest.mark.asyncio
async def test_launch_honors_an_overnight_park(monkeypatch, tmp_path):
    """Restart must not burn a look through an overnight park."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    from abcxauto.park_clock import set_wake

    set_wake(wake_in_s=30, session="closed", flat=True)
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {"cycle": n, "pnl": 0, "equity": 100000, "_failed": False}

    _wire_stay_up_engine(monkeypatch, session="closed", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 1.2
    while time.time() < deadline:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_launch_ignores_a_leftover_rth_clock(monkeypatch, tmp_path):
    """A leftover grok_wake.json from the old RTH launcher must not sit Start."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    from datetime import datetime, timedelta, timezone

    from abcxauto.park_clock import GrokAlarm, save_alarm

    later = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    save_alarm(GrokAlarm(wake_at=later, set_at=later))
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {"cycle": n, "pnl": 0, "equity": 100000, "_failed": False}

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 2
    while time.time() < deadline and calls["n"] < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_a_grok_park_in_rth_stay_up_writes_no_sit_clock(monkeypatch, tmp_path):
    """RTH has no sit clock. A parked flag still stay-ups on this process."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_parked": True,
            "rationale": "napping",
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and calls["n"] < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert calls["n"] == 1
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert eng._think_parked is False
    assert eng._cold_next is False


@pytest.mark.asyncio
async def test_paper_premarket_stay_up_writes_no_sit_clock(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "premarket looking",
        }

    _wire_stay_up_engine(monkeypatch, session="premarket", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and calls["n"] < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert calls["n"] == 1
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None



@pytest.mark.asyncio
async def test_junk_look_idles_without_cold_restart(monkeypatch, tmp_path):
    """After empty/? the host sits in the same chat. No sit clock. No _cold_next."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    times: list[float] = []
    resumes: list[bool] = []
    ensure_calls: list[object] = []

    def boom_ensure(*_a, **_k):
        ensure_calls.append(_k)
        raise AssertionError("junk RTH look must not call ensure_next_look")

    async def think(self, n, g, s, *, resume=False):
        times.append(time.monotonic())
        resumes.append(resume)
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": True,
            "rationale": "?",
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    monkeypatch.setattr("abcxauto.park_clock.ensure_next_look", boom_ensure)
    monkeypatch.setattr("abcxauto.park_clock.set_wake", boom_ensure)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(times) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(times) == 1
    assert resumes == [True]
    assert eng._cold_next is False
    assert eng._resume_think is False
    assert ensure_calls == []
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert not getattr(eng, "_session_capped", False)


@pytest.mark.asyncio
async def test_failed_look_idles_without_set_wake_clock(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_STAY_UP_RETRY_S", "0.4")
    times: list[float] = []

    async def think(self, n, g, s, *, resume=False):
        times.append(time.monotonic())
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": True,
            "_stream_error": "stream stalled",
            "rationale": "?",
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(times) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(times) == 1
    assert eng._cold_next is False
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert not getattr(eng, "_session_capped", False)


@pytest.mark.asyncio
async def test_closed_session_skips_grok_and_keeps_a_clock(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {"_parked": True, "cycle": n}

    _wire_stay_up_engine(monkeypatch, session="closed", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 2
    while time.time() < deadline:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert calls["n"] == 0
    assert eng._think_parked is False
    assert eng.state.autonomous is True
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at
    eng.stop_engine()
    eng.drain_apply()


@pytest.mark.asyncio
async def test_closed_session_skip_drops_live_chat(monkeypatch, tmp_path):
    """Overnight skip is a park: the next think must not resume yesterday's chat."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    brains: list[SimpleNamespace] = []

    def make_g():
        g = SimpleNamespace(chat=object(), _wake_n=1)
        brains.append(g)
        return g

    async def think(self, n, g, s, *, resume=False):
        return {"_parked": True, "cycle": n}

    _wire_stay_up_engine(monkeypatch, session="closed", think=think)
    monkeypatch.setattr("abcxauto.pro_engine.GrokClient", make_g)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 2
    while time.time() < deadline:
        eng.drain_apply()
        await asyncio.sleep(0.05)
        if brains and brains[0].chat is None:
            break
    eng.stop_engine()
    eng.drain_apply()
    assert brains
    assert brains[0].chat is None
    assert brains[0]._wake_n == 0


@pytest.mark.asyncio
async def test_fill_poke_after_sit_opens_a_look(monkeypatch, tmp_path):
    """Stay-up sits after a look. A fill poke starts the next one."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    resumes: list[bool] = []

    async def think(self, n, g, s, *, resume=False):
        from abcxauto.park_clock import take_interrupt

        take_interrupt()
        resumes.append(resume)
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "watching the book",
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(resumes) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert len(resumes) == 1
    idle_until = time.time() + 0.25
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert len(resumes) == 1
    assert eng._resume_think is False
    _poke_book(eng, "fill", "QQQ")
    deadline = time.time() + 4
    while time.time() < deadline and len(resumes) < 2:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(resumes) == 2
    assert eng._cold_next is False


@pytest.mark.asyncio
async def test_pulse_timeout_unchanged_wom_set_sits(monkeypatch, tmp_path):
    """After a look, 10s+ with no poke and unchanged WOM → zero extra looks."""
    from abcxauto.park_clock import PULSE_S

    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    wom = "fact: working_order_missing QQQ 260918C500 long 1,SPY STK long 10."
    resumes: list[bool] = []

    async def think(self, n, g, s, *, resume=False):
        from abcxauto.park_clock import take_interrupt

        take_interrupt()
        resumes.append(resume)
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "watching the book",
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)

    async def fake_snap(_c):
        return _stay_up_snap("regular")

    monkeypatch.setattr("abcxauto.agent_loop.snap", fake_snap)
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: wom,
    )
    chat = SimpleNamespace(_abcx_last_desk_fact=wom)
    monkeypatch.setattr(
        "abcxauto.pro_engine.GrokClient",
        lambda: SimpleNamespace(chat=chat, _last_desk_fact=wom),
    )

    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(resumes) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert len(resumes) == 1
    ev = getattr(eng, "_wake_event", None)
    loop = getattr(eng, "_worker_loop", None)
    if loop is not None and ev is not None:
        # halt / flat_confirmed: event with no live poke must sit.
        loop.call_soon_threadsafe(ev.set)
    idle_until = time.time() + float(PULSE_S) + 1.5
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(resumes) == 1
    assert eng._cold_next is False
    assert eng._resume_think is False


@pytest.mark.asyncio
async def test_stay_up_lead_changed_detects_wom_set_identity(monkeypatch):
    from abcxauto.pro_engine import ProEngine
    from abcxauto.world_state import WAKE_FACT_PREFIX

    prev = f"{WAKE_FACT_PREFIX} working_order_missing QQQ 260918C500 long 1."
    same = (
        f"{WAKE_FACT_PREFIX} working_order_missing QQQ 260918C500 long 1."
    )
    changed = (
        f"{WAKE_FACT_PREFIX} working_order_missing "
        "QQQ 260918C500 long 1,SPY STK long 10."
    )

    async def fake_snap(_c):
        return _stay_up_snap("regular")

    monkeypatch.setattr("abcxauto.agent_loop.snap", fake_snap)
    eng = ProEngine()
    eng.conn = SimpleNamespace(connected=True)
    g = SimpleNamespace(
        chat=SimpleNamespace(**{"_abcx_last_desk_fact": prev}),
        _last_desk_fact=prev,
    )
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: same,
    )
    assert await eng._stay_up_lead_changed(g) is False
    monkeypatch.setattr(
        "abcxauto.world_state.worst_wake_fact",
        lambda **_k: changed,
    )
    assert await eng._stay_up_lead_changed(g) is True


@pytest.mark.asyncio
async def test_live_regular_does_not_rearm(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "live look",
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think, paper=False)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 6
    while time.time() < deadline and calls["n"] < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert calls["n"] == 1
    await asyncio.sleep(0.45)
    eng.drain_apply()
    assert calls["n"] == 1
    assert eng._resume_think is False
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    eng.stop_engine()
    eng.drain_apply()


def test_settings_change_rebuilds_brain_and_monitor(monkeypatch):
    """Pro Settings must land without a restart: both fingerprints move."""

    class _Box:
        model = "grok-4.6"
        temperature = 0.3
        max_tokens = 8192
        monitor_enabled = True
        monitor_poll_s = 30
        monitor_review_s = 300
        monitor_extended_hours = False

    box = _Box()
    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: box)
    eng = ProEngine()
    brain, mon = eng._brain_fingerprint(), eng._monitor_fingerprint()
    assert eng._brain_key == ()  # nothing built yet

    box.model = "grok-4.6-fast"
    assert eng._brain_fingerprint() != brain
    assert eng._monitor_fingerprint() == mon  # brain change must not churn the monitor

    box.monitor_poll_s = 60
    assert eng._monitor_fingerprint() != mon

    eng._monitor_key = eng._monitor_fingerprint()
    eng._stop_monitor()
    assert eng._monitor_key == ()  # a stopped monitor cannot look current


@pytest.mark.asyncio
async def test_empty_grok_live_worker_reenters_think_without_cold(monkeypatch, tmp_path):
    """Empty GROK after tools + live worker: same-chat recover. No _cold_next."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0.01")
    resumes: list[bool] = []
    traces: list[list[str]] = []

    async def think(self, n, g, s, *, resume=False):
        resumes.append(resume)
        if len(resumes) == 1:
            traces.append(["book", "quote"])
            return {
                "cycle": n,
                "pnl": 0,
                "equity": 100000,
                "_failed": True,
                "rationale": "",
                "tool_trace": ["book", "quote"],
                "sends": 0,
            }
        traces.append(["book", "quote"])
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "holding IWM vert. No ticket.",
            "tool_trace": ["book", "quote"],
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(resumes) < 2:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.3
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(resumes) == 2
    assert resumes[0] is True
    assert resumes[1] is True
    assert traces[0] == ["book", "quote"]
    assert eng._cold_next is False
    assert eng._recover_same_chat is False
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None


@pytest.mark.asyncio
async def test_empty_grok_after_send_live_worker_reenters_without_cold(
    monkeypatch, tmp_path
):
    """Empty GROK after send on a live worker: same-chat recover. No _cold_next."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0.01")
    resumes: list[bool] = []

    async def think(self, n, g, s, *, resume=False):
        resumes.append(resume)
        if len(resumes) == 1:
            self.state.think_live = "--- GROK ---\n[send]\n--- GROK ---\n"
            return {
                "cycle": n,
                "pnl": 0,
                "equity": 100000,
                "_failed": False,
                "_empty_grok": True,
                "rationale": "QQQ calendar",
                "tool_trace": ["send"],
                "sends": 5,
            }
        self.state.think_live = (
            "--- GROK ---\n[send]\n--- GROK ---\n[say]\n"
            "QQQ calendar working. Watching the fill.\n"
        )
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "QQQ calendar working. Watching the fill.",
            "tool_trace": ["send"],
            "sends": 5,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(resumes) < 2:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.3
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(resumes) == 2
    assert resumes[0] is True
    assert resumes[1] is True
    assert eng._cold_next is False
    assert eng._recover_same_chat is False
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None


def test_same_chat_recover_needed_is_empty_after_tools_not_spoken():
    eng = ProEngine()
    g = SimpleNamespace(chat=object())
    assert (
        eng._same_chat_recover_needed(
            {"rationale": "", "tool_trace": ["book", "quote"], "sends": 0},
            g,
        )
        is True
    )
    assert (
        eng._same_chat_recover_needed(
            {
                "_stream_error": "StatusCode.UNAVAILABLE connection aborted",
                "rationale": "",
                "sends": 0,
            },
            g,
        )
        is True
    )
    assert (
        eng._same_chat_recover_needed(
            {"rationale": "holding IWM. Watching the book.", "sends": 0},
            g,
        )
        is False
    )
    assert eng._same_chat_recover_needed(
        {"rationale": "", "tool_trace": ["book"], "sends": 0},
        SimpleNamespace(chat=None),
    ) is False
    # Production 2026-09-01: send_calls>0 then bare --- GROK ---. #147
    # returned False here and stay-up sat.
    assert (
        eng._same_chat_recover_needed(
            {
                "rationale": "QQQ calendar",
                "tool_trace": ["send"],
                "sends": 5,
                "_empty_grok": True,
            },
            g,
        )
        is True
    )
    eng.state.think_live = "--- GROK ---\n[send]\n--- GROK ---\n"
    assert (
        eng._same_chat_recover_needed(
            {
                "rationale": "QQQ calendar",
                "tool_trace": ["send"],
                "sends": 5,
            },
            g,
        )
        is True
    )
    eng.state.think_live = (
        "--- GROK ---\n[send]\n--- GROK ---\n[say]\nQQQ calendar working\n"
    )
    assert (
        eng._same_chat_recover_needed(
            {
                "rationale": "QQQ calendar working. Watching the fill.",
                "tool_trace": ["send"],
                "sends": 5,
            },
            g,
        )
        is False
    )
    eng._cold_next = True
    eng._arm_same_chat_recover()
    assert eng._cold_next is False
    assert eng._resume_think is True
    assert eng._recover_same_chat is True
