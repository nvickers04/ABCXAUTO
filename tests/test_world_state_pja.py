"""WorldState and trade plan unit tests."""

from __future__ import annotations

from abcxauto.trade_plan import (
    ActiveTradePlan,
    clear_trade_plan,
    load_trade_plan,
    plan_from_bracket_action,
    save_trade_plan,
)
from abcxauto.world_state import (
    book_is_flat,
    build_world_state,
    combo_partner,
    concentration,
    day_facts,
    format_wake,
    format_working_exits,
    lot_ident,
    lot_labels,
    position_avg_facts,
    reconcile_book_with_fills,
    single_leg_vertical_block,
    structure_mix,
    vertical_partner,
)


def test_lot_ident_matches_lot_labels_without_mtm():
    pos = {
        "symbol": "IWM",
        "secType": "OPT",
        "quantity": 1,
        "expiration": "20260821",
        "right": "C",
        "strike": 306.0,
        "avgCost": 310.0,
        "market_price": 2.4,
        "conId": 7,
    }
    assert lot_ident(pos) == "IWM 260821C306.0 long 1"
    labels = lot_labels([pos])
    assert labels[0].startswith("IWM 260821C306.0 long 1")
    assert "-23%" in labels[0]


def test_structure_mix_counts_vertical():
    mix = structure_mix(
        [
            {
                "symbol": "QQQ",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260918",
                "right": "C",
                "strike": 745,
            },
            {
                "symbol": "QQQ",
                "secType": "OPT",
                "quantity": -1,
                "expiration": "20260918",
                "right": "C",
                "strike": 750,
            },
            {
                "symbol": "IWM",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260821",
                "right": "C",
                "strike": 306,
            },
        ]
    )
    assert mix["long_c"] == 2
    assert mix["short_c"] == 1
    assert mix["vert"] == 1


def test_vertical_partner_pairs_closest_opposite_wing():
    long_leg = {
        "symbol": "JPM",
        "secType": "OPT",
        "quantity": 1,
        "expiration": "20260918",
        "right": "C",
        "strike": 370.0,
        "conId": 1,
    }
    short_leg = {
        "symbol": "JPM",
        "secType": "OPT",
        "quantity": -1,
        "expiration": "20260918",
        "right": "C",
        "strike": 375.0,
        "conId": 2,
    }
    lone = {
        "symbol": "IWM",
        "secType": "OPT",
        "quantity": 1,
        "expiration": "20260821",
        "right": "C",
        "strike": 306.0,
        "conId": 3,
    }
    book = [long_leg, short_leg, lone]
    assert vertical_partner(long_leg, book)["conId"] == 2
    assert vertical_partner(short_leg, book)["conId"] == 1
    assert vertical_partner(lone, book) is None
    assert single_leg_vertical_block("close_option", {"conId": 1}, book)
    assert single_leg_vertical_block("close_option", {"conId": 3}, book) is None
    assert single_leg_vertical_block("vertical_spread", {"conId": 1}, book) is None


def test_combo_partner_pairs_calendar_and_short_strangle_not_long_strangle():
    near = {
        "symbol": "SPY", "secType": "OPT", "quantity": -1,
        "expiration": "20260718", "right": "C", "strike": 500.0, "conId": 11,
    }
    far = {
        "symbol": "SPY", "secType": "OPT", "quantity": 1,
        "expiration": "20260815", "right": "C", "strike": 500.0, "conId": 12,
    }
    short_p = {
        "symbol": "IWM", "secType": "OPT", "quantity": -1,
        "expiration": "20260821", "right": "P", "strike": 220.0, "conId": 21,
    }
    short_c = {
        "symbol": "IWM", "secType": "OPT", "quantity": -1,
        "expiration": "20260821", "right": "C", "strike": 230.0, "conId": 22,
    }
    long_p = {
        "symbol": "QQQ", "secType": "OPT", "quantity": 1,
        "expiration": "20260821", "right": "P", "strike": 560.0, "conId": 31,
    }
    long_c = {
        "symbol": "QQQ", "secType": "OPT", "quantity": 1,
        "expiration": "20260821", "right": "C", "strike": 580.0, "conId": 32,
    }
    cal = [near, far]
    assert combo_partner(near, cal)["conId"] == 12
    assert combo_partner(far, cal)["conId"] == 11
    assert single_leg_vertical_block("close_option", {"conId": 11}, cal)
    shorts = [short_p, short_c]
    assert combo_partner(short_p, shorts)["conId"] == 22
    assert single_leg_vertical_block("close_option", {"conId": 21}, shorts)
    longs = [long_p, long_c]
    assert combo_partner(long_p, longs) is None
    assert single_leg_vertical_block("close_option", {"conId": 31}, longs) is None


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
    assert out["by_name"]["XLE"]["extra"] == 2


