"""Rocket loop — drives shipped snap/run_cycle on real code paths."""

import json
from pathlib import Path

import pytest

from abcxauto.executor import safe_execute
from abcxauto.rocket import (
    ALLOWED_ACTIONS,
    TWEAKS,
    apply_tweak,
    equity_of,
    normalize_action,
    parse_json,
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


@pytest.mark.asyncio
async def test_snap_with_fake_connector(monkeypatch):
    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    out = await snap(FakeConnector())
    assert {
        "taken_at", "account", "positions", "open_orders", "market_hours",
        "spy_quote", "protection", "reality_pulse", "vix_quote",
    }.issubset(out.keys())
    assert out["account"]["netliquidation"] == 1000
    assert "narrative" in out["reality_pulse"]
    assert out["reality_pulse"]["session"]["status"] == "regular"


@pytest.mark.asyncio
async def test_run_cycle_hold_path(monkeypatch):
    before = dict(TWEAKS)
    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)

    async def fake_grok(_g, prompt: str) -> str:
        if "ONE tweak" in prompt:
            return json.dumps({"type": "config", "config": {"cycle_sleep_s": 2}, "summary": "faster"})
        return json.dumps({"action": "hold", "strategy": "hold", "rationale": "wait"})

    monkeypatch.setattr("abcxauto.rocket.grok", fake_grok)
    try:
        hist = []
        out = await run_cycle(1, FakeConnector(), None, hist, 0.0)
        assert out["strat"] == "hold"
        assert out["result"]["status"] == "hold"
        assert out["pnl"] == 5.0
        assert out["positions"] == []
        assert out["open_orders"] == []
        assert "unprotected_symbols" in out["protection"]
        assert out["action_obj"]["strategy"] == "hold"
        assert out["rationale"] == "wait"
        out2 = await run_cycle(2, FakeConnector(), None, hist, out["pnl"])
        # v0.2: auto-reconfig from order lab (no manual force-tweak summary)
        assert "auto-reconfig" in str(out2["tweak"])
        assert out2.get("order_lab") and out2["order_lab"].get("pass_rate") is not None
        assert out2.get("simplify") and "round1" in out2["simplify"]
        assert out2.get("retest") and out2["retest"].get("after_fix") is True
        assert "re-test after fix" in str(out2["retest"].get("summary", ""))
        assert out2["portfolio"].startswith("0 positions")
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_normalize_action_rejects_unknown():
    strat, forced = normalize_action({"action": "hold_existing", "strategy": "hold_existing"})
    assert strat == "hold"
    assert forced["status"] == "hold"
    assert "coerced" in forced["note"]


def test_normalize_action_rejects_trailing_stop_not_in_prompt():
    assert "trailing_stop" not in ALLOWED_ACTIONS
    strat, forced = normalize_action({"action": "trailing_stop", "strategy": "trailing_stop"})
    assert strat == "hold"
    assert forced is not None


@pytest.mark.asyncio
async def test_run_cycle_coerces_invalid_strategy(monkeypatch):
    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)

    async def bad_grok(_g, _p: str) -> str:
        return json.dumps({"action": "hold_existing", "strategy": "hold_existing"})

    monkeypatch.setattr("abcxauto.rocket.grok", bad_grok)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "hold"
    assert out["result"]["status"] == "hold"
    assert "coerced" in out["result"].get("note", "")


@pytest.mark.asyncio
async def test_cycle_smoke_run_cycle_bracket_dispatch(monkeypatch, tmp_path):
    """Shipped run_cycle path: snapshot + bracket action + safe_execute (not direct hold call)."""
    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    calls: list[dict] = []

    async def record_safe_execute(action, conn):
        calls.append(action)
        return {"status": "executed", "strategy": action.get("strategy")}

    async def bracket_grok(_g, prompt: str) -> str:
        if "ONE tweak" in prompt:
            return json.dumps({"type": "none", "summary": "none"})
        return json.dumps({
            "action": "bracket", "strategy": "bracket",
            "params": {
                "symbol": "SPY", "quantity": 1, "direction": "LONG",
                "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
            },
            "rationale": "Current reality: RTH; System 2 ok. smoke",
            "kahneman": {
                "system1_scan": "SPY liquid RTH",
                "system2_base_rate": "equity long brackets ~55% base rate",
                "debias": {"overconfidence": "p_win=0.5"},
                "pre_mortem": "gap through stop",
                "alternatives": ["hold"],
                "bias_audit": ["availability"],
            },
        })

    monkeypatch.setattr("abcxauto.rocket.grok", bracket_grok)
    monkeypatch.setattr("abcxauto.rocket.safe_execute", record_safe_execute)
    snap_out = await snap(FakeConnector())
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    payload = {
        "snapshot_keys": list(snap_out.keys()),
        "strat": out["strat"],
        "result": out["result"],
        "safe_execute_calls": len(calls),
        "dispatched_strategy": calls[0]["strategy"] if calls else None,
    }
    text = json.dumps(payload, indent=2)
    (tmp_path / "cycle_smoke.json").write_text(text, encoding="utf-8")
    scratch = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "cycle_smoke.json").write_text(text, encoding="utf-8")
    assert "protection" in payload["snapshot_keys"]
    assert out["strat"] == "bracket"
    assert payload["safe_execute_calls"] == 1


def test_parse_json_plain():
    assert parse_json('{"action":"hold"}')["action"] == "hold"


def test_parse_json_bad_extract_defaults_hold():
    assert parse_json("noise {not valid json} tail") == {"action": "hold"}


@pytest.mark.asyncio
async def test_run_cycle_dispatches_bracket_to_safe_execute(monkeypatch):
    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    calls: list[dict] = []

    async def record_safe_execute(action, conn):
        calls.append(action)
        return {"status": "executed", "strategy": action.get("strategy")}

    async def bracket_grok(_g, prompt: str) -> str:
        if "ONE tweak" in prompt:
            return json.dumps({"type": "none", "summary": "none"})
        return json.dumps({
            "action": "bracket", "strategy": "bracket",
            "params": {
                "symbol": "SPY", "quantity": 1, "direction": "LONG",
                "entry_price": 500.0, "stop_price": 490.0, "target_price": 510.0,
            },
            "rationale": "test bracket dispatch",
            "kahneman": {
                "system1_scan": "entry setup",
                "system2_base_rate": "RTH liquid ETF base rate",
                "pre_mortem": "false breakout",
                "alternatives": ["hold"],
                "bias_audit": ["anchoring"],
            },
        })

    monkeypatch.setattr("abcxauto.rocket.grok", bracket_grok)
    monkeypatch.setattr("abcxauto.rocket.safe_execute", record_safe_execute)
    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert out["strat"] == "bracket"
    assert len(calls) == 1
    assert calls[0]["strategy"] == "bracket"
    assert out.get("kahneman", {}).get("complete") is True


def test_apply_tweak_merges_config():
    before = dict(TWEAKS)
    try:
        summary = apply_tweak({"type": "config", "config": {"cycle_sleep_s": 3}, "summary": "faster"})
        assert summary == "faster"
        assert TWEAKS["cycle_sleep_s"] == 3
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_pnl_and_equity():
    assert pnl_of({"unrealizedpnl": -12.5}) == -12.5
    assert equity_of({"netliquidation": 50000}) == 50000.0


def test_risk_label_compliant():
    assert risk_label({"protection": {"unprotected_symbols": []}}) == "COMPLIANT"