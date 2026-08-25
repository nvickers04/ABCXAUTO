"""Tool-call aliases: wrong names/keys still hit IBKR/MDA correctly."""

from __future__ import annotations

import json

import pytest

from abcxauto.brain import BrainTurn, _run_tool
from abcxauto.tool_args import (
    OPTION_QUOTE_CAP,
    fallback_quote_symbols,
    hoist_send_params,
    normalize_tool_call,
    option_quote_specs,
    strip_ambiguous_last,
)
from abcxauto.world_state import WorldState


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
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def test_alias_positions_to_book():
    name, args = normalize_tool_call("positions", {})
    assert name == "book"
    assert args == {}


def test_alias_get_quote_ticker():
    name, args = normalize_tool_call("get_quote", {"ticker": "spy"})
    assert name == "quote"
    assert args["symbol"] == "spy"


def test_bare_quote_uses_book_symbols():
    name, args = normalize_tool_call(
        "quote",
        {},
        fallback_symbols=fallback_quote_symbols(
            _world(positions=[{"symbol": "IWM", "sec_type": "STK", "quantity": 10}]),
            {},
        ),
    )
    assert name == "quote"
    assert "IWM" in args["symbols"]
    assert args["symbols"][0] == "IWM"


def test_bare_quote_uses_scan_hits_not_spy():
    snap = {
        "scan_hits": {
            "rows": [
                {"symbol": "SNDK", "open_gap_pct": -6.5},
                {"symbol": "MU", "open_gap_pct": -3.3},
            ]
        }
    }
    fb = fallback_quote_symbols(_world(positions=[]), snap)
    assert fb[:2] == ["SNDK", "MU"]
    assert "SPY" not in fb
    name, args = normalize_tool_call("quote", {}, fallback_symbols=fb)
    assert args["symbols"][:2] == ["SNDK", "MU"]
    news_name, news_args = normalize_tool_call("news", {}, fallback_symbols=fb)
    assert news_name == "news"
    assert news_args["symbols"][:2] == ["SNDK", "MU"]
    candle_name, candle_args = normalize_tool_call("candles", {}, fallback_symbols=fb)
    assert candle_name == "candles"
    assert candle_args["symbols"][:2] == ["SNDK", "MU"]


def test_bare_quote_empty_book_still_has_spy(monkeypatch):
    monkeypatch.setattr("abcxauto.think_stream.last_look_facts", lambda: {})
    monkeypatch.setattr("abcxauto.think_stream.last_look_for_hunt", lambda: {})
    fb = fallback_quote_symbols(_world(positions=[]), {})
    assert fb == ["SPY"]


def test_bare_quote_empty_tape_does_not_invent_spy_on_a_live_card(monkeypatch):
    from abcxauto.lab_playbook import clamp_update, save_lab

    monkeypatch.setattr("abcxauto.think_stream.last_look_for_hunt", lambda: {})
    update = clamp_update(
        {
            "instructions": "Skip SPY same-session scrape.",
            "types": {
                "market_bracket": {
                    "gotchas": "do not re-ticket SPY the same session",
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            },
        }
    )
    assert update is not None
    save_lab(update)
    fb = fallback_quote_symbols(_world(positions=[]), {})
    assert fb == []
    name, args = normalize_tool_call("quote", {}, fallback_symbols=fb)
    assert args.get("symbols") in (None, "", [])
    assert args.get("symbol") in (None, "")


def test_send_hoists_flat_fields():
    out = hoist_send_params(
        {"strategy": "market_bracket", "symbol": "SPY", "direction": "LONG", "qty": 4}
    )
    assert out["params"]["symbol"] == "SPY"
    assert out["params"]["direction"] == "LONG"
    assert out["params"]["quantity"] == 4


def test_candles_keeps_symbols_batch():
    name, args = normalize_tool_call("candles", {"symbols": ["SPY", "QQQ"]})
    assert name == "candles"
    assert args["symbols"] == ["SPY", "QQQ"]
    assert args.get("symbol") != "SPY"


def test_option_chain_symbol_list_becomes_symbols():
    name, args = normalize_tool_call("chain", {"symbol": ["IWM", "XLE"]})
    assert name == "option_chain"
    assert args["symbols"] == ["IWM", "XLE"]


def test_set_risk_alias_is_self_tune():
    name, args = normalize_tool_call("set_risk", {"max_risk_per_trade_pct": 0.5})
    assert name == "self_tune"
    assert args["max_risk_per_trade_pct"] == 0.5


def test_option_quote_aliases():
    name, args = normalize_tool_call(
        "greeks",
        {"ticker": "SPY", "expiry": "2026-08-21", "strike": 500, "right": "call"},
    )
    assert name == "option_quote"
    assert args["symbol"] == "SPY"
    assert args["expiration"] == "20260821"
    assert args["right"] == "C"
    assert args["contracts"][0]["symbol"] == "SPY"


def test_option_quote_specs_batch():
    specs = option_quote_specs({
        "contracts": [
            {"symbol": "SPY", "expiration": "2026-08-21", "strike": 500, "right": "call"},
            {"ticker": "QQQ", "expiry": "20260821", "strike": 400, "right": "P"},
        ]
    })
    assert len(specs) == 2
    assert specs[0] == {"symbol": "SPY", "expiration": "20260821", "strike": 500, "right": "C"}
    assert specs[1]["symbol"] == "QQQ"
    assert specs[1]["right"] == "P"


def test_option_quote_specs_cap():
    specs = option_quote_specs({
        "contracts": [
            {"symbol": "SPY", "expiration": "20260821", "strike": 500 + i, "right": "C"}
            for i in range(12)
        ]
    })
    assert len(specs) == OPTION_QUOTE_CAP


def test_strip_mda_last():
    row = strip_ambiguous_last({"symbol": "SPY", "last": 500.0, "source": "mda"})
    assert "last" not in row
    assert row["mda_last"] == 500.0
    live = strip_ambiguous_last({"symbol": "SPY", "last": 501.0, "source": "ibkr"})
    assert live["last"] == 501.0


@pytest.mark.asyncio
async def test_run_tool_accepts_ticker_alias():
    class Conn:
        async def get_live_quote(self, symbol, fresh=False):
            return {
                "symbol": symbol,
                "last": 501.0,
                "source": "ibkr",
                "freshness": "live",
            }

    raw = await _run_tool(
        "get_quote",
        {"ticker": "SPY"},
        connector=Conn(),
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    data = json.loads(raw)
    assert data["source"] == "ibkr"
    assert data["last"] == 501.0


@pytest.mark.asyncio
async def test_run_tool_positions_alias_is_book():
    raw = await _run_tool(
        "positions",
        {},
        connector=object(),
        world=_world(),
        snap={},
        turn=BrainTurn(),
    )
    data = json.loads(raw)
    assert "world" in data
