"""Smoke tests for the Grok-tool wake + clerk gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.agent_loop import (
    ALLOWED_ACTIONS,
    _wake_grok_for_session,
    gate_ticket,
    is_new_risk,
    normalize_action,
    run_cycle,
    snap,
    stance_from_book,
    turn_did_work,
)
from abcxauto.brain import brain_system_prompt
from abcxauto.world_state import WorldState
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


async def _fake_tool(_c, name: str, _a=None):
    return {
        "account_summary": {"netliquidation": 1000, "unrealizedpnl": 0},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": "regular"},
        "quote": {"symbol": "SPY", "last": 500},
    }.get(name, {})


def _hold_act(rationale: str = "flat"):
    return {"action": "hold", "strategy": "hold", "rationale": rationale}


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={"max_risk_per_trade_pct": 1.0},
        envelope={},
        regime={"session_phase": "mid", "trend_bias": "mixed", "vol_proxy": "normal"},
        portfolio_risk={"n_positions": 0, "top_symbol": "", "top_concentration_pct": 0},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mode="paper",
            risk_posture="balanced",
            is_paper=True,
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
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _empty)
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)


@pytest.mark.asyncio
async def test_hold_path_skips_send(monkeypatch):
    """Live: hold is valid and does not send."""
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mode="live",
            risk_posture="balanced",
            is_paper=False,
        ),
    )
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    send_calls: list = []

    async def boom_send(*_a, **_k):
        send_calls.append(1)
        raise AssertionError("send_action must not run on hold")

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", fake_grok_turn(_hold_act()))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "hold"
    assert out["result"]["status"] == "hold"
    assert send_calls == []
    assert "world_state" in out


@pytest.mark.asyncio
async def test_paper_flat_rth_hold_does_not_send(monkeypatch):
    send_calls: list = []

    async def boom_send(*_a, **_k):
        send_calls.append(1)
        raise AssertionError("send_action must not run on blocked hold")

    monkeypatch.setattr(
        "abcxauto.agent_loop.build_world_state",
        lambda **_k: _world(flat=True, session_status="regular"),
    )
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", fake_grok_turn(_hold_act()))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "blocked"
    assert "paper hunt" in str((out.get("result") or {}).get("note") or out.get("validation") or "")
    assert send_calls == []


def test_paper_flat_rth_hold_is_blocked():
    strat, forced = gate_ticket(_hold_act(), _world(flat=True, session_status="regular"))
    assert strat == "blocked"
    assert "paper hunt" in str((forced or {}).get("note") or "")


def test_paper_open_book_hold_is_allowed():
    strat, forced = gate_ticket(
        _hold_act(),
        _world(flat=False, session_status="regular", positions=[{"symbol": "SPY"}]),
    )
    assert strat == "hold"
    assert forced is None


def test_live_flat_rth_hold_is_allowed(monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    strat, forced = gate_ticket(_hold_act(), _world(flat=True, session_status="regular"))
    assert strat == "hold"
    assert forced is None


def test_paper_halted_flat_hold_is_allowed(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_risk_gate",
        lambda: type("G", (), {"is_halted": True})(),
    )
    strat, forced = gate_ticket(
        _hold_act(),
        _world(flat=True, session_status="regular", book={"halted": True}),
    )
    assert strat == "hold"
    assert forced is None


def test_brain_system_has_order_examples():
    text = brain_system_prompt().lower()
    assert "order examples" in text
    assert "clock in" not in text
    assert "mandate:" not in text
    assert "the operator gives" not in text


def test_normalize_noop_to_hold():
    strat, forced = normalize_action({"action": "noop"})
    assert strat == "hold"
    assert forced is None
    assert "hold" in ALLOWED_ACTIONS
    assert "self_tune" in ALLOWED_ACTIONS


@pytest.mark.asyncio
async def test_snap_has_reality_pulse():
    out = await snap(FakeConnector())
    assert "reality_pulse" in out
    assert "portfolio_state" in out
    assert out["ibkr_live_quotes"].get("SPY") == 500


@pytest.mark.asyncio
async def test_hunt_quote_ignores_mda_tape():
    from abcxauto.agent_loop import _quote_for_action

    act = {
        "strategy": "market_bracket",
        "params": {"symbol": "QQQ", "price_hint": 100.0, "entry_price": 101.0},
    }
    snap_d = {
        "opportunities": [{"symbol": "QQQ", "mda_last": 555.0, "last": 555.0}],
        "ibkr_live_quotes": {},
        "spy_quote": {"last": 500},
    }
    got = await _quote_for_action(act, snap_d, connector=None)
    assert got is None


def test_unprotected_blocks_hold_and_new_risk():
    world = _world(needs_protection=True, unprotected=["SPY"], flat=False)
    strat, forced = gate_ticket({"action": "hold", "strategy": "hold"}, world)
    assert strat == "blocked"
    assert "hold_forbidden" in str(forced.get("note") or "")
    strat2, forced2 = gate_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {"symbol": "QQQ", "quantity": 1, "direction": "LONG"},
        },
        world,
    )
    assert strat2 == "blocked"
    assert "protect" in str(forced2.get("note") or "").lower()


def test_vertical_spread_close_is_not_new_risk_when_unprotected():
    world = _world(
        needs_protection=True,
        unprotected=["JPM 260918C370.0 long 1"],
        flat=False,
        positions=[
            {
                "symbol": "JPM",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260918",
                "right": "C",
                "strike": 370.0,
                "conId": 1,
            },
            {
                "symbol": "JPM",
                "secType": "OPT",
                "quantity": -1,
                "expiration": "20260918",
                "right": "C",
                "strike": 375.0,
                "conId": 2,
            },
        ],
    )
    assert is_new_risk("vertical_spread") is True
    assert is_new_risk("vertical_spread", {"closing_position": True}) is False
    assert is_new_risk("iron_condor", {"closing_position": True}) is False
    assert is_new_risk("calendar_spread", {"closing_position": True}) is False
    assert is_new_risk("straddle", {"closing_position": True}) is False
    strat, forced = gate_ticket(
        {
            "action": "vertical_spread",
            "strategy": "vertical_spread",
            "params": {
                "symbol": "JPM",
                "expiration": "20260918",
                "long_strike": 370.0,
                "short_strike": 375.0,
                "right": "C",
                "quantity": 1,
                "limit_price": 0.71,
                "closing_position": True,
            },
        },
        world,
    )
    assert strat == "vertical_spread"
    assert forced is None
    blocked, note = gate_ticket(
        {
            "action": "close_option",
            "strategy": "close_option",
            "params": {"conId": 1, "quantity": 1, "symbol": "JPM"},
        },
        world,
    )
    assert blocked == "blocked"
    assert "combo" in str((note or {}).get("note") or "").lower() or "BAG" in str((note or {}).get("note") or "")


def test_paper_may_not_hold_when_flat_rth(monkeypatch):
    world = _world(flat=True, session_status="regular")
    strat, forced = gate_ticket(_hold_act(), world)
    assert strat == "blocked"
    assert "paper hunt" in str((forced or {}).get("note") or "")


def test_wake_grok_for_session():
    assert _wake_grok_for_session("premarket", needs_prot=False) is True
    assert _wake_grok_for_session("regular", needs_prot=False) is True
    assert _wake_grok_for_session("postmarket", needs_prot=False) is False
    assert _wake_grok_for_session("closed", needs_prot=False) is False
    assert _wake_grok_for_session("", needs_prot=False) is False
    assert _wake_grok_for_session("closed", needs_prot=True) is True
    assert _wake_grok_for_session("premarket", needs_prot=True) is True


@pytest.mark.asyncio
async def test_run_cycle_skips_when_ibkr_down(monkeypatch):
    called: list[int] = []

    async def boom(*_a, **_k):
        called.append(1)
        raise AssertionError("grok must not run while IBKR is down")

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", boom)
    c = FakeConnector()
    c.connected = False
    out = await run_cycle(1, c, None, [], 0.0)
    assert out["strat"] == "skipped"
    assert "ibkr_down" in (out.get("validation") or "")
    assert called == []


@pytest.mark.asyncio
async def test_snap_timeout_returns_empty_book(monkeypatch):
    import asyncio

    async def slow(*_a, **_k):
        await asyncio.sleep(2)
        return {}

    monkeypatch.setattr("abcxauto.agent_loop._tool", slow)
    monkeypatch.setattr("abcxauto.agent_loop.SNAP_S", 0.05)
    out = await snap(FakeConnector())
    assert out["account"] == {}
    assert out["positions"] == []
    assert out["book_unreliable"] is True
    assert "reality_pulse" in out


@pytest.mark.asyncio
async def test_run_cycle_skips_when_book_unreliable(monkeypatch):
    called: list[int] = []

    async def boom(*_a, **_k):
        called.append(1)
        raise AssertionError("grok must not run on an unreliable book")

    async def bad_tool(_c, name: str, _a=None):
        if name == "positions":
            return {"error": "positions failed"}
        return {
            "account_summary": {"netliquidation": 1000, "unrealizedpnl": 0},
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    monkeypatch.setattr("abcxauto.agent_loop._tool", bad_tool)
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", boom)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "skipped"
    assert "book_unreliable" in (out.get("validation") or "")
    assert called == []


@pytest.mark.asyncio
async def test_snap_empty_account_is_unreliable(monkeypatch):
    async def no_nl(_c, name: str, _a=None):
        if name == "account_summary":
            return {}
        return await _fake_tool(_c, name, _a)

    monkeypatch.setattr("abcxauto.agent_loop._tool", no_nl)
    out = await snap(FakeConnector())
    assert out["book_unreliable"] is True


@pytest.mark.asyncio
async def test_snap_loads_option_facts_for_open_legs(monkeypatch):
    async def with_opt(_c, name: str, _a=None):
        if name == "positions":
            return [{
                "symbol": "SPY",
                "secType": "OPT",
                "quantity": 1,
                "strike": 500,
                "right": "C",
                "expiration": "20260821",
            }]
        return await _fake_tool(_c, name, _a)

    async def fake_facts(_positions, **_k):
        return [{"symbol": "SPY", "source": "snap"}]

    monkeypatch.setattr("abcxauto.agent_loop._tool", with_opt)
    monkeypatch.setattr("abcxauto.option_facts.fetch_option_facts", fake_facts)
    out = await snap(FakeConnector())
    assert out["option_facts"][0]["symbol"] == "SPY"


def test_new_risk_blocked_when_book_unreliable():
    world = _world(flat=False, gates={"book_unreliable": True})
    strat, forced = gate_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {"symbol": "SPY", "quantity": 1, "direction": "LONG"},
        },
        world,
    )
    assert strat == "blocked"
    assert "unreliable" in str(forced.get("note") or "").lower()


def test_stance_from_book():
    assert stance_from_book("hold", {"protection": {"unprotected_symbols": ["SPY"]}}) == "protect"
    assert stance_from_book("market_bracket", {"positions": []}) == "hunt"
    assert stance_from_book("hold", {"positions": [{"symbol": "SPY"}]}) == "manage"
    assert stance_from_book("hold", {"positions": []}) == "idle"


@pytest.mark.asyncio
async def test_safe_execute_disconnected_is_error():
    from abcxauto.executor import safe_execute

    class Down:
        connected = False

    out = await safe_execute(
        {"strategy": "market_bracket", "params": {"symbol": "SPY"}},
        Down(),
    )
    assert out["status"] == "error"
    assert out["note"] == "ibkr_disconnected"


def test_turn_did_work_facts_or_tune():
    from abcxauto.brain import BrainTurn

    assert turn_did_work(BrainTurn()) is False
    assert turn_did_work(BrainTurn(tool_trace=["book", "scan"])) is True
    assert turn_did_work(BrainTurn(lab_playbook={"rev": 1})) is False
    assert turn_did_work(BrainTurn(tool_trace=["write_lab_playbook"])) is False
    assert turn_did_work(BrainTurn(sends=[{"strat": "self_tune"}])) is True


@pytest.mark.asyncio
async def test_paper_flat_rth_implicit_hold_after_tools(monkeypatch):
    from abcxauto.brain import BrainTurn

    async def worked(*_a, **_k):
        return BrainTurn(
            last_act={"action": "hold", "strategy": "hold", "rationale": "no send"},
            last_strat="hold",
            last_result={"status": "hold", "strategy": "hold"},
            tool_trace=["book", "option_facts"],
        )

    monkeypatch.setattr(
        "abcxauto.agent_loop.build_world_state",
        lambda **_k: _world(flat=True, session_status="regular"),
    )
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", worked)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "blocked"
    assert "paper hunt" in str((out.get("result") or {}).get("note") or out.get("validation") or "")


@pytest.mark.asyncio
async def test_paper_flat_rth_idle_hold_is_blocked(monkeypatch):
    from abcxauto.brain import BrainTurn

    async def idle(*_a, **_k):
        return BrainTurn(
            last_act={"action": "hold", "strategy": "hold", "rationale": "no send"},
            last_strat="hold",
            last_result={"status": "hold", "strategy": "hold"},
        )

    monkeypatch.setattr(
        "abcxauto.agent_loop.build_world_state",
        lambda **_k: _world(flat=True, session_status="regular"),
    )
    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", idle)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "blocked"


@pytest.mark.asyncio
async def test_notes_only_unprotected_still_forbids_hold(monkeypatch):
    from abcxauto.brain import BrainTurn

    async def notes(*_a, **_k):
        return BrainTurn(
            lab_playbook={"revision": 1},
            tool_trace=["write_lab_playbook"],
        )

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", notes)
    monkeypatch.setattr(
        "abcxauto.agent_loop.build_world_state",
        lambda **_k: _world(needs_protection=True, unprotected=["SPY"], flat=False),
    )
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "blocked"
    assert "hold_forbidden" in str((out.get("result") or {}).get("note") or out.get("validation") or "")


def test_stale_playbook_is_not_a_hold_gate(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto.lab_playbook import save_lab

    save_lab(
        {
            "mode": "explore",
            "instructions": "Debit verticals only.",
            "do_more": "verticals",
            "stop_doing": "lottery calls",
            "ready_to_promote": False,
        }
    )
    lab = __import__("json").loads((tmp_path / "lab.json").read_text(encoding="utf-8"))
    lab["written_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    (tmp_path / "lab.json").write_text(__import__("json").dumps(lab), encoding="utf-8")
    strat, forced = gate_ticket(_hold_act(), _world(flat=False, session_status="regular"))
    assert strat == "hold"
    assert forced is None


@pytest.mark.asyncio
async def test_stale_playbook_tool_tour_is_not_work(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from abcxauto.brain import BrainTurn
    from abcxauto.lab_playbook import save_lab

    save_lab(
        {
            "mode": "explore",
            "instructions": "Debit verticals only.",
            "do_more": "verticals",
            "stop_doing": "lottery calls",
            "ready_to_promote": False,
        }
    )
    lab = __import__("json").loads((tmp_path / "lab.json").read_text(encoding="utf-8"))
    lab["written_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    (tmp_path / "lab.json").write_text(__import__("json").dumps(lab), encoding="utf-8")

    async def worked(*_a, **_k):
        return BrainTurn(
            last_act={"action": "hold", "strategy": "hold", "rationale": "no send"},
            last_strat="hold",
            last_result={"status": "hold", "strategy": "hold"},
            tool_trace=["book", "scan", "quote"],
        )

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", worked)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "blocked"
    assert "paper hunt" in str((out.get("result") or {}).get("note") or out.get("validation") or "")
