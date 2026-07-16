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


def _cfg(**overrides):
    """Minimal config stub for cycle cadence tests."""
    base = dict(
        signal_only=True,
        grok_min_interval_s=300.0,
        cycle_sleep_s=300.0,
        max_risk_per_trade_pct=1.0,
        max_position_pct=10.0,
        marketdata_token="",
        trading_mandate="RELY ON YOUR INTELLIGENCE. Trade actively with brackets.",
        trading_mode="paper",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset_cadence(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=False, grok_min_interval_s=0),
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
async def test_run_cycle_hold_when_flat(monkeypatch):
    """Agent proposing hold on a flat/protected book succeeds without send_action."""
    before = dict(TWEAKS)
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    send_calls: list[dict] = []

    async def fake_grok(_g, prompt: str) -> str:
        assert "PORTFOLIO STATE:" in prompt
        assert "hold allowed" in prompt.lower()
        assert "ORDER EXAMPLES" in prompt
        return json.dumps({"action": "hold", "strategy": "hold", "rationale": "wait"})

    async def record_send(action, conn):
        send_calls.append(action)
        return {"status": "executed"}

    monkeypatch.setattr("abcxauto.agent_loop.grok", fake_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    try:
        hist = []
        out = await run_cycle(1, FakeConnector(), None, hist, 0.0)
        assert out["strat"] == "hold"
        assert out["result"]["status"] == "hold"
        assert send_calls == []
        assert out["pnl"] == 5.0
        assert out["positions"] == []
        assert "unprotected_symbols" in out["protection"]
        out2 = await run_cycle(2, FakeConnector(), None, hist, out["pnl"])
        assert out2["tweak"] == "none"
        assert out2.get("order_lab") == {}
        assert out2["portfolio"].startswith("0 positions")
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


@pytest.mark.asyncio
async def test_run_cycle_rth_always_calls_grok(monkeypatch):
    """RTH decision cycles always call Grok (signal_only no longer skips)."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: _cfg(signal_only=True, grok_min_interval_s=300),
    )
    grok_calls: list[str] = []

    async def tracking_grok(_g, prompt: str) -> str:
        grok_calls.append(prompt)
        return json.dumps({
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
        })

    async def _ok_send(a, c):
        return {"status": "executed", "strategy": a.get("strategy")}

    monkeypatch.setattr("abcxauto.agent_loop.grok", tracking_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", _ok_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert len(grok_calls) >= 1
    assert "PORTFOLIO STATE:" in grok_calls[0]
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

    async def tracking_grok(_g, prompt: str) -> str:
        grok_calls.append(prompt)
        return json.dumps({
            "action": "hold",
            "strategy": "hold",
            "rationale": "protection review hold",
        })

    monkeypatch.setattr("abcxauto.agent_loop.grok", tracking_grok)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert len(grok_calls) == 1
    assert "PROTECTION ONLY" in grok_calls[0]
    assert "HOLD FORBIDDEN" in grok_calls[0]
    assert "LIVE BOOK" in grok_calls[0]
    assert "JOURNAL MEMORY" in grok_calls[0]
    assert "PORTFOLIO STATE:" in grok_calls[0]
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

    async def tracking_grok(_g, prompt: str) -> str:
        grok_calls.append(prompt)
        return json.dumps({
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
            "rationale": "intelligent entry",
        })

    monkeypatch.setattr("abcxauto.agent_loop.grok", tracking_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert len(grok_calls) == 1
    assert "LIVE BOOK" in grok_calls[0]
    assert "JOURNAL MEMORY" in grok_calls[0]
    assert "ORDER EXAMPLES" in grok_calls[0]
    assert "RELY ON YOUR INTELLIGENCE" in grok_calls[0] or "hold allowed" in grok_calls[0].lower()
    assert "PORTFOLIO STATE:" in grok_calls[0]
    assert "MARKET HINTS" not in grok_calls[0]
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

    async def tracking_grok(_g, prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({
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
        })

    async def _noop_send(action, conn):
        return {"status": "executed", "strategy": action.get("strategy")}

    monkeypatch.setattr("abcxauto.agent_loop.grok", tracking_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", _noop_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert prompts
    assert "LIVE BOOK" in prompts[0]
    assert "REALITY CHECK" in prompts[0]
    assert "UNPROTECTED" in prompts[0] or "naked" in prompts[0].lower()
    assert "PROTECTION ONLY" in prompts[0]
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

    monkeypatch.setattr("abcxauto.agent_loop.grok", tracking_grok)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert grok_calls == []
    assert out["strat"] == "skipped"
    assert "session_closed" in (out.get("validation") or "")


@pytest.mark.asyncio
async def test_run_cycle_does_not_write_jsonl(monkeypatch, tmp_path):
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)

    async def fake_grok(_g, _p: str) -> str:
        return json.dumps({
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
            "rationale": "active",
        })

    async def _noop_send(action, conn):
        return {"status": "executed"}

    monkeypatch.setattr("abcxauto.agent_loop.grok", fake_grok)
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
    assert "trailing_stop" not in ALLOWED_ACTIONS
    strat, forced = normalize_action({"action": "trailing_stop", "strategy": "trailing_stop"})
    assert strat == "blocked"
    assert forced is not None


@pytest.mark.asyncio
async def test_run_cycle_blocks_invalid_strategy(monkeypatch):
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)

    async def bad_grok(_g, _p: str) -> str:
        return json.dumps({"action": "hold_existing", "strategy": "hold_existing"})

    monkeypatch.setattr("abcxauto.agent_loop.grok", bad_grok)
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

    async def bare_bracket(_g, _p: str) -> str:
        return json.dumps({
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
        })

    monkeypatch.setattr("abcxauto.agent_loop.grok", bare_bracket)
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

    async def bracket_grok(_g, prompt: str) -> str:
        return json.dumps({
            "action": "bracket", "strategy": "bracket",
            "params": {
                "symbol": "SPY", "quantity": 1, "direction": "LONG",
                "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
            },
            "rationale": "Current reality: RTH; smoke",
        })

    monkeypatch.setattr("abcxauto.agent_loop.grok", bracket_grok)
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
    """order_suite is not invoked on the autonomous agent path."""
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed"}

    async def bracket_grok(_g, _p: str) -> str:
        return json.dumps({
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
        })

    monkeypatch.setattr("abcxauto.agent_loop.grok", bracket_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "bracket"
    assert len(calls) == 1
    assert out.get("order_lab") == {}
    assert "low_pass_rate" not in out.get("validation", "")


@pytest.mark.asyncio
async def test_no_prefer_bracket_only_playbook(monkeypatch):
    """prefer_bracket_only is removed — agent chooses structure; gates constrain."""
    assert "prefer_bracket_only" not in TWEAKS
    monkeypatch.setattr("abcxauto.agent_loop._tool", _fake_tool)
    monkeypatch.setattr(
        "abcxauto.agent_loop.ALLOWED_ACTIONS",
        frozenset(ALLOWED_ACTIONS | {"trailing_stop"}),
    )
    calls: list[dict] = []

    async def record_send(action, conn):
        calls.append(action)
        return {"status": "executed"}

    async def trail_grok(_g, _p: str) -> str:
        return json.dumps({
            "action": "trailing_stop",
            "strategy": "trailing_stop",
            "params": {"symbol": "SPY", "quantity": 1, "direction": "LONG", "trail_percent": 1.0},
            "rationale": "trail",
        })

    monkeypatch.setattr("abcxauto.agent_loop.grok", trail_grok)
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

    async def bracket_grok(_g, prompt: str) -> str:
        return json.dumps({
            "action": "bracket", "strategy": "bracket",
            "params": {
                "symbol": "SPY", "quantity": 1, "direction": "LONG",
                "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
            },
            "rationale": "test bracket dispatch",
        })

    monkeypatch.setattr("abcxauto.agent_loop.grok", bracket_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", record_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "bracket"
    assert len(calls) == 1
    assert calls[0]["strategy"] == "bracket"
    # Kahneman soft-gate removed — stub reports incomplete.
    assert out.get("kahneman", {}).get("complete") is False


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
    assert cfg.cycle_sleep_s == 120.0
    assert cfg.grok_min_interval_s == 120.0
    assert "RELY ON YOUR INTELLIGENCE" in cfg.trading_mandate
    assert "cycle_sleep_s" in Config.__dataclass_fields__
    get_config.cache_clear()
