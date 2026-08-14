"""WorldState and trade plan unit tests."""

from __future__ import annotations

from abcxauto.trade_plan import (
    ActiveTradePlan,
    clear_trade_plan,
    load_trade_plan,
    plan_from_hunt_action,
    save_trade_plan,
)
from abcxauto.world_state import (
    book_is_flat,
    build_world_state,
    concentration,
    day_facts,
    format_wake,
    position_avg_facts,
    reconcile_book_with_fills,
    reset_idle_streak,
)


def test_concentration_flags_cloned_names():
    out = concentration(
        [
            {"symbol": "XLE", "quantity": 1},
            {"symbol": "XLE", "quantity": 1},
            {"symbol": "XLF", "quantity": 1},
            {"symbol": "IWM", "quantity": 1},
        ]
    )
    assert out["names"] == 3
    assert out["lots"] == 4
    assert out["cloned"] == ["XLE"]
    assert out["by_name"]["XLE"]["lots"] == 2


def test_day_facts_carry_edge_and_clones():
    world = type("W", (), {})()
    world.positions = [
        {"symbol": "XLF", "quantity": 1},
        {"symbol": "XLF", "quantity": 1},
    ]
    world.net_liquidation = 36000.0
    world.daily_pnl = -80.0
    world.capacity = {"open": 2, "max": 15}
    day = day_facts(
        world,
        {
            "beating_model": False,
            "edge_usd": -400.0,
            "book_return_pct": -1.2,
            "model_cost_usd": 3.0,
        },
    )
    assert day["beating_model"] is False
    assert day["edge_usd"] == -400.0
    assert day["cloned"] == ["XLF"]
    assert day["names"] == 1
    assert day["lots"] == 2


def test_format_wake_includes_day_facts():
    text = format_wake(
        cycle=3,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 3,
            "lots": 8,
            "cloned": ["IWM", "XLE"],
            "edge_usd": -549.0,
            "beating_model": False,
            "risk_per_trade_pct": 25.0,
            "capacity": {"open_count": 8, "max_open_positions": 15},
            "playbook": {
                "revision": 51,
                "age_h": 20.4,
                "ready_to_promote": False,
                "at_write_edge": -549.0,
                "now_edge": -640.0,
            },
        },
    )
    assert "Cycle 3." in text
    assert "session=regular" in text
    assert "names=3" in text
    assert "lots=8" in text
    assert "cloned=IWM,XLE" in text
    assert "edge=-549.0" in text
    assert "beating=False" in text
    assert "risk/trade=25.0%" in text
    assert "open=8/15" in text
    assert "playbook rev=51" in text
    assert "age=20.4h" in text
    assert "at_write_edge=-549.0" in text
    assert "now_edge=-640.0" in text
    assert "Use tools." in text


def test_book_is_flat_false_when_working_order_or_pending_fill():
    assert book_is_flat([], [], []) is True
    assert book_is_flat([], [{"order_id": 426, "symbol": "IWM"}], []) is False
    assert book_is_flat(
        [],
        [],
        [{
            "side": "BOT",
            "symbol": "IWM",
            "conId": 900337571,
            "price": 1.18,
            "ts": "2099-01-01T00:00:00+00:00",
        }],
    ) is False
    assert book_is_flat(
        [],
        [],
        [{
            "side": "BOT",
            "symbol": "IWM",
            "conId": 1,
            "ts": "2020-01-01T00:00:00+00:00",
        }],
    ) is True
    assert book_is_flat(
        [{"symbol": "IWM", "conId": 900337571, "quantity": 1}],
        [],
        [{"side": "BOT", "conId": 900337571}],
    ) is False


def test_compact_position_keeps_option_identity():
    from abcxauto.world_state import compact_position

    row = compact_position({
        "symbol": "XLE",
        "conId": 9,
        "secType": "OPT",
        "quantity": 1,
        "avg_cost": 119.0,
        "market_price": 1.19,
        "expiration": "20260828",
        "strike": 63.0,
        "right": "C",
        "localSymbol": "XLE   260828C00063000",
    })
    assert row["expiration"] == "20260828"
    assert row["strike"] == 63.0
    assert row["right"] == "C"
    assert "XLE" in str(row["local"])


