"""Option facts + stop qty mismatch (B2/B4 Fact surfaces)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.option_facts import (
    format_option_facts_for_prompt,
    occ_symbol,
)
from abcxauto.trade_plan import (
    ActiveTradePlan,
    stop_qty_mismatch_fact,
    working_stop_qty,
)


def test_occ_symbol():
    assert occ_symbol("SPY", "20260718", "C", 500.0) == "SPY260718C00500000"
    assert occ_symbol("AAPL", "260120", "P", 150.0) == "AAPL260120P00150000"
    assert occ_symbol("", "20260718", "C", 1.0) is None


def test_format_option_facts_empty():
    assert "none" in format_option_facts_for_prompt([]).lower()


def test_format_option_facts_lists_legs():
    text = format_option_facts_for_prompt(
        [
            {
                "conId": 1,
                "symbol": "SPY",
                "right": "C",
                "strike": 500,
                "expiration": "20260718",
                "qty": -1,
                "source": "mda",
                "freshness": "delayed",
                "iv": 0.2,
                "delta": 0.4,
            }
        ]
    )
    assert "OPTION FACTS" in text
    assert "SPY" in text
    assert "heuristic" not in text.lower() or "≠" in text or "recommendation" in text


def test_working_stop_qty_and_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    from abcxauto.trade_plan import save_trade_plan

    plan = ActiveTradePlan(
        symbol="SPY",
        direction="LONG",
        entry_price=100.0,
        stop_price=95.0,
        quantity=10,
    )
    save_trade_plan(plan)
    positions = [{"symbol": "SPY", "sec_type": "STK", "quantity": 5}]
    orders = [
        {
            "symbol": "SPY",
            "sec_type": "STK",
            "order_type": "STP",
            "action": "SELL",
            "aux_price": 95.0,
            "quantity": 10,
        }
    ]
    assert working_stop_qty(orders, "SPY", "LONG") == 10.0
    fact = stop_qty_mismatch_fact(positions, orders, plan)
    assert fact is not None
    assert fact["mismatch"] is True
    assert fact["held_qty"] == 5.0
    assert "heuristic" in fact


@pytest.mark.asyncio
async def test_fetch_option_facts_book_only_without_mda(monkeypatch):
    from abcxauto import option_facts as of

    class FakeMDA:
        is_configured = False

    monkeypatch.setattr(
        "abcxauto.marketdata.client.get_marketdata_client",
        lambda: FakeMDA(),
    )
    facts = await of.fetch_option_facts(
        [
            {
                "symbol": "SPY",
                "sec_type": "OPT",
                "quantity": -1,
                "conId": 9,
                "expiration": "20260718",
                "strike": 500,
                "right": "C",
            }
        ]
    )
    assert len(facts) == 1
    assert facts[0]["source"] == "book"
    assert facts[0]["conId"] == 9


@pytest.mark.asyncio
async def test_fetch_option_facts_keeps_ibkr_and_strips_mda_prices(monkeypatch):
    from abcxauto import option_facts as of

    class FakeMDA:
        is_configured = True

        async def get_option_quote(self, occ, **_k):
            return {"delta": 0.4, "iv": 0.2, "bid": 9.9, "ask": 10.1, "last": 10.0, "mid": 10.0}

    class Conn:
        async def get_live_option_quote(self, symbol, expiration, strike, right):
            return {
                "symbol": symbol,
                "bid": 1.1,
                "ask": 1.2,
                "mid": 1.15,
                "source": "ibkr",
                "freshness": "live",
            }

    monkeypatch.setattr(
        "abcxauto.marketdata.client.get_marketdata_client",
        lambda: FakeMDA(),
    )
    facts = await of.fetch_option_facts(
        [
            {
                "symbol": "SPY",
                "sec_type": "OPT",
                "quantity": 1,
                "conId": 9,
                "expiration": "20260718",
                "strike": 500,
                "right": "C",
            }
        ],
        connector=Conn(),
    )
    assert facts[0]["ibkr"]["bid"] == 1.1
    assert facts[0]["mda"]["delta"] == 0.4
    assert "bid" not in facts[0]["mda"]
    assert "ask" not in facts[0]["mda"]
    assert "last" not in facts[0]["mda"]
    assert "mid" not in facts[0]["mda"]


@pytest.mark.asyncio
async def test_fetch_option_facts_covers_capacity(monkeypatch):
    from abcxauto import option_facts as of

    class FakeMDA:
        is_configured = False

    monkeypatch.setattr(
        "abcxauto.marketdata.client.get_marketdata_client",
        lambda: FakeMDA(),
    )
    rows = [
        {
            "symbol": "SPY",
            "sec_type": "OPT",
            "quantity": 1,
            "conId": i,
            "expiration": "20260718",
            "strike": 500 + i,
            "right": "C",
        }
        for i in range(15)
    ]
    facts = await of.fetch_option_facts(rows)
    assert len(facts) == 15


def test_close_option_accepts_conId_only():
    from abcxauto.proposals import validate_proposal

    p = validate_proposal(
        "close_option",
        {"symbol": "SPY", "conId": 999001, "quantity": 1},
        "partial close by conId",
    )
    assert p.strategy == "close_option"
    assert p.params.conId == 999001


def test_world_prompt_includes_option_facts():
    from abcxauto.world_state import WorldState

    ws = WorldState(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=1.0,
        daily_pnl=0.0,
        positions=[
            {
                "symbol": "SPY",
                "sec_type": "OPT",
                "quantity": 1,
                "conId": 1,
                "strike": 500,
                "right": "C",
                "expiration": "20260718",
            }
        ],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="aggressive",
        effective_posture="aggressive",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        option_facts=[
            {
                "conId": 1,
                "symbol": "SPY",
                "right": "C",
                "strike": 500,
                "expiration": "20260718",
                "qty": 1,
                "source": "mda",
                "freshness": "delayed",
                "iv": 0.18,
            }
        ],
    )
    block = ws.prompt_block()
    assert "OPTION FACTS" in block
    assert "SPY" in block
