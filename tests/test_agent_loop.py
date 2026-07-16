"""Smoke tests for the lean agent_loop hot path."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.agent_loop import (
    ALLOWED_ACTIONS,
    extract_kahneman,
    normalize_action,
    run_cycle,
    snap,
)


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


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            trading_mandate="RELY ON YOUR INTELLIGENCE.",
            trading_mode="paper",
            grok_min_interval_s=0,
            signal_only=False,
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


@pytest.mark.asyncio
async def test_hold_path_skips_send(monkeypatch):
    send_calls: list = []

    async def fake_grok(_g, prompt: str) -> str:
        assert "ORDER EXAMPLES" in prompt
        assert "PORTFOLIO STATE:" in prompt
        return json.dumps({"action": "hold", "strategy": "hold", "rationale": "flat"})

    async def boom_send(*_a, **_k):
        send_calls.append(1)
        raise AssertionError("send_action must not run on hold")

    monkeypatch.setattr("abcxauto.agent_loop.grok", fake_grok)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom_send)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "hold"
    assert out["result"]["status"] == "hold"
    assert send_calls == []
    assert out["order_lab"] == {}
    assert "reality_pulse" in out


@pytest.mark.asyncio
async def test_prompt_includes_order_examples(monkeypatch):
    prompts: list[str] = []

    async def tracking_grok(_g, prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"action": "hold", "strategy": "hold"})

    monkeypatch.setattr("abcxauto.agent_loop.grok", tracking_grok)
    await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert prompts
    assert "ORDER EXAMPLES" in prompts[0]
    assert "market_bracket" in prompts[0]
    assert "CONNECTION:" in prompts[0]
    assert "MANDATE:" in prompts[0]


def test_extract_kahneman_stub_incomplete():
    k = extract_kahneman({"kahneman": {"system1_scan": "x"}})
    assert k["complete"] is False


def test_normalize_noop_to_hold():
    strat, forced = normalize_action({"action": "noop"})
    assert strat == "hold"
    assert forced is None
    assert "hold" in ALLOWED_ACTIONS


@pytest.mark.asyncio
async def test_snap_has_reality_pulse():
    out = await snap(FakeConnector())
    assert "reality_pulse" in out
    assert "portfolio_state" in out