def test_option_avg_is_per_share_when_ibkr_sends_contract_cash():
    row = position_avg_facts({
        "secType": "OPT",
        "avg_cost": 126.0,
        "market_price": 1.26,
    })
    assert abs(row["avg"] - 1.26) < 1e-9
    assert row["avg_usd"] == 126.0
    stk = position_avg_facts({"secType": "STK", "avg_cost": 119.5, "market_price": 120.0})
    assert stk["avg"] == 119.5
    assert "avg_usd" not in stk


def test_reconcile_book_drops_sold_lot_and_filled_order():
    pos, orders, rec = reconcile_book_with_fills(
        [{"symbol": "IWM", "conId": "9", "quantity": 1, "secType": "OPT"}],
        [{"order_id": 426, "symbol": "IWM"}],
        [
            {
                "side": "SLD",
                "conId": "9",
                "quantity": 1,
                "order_id": 426,
                "ts": "2099-01-01T00:00:00+00:00",
            }
        ],
    )
    assert rec is True
    assert pos == []
    assert orders == []


def test_build_world_state_regime_and_portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
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
        {"symbol": "QQQ", "bias": "LONG", "score": 0.8, "above_sma20": True},
        {"symbol": "IWM", "bias": "LONG", "score": 0.7, "above_sma20": True},
        {"symbol": "SPY", "bias": "LONG", "score": 0.6, "above_sma20": True},
    ]
    ws = build_world_state(cycle=2, snap=snap, opportunities=opps, news_items=[])
    d = ws.to_dict()
    assert d["cycle"] == 2
    assert d["regime"]["trend_bias"] == "bullish"
    assert d["portfolio_risk"]["n_positions"] == 1
    assert d["portfolio_risk"]["top_symbol"] == "QQQ"
    assert "WORLDSTATE" in ws.prompt_block()


def test_prompt_block_includes_working_orders_and_avg(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_IDLE_STREAK_PATH", str(tmp_path / "idle.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
    reset_idle_streak()
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "j.db"), enabled=True)
    snap = {
        "taken_at": "2026-08-13T17:00:00Z",
        "account": {"netliquidation": 37000, "dailypnl": 0},
        "positions": [
            {
                "symbol": "ARX",
                "conId": 801327622,
                "secType": "STK",
                "quantity": 280,
                "avgCost": 19.535,
                "market_price": 19.62,
            },
        ],
        "open_orders": [
            {
                "order_id": 270,
                "symbol": "ARX",
                "order_type": "TRAIL",
                "action": "SELL",
                "quantity": 280,
                "trail_percent": 1.5,
            }
        ],
        "protection": {"unprotected_symbols": []},
        "reality_pulse": {"session": {"status": "regular"}},
        "portfolio_state": {},
    }
    ws = build_world_state(cycle=3, snap=snap, opportunities=[], news_items=[])
    block = ws.prompt_block()
    assert "working_orders" in block
    assert "270" in block
    assert "TRAIL" in block
    assert "19.535" in block


def test_compact_working_orders_keeps_option_identity():
    from abcxauto.world_state import compact_working_orders

    rows = compact_working_orders(
        [
            {
                "order_id": 426,
                "symbol": "IWM",
                "sec_type": "OPT",
                "order_type": "LMT",
                "action": "BUY",
                "quantity": 1,
                "lmt_price": 1.18,
                "strike": 310.0,
                "right": "C",
                "expiration": "20260828",
                "local_symbol": "IWM   260828C00310000",
            }
        ]
    )
    assert rows[0]["sec"] == "OPT"
    assert rows[0]["strike"] == 310.0
    assert rows[0]["right"] == "C"
    assert rows[0]["lmt"] == 1.18
    assert "310" in str(rows[0].get("local") or "")


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
