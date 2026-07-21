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
        idle_streak=0,
        idle_top_symbol="",
        prep={},
        review={},
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


def test_operator_card_example_not_loaded_by_default(tmp_path, monkeypatch):
    """example file must not become the live Card."""
    monkeypatch.delenv("ABCXAUTO_OPERATOR_CARD", raising=False)
    monkeypatch.setenv(
        "ABCXAUTO_OPERATOR_CARD_PATH", str(tmp_path / "missing_card.txt")
    )
    from abcxauto.config import load_operator_card

    assert load_operator_card() == ""
    # example exists in repo but is not the load path
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "operator_card.example.txt"
    assert example.is_file()
    assert "shell defaults" in example.read_text(encoding="utf-8").lower() or (
        "not shell" in example.read_text(encoding="utf-8").lower()
    )
