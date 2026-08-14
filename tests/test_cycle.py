"""Pro cycle — drives shipped snap/run_cycle on real code paths."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from abcxauto.cycle import (
    ALLOWED_ACTIONS,
    TWEAKS,
    apply_tweak,
    equity_of,
    normalize_action,
    pnl_of,
    risk_label,
    run_cycle,
    snap,
)


class FakeConnector:
    connected = True

    async def connect(self):
        return True

    async def get_positions(self):
        return [{"symbol": "AAPL", "quantity": 10, "sec_type": "STK", "unrealized_pnl": 5.0}]

    async def get_open_orders(self):
        return []

    async def get_account_summary(self):
        return {"netliquidation": 50000, "unrealizedpnl": 12.5}

    async def get_recent_executions(self):
        return []


async def _fake_tool(_c, name: str, _a=None):
    return {
        "account_summary": {"netliquidation": 1000, "unrealizedpnl": 5},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": "regular"},
        "quote": {"symbol": "SPY", "last": 500},
    }.get(name, {})


async def _fake_tool_closed(_c, name: str, _a=None):
    return {
        "account_summary": {"netliquidation": 1000, "unrealizedpnl": 5},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": "closed"},
        "quote": {"symbol": "SPY", "last": 500},
    }.get(name, {})


async def _fake_tool_session(session: str):
    async def _tool(_c, name: str, _a=None):
        return {
            "account_summary": {"netliquidation": 1000, "unrealizedpnl": 5},
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": session, "minutes_to_open": 80},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    return _tool


def _cfg(**overrides):
    """Minimal config stub for cycle cadence tests."""
    base = dict(
        signal_only=True,
        grok_min_interval_s=300.0,
        cycle_sleep_s=300.0,
        max_risk_per_trade_pct=1.0,
        max_position_pct=10.0,
        max_open_positions=6,
        marketdata_token="",
        trading_mandate="RELY ON YOUR INTELLIGENCE. Trade actively with brackets.",
        trading_mode="paper",
        is_paper=True,
        risk_posture="balanced",
        # S2 lean so Act path runs (tests assert Act prompts / invalid strat).
        control_deliberation_pct=80,
        control_budget_pct=50,
        control_frequency_pct=50,
        control_complexity_pct=50,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _judgment_for_act(act: dict, **extra) -> dict:
    """Build a coherent Judge payload for a given Act dict."""
    strat = str(act.get("strategy") or act.get("action") or "hold").lower()
    params = act.get("params") or {}
    if strat in ("bracket", "market_bracket"):
        stance, kind = "hunt", "hunt"
    elif strat in ("oca", "modify_stop", "modify_target", "market_order", "close_option"):
        stance, kind = "protect", "protect"
    elif strat in ("set_risk", "self_tune"):
        stance, kind = "idle", "idle"
    elif strat == "hold":
        stance, kind = "idle", "idle"
    else:
        stance, kind = "manage", "manage"
    j = {
        "stance": stance,
        "thesis": str(act.get("rationale") or "test thesis"),
        "focus": "test focus",
        "dismissed": "",
        "intent": {
            "kind": kind,
            "symbol": params.get("symbol"),
            "direction": params.get("direction"),
            "urgency": "med",
        },
        "risk_budget_pct": 2.0,
        "regime_fit": True,
        "setup_grade": "A",
    }
    j.update(extra)
    return j


def _pja_grok(act: dict, judgment: dict | None = None, prompts: list | None = None):
    """Return grok_turn stub that sends ``act`` through the clerk."""
    from tests.conftest import fake_grok_turn

    return fake_grok_turn(act, wakes=prompts)


@pytest.fixture(autouse=True)
def _reset_cadence(monkeypatch, tmp_path):
    stub = _cfg(signal_only=False, grok_min_interval_s=0)
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: stub)
    monkeypatch.setattr("abcxauto.world_state.get_config", lambda: stub)
    # Force Act path (S2) without replacing full config.get_config.
    monkeypatch.setattr(
        "abcxauto.config.deliberation_requires_act", lambda cfg=None: True
    )
    # Avoid real MDA/connector in connection_status during prompts.
    monkeypatch.setattr(
        "abcxauto.agent_loop.connection_status",
        lambda _c=None: {
            "ibkr_connected": True,
            "mda_configured": False,
            "trading_mode": "paper",
        },
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

    async def _no_opps(*_a, **_k):
        return []

    async def _no_news(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", _no_news)
    yield


@pytest.mark.asyncio
async def test_snap_with_fake_connector(monkeypatch):
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    out = await snap(FakeConnector())
    assert {
        "taken_at", "account", "positions", "open_orders", "market_hours",
        "spy_quote", "protection", "reality_pulse", "vix_quote", "portfolio_state",
    }.issubset(out.keys())
    assert out["account"]["netliquidation"] == 1000
    assert "narrative" in out["reality_pulse"]
    assert out["reality_pulse"]["session"]["status"] == "regular"


@pytest.mark.asyncio
async def test_run_cycle_paper_flat_rth_blocks_hold(monkeypatch):
    """Paper lab while flat in RTH cannot sit — hold is live-only / open-book."""
    before = dict(TWEAKS)
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=False, grok_min_interval_s=0, is_paper=True),
    )
    send_calls: list[dict] = []
    prompts: list[str] = []
    act = {"action": "hold", "strategy": "hold", "rationale": "wait"}

    async def record_send(action, conn):
        send_calls.append(action)
        return {"status": "executed"}

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, prompts=prompts),
    )
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    try:
        hist = []
        out = await run_cycle(1, FakeConnector(), None, hist, 0.0)
        assert out["strat"] == "blocked"
        assert "hold_forbidden" in str(out["result"].get("note") or "").lower()
        assert send_calls == []
        assert prompts  # Grok woke
        from abcxauto.brain import brain_system_prompt

        assert "ORDER EXAMPLES" in brain_system_prompt()
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


@pytest.mark.asyncio
async def test_run_cycle_live_hold_when_flat(monkeypatch):
    """Live follower may hold when flat."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(
            signal_only=False,
            grok_min_interval_s=0,
            trading_mode="live",
            is_paper=False,
        ),
    )
    send_calls: list[dict] = []
    act = {"action": "hold", "strategy": "hold", "rationale": "wait"}

    async def record_send(action, conn):
        send_calls.append(action)
        return {"status": "executed"}

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", _pja_grok(act))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "hold"
    assert out["result"]["status"] == "hold"
    assert send_calls == []