def test_concentration_vertical_is_not_cloned():
    out = concentration(
        [
            {
                "symbol": "QQQ",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260821",
                "right": "C",
                "strike": 735,
            },
            {
                "symbol": "QQQ",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260918",
                "right": "C",
                "strike": 745,
            },
            {
                "symbol": "QQQ",
                "secType": "OPT",
                "quantity": -1,
                "expiration": "20260918",
                "right": "C",
                "strike": 750,
            },
            {
                "symbol": "JPM",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260918",
                "right": "C",
                "strike": 370,
            },
            {
                "symbol": "JPM",
                "secType": "OPT",
                "quantity": -1,
                "expiration": "20260918",
                "right": "C",
                "strike": 375,
            },
            {
                "symbol": "XLE",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260821",
                "right": "C",
                "strike": 62.5,
            },
            {
                "symbol": "XLE",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260821",
                "right": "C",
                "strike": 63,
            },
            {
                "symbol": "XLE",
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260828",
                "right": "C",
                "strike": 63,
            },
        ]
    )
    assert out["lots"] == 8
    assert out["by_name"]["QQQ"]["lots"] == 3
    assert out["by_name"]["QQQ"]["vert"] == 1
    assert out["by_name"]["QQQ"]["extra"] == 1
    assert out["by_name"]["QQQ"]["structures"] == 2
    assert out["by_name"]["JPM"]["vert"] == 1
    assert out["by_name"]["JPM"]["extra"] == 0
    assert "QQQ" not in out["cloned"]
    assert "JPM" not in out["cloned"]
    assert out["cloned"] == ["XLE"]


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
    assert day["edge_meaning"] == "nl_vs_start_minus_model"
    assert day["ibkr_daily_pnl"] == -80.0
    assert day["open_upnl"] is None
    assert day["cloned"] == ["XLF"]
    assert day["names"] == 1
    assert day["lots"] == 2
    assert day["structures"] == 2
    assert day["open_lots"] == ["XLF STK long 1", "XLF STK long 1"]
    assert "max_risk_per_trade_pct" in day
    assert day["max_risk_per_trade_pct"] == day.get("risk_per_trade_pct")


