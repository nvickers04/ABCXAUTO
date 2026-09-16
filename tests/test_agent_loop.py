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
    snap,
    stance_from_book,
    turn_did_work,
)
from abcxauto.brain import brain_system_prompt
from abcxauto.world_state import WorldState


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


async def _think(monkeypatch, grok_fn, conn=None, snap_d=None):
    from abcxauto.pro_engine import ProEngine

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_fn)
    eng = ProEngine()
    c = conn or FakeConnector()
    eng.conn = c
    s = snap_d if snap_d is not None else await snap(c)
    return await eng._host_think(1, None, s)


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


def _no_send_turn(**kwargs):
    from abcxauto.brain import BrainTurn

    async def grok_turn(*_a, **_k):
        return BrainTurn(**kwargs)

    return grok_turn


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
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _empty)
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)


@pytest.mark.asyncio
async def test_no_send_is_yield_not_a_ticket(monkeypatch):
    """No send() is rest. Clerk does not invent a hold ticket."""
    send_calls: list = []

    async def boom_send(*_a, **_k):
        send_calls.append(1)
        raise AssertionError("send_action must not run on no-send")

    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await _think(
        monkeypatch,
        _no_send_turn(tool_trace=["book"], text="watching"),
    )
    assert out["strat"] != "hold"
    assert out["strat"] != "blocked"
    assert send_calls == []
    assert "world_state" in out
    assert (out.get("action_obj") or {}).get("strategy") != "hold"


@pytest.mark.asyncio
async def test_paper_flat_rth_no_send_does_not_send(monkeypatch):
    send_calls: list = []

    async def boom_send(*_a, **_k):
        send_calls.append(1)
        raise AssertionError("send_action must not run on no-send")

    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await _think(monkeypatch, _no_send_turn(tool_trace=["book"]))
    assert out["strat"] != "hold"
    assert out["strat"] != "blocked"
    assert send_calls == []


def test_explicit_hold_send_is_not_a_ticket():
    strat, forced = gate_ticket(_hold_act(), _world(flat=True, session_status="regular"))
    assert strat == "blocked"
    note = str((forced or {}).get("note") or "")
    assert "hold is not a ticket" not in note
    assert "invalid" in note or "allowlist" in note


def test_brain_system_has_order_examples():
    text = brain_system_prompt().lower()
    assert "order examples" in text
    assert "clock in" not in text
    assert "mandate:" not in text
    assert "the operator gives" not in text


def test_brain_system_send_closer_allows_no_send():
    from abcxauto.llm import SYSTEM_PROMPT
    from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

    text = brain_system_prompt()
    closer = "send changes the book; a look may end with no send."
    assert closer in text
    assert text.rstrip().splitlines()[-1] == closer
    assert "send is the only way to change the book." not in text
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_normalize_noop_is_not_a_ticket():
    strat, forced = normalize_action({"action": "noop"})
    assert strat == "blocked"
    assert forced is not None
    assert "hold" not in ALLOWED_ACTIONS
    assert "self_tune" in ALLOWED_ACTIONS


@pytest.mark.asyncio
async def test_snap_has_reality_pulse():
    out = await snap(FakeConnector())
    assert "reality_pulse" in out
    assert "portfolio_state" in out
    assert out["ibkr_live_quotes"].get("SPY") == 500


@pytest.mark.asyncio
async def test_new_entry_quote_ignores_mda_tape():
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


@pytest.mark.asyncio
async def test_new_entry_quote_uses_this_look_scan_print():
    from abcxauto.agent_loop import _quote_for_action

    act = {
        "strategy": "market_bracket",
        "params": {"symbol": "SNDK", "price_hint": 100.0},
    }
    snap_d = {
        "ibkr_live_quotes": {},
        "scan_hits": {
            "quoted": 1,
            "rows": [{"symbol": "SNDK", "last": 91.5, "ibkr": {"last": 91.5}}],
        },
        "spy_quote": {"last": 500},
    }
    got = await _quote_for_action(act, snap_d, connector=None)
    assert got == 91.5


@pytest.mark.asyncio
async def test_new_entry_quote_does_not_use_prior_close_as_last(monkeypatch):
    from abcxauto.agent_loop import _quote_for_action

    class Conn:
        connected = True

    async def _tool(_c, name, a=None):
        assert name == "quote"
        return {"symbol": "SNDK", "close": 100.0, "open": 90.0, "open_gap_pct": -10.0}

    monkeypatch.setattr("abcxauto.agent_loop._tool", _tool)
    act = {
        "strategy": "market_bracket",
        "params": {"symbol": "SNDK", "stop_price": 88.0, "target_price": 93.0},
    }
    got = await _quote_for_action(act, {"ibkr_live_quotes": {}}, Conn())
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


def test_paper_may_not_send_hold_when_flat_rth():
    world = _world(flat=True, session_status="regular")
    strat, forced = gate_ticket(_hold_act(), world)
    assert strat == "blocked"
    note = str((forced or {}).get("note") or "")
    assert "hold is not a ticket" not in note
    assert "invalid" in note or "allowlist" in note