@pytest.mark.asyncio
async def test_run_cycle_rth_always_calls_grok(monkeypatch):
    """RTH decision cycles always call Grok (signal_only no longer skips)."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=True, grok_min_interval_s=300),
    )
    grok_calls: list[str] = []
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "direction": "LONG",
            "stop_price": 490.0,
            "target_price": 520.0,
            "price_hint": 500.0,
            "entry_price": 500.0,
        },
        "rationale": "active entry",
    }

    async def _ok_send(a, c):
        return {"status": "executed", "strategy": a.get("strategy")}

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, prompts=grok_calls),
    )
    monkeypatch.setattr("abcxauto.agent_loop.send_action", _ok_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert len(grok_calls) >= 1
    assert any("Cycle" in p for p in grok_calls)
    assert out["strat"] in ("market_bracket", "blocked", "hold")


@pytest.mark.asyncio
async def test_run_cycle_protection_blocks_hold(monkeypatch):
    """Unprotected STK → protection Grok; hold response is blocked (no retry)."""
    async def _fake_unprotected(_c, name: str, _a=None):
        return {
            "account_summary": {"netliquidation": 1000, "unrealizedpnl": 5},
            "positions": [
                {
                    "symbol": "SPY",
                    "quantity": 5,
                    "sec_type": "STK",
                    "secType": "STK",
                    "conId": 42,
                    "unrealized_pnl": 1.0,
                }
            ],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_unprotected)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=False, grok_min_interval_s=0),
    )
    grok_calls: list[str] = []
    act = {
        "action": "hold",
        "strategy": "hold",
        "rationale": "protection review hold",
    }
    judgment = _judgment_for_act(
        {"strategy": "oca", "params": {"symbol": "SPY"}},
        stance="protect",
        thesis="Must protect SPY",
        focus="unprotected SPY",
    )

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, judgment=judgment, prompts=grok_calls),
    )
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert len(grok_calls) == 1
    assert any("unprotected" in p.lower() for p in grok_calls)
    assert out["strat"] == "blocked"
    assert "hold_forbidden" in str(out["result"].get("note") or "")


@pytest.mark.asyncio
async def test_run_cycle_agent_decides_with_journal_memory(monkeypatch):
    """Default path: Grok decides; portfolio + journal + examples in prompt."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=False, grok_min_interval_s=0),
    )
    grok_calls: list[str] = []
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed", "strategy": action.get("strategy")}

    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "direction": "LONG",
            "stop_price": 490.0,
            "target_price": 520.0,
            "price_hint": 500.0,
            "entry_price": 500.0,
        },
        "rationale": "intelligent entry",
    }

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, prompts=grok_calls),
    )
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert len(grok_calls) == 1
    joined = "\n".join(grok_calls)
    from abcxauto.brain import brain_system_prompt

    assert "ORDER EXAMPLES" in brain_system_prompt()
    assert "Cycle" in joined
    assert "MARKET HINTS" not in joined
    assert out["strat"] == "market_bracket"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_cycle_prompt_flags_naked_stk(monkeypatch):
    """Unprotected STK + zero orders → REALITY CHECK in prompt."""

    async def _fake_naked(_c, name: str, _a=None):
        return {
            "account_summary": {
                "netliquidation": 37000,
                "unrealizedpnl": -1,
                "totalcashvalue": 34000,
            },
            "positions": [
                {
                    "symbol": "SPY",
                    "quantity": 3,
                    "sec_type": "STK",
                    "secType": "STK",
                    "conId": 756733,
                    "unrealized_pnl": -1.0,
                }
            ],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 751},
        }.get(name, {})

    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_naked)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=False, grok_min_interval_s=0),
    )
    prompts: list[str] = []
    act = {
        "action": "oca",
        "strategy": "oca",
        "params": {
            "symbol": "SPY",
            "quantity": 3,
            "direction": "LONG",
            "stop_price": 749.0,
            "target_price": 755.0,
        },
        "rationale": "protect naked SPY",
    }

    async def _noop_send(action, conn):
        return {"status": "executed", "strategy": action.get("strategy")}

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, prompts=prompts),
    )
    monkeypatch.setattr("abcxauto.agent_loop.send_action", _noop_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert prompts
    joined = "\n".join(prompts)
    assert "unprotected" in joined.lower()
    assert "GATE: unprotected" not in joined
    assert "stance MUST be protect" not in joined
    assert out["strat"] == "oca"


@pytest.mark.asyncio
async def test_run_cycle_market_closed_skips_grok(monkeypatch):
    """Closed session + no unprotected positions → skipped (not hold), no Grok."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool_closed)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=False, grok_min_interval_s=0),
    )
    grok_calls: list[int] = []

    async def tracking_grok(_g, _p: str) -> str:
        grok_calls.append(1)
        return json.dumps({"action": "market_bracket", "strategy": "market_bracket"})

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", tracking_grok)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert grok_calls == []
    assert out["strat"] == "skipped"
    assert "session_closed" in (out.get("validation") or "")


@pytest.mark.asyncio
async def test_run_cycle_premarket_wakes_grok(monkeypatch):
    """Premarket is prep time — Grok runs; hold is allowed (not RTH hunt)."""
    monkeypatch.setattr(
        "abcxauto.agent_loop._tool", await _fake_tool_session("premarket")
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=False, grok_min_interval_s=0),
    )
    wakes: list[str] = []
    act = {"action": "hold", "strategy": "hold", "rationale": "premarket tape"}
    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, prompts=wakes),
    )
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert wakes
    assert "session=premarket" in wakes[0]
    assert out["strat"] == "hold"


@pytest.mark.asyncio
async def test_run_cycle_postmarket_wakes_grok(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.agent_loop._tool", await _fake_tool_session("postmarket")
    )
    wakes: list[str] = []
    act = {"action": "hold", "strategy": "hold", "rationale": "postmarket book"}
    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, prompts=wakes),
    )
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert wakes
    assert out["strat"] == "hold"


@pytest.mark.asyncio
async def test_run_cycle_does_not_write_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "direction": "LONG",
            "stop_price": 490.0,
            "target_price": 520.0,
            "price_hint": 500.0,
            "entry_price": 500.0,
        },
        "rationale": "active",
    }

    async def _noop_send(action, conn):
        return {"status": "executed"}

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", _pja_grok(act))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", _noop_send)
    monkeypatch.chdir(tmp_path)
    await run_cycle(1, FakeConnector(), None, [], 0.0)
    logs = tmp_path / "logs"
    if logs.exists():
        names = {p.name for p in logs.iterdir()}
        for banned in (
            "cycle.log",
            "improvements.log",
            "order_lab.log",
            "order_suite.log",
            "decision_space.log",
        ):
            assert banned not in names


def test_normalize_action_rejects_unknown():
    strat, forced = normalize_action({"action": "hold_existing", "strategy": "hold_existing"})
    assert strat == "blocked"
    assert forced["status"] == "blocked"
    assert "invalid" in forced["note"] or "allowlist" in forced["note"]


def test_normalize_action_accepts_hold_and_noop():
    strat, forced = normalize_action({"action": "hold", "strategy": "hold"})
    assert strat == "hold"
    assert forced is None
    strat2, forced2 = normalize_action({"action": "noop", "strategy": "noop"})
    assert strat2 == "hold"
    assert forced2 is None


def test_normalize_action_hold_in_allowlist():
    assert "hold" in ALLOWED_ACTIONS
    assert "trailing_stop" in ALLOWED_ACTIONS  # structure vocab for manage/protect
    strat, forced = normalize_action({"action": "trailing_stop", "strategy": "trailing_stop"})
    assert strat == "trailing_stop"
    assert forced is None


@pytest.mark.asyncio
async def test_run_cycle_blocks_invalid_strategy(monkeypatch):
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    # Valid idle judgment, then invalid act strategy → blocked at normalize.
    act = {"action": "hold_existing", "strategy": "hold_existing"}
    judgment = _judgment_for_act({"strategy": "hold"})

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, judgment=judgment),
    )
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "blocked"
    assert out["result"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_run_cycle_allows_trade_without_kahneman(monkeypatch):
    """Incomplete System 2 is scaffolding only — not a soft block."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed"}

    act = {
        "action": "bracket",
        "strategy": "bracket",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "direction": "LONG",
            "entry_price": 500.0,
            "stop_price": 490.0,
            "target_price": 510.0,
        },
        "rationale": "no system2",
    }

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", _pja_grok(act))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "bracket"
    assert len(calls) == 1
    assert "system2_gate" not in str(out.get("validation", "")).lower()