def test_format_wake_includes_day_facts():
    from abcxauto.park_clock import note_wake

    note_wake(None)
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
            "daily_pnl": -95.0,
            "open_upnl": -88.0,
            "nl_vs_start": -500.0,
            "beating_model": False,
            "max_risk_per_trade_pct": 25.0,
            "risk_per_trade_pct": 25.0,
            "sizing_floors": True,
            "open_lots": ["IWM 260821C306 x1", "QQQ 260821C735 x1"],
            "capacity": {"open_count": 8, "max_open_positions": 15},
            "mix": {"long_c": 7, "short_c": 1, "vert": 1},
            "playbook": {
                "revision": 51,
                "age_h": 20.4,
                "stale": True,
                "ready_to_promote": False,
                "at_write_edge": -549.0,
                "since_write_edge": -91.0,
                "now_edge": -640.0,
                "win_4h": -12.0,
                "ledger": [
                    {"revision": 50, "edge_usd": -400.0},
                    {"revision": 51, "edge_usd": -549.0},
                ],
            },
        },
    )
    from tests.conftest import assert_no_cycle_counter

    assert_no_cycle_counter(text)
    assert "Cycle 3." not in text
    assert "session=regular" in text
    assert "names=3" in text
    assert "lots=8" in text
    assert "cloned=IWM,XLE" not in text
    assert "cloned=" not in text
    assert "struct=" not in text
    assert "edge=-549.0" not in text
    assert "dayPnL=" not in text
    assert "edgeVsModel=$-549.0" in text
    assert "ibkrDay=$-95.0" in text
    assert "openU=$-88.0" in text
    assert "vsStart=$-500.0(inception)" in text
    assert "beating=False" in text
    assert "max_risk=25.0%" in text
    assert "floors=on" in text
    assert "risk/trade=" not in text
    assert "open=8/15" in text
    assert "open_lots=IWM 260821C306 x1,QQQ 260821C735 x1" in text
    assert "haltAt=" in text
    assert "playbook rev=51" not in text
    assert "age=" not in text
    assert "at_write_edge=" not in text
    assert "since_write=" not in text
    assert "now_edge=-640.0" not in text
    assert "4h=-12.0" not in text
    assert "stale=" not in text
    assert "mix=longC:7,shortC:1,vert:1" in text
    assert "ledger r50:-400.0 r51:-549.0" not in text
    assert "looks" not in text
    assert "0sends" not in text
    assert "next=" not in text
    assert "set_wake" not in text
    assert "This is a delta" not in text
    assert "no operator" not in text.lower()
    assert "clerk wake" not in text.lower()


def test_format_wake_prints_lab_waiting_when_the_glance_has_it():
    from abcxauto.park_clock import note_wake

    note_wake(None)
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 0,
            "lots": 0,
            "open_lots": [],
            "capacity": {"open_count": 0, "max_open_positions": 5},
            "max_risk_per_trade_pct": 25.0,
            "playbook": {
                "lab_wake": "unused=bracket+vertical_spread+iron_condor",
            },
        },
    )
    assert "unused=" not in text
    assert "unused=bracket+vertical_spread+iron_condor" not in text
    assert "147looks" not in text
    assert "playbook rev=" not in text
    assert "next=" not in text
    assert "Nlooks" not in text


def test_format_wake_floors_on_still_paints_max_risk():
    from abcxauto.park_clock import note_wake

    note_wake(None)
    text = format_wake(
        cycle=3,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 1,
            "lots": 1,
            "nl": 80_000.0,
            "daily_pnl": -10.0,
            "risk_per_trade_pct": 0.75,
            "sizing_floors": True,
            "open_lots": ["NVDA STK long 5"],
            "capacity": {"open_count": 1, "max_open_positions": 15},
        },
    )
    assert "max_risk=0.75%" in text
    assert "floors=on" in text
    assert "floors=off" not in text
    assert "risk/trade=" not in text
    assert "open_lots=NVDA STK long 5" in text
    assert "qty=" not in text
    assert "next=" not in text
    assert "playbook rev=" not in text


def test_format_working_exits_and_wake_lasts():
    orders = [
        {
            "order_id": 3878,
            "symbol": "AAPL",
            "secType": "STK",
            "orderType": "STP",
            "action": "SELL",
            "quantity": 20,
            "auxPrice": 305.5,
            "conId": 1,
        },
        {
            "order_id": 3879,
            "symbol": "AAPL",
            "secType": "STK",
            "orderType": "LMT",
            "action": "SELL",
            "quantity": 20,
            "lmtPrice": 317.0,
            "conId": 1,
        },
    ]
    positions = [{"symbol": "AAPL", "secType": "STK", "quantity": 20, "conId": 1}]
    exits = format_working_exits(orders, positions)
    assert "AAPL STP 305.5 oid 3878" in exits
    assert "oid 3879" in exits
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "nl": 35221,
            "daily_pnl": -370.0,
            "halt_trips_at_usd": -704.0,
            "open_lots": ["AAPL STK long 20 +0% pre"],
            "lot_lasts": "AAPL last=310.72",
            "working_exits": exits,
            "candle_source": "ibkr_rt_5s",
            "capacity": {"open_count": 1, "max_open_positions": 5},
        },
    )
    assert "AAPL last=310.72" in text
    assert "exits=" in text
    assert "candles=ibkr_rt_5s" in text
    assert "haltAt=$-704.0" in text


