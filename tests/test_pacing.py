"""Wake-bus pulse: debounce + interruptible sleep."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.pacing import WakeGate, wait_for_pace


def test_wake_whitelist_and_debounce():
    g = WakeGate(debounce_s=15.0)
    assert g.try_wake("news", now_mono=1.0) is False
    assert g.try_wake("fill", now_mono=10.0) is True
    assert g.try_wake("fill", now_mono=12.0) is False
    assert g.try_wake("fill", now_mono=30.0) is True
    assert g.try_wake("unprotected", now_mono=31.0) is True
    assert g.try_wake("unprotected", now_mono=31.5) is True  # urgent always


@pytest.mark.asyncio
async def test_wait_for_pace_wakes():
    import asyncio

    ev = asyncio.Event()

    async def setter():
        await asyncio.sleep(0.05)
        ev.set()

    asyncio.create_task(setter())
    result = await wait_for_pace(5.0, ev, chunk_s=0.05)
    assert result == "woken"


@pytest.mark.asyncio
async def test_idle_still_runs_act(monkeypatch, tmp_path):
    from abcxauto.agent_loop import run_cycle

    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mode="paper",
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: SimpleNamespace(
            trading_mode="paper",
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.connection_status",
        lambda _c=None: {
            "ibkr_connected": True,
            "mda_configured": False,
            "trading_mode": "paper",
        },
    )

    async def _tool(_c, name: str, _a=None):
        return {
            "account_summary": {"netliquidation": 1000, "unrealizedpnl": 0},
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    monkeypatch.setattr("abcxauto.agent_loop._tool", _tool)

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _empty)
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    from abcxauto.memory import reset_journal
    from abcxauto.world_state import reset_idle_streak

    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)
    reset_idle_streak()

    from tests.conftest import fake_grok_turn

    class FakeConnector:
        connected = True

        async def connect(self):
            return True

        async def get_positions(self):
            return []

        async def get_open_orders(self):
            return []

        async def get_account_summary(self):
            return {"netliquidation": 1000, "unrealizedpnl": 0}

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        fake_grok_turn({"action": "hold", "strategy": "hold", "rationale": "act hold"}),
    )
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "hold"


@pytest.mark.asyncio
async def test_protect_still_calls_act(monkeypatch, tmp_path):
    from abcxauto.agent_loop import run_cycle

    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mode="paper",
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: SimpleNamespace(
            trading_mode="paper",
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.connection_status",
        lambda _c=None: {
            "ibkr_connected": True,
            "mda_configured": False,
            "trading_mode": "paper",
        },
    )

    pos = [{
        "symbol": "IWM", "quantity": 10, "sec_type": "STK",
        "conId": 1, "avg_cost": 200.0,
    }]

    async def _tool(_c, name: str, _a=None):
        return {
            "account_summary": {"netliquidation": 10000, "unrealizedpnl": 0},
            "positions": pos,
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "IWM", "last": 200},
        }.get(name, {})

    monkeypatch.setattr("abcxauto.agent_loop._tool", _tool)

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _empty)
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    from abcxauto.memory import reset_journal
    from abcxauto.world_state import reset_idle_streak

    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)
    reset_idle_streak()

    calls: list[str] = []
    act = {
        "action": "hold",
        "strategy": "hold",
        "rationale": "waiting for levels",
    }

    from tests.conftest import fake_grok_turn

    class FakeConnector:
        connected = True

        async def connect(self):
            return True

        async def get_positions(self):
            return list(pos)

        async def get_open_orders(self):
            return []

        async def get_account_summary(self):
            return {"netliquidation": 10000, "unrealizedpnl": 0}

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        fake_grok_turn(act, wakes=calls),
    )

    async def boom_send(*_a, **_k):
        return {"status": "blocked", "note": "test"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert calls
    assert out["strat"] == "blocked"


@pytest.mark.asyncio
async def test_manage_hold_still_runs_act(monkeypatch, tmp_path):
    from abcxauto.agent_loop import run_cycle
    from abcxauto.memory import reset_journal
    from abcxauto.world_state import reset_idle_streak

    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)
    reset_idle_streak()

    cfg = SimpleNamespace(
        trading_mode="paper",
        risk_posture="aggressive",
    )
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.world_state.get_config", lambda: cfg)
    monkeypatch.setattr(
        "abcxauto.agent_loop.connection_status",
        lambda _c=None: {
            "ibkr_connected": True,
            "mda_configured": False,
            "trading_mode": "paper",
        },
    )

    pos = [{
        "symbol": "SPY", "quantity": 8, "sec_type": "STK",
        "conId": 1, "avg_cost": 745.80,
    }]
    stop = {
        "symbol": "SPY",
        "sec_type": "STK",
        "order_type": "STP",
        "action": "SELL",
        "aux_price": 737.34,
        "quantity": 8,
        "conId": 1,
    }

    async def _tool(_c, name: str, _a=None):
        return {
            "account_summary": {"netliquidation": 10000, "unrealizedpnl": -7},
            "positions": pos,
            "open_orders": [stop],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 746.0},
        }.get(name, {})

    monkeypatch.setattr("abcxauto.agent_loop._tool", _tool)

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _empty)

    calls: list[str] = []

    from tests.conftest import fake_grok_turn

    class FakeConnector:
        connected = True

        async def connect(self):
            return True

        async def get_positions(self):
            return list(pos)

        async def get_open_orders(self):
            return [stop]

        async def get_account_summary(self):
            return {"netliquidation": 10000, "unrealizedpnl": -7}

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        fake_grok_turn({
            "action": "hold",
            "strategy": "hold",
            "rationale": "manage book — stop working",
        }, wakes=calls),
    )
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert calls
    assert out["strat"] == "hold"