def test_wake_grok_for_session():
    assert _wake_grok_for_session("premarket", needs_prot=False) is True
    assert _wake_grok_for_session("regular", needs_prot=False) is True
    assert _wake_grok_for_session("postmarket", needs_prot=False) is False
    assert _wake_grok_for_session("closed", needs_prot=False) is False
    assert _wake_grok_for_session("", needs_prot=False) is False
    assert _wake_grok_for_session("closed", needs_prot=True) is True
    assert _wake_grok_for_session("premarket", needs_prot=True) is True


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
    assert stance_from_book("market_bracket", {"positions": []}) == "new_entry"
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
    assert turn_did_work(BrainTurn(text="notes only")) is False
    assert turn_did_work(BrainTurn(tool_trace=["write_lab_playbook"])) is False
    assert turn_did_work(BrainTurn(sends=[{"strat": "self_tune"}])) is True


@pytest.mark.asyncio
async def test_paper_no_send_after_tools_is_rest_not_hold_ticket(monkeypatch):
    """No-send must not invent hold."""
    from abcxauto.brain import BrainTurn

    async def worked(*_a, **_k):
        return BrainTurn(
            last_act={"action": "hold", "strategy": "hold", "rationale": "no send"},
            last_strat="hold",
            last_result={"status": "hold", "strategy": "hold"},
            tool_trace=["book", "option_facts"],
        )

    out = await _think(monkeypatch, worked)
    assert out["strat"] != "hold"
    assert out["strat"] != "blocked"
    assert "hold is not a ticket" not in str(
        (out.get("result") or {}).get("note") or out.get("validation") or out.get("rationale") or ""
    )
    assert (out.get("action_obj") or {}).get("strategy") != "hold"


@pytest.mark.asyncio
async def test_paper_no_send_idle_is_rest_not_blocked(monkeypatch):
    from abcxauto.brain import BrainTurn

    async def idle(*_a, **_k):
        return BrainTurn(
            last_act={"action": "hold", "strategy": "hold", "rationale": "no send"},
            last_strat="hold",
            last_result={"status": "hold", "strategy": "hold"},
        )

    out = await _think(monkeypatch, idle)
    assert out["strat"] != "blocked"
    assert out["strat"] != "hold"
    assert "hold is not a ticket" not in str(
        (out.get("result") or {}).get("note") or out.get("validation") or ""
    )


@pytest.mark.asyncio
async def test_notes_only_unprotected_no_send_is_rest(monkeypatch):
    """No-send is not a hold ticket — unprotected last-stop only gates real hold sends."""
    from abcxauto.brain import BrainTurn

    async def notes(*_a, **_k):
        return BrainTurn(
            tool_trace=["write_lab_playbook"],
        )

    out = await _think(monkeypatch, notes)
    assert out["strat"] != "hold"
    assert "hold_forbidden" not in str(
        (out.get("result") or {}).get("note") or out.get("validation") or ""
    )
    assert "hold is not a ticket" not in str(
        (out.get("result") or {}).get("note") or out.get("validation") or ""
    )



@pytest.mark.asyncio
async def test_live_no_send_is_yield(monkeypatch):
    """Live follower: no send() is yield, not a hold ticket."""
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mode="live",
            risk_posture="balanced",
            is_paper=False,
        ),
    )
    out = await _think(
        monkeypatch,
        _no_send_turn(tool_trace=["book", "set_wake"], text="watching"),
    )
    assert out.get("sends") == 0
    assert out["strat"] != "hold"
    assert out["strat"] != "blocked"
    assert (out.get("action_obj") or {}).get("strategy") != "hold"


@pytest.mark.asyncio
async def test_explicit_hold_send_unprotected_still_forbidden(monkeypatch):
    """Unprotected STK last-stop still blocks a real hold send."""
    from abcxauto.brain import BrainTurn

    blocked = {
        "status": "blocked",
        "note": "hold_forbidden - unprotected STK needs a last-stop",
    }

    async def sent_hold(*_a, **_k):
        act = {
            "action": "blocked",
            "strategy": "blocked",
            "rationale": blocked["note"],
        }
        return BrainTurn(
            sends=[{"act": dict(act), "result": blocked, "strat": "blocked"}],
            last_act=act,
            last_strat="blocked",
            last_result=blocked,
            tool_trace=["send"],
        )

    out = await _think(monkeypatch, sent_hold)
    assert out["strat"] == "blocked"
    assert "hold_forbidden" in str(
        (out.get("result") or {}).get("note") or out.get("validation") or ""
    )


def test_result_dict_keeps_hunt_tape():
    from abcxauto.agent_loop import _result_dict

    out = _result_dict(
        n=1,
        s={
            "scan_hits": {
                "quoted": 1,
                "rows": [{"symbol": "SNDK", "open_gap_pct": -6.5}],
            },
            "session_range": {"SNDK": {"today": True, "low": 88.0}},
        },
        act={},
        strat="",
        result={},
        pnl=0.0,
        eq=1.0,
        prev=0.0,
        inventory="",
        validation="ok",
    )
    assert out["scan_hits"]["rows"][0]["symbol"] == "SNDK"
    assert out["session_range"]["SNDK"]["low"] == 88.0