def test_format_wake_omits_candles_none():
    """candles=none invited a re-fetch every look after a fresh snap."""
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "nl": 35000,
            "names": 0,
            "lots": 0,
            "candle_source": "none",
            "capacity": {"open_count": 0, "max_open_positions": 5},
        },
    )
    assert "candles=" not in text


def test_format_wake_fill_is_delta_not_discovery():
    from abcxauto.think_stream import write_desk_brief
    from abcxauto.park_clock import BookEvent, note_wake

    write_desk_brief({
        "cycle": 1,
        "strat": "close_option",
        "sends": 2,
        "open_lots": ["XLF 260828C58.5 x1 -42%"],
        "net_liquidation": 35800,
        "mix": {"long_c": 10, "vert": 2},
        "rationale": "close tails",
    })
    note_wake(BookEvent(kind="fill", detail="SPY 260821C780 filled"))
    try:
        text = format_wake(
            cycle=2,
            session="regular",
            flat=False,
            unprotected=[],
            ibkr_up=True,
            day={
                "nl": 35805,
                "daily_pnl": -40.0,
                "open_upnl": -12.0,
                "nl_vs_start": -200.0,
                "names": 3,
                "lots": 11,
                "open_lots": ["XLF 260828C58.5 x1 -42%", "QQQ 260918C745 x1"],
                "capacity": {"open_count": 11, "max_open_positions": 15},
                "mix": {"long_c": 10, "vert": 2},
                "playbook": {
                    "revision": 1,
                    "since_write_edge": -200.0,
                    "now_edge": -800.0,
                    "win_4h": -12.0,
                },
            },
        )
    finally:
        note_wake(None)
    assert "event=fill SPY 260821C780 filled." in text
    assert "prev=close_option sends=2" in text
    assert "This is a delta" not in text
    assert "yield resume" not in text
    assert "next=" not in text
    assert "playbook rev=" not in text
    assert "set_wake" not in text
    assert "send or set_wake" not in text
    assert "Cycle 2." not in text
    assert "open_lots=XLF 260828C58.5 x1 -42%,QQQ 260918C745 x1" in text
    assert "dayPnL=" not in text
    assert "ibkrDay=$-40.0" in text
    assert "openU=$-12.0" in text
    assert "edgeVsModel=" in text


def test_format_wake_spy_stk11_overrides_stale_last_turn_flat():
    """Aug 19: last_turn.flat is not the book. SPY STK 11 / NL 35339.16 stays live."""
    from abcxauto.park_clock import note_wake

    note_wake(None)
    world = type("W", (), {})()
    world.positions = [
        {
            "symbol": "SPY",
            "secType": "STK",
            "quantity": 11,
            "avgCost": 769.5,
            "conId": 136,
        }
    ]
    world.net_liquidation = 35339.16
    world.daily_pnl = 0.0
    world.capacity = {"open_count": 1, "max_open_positions": 15}
    day = day_facts(world, {})
    text = format_wake(
        cycle=9,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day=day,
    )
    assert "flat=False" in text
    assert "flat=True" not in text
    assert "SPY STK long 11" in text
    assert "max_risk=" in text
    assert "floors=" in text
    assert "risk/trade=" not in text


def test_format_wake_wiped_playbook_omits_glance_and_ledger():
    from abcxauto.park_clock import note_wake

    note_wake(None)
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "nl": 35339.16,
            "names": 0,
            "lots": 0,
            "open_lots": [],
            "capacity": {"open_count": 0, "max_open_positions": 15},
            "max_risk_per_trade_pct": 25.0,
            "sizing_floors": False,
            "playbook": {
                "revision": 51,
                "has_instructions": False,
                "instructions": "",
                "since_write_edge": -91.0,
                "now_edge": -640.0,
                "win_4h": -12.0,
                "ledger": [
                    {"revision": 50, "edge_usd": -400.0},
                    {"revision": 51, "edge_usd": -549.0},
                ],
            },
        },
    )
    assert "playbook" not in text
    assert "ledger" not in text
    assert "max_risk=25.0% floors=off" in text
    assert "risk/trade=" not in text
    assert "prev=" not in text


