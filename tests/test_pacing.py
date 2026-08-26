"""Overnight park clock + book-event pulse. RTH has no sit clock."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from abcxauto.pacing import STAY_UP_RETRY_CAP_S, WakeGate, stay_up_retry_s, wait_for_pace


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


def test_stay_up_retry_cap_is_tens_of_seconds():
    """Noah rejected ~30 min windows. The cap is the 20–45s class."""
    assert 20.0 <= STAY_UP_RETRY_CAP_S <= 45.0


def test_stay_up_retry_caps_thirty_minute_window():
    assert stay_up_retry_s(30 * 60) == STAY_UP_RETRY_CAP_S
    assert stay_up_retry_s(1800.0) == STAY_UP_RETRY_CAP_S
    assert stay_up_retry_s(33 * 60) == STAY_UP_RETRY_CAP_S
    assert stay_up_retry_s(float("inf")) == 0.0


def test_stay_up_retry_caps_bell_style_remaining():
    """9:03 → 9:33 is a 30-minute remaining-to-bell. Pace cannot sit that out."""
    from datetime import datetime

    now = datetime(2026, 8, 25, 9, 3, 0)
    bell = datetime(2026, 8, 25, 9, 33, 0)
    remaining = (bell - now).total_seconds()
    assert remaining == 1800.0
    assert stay_up_retry_s(remaining) == STAY_UP_RETRY_CAP_S

    earlier = datetime(2026, 8, 25, 8, 0, 0)
    assert stay_up_retry_s((bell - earlier).total_seconds()) == STAY_UP_RETRY_CAP_S


def test_stay_up_retry_keeps_short_waits():
    assert stay_up_retry_s(8.0) == 8.0
    assert stay_up_retry_s(20.0) == 20.0
    assert stay_up_retry_s(44.0) == 44.0
    assert stay_up_retry_s(45.0) == STAY_UP_RETRY_CAP_S
    assert stay_up_retry_s(45.1) == STAY_UP_RETRY_CAP_S
    assert stay_up_retry_s(0.0) == 0.0
    assert stay_up_retry_s(-12.0) == 0.0
    assert stay_up_retry_s("nope") == 0.0  # type: ignore[arg-type]
    assert stay_up_retry_s(None) == 0.0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wait_for_pace_cannot_park_thirty_minutes(monkeypatch):
    import time

    import abcxauto.pacing as pacing

    monkeypatch.setattr(pacing, "STAY_UP_RETRY_CAP_S", 0.08)
    ev = asyncio.Event()
    t0 = time.monotonic()
    result = await wait_for_pace(1800.0, ev, chunk_s=0.02)
    elapsed = time.monotonic() - t0
    assert result == ""
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_wait_for_pace_wakes_even_when_asked_to_park():
    import time

    ev = asyncio.Event()
    ev.set()
    t0 = time.monotonic()
    result = await wait_for_pace(1800.0, ev, chunk_s=0.05)
    assert result == "woken"
    assert time.monotonic() - t0 < 1.0


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
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)

    from abcxauto.brain import BrainTurn

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

    async def grok_turn(*_a, **_k):
        return BrainTurn(tool_trace=["book"], text="act yield")

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_turn)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] != "hold"
    assert out["strat"] != "blocked"


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
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)

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

    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)

    cfg = SimpleNamespace(
        trading_mode="paper",
        risk_posture="aggressive",
    )
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.world_state.get_config", lambda: cfg)

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

    from abcxauto.brain import BrainTurn

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

    async def grok_turn(_g, *, connector, world, snap, wake=""):
        calls.append(wake)
        return BrainTurn(tool_trace=["book"], text="manage book — stop working")

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", grok_turn)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert calls
    assert out["strat"] != "hold"
    assert out["strat"] != "blocked"
