"""Option facts + stop qty mismatch (B2/B4 Fact surfaces)."""

from __future__ import annotations

import pytest

from abcxauto.option_facts import (
    net_fill_premium_usd,
    occ_symbol,
    signed_fill_premium_usd,
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


def test_signed_fill_keeps_debit_and_credit():
    debit = signed_fill_premium_usd(
        {
            "avg_cost": 1.25,
            "quantity": 2,
            "sec_type": "OPT",
            "market_price": 1.25,
        }
    )
    credit = signed_fill_premium_usd(
        {
            "avg_cost": 1.25,
            "quantity": -2,
            "sec_type": "OPT",
            "market_price": 1.25,
        }
    )
    assert debit == -250.0
    assert credit == 250.0
    # contract-cash IBKR averageCost (126 vs mark 1.26) stays 1-lot debit, not 126*100
    assert signed_fill_premium_usd(
        {
            "avg_cost": 126.0,
            "quantity": 1,
            "sec_type": "OPT",
            "market_price": 1.26,
        }
    ) == -126.0


def test_last_is_not_a_fill_premium():
    last_only = {
        "last": 2.50,
        "mid": 2.48,
        "market_price": 2.50,
        "quantity": 2,
        "sec_type": "OPT",
        "right": "C",
        "strike": 500,
    }
    assert signed_fill_premium_usd(last_only) is None
    assert net_fill_premium_usd([last_only, {**last_only, "quantity": -2}]) is None
    # last sitting next to a real avg cost must not replace the fill
    assert signed_fill_premium_usd(
        {
            "avg_cost": 2.00,
            "last": 9.99,
            "market_price": 2.00,
            "quantity": 1,
            "sec_type": "OPT",
        }
    ) == -200.0


def test_qty_blind_premium_is_not_cash():
    assert signed_fill_premium_usd(
        {"avg_cost": 1.25, "side": "BUY", "sec_type": "OPT"}
    ) is None
    assert signed_fill_premium_usd(
        {"avg_cost": 1.25, "quantity": 0, "side": "SELL", "sec_type": "OPT"}
    ) is None


def test_debit_vertical_does_not_invert():
    long_call = {
        "avg_cost": 2.00,
        "quantity": 1,
        "sec_type": "OPT",
        "right": "C",
        "strike": 370,
        "market_price": 2.00,
    }
    short_call = {
        "avg_cost": 0.75,
        "quantity": -1,
        "sec_type": "OPT",
        "right": "C",
        "strike": 375,
        "market_price": 0.75,
    }
    assert signed_fill_premium_usd(long_call) == -200.0
    assert signed_fill_premium_usd(short_call) == 75.0
    assert net_fill_premium_usd([long_call, short_call]) == -125.0
    # last on a wing fails the combo — do not invent a credit
    assert net_fill_premium_usd(
        [long_call, {**short_call, "avg_cost": None, "last": 0.75}]
    ) is None
    # credit vertical stays credit (unsigned +qty + SELL must not flip it)
    assert net_fill_premium_usd(
        [
            {**long_call, "quantity": 1, "side": "SELL"},
            {**short_call, "quantity": 1, "side": "BUY"},
        ]
    ) == 125.0


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


@pytest.mark.asyncio
async def test_fetch_option_facts_fill_premium_not_last(monkeypatch):
    from abcxauto import option_facts as of

    class FakeMDA:
        is_configured = False

    class Conn:
        async def get_live_option_quote(self, symbol, expiration, strike, right):
            return {
                "symbol": symbol,
                "bid": 2.3,
                "ask": 2.5,
                "last": 2.40,
                "mid": 2.40,
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
                "conId": 1,
                "expiration": "20260718",
                "strike": 500,
                "right": "C",
                "avg_cost": 2.00,
                "market_price": 2.40,
                "last": 2.40,
            },
            {
                "symbol": "SPY",
                "sec_type": "OPT",
                "quantity": -1,
                "conId": 2,
                "expiration": "20260718",
                "strike": 505,
                "right": "C",
                "avg_cost": 0.75,
                "market_price": 0.80,
                "last": 0.80,
            },
        ],
        connector=Conn(),
    )
    by_id = {f["conId"]: f for f in facts}
    assert by_id[1]["fill_premium_usd"] == -200.0
    assert by_id[1]["fill_px"] == 2.00
    assert by_id[2]["fill_premium_usd"] == 75.0
    assert by_id[1]["combo_net_usd"] == -125.0
    assert by_id[2]["combo_net_usd"] == -125.0
    assert by_id[1]["ibkr"]["last"] == 2.40
    last_only = await of.fetch_option_facts(
        [
            {
                "symbol": "SPY",
                "sec_type": "OPT",
                "quantity": 1,
                "conId": 3,
                "expiration": "20260718",
                "strike": 500,
                "right": "C",
                "last": 2.40,
                "market_price": 2.40,
            }
        ]
    )
    assert "fill_premium_usd" not in last_only[0]
    assert "combo_net_usd" not in last_only[0]


def test_close_option_accepts_conId_only():
    from abcxauto.proposals import validate_proposal

    p = validate_proposal(
        "close_option",
        {"symbol": "SPY", "conId": 999001, "quantity": 1},
        "partial close by conId",
    )
    assert p.strategy == "close_option"
    assert p.params.conId == 999001


def test_world_state_carries_option_facts():
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
    legs = ws.to_dict()["option_facts"]
    assert [leg["symbol"] for leg in legs] == ["SPY"]
    assert legs[0]["iv"] == 0.18