def test_format_wake_flat_empty_omits_leftover_prev():
    from abcxauto.think_stream import write_desk_brief
    from abcxauto.park_clock import note_wake

    write_desk_brief({
        "strat": "market_bracket",
        "sends": 0,
        "send_calls": 0,
        "open_lots": [],
        "tool_trace": ["book", "playbook", "scan", "news", "set_wake"],
        "scan_hits": {
            "arena": "mega_cap",
            "scan_code": "TOP_PERC_LOSE",
            "rows": [{"symbol": "SNDK", "open_gap_pct": -6.5}],
        },
    })
    note_wake(None)
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={
            "nl": 35339.16,
            "names": 0,
            "lots": 0,
            "open_lots": [],
            "capacity": {"open_count": 0, "max_open_positions": 15},
            "risk_per_trade_pct": 25.0,
            "sizing_floors": True,
        },
    )
    assert "flat=True" in text
    assert "prev=" not in text
    assert "open_lots=" not in text
    assert "max_risk=25.0% floors=on" in text
    assert "risk/trade=" not in text
    assert "last_look" not in text
    assert "last_scan" not in text
    assert "looks" not in text


def test_day_facts_wiped_instructions_drop_playbook_glance():
    import json
    import os
    from pathlib import Path

    path = Path(os.environ["ABCXAUTO_PLAYBOOK_LAB_PATH"])
    path.write_text(
        json.dumps(
            {
                "revision": 51,
                "instructions": "   ",
                "ledger": [{"revision": 51, "edge_usd": -12.0}],
            }
        ),
        encoding="utf-8",
    )
    world = type("W", (), {})()
    world.positions = []
    world.net_liquidation = 35339.16
    world.daily_pnl = 0.0
    world.capacity = {}
    day = day_facts(world, {"edge_usd": -12.0})
    assert day.get("playbook") == {}


def _empty_book_snap(**extra: object) -> dict:
    snap: dict = {
        "taken_at": "2026-08-19T18:54:00Z",
        "account": {"netliquidation": 35339.16, "dailypnl": 0},
        "positions": [],
        "open_orders": [],
        "protection": {"unprotected_symbols": []},
        "reality_pulse": {"session": {"status": "regular"}},
        "portfolio_state": {},
        "ibkr_live_quotes": {"SPY": 769.5},
    }
    snap.update(extra)
    return snap


def test_build_world_state_empty_book_no_spy_default_or_leftover_plan():
    save_trade_plan(
        ActiveTradePlan(
            symbol="SPY",
            direction="LONG",
            quantity=11,
            stop_price=760.0,
            thesis="leftover residue",
        )
    )
    assert load_trade_plan() is not None
    ws = build_world_state(
        cycle=1,
        snap=_empty_book_snap(),
        opportunities=[],
        news_items=[],
    )
    assert ws.flat is True
    assert ws.trade_plan is None
    assert ws.trade_plans == []
    assert ws.ibkr_live_symbol == ""
    assert ws.ibkr_live_last is None
    assert load_trade_plan() is not None
    assert ws.to_dict()["open_lots"] == []


def test_build_world_state_spy_stk11_keeps_matching_plan_not_stale_flat():
    save_trade_plan(
        ActiveTradePlan(
            symbol="SPY",
            direction="LONG",
            quantity=11,
            stop_price=760.0,
            thesis="live lot",
        )
    )
    snap = _empty_book_snap(
        positions=[
            {
                "symbol": "SPY",
                "secType": "STK",
                "quantity": 11,
                "avgCost": 769.5,
                "conId": 136,
            }
        ]
    )
    ws = build_world_state(cycle=2, snap=snap, opportunities=[], news_items=[])
    assert ws.flat is False
    assert ws.trade_plan is not None
    assert ws.trade_plan["symbol"] == "SPY"
    assert any("SPY STK long 11" in x for x in ws.to_dict()["open_lots"])
    assert load_trade_plan() is not None


