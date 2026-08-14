"""Adaptive pacing: tiers, wakes, market-rhythm sleep (not Act thrift)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.pacing import (
    PaceFacts,
    WakeGate,
    allow_grok_call,
    compute_pace,
    facts_from_cycle,
    wait_for_pace,
)


def _cfg(**kw):
    base = dict(
        cycle_sleep_s=120.0,
        grok_min_interval_s=120.0,
        pace_protect_s=20.0,
        pace_manage_s=60.0,
        pace_idle_s=240.0,
        risk_posture="balanced",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_protect_beats_idle():
    d = compute_pace(
        PaceFacts(
            needs_protection=True,
            flat=False,
            last_stance="idle",
            session_status="regular",
        ),
        _cfg(),
    )
    assert d.tier == "protect"
    assert d.sleep_s == 20.0
    assert d.bypass_grok_min is True


def test_closed_stretches():
    d = compute_pace(
        PaceFacts(session_status="closed", flat=True),
        _cfg(cycle_sleep_s=120),
    )
    assert d.tier == "closed"
    assert d.sleep_s >= 900.0


def test_premarket_paper_uses_prep_cadence():
    d = compute_pace(
        PaceFacts(session_status="premarket", flat=True, open_count=0),
        _cfg(trading_mode="paper", cycle_sleep_s=300, pace_spinup_s=15),
    )
    assert d.tier == "spinup"
    assert d.sleep_s == 15.0
    assert d.reason == "extended_prep"
    assert d.bypass_grok_min is True


def test_postmarket_open_book_manages():
    d = compute_pace(
        PaceFacts(
            session_status="postmarket",
            flat=False,
            has_open_risk=True,
            open_count=9,
        ),
        _cfg(trading_mode="paper", pace_manage_s=60, pace_spinup_s=15),
    )
    assert d.tier == "manage"
    assert d.reason == "extended_open_risk"


def test_hunt_uses_cycle_floor():
    d = compute_pace(
        PaceFacts(
            flat=True,
            features_present=True,
            posture="aggressive",
            last_stance="hunt",
            session_status="regular",
        ),
        _cfg(trading_mode="live", cycle_sleep_s=120),
    )
    assert d.tier == "hunt"
    assert d.sleep_s == 120.0


def test_idle_stance_longer_sleep():
    d = compute_pace(
        PaceFacts(
            flat=True,
            features_present=True,
            posture="balanced",
            last_stance="idle",
            session_status="regular",
        ),
        _cfg(trading_mode="live"),
    )
    assert d.tier == "idle"
    assert d.sleep_s >= 240.0


def test_paper_defensive_flat_uses_spinup_cadence():
    """0–1 positions on paper RTH: fast research, not 5-minute idle."""
    d = compute_pace(
        PaceFacts(
            flat=True,
            features_present=True,
            posture="defensive",
            last_stance="idle",
            session_status="regular",
            open_count=0,
        ),
        _cfg(trading_mode="paper", cycle_sleep_s=300, pace_spinup_s=15),
    )
    assert d.tier == "spinup"
    assert d.sleep_s == 15.0
    assert d.bypass_grok_min is True
    assert d.reason == "spinup_research"


def test_paper_one_position_still_spinup():
    d = compute_pace(
        PaceFacts(
            flat=False,
            has_open_risk=True,
            open_count=1,
            posture="defensive",
            session_status="regular",
        ),
        _cfg(trading_mode="paper", pace_spinup_s=15, pace_manage_s=60),
    )
    assert d.tier == "spinup"
    assert d.sleep_s == 15.0


def test_paper_two_positions_still_spinup():
    d = compute_pace(
        PaceFacts(
            flat=False,
            has_open_risk=True,
            open_count=2,
            needs_protection=False,
            session_status="regular",
        ),
        _cfg(trading_mode="paper", pace_manage_s=60, pace_spinup_s=15),
    )
    assert d.tier == "spinup"
    assert d.sleep_s == 15.0


def test_paper_full_book_manages():
    d = compute_pace(
        PaceFacts(
            flat=False,
            has_open_risk=True,
            open_count=9,
            needs_protection=False,
            session_status="regular",
        ),
        _cfg(trading_mode="paper", pace_manage_s=60),
    )
    assert d.tier == "manage"
    assert d.sleep_s == 60.0


def test_manage_open_risk():
    d = compute_pace(
        PaceFacts(
            flat=False,
            has_open_risk=True,
            open_count=2,
            needs_protection=False,
            last_stance="manage",
            session_status="regular",
        ),
        _cfg(trading_mode="live"),
    )
    assert d.tier == "manage"
    assert d.sleep_s == 60.0


def test_grok_min_blocks_idle_allows_protect():
    ok, why = allow_grok_call(
        tier="idle",
        wake_reason="",
        last_grok_mono=100.0,
        now_mono=150.0,
        grok_min_interval_s=120.0,
    )
    assert ok is False
    assert why == "pace_budget"
    ok2, why2 = allow_grok_call(
        tier="protect",
        wake_reason="",
        last_grok_mono=100.0,
        now_mono=150.0,
        grok_min_interval_s=120.0,
    )
    assert ok2 is True
    assert why2 == "urgent"
    ok3, _ = allow_grok_call(
        tier="hunt",
        wake_reason="unprotected",
        last_grok_mono=100.0,
        now_mono=150.0,
        grok_min_interval_s=120.0,
    )
    assert ok3 is True
    ok4, why4 = allow_grok_call(
        tier="spinup",
        wake_reason="",
        last_grok_mono=100.0,
        now_mono=105.0,
        grok_min_interval_s=300.0,
    )
    assert ok4 is True
    assert why4 == "spinup"


def test_wake_whitelist_and_debounce():
    g = WakeGate(debounce_s=15.0)
    assert g.try_wake("news", now_mono=1.0) is False
    assert g.try_wake("fill", now_mono=10.0) is True
    assert g.try_wake("fill", now_mono=12.0) is False
    assert g.try_wake("fill", now_mono=30.0) is True
    assert g.try_wake("unprotected", now_mono=31.0) is True
    assert g.try_wake("unprotected", now_mono=31.5) is True  # urgent always


def test_facts_from_cycle():
    facts = facts_from_cycle(
        {
            "world_state": {
                "needs_protection": True,
                "flat": False,
                "unprotected": ["IWM"],
                "effective_posture": "aggressive",
                "session_status": "regular",
            },
            "judgment": {"stance": "protect"},
            "opportunities": [{"symbol": "QQQ"}],
            "positions": [{"symbol": "IWM", "quantity": 100, "sec_type": "STK"}],
            "trade_plan": {"symbol": "IWM"},
        },
        wake_reason="unprotected",
    )
    assert facts.needs_protection is True
    assert facts.has_open_risk is True
    assert facts.wake_reason == "unprotected"


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
            trading_mandate="RELY ON YOUR INTELLIGENCE.",
            trading_mode="paper",
            grok_min_interval_s=0,
            signal_only=False,
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: SimpleNamespace(
            trading_mandate="RELY ON YOUR INTELLIGENCE.",
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
    monkeypatch.setattr(
        "abcxauto.news_feed.fetch_agent_news", _empty
    )
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
    assert out["strat"] == "blocked"
    assert "hold_forbidden" in str(out["result"].get("note") or "").lower()


@pytest.mark.asyncio
async def test_protect_still_calls_act(monkeypatch, tmp_path):
    from abcxauto.agent_loop import run_cycle

    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mandate="RELY ON YOUR INTELLIGENCE.",
            trading_mode="paper",
            grok_min_interval_s=0,
            signal_only=False,
            risk_posture="balanced",
        ),
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: SimpleNamespace(
            trading_mandate="RELY ON YOUR INTELLIGENCE.",
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
    judgment = {
        "stance": "protect",
        "thesis": "Need stop on IWM.",
        "focus": "Unprotected STK.",
        "dismissed": "",
        "intent": {
            "kind": "protect", "symbol": "IWM", "direction": "LONG", "urgency": "high",
        },
        "risk_budget_pct": 0.5,
        "regime_fit": True,
        "setup_grade": "A",
    }
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
    """Judge manage + hold intent → Act still runs (no thrift skip)."""
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
        trading_mandate="RELY ON YOUR INTELLIGENCE.",
        trading_mode="paper",
        grok_min_interval_s=0,
        signal_only=False,
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
    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _empty)

    calls: list[str] = []
    judgment = {
        "stance": "manage",
        "thesis": "Hold SPY with stop working.",
        "focus": "Protected.",
        "dismissed": "",
        "intent": {
            "kind": "hold", "symbol": "SPY", "direction": "LONG", "urgency": "low",
        },
        "risk_budget_pct": 0.5,
        "regime_fit": True,
        "setup_grade": "B",
    }

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
