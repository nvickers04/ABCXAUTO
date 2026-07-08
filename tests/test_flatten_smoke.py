"""Panic flatten — per-position STK vs OPT routing on shipped _flatten_one_position."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from abcxauto.broker.connector import IBKRConnector

SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-2e00de788c89\implementer")

MIXED = [
    {"symbol": "SPY", "quantity": 10, "sec_type": "STK", "conId": 270639},
    {
        "symbol": "SPY", "quantity": 2, "sec_type": "OPT",
        "expiration": "20260718", "strike": 450.0, "right": "C", "conId": 12345678,
    },
]


@pytest.mark.asyncio
async def test_flatten_one_position_routes_stk_and_opt_independently():
    calls: list[tuple] = []
    conn = SimpleNamespace()

    async def _place_order(**kwargs):
        calls.append(("stock_mkt", kwargs))
        return {"success": True}

    async def close_option_position(symbol, **kwargs):
        calls.append(("close_option_position", symbol, kwargs))
        return {"success": True}

    conn._place_order = _place_order
    conn.close_option_position = close_option_position
    flatten = IBKRConnector._flatten_one_position.__get__(conn, IBKRConnector)

    stk_out = await flatten(MIXED[0])
    opt_out = await flatten(MIXED[1])

    assert stk_out["method"] == "stock_mkt"
    assert opt_out["method"] == "close_option_position"
    assert stk_out["success"] and opt_out["success"]
    assert "Closing target = conId" in stk_out["reasoning"]
    assert calls[0][0] == "stock_mkt"
    assert calls[1][0] == "close_option_position"
    assert calls[1][2]["expiration"] == "20260718"

    payload = {"position_results": [stk_out, opt_out], "methods": [c[0] for c in calls], "conId_evidence": "independent per conId, never by symbol"}
    SCRATCH.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "flatten_smoke.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Also write to goal SCRATCH for verification
    goal_scratch = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-2e00de788c89\implementer")
    goal_scratch.mkdir(parents=True, exist_ok=True)
    (goal_scratch / "flatten_smoke.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")