def test_daily_pnl_of_ignores_unrealized_and_keeps_zero():
    from abcxauto.world_state import daily_pnl_of

    assert daily_pnl_of({"dailypnl": 0.0, "unrealizedpnl": -800.0}) == 0.0
    assert daily_pnl_of({"DailyPnL": -12.5, "unrealizedpnl": 99.0}) == -12.5
    assert daily_pnl_of({"unrealizedpnl": -12.5}) is None


def test_open_upnl_of_sums_lot_dollars_not_account_unrealized():
    from abcxauto.world_state import open_upnl_of

    assert open_upnl_of([]) is None
    assert (
        open_upnl_of(
            [
                {"symbol": "JPM", "unrealized_pnl": -64.0},
                {"symbol": "JPM", "unrealizedPNL": 12.0},
                {"symbol": "QQQ", "uPnL": -40.0},
                {"symbol": "QQQ"},
            ]
        )
        == -92.0
    )
    assert open_upnl_of([{"unrealized_pnl": 0.0}]) == 0.0


def test_day_facts_open_upnl_is_not_edge():
    world = type("W", (), {})()
    world.positions = [
        {"symbol": "JPM", "quantity": 1, "unrealized_pnl": -80.0},
        {"symbol": "QQQ", "quantity": 1, "unrealized_pnl": -70.0},
    ]
    world.net_liquidation = 35279.0
    world.daily_pnl = -315.0
    world.capacity = {"open": 2, "max": 15}
    day = day_facts(
        world,
        {
            "beating_model": False,
            "edge_usd": -1374.0,
            "book_pnl": -1300.0,
            "startup_cash": 36579.0,
            "model_cost_usd": 74.0,
        },
    )
    assert day["ibkr_daily_pnl"] == -315.0
    assert day["open_upnl"] == -150.0
    assert day["nl_vs_start"] == -1300.0
    assert day["edge_usd"] == -1374.0
    assert day["edge_meaning"] == "nl_vs_start_minus_model"
    assert day["open_upnl"] != day["edge_usd"]


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


def test_compact_position_includes_mtm_pct():
    from abcxauto.world_state import compact_position

    row = compact_position({
        "symbol": "QQQ",
        "secType": "OPT",
        "quantity": 1,
        "avgCost": 4.0,
        "market_price": 5.0,
    })
    assert row.get("mtm_pct") == 25.0


def test_compact_position_includes_upnl():
    from abcxauto.world_state import compact_position

    row = compact_position({
        "symbol": "QQQ",
        "secType": "OPT",
        "quantity": 1,
        "avgCost": 4.0,
        "market_price": 5.0,
        "unrealized_pnl": -41.5,
    })
    assert row.get("uPnL") == -41.5


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


def test_reconcile_bot_covers_short_lot():
    pos, orders, rec = reconcile_book_with_fills(
        [{"symbol": "SPY", "conId": "11", "quantity": -2, "secType": "OPT"}],
        [{"order_id": 500, "symbol": "SPY"}],
        [
            {
                "side": "BOT",
                "conId": "11",
                "quantity": 2,
                "order_id": 500,
                "ts": "2099-01-01T00:00:00+00:00",
            }
        ],
    )
    assert rec is True
    assert pos == []
    assert orders == []


