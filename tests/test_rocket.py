"""Rocket loop — drives shipped snap/run_cycle on real code paths."""

import json
from pathlib import Path

import pytest

from abcxauto.executor import safe_execute
from abcxauto.rocket import (
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
    assert set(out.keys()) == {
        "taken_at", "account", "positions", "open_orders", "market_hours", "spy_quote", "protection",
    }
    assert out["account"]["netliquidation"] == 1000


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
        out2 = await run_cycle(2, FakeConnector(), None, hist, out["pnl"])
        assert out2["tweak"] == "faster"
        assert TWEAKS.get("cycle_sleep_s") == 2
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_normalize_action_rejects_unknown():
    strat, forced = normalize_action({"action": "hold_existing", "strategy": "hold_existing"})
    assert strat == "hold"
    assert forced["status"] == "hold"
    assert "coerced" in forced["note"]


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
async def test_cycle_smoke_snap_and_safe_execute(monkeypatch, tmp_path):
    monkeypatch.setattr("abcxauto.rocket._tool", _fake_tool)
    snap_out = await snap(FakeConnector())
    hold = await safe_execute({"action": "hold"}, FakeConnector())
    payload = {"snapshot_keys": list(snap_out.keys()), "hold": hold}
    text = json.dumps(payload, indent=2)
    (tmp_path / "cycle_smoke.json").write_text(text, encoding="utf-8")
    scratch = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "cycle_smoke.json").write_text(text, encoding="utf-8")
    assert "protection" in payload["snapshot_keys"]
    assert hold["status"] == "hold"


def test_parse_json_plain():
    assert parse_json('{"action":"hold"}')["action"] == "hold"


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