@pytest.mark.asyncio
async def test_cycle_smoke_run_cycle_bracket_dispatch(monkeypatch, tmp_path):
    """Shipped run_cycle path: snapshot + bracket action + send_action."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed", "strategy": action.get("strategy")}

    act = {
        "action": "bracket", "strategy": "bracket",
        "params": {
            "symbol": "SPY", "quantity": 1, "direction": "LONG",
            "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
        },
        "rationale": "Current reality: RTH; smoke",
    }

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", _pja_grok(act))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    snap_out = await snap(FakeConnector())
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    payload = {
        "snapshot_keys": list(snap_out.keys()),
        "strat": out["strat"],
        "result": out["result"],
        "send_action_calls": len(calls),
        "dispatched_strategy": calls[0]["strategy"] if calls else None,
    }
    text = json.dumps(payload, indent=2)
    (tmp_path / "cycle_smoke.json").write_text(text, encoding="utf-8")
    assert "protection" in payload["snapshot_keys"]
    assert out["strat"] == "bracket"
    assert payload["send_action_calls"] == 1


@pytest.mark.asyncio
async def test_no_suite_gate_on_autonomous_path(monkeypatch):
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed"}

    act = {
        "action": "bracket",
        "strategy": "bracket",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "direction": "LONG",
            "entry_price": 500.0,
            "stop_price": 490.0,
            "target_price": 510.0,
        },
        "rationale": "try entry",
    }

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", _pja_grok(act))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "bracket"
    assert len(calls) == 1
    assert "low_pass_rate" not in out.get("validation", "")


@pytest.mark.asyncio
async def test_no_prefer_bracket_only_playbook(monkeypatch):
    """prefer_bracket_only is removed — agent chooses structure; gates constrain."""
    assert "prefer_bracket_only" not in TWEAKS
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed"}

    act = {
        "action": "trailing_stop",
        "strategy": "trailing_stop",
        "params": {"symbol": "SPY", "quantity": 1, "direction": "LONG", "trail_percent": 1.0},
        "rationale": "trail",
    }
    judgment = _judgment_for_act(act, stance="manage")

    monkeypatch.setattr(
        "abcxauto.agent_loop.grok_turn",
        _pja_grok(act, judgment=judgment),
    )
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert "prefer_bracket_only" not in (out.get("validation") or "")
    assert out["strat"] == "trailing_stop"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_cycle_dispatches_bracket_to_send_action(monkeypatch):
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed", "strategy": action.get("strategy")}

    act = {
        "action": "bracket", "strategy": "bracket",
        "params": {
            "symbol": "SPY", "quantity": 1, "direction": "LONG",
            "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
        },
        "rationale": "test bracket dispatch",
    }

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", _pja_grok(act))
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "bracket"
    assert len(calls) == 1
    assert calls[0]["strategy"] == "bracket"


def test_apply_tweak_merges_config():
    before = dict(TWEAKS)
    try:
        summary = apply_tweak({"type": "config", "config": {"cycle_sleep_s": 3}, "summary": "faster"})
        assert summary == "faster"
        assert TWEAKS["cycle_sleep_s"] == 3
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_tweaks_static_safety_defaults():
    assert "lab_min_pass_rate" not in TWEAKS
    assert "prefer_bracket_only" not in TWEAKS
    assert float(TWEAKS.get("max_risk_pct", 0)) == 0.5


def test_pnl_and_equity():
    assert pnl_of({"unrealizedpnl": -12.5}) == -12.5
    assert equity_of({"netliquidation": 50000}) == 50000.0


def test_risk_label_compliant():
    assert risk_label({"protection": {"unprotected_symbols": []}}) == "COMPLIANT"


def test_config_cadence_defaults(monkeypatch):
    from abcxauto.config import Config, get_config

    for key in (
        "ABCXAUTO_CYCLE_SLEEP_S",
        "ABCXAUTO_GROK_MIN_INTERVAL_S",
        "ABCXAUTO_SIGNAL_ONLY",
    ):
        monkeypatch.delenv(key, raising=False)
    get_config.cache_clear()
    cfg = get_config()
    assert cfg.cycle_sleep_s == 15.0
    assert cfg.grok_min_interval_s == 15.0
    assert "operator gives no strategy" in cfg.trading_mandate.lower()
    assert "promoted" in cfg.trading_mandate.lower()
    assert "cycle_sleep_s" in Config.__dataclass_fields__
    get_config.cache_clear()