def test_reconcile_keeps_short_wing_after_opening_bag_sld():
    """Opening SLD must not erase the short leg of a live debit vertical."""
    long_leg = {
        "symbol": "SPY",
        "conId": "7701",
        "quantity": 1,
        "secType": "OPT",
        "expiration": "20260828",
        "strike": 770.0,
        "right": "C",
    }
    short_leg = {
        "symbol": "SPY",
        "conId": "7711",
        "quantity": -1,
        "secType": "OPT",
        "expiration": "20260828",
        "strike": 771.0,
        "right": "C",
    }
    fills = [
        {
            "side": "BOT",
            "conId": "7701",
            "quantity": 1,
            "order_id": 3949,
            "sec_type": "OPT",
            "symbol": "SPY",
            "expiration": "20260828",
            "strike": 770.0,
            "right": "C",
            "price": 6.13,
            "ts": "2099-01-01T00:00:00+00:00",
        },
        {
            "side": "SLD",
            "conId": "7711",
            "quantity": 1,
            "order_id": 3949,
            "sec_type": "OPT",
            "symbol": "SPY",
            "expiration": "20260828",
            "strike": 771.0,
            "right": "C",
            "price": 5.53,
            "ts": "2099-01-01T00:00:00+00:00",
        },
    ]
    pos, orders, rec = reconcile_book_with_fills(
        [long_leg, short_leg],
        [{"order_id": 3949, "symbol": "SPY", "sec_type": "BAG"}],
        fills,
    )
    assert orders == []
    assert rec is True
    by_cid = {str(p.get("conId")): p for p in pos}
    assert by_cid["7701"]["quantity"] == 1
    assert by_cid["7711"]["quantity"] == -1
    mix = structure_mix(pos)
    assert mix["long_c"] == 1
    assert mix["short_c"] == 1
    assert mix["vert"] == 1


def test_reconcile_attaches_missing_short_wing_from_bag_fills():
    """Orphan long after BAG fill: paint the short wing from fills on that look."""
    long_leg = {
        "symbol": "SPY",
        "conId": "7701",
        "quantity": 1,
        "secType": "OPT",
        "expiration": "20260828",
        "strike": 770.0,
        "right": "C",
    }
    fills = [
        {
            "side": "BOT",
            "conId": "7701",
            "quantity": 1,
            "order_id": 3949,
            "sec_type": "OPT",
            "symbol": "SPY",
            "expiration": "20260828",
            "strike": 770.0,
            "right": "C",
            "price": 6.13,
            "ts": "2099-01-01T00:00:00+00:00",
        },
        {
            "side": "SLD",
            "conId": "7711",
            "quantity": 1,
            "order_id": 3949,
            "sec_type": "OPT",
            "symbol": "SPY",
            "expiration": "20260828",
            "strike": 771.0,
            "right": "C",
            "price": 5.53,
            "ts": "2099-01-01T00:00:00+00:00",
        },
    ]
    pos, _orders, rec = reconcile_book_with_fills([long_leg], [], fills)
    assert rec is True
    mix = structure_mix(pos)
    assert mix["long_c"] == 1
    assert mix["short_c"] == 1
    assert mix["vert"] == 1
    labels = lot_labels(pos)
    assert any("770" in x and "long" in x for x in labels)
    assert any("771" in x and "short" in x for x in labels)
    assert single_leg_vertical_block(
        "close_option", {"conId": 7701, "symbol": "SPY"}, pos
    )
    assert single_leg_vertical_block(
        "vertical_spread",
        {
            "symbol": "SPY",
            "expiration": "20260828",
            "long_strike": 770.0,
            "short_strike": 771.0,
            "right": "C",
            "quantity": 1,
            "closing_position": True,
            "limit_price": 0.55,
        },
        pos,
    ) is None


def test_reconcile_does_not_resurrect_closed_bag_from_fills_alone():
    """Closing BAG fills must not invent a ghost inverted combo when flat."""
    fills = [
        {
            "side": "SLD",
            "conId": "7701",
            "quantity": 1,
            "order_id": 4001,
            "sec_type": "OPT",
            "symbol": "SPY",
            "expiration": "20260828",
            "strike": 770.0,
            "right": "C",
            "ts": "2099-01-01T00:00:00+00:00",
        },
        {
            "side": "BOT",
            "conId": "7711",
            "quantity": 1,
            "order_id": 4001,
            "sec_type": "OPT",
            "symbol": "SPY",
            "expiration": "20260828",
            "strike": 771.0,
            "right": "C",
            "ts": "2099-01-01T00:00:00+00:00",
        },
    ]
    pos, _orders, rec = reconcile_book_with_fills([], [], fills)
    assert pos == []
    assert rec is False


