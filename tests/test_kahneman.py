"""Kahneman System 2 deliberative scaffolding — heart bone structure."""

import json

import pytest

from abcxauto.kahneman import (
    KAHNEMAN_HEART,
    extract_kahneman,
    format_kahneman_trace,
    gate_incomplete_system2,
)
from abcxauto.rocket import RULES, run_cycle


def test_kahneman_heart_in_system_rules():
    assert "KAHNEMAN" in KAHNEMAN_HEART or "System 2" in KAHNEMAN_HEART
    assert "pre-mortem" in KAHNEMAN_HEART.lower() or "Pre-mortem" in KAHNEMAN_HEART
    assert "bias" in KAHNEMAN_HEART.lower()
    assert KAHNEMAN_HEART in RULES or "System 2" in RULES


def test_extract_complete_kahneman():
    act = {
        "kahneman": {
            "system1_scan": "SPY up",
            "system2_base_rate": "liquid ETF longs ~55%",
            "debias": {"anchoring": "ignore last print"},
            "pre_mortem": "stop run",
            "alternatives": ["hold", "wait for open"],
            "bias_audit": ["availability", "overconfidence"],
        }
    }
    k = extract_kahneman(act)
    assert k["complete"] is True
    assert k["missing"] == []
    assert "availability" in k["bias_audit"]
    trace = format_kahneman_trace(k)
    assert "KAHNEMAN" in trace
    assert "pre-mortem" in trace.lower()


def test_extract_incomplete_missing_fields():
    k = extract_kahneman({"kahneman": {"system1_scan": "fast only"}})
    assert k["complete"] is False
    assert "system2_base_rate" in k["missing"]
    assert "pre_mortem" in k["missing"]
    assert "bias_audit" in k["missing"]


def test_gate_hold_always_ok():
    ok, msg = gate_incomplete_system2("hold", extract_kahneman({}))
    assert ok is True


def test_gate_blocks_trade_without_system2():
    ok, msg = gate_incomplete_system2("bracket", extract_kahneman({}))
    assert ok is False
    assert "incomplete" in msg.lower() or "missing" in msg.lower()


@pytest.mark.asyncio
async def test_run_cycle_coerces_trade_without_kahneman(monkeypatch):
    async def _fake_tool(_c, name: str, _a=None):
        return {
            "account_summary": {"netliquidation": 1000, "unrealizedpnl": 5},
            "positions": [],
            "open_orders": [],
            "market_hours": {"session": "regular"},
            "quote": {"symbol": "SPY", "last": 500},
        }.get(name, {})

    calls: list = []

    async def record_safe_execute(action, conn):
        calls.append(action)
        return {"status": "executed"}

    async def bare_bracket(_g, prompt: str) -> str:
        if "ONE tweak" in prompt:
            return json.dumps({"type": "none", "summary": "none"})
        # Deliberately omit kahneman — System 2 gate must block execute
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

    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    monkeypatch.setattr("abcxauto.rocket.grok", bare_bracket)
    monkeypatch.setattr("abcxauto.rocket.safe_execute", record_safe_execute)
    out = await run_cycle(1, object(), None, [], 0.0)
    assert out["strat"] == "hold"
    assert calls == []
    assert out.get("kahneman", {}).get("complete") is False
    assert "system2" in str(out.get("validation", "")).lower() or out["result"].get(
        "kahneman_incomplete"
    )
