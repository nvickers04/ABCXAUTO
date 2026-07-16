"""WorldState, trade plan, session cadence unit tests."""

from __future__ import annotations

from abcxauto.session_cadence import load_prep, maybe_auto_review_from_cycle, write_prep
from abcxauto.trade_plan import (
    ActiveTradePlan,
    clear_trade_plan,
    load_trade_plan,
    plan_from_hunt_action,
    save_trade_plan,
)
from abcxauto.world_state import build_world_state, reset_idle_streak


def test_build_world_state_regime_and_portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
    reset_idle_streak()
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "j.db"), enabled=True)

    snap = {
        "taken_at": "2026-07-16T14:00:00Z",
        "account": {"netliquidation": 37000, "dailypnl": 12},
        "positions": [
            {"symbol": "QQQ", "conId": 1, "secType": "STK", "quantity": 10, "marketValue": 5000},
        ],
        "open_orders": [],
        "protection": {"unprotected_symbols": []},
        "reality_pulse": {"session": {"status": "regular", "current_time_et": "11:00"}},
        "portfolio_state": {},
    }
    opps = [
        {"symbol": "QQQ", "bias": "LONG", "score": 0.8},
        {"symbol": "IWM", "bias": "LONG", "score": 0.7},
        {"symbol": "SPY", "bias": "LONG", "score": 0.6},
    ]
    ws = build_world_state(cycle=2, snap=snap, opportunities=opps, news_items=[])
    d = ws.to_dict()
    assert d["cycle"] == 2
    assert d["regime"]["trend_bias"] == "bullish"
    assert d["portfolio_risk"]["n_positions"] == 1
    assert d["portfolio_risk"]["top_symbol"] == "QQQ"
    assert "WORLDSTATE" in ws.prompt_block()
    assert ws.prep  # auto-prep


def test_trade_plan_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    clear_trade_plan()
    plan = ActiveTradePlan(
        symbol="QQQ",
        direction="LONG",
        thesis="pullback",
        stop_price=400.0,
        target_price=420.0,
        quantity=2,
    )
    save_trade_plan(plan)
    loaded = load_trade_plan()
    assert loaded is not None
    assert loaded.symbol == "QQQ"
    assert loaded.stop_price == 400.0
    act = {
        "strategy": "market_bracket",
        "params": {
            "symbol": "IWM",
            "direction": "SHORT",
            "stop_price": 200,
            "target_price": 190,
            "quantity": 1,
        },
        "rationale": "fade",
    }
    from_hunt = plan_from_hunt_action(act, "fade thesis")
    assert from_hunt is not None
    assert from_hunt.direction == "SHORT"


def test_session_prep_review(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_SESSION_PREP_PATH", str(tmp_path / "prep.json"))
    monkeypatch.setenv("ABCXAUTO_SESSION_REVIEW_PATH", str(tmp_path / "review.json"))
    write_prep(bias="bullish", watchlist=["QQQ", "IWM"], notes="test")
    p = load_prep()
    assert p["bias"] == "bullish"
    assert "QQQ" in p["watchlist"]
    rev = maybe_auto_review_from_cycle(
        {"force": True, "thesis": "worked", "next_change": "size smaller"}
    )
    assert rev is not None
    assert rev["next_change"] == "size smaller"