def test_build_world_state_bag_fill_shows_vert_same_look(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
    from abcxauto.memory import reset_journal

    reset_journal(path=str(tmp_path / "j.db"), enabled=True)
    snap = {
        "taken_at": "2026-08-19T15:00:00Z",
        "account": {"netliquidation": 100000, "dailypnl": 0},
        "positions": [
            {
                "symbol": "SPY",
                "conId": 7701,
                "secType": "OPT",
                "quantity": 1,
                "expiration": "20260828",
                "strike": 770.0,
                "right": "C",
                "avgCost": 6.13,
                "market_price": 6.13,
            }
        ],
        "open_orders": [],
        "fills": [
            {
                "side": "BOT",
                "conId": 7701,
                "quantity": 1,
                "order_id": 3949,
                "sec_type": "OPT",
                "symbol": "SPY",
                "expiration": "20260828",
                "strike": 770.0,
                "right": "C",
                "price": 6.13,
                "ts": "2099-01-01T00:00:00+00:00",
            },
            {
                "side": "SLD",
                "conId": 7711,
                "quantity": 1,
                "order_id": 3949,
                "sec_type": "OPT",
                "symbol": "SPY",
                "expiration": "20260828",
                "strike": 771.0,
                "right": "C",
                "price": 5.53,
                "ts": "2099-01-01T00:00:00+00:00",
            },
        ],
        "protection": {"unprotected_symbols": []},
        "reality_pulse": {"session": {"status": "regular"}},
        "portfolio_state": {},
    }
    ws = build_world_state(cycle=4, snap=snap, opportunities=[], news_items=[])
    d = ws.to_dict()
    assert d["mix"]["vert"] == 1
    assert d["mix"]["long_c"] == 1
    assert d["mix"]["short_c"] == 1
    assert any("771" in x and "short" in x for x in d["open_lots"])
    assert ws.book_reconciled is True


def test_build_world_state_regime_and_portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
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
    assert d["portfolio_risk"]["top_concentration_pct"] == round(100.0 * 5000 / 37000, 2)
    assert d["portfolio_risk"]["exposure"]["symbols"][0]["pct_nl"] == round(
        100.0 * 5000 / 37000, 2
    )
    assert "cash_pct_nl" in d["portfolio_risk"]["capital_liquidity"]
    assert "deployed_long_pct_nl" in d["portfolio_risk"]["capital_liquidity"]


def test_compact_rows_keep_trail_order_and_avg_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
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
    from abcxauto.world_state import compact_position, compact_working_orders

    rows = compact_working_orders(ws.open_orders, positions=ws.positions)
    assert [r["order_id"] for r in rows] == [270]
    assert rows[0]["type"] == "TRAIL"
    lot = compact_position(ws.positions[0], extra=True, net_liq=ws.net_liquidation)
    assert lot["avg"] == 19.535


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
    assert rows[0]["role"] == "entry"


def test_compact_working_orders_tags_exit_of_long_call():
    from abcxauto.world_state import compact_working_orders

    pos = {
        "symbol": "XLF",
        "sec_type": "OPT",
        "quantity": 1,
        "strike": 58.5,
        "right": "C",
        "expiration": "20260828",
        "conId": 899950329,
    }
    rows = compact_working_orders(
        [
            {
                "order_id": 3421,
                "symbol": "XLF",
                "sec_type": "OPT",
                "order_type": "LMT",
                "action": "SELL",
                "quantity": 1,
                "lmt_price": 0.26,
                "strike": 58.5,
                "right": "C",
                "expiration": "20260828",
                "conId": 899950329,
            }
        ],
        positions=[pos],
    )
    assert rows[0]["role"] == "exit"
    assert "XLF" in str(rows[0]["covers"])
    assert "long" in str(rows[0]["covers"])
    assert rows[0]["conId"] == 899950329


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
    from_bracket = plan_from_bracket_action(act, "fade thesis")
    assert from_bracket is not None
    assert from_bracket.direction == "SHORT"
