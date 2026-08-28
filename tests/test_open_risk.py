"""Open-risk continuity: reconcile, confirmed-flat, pause keeps plan file."""

from __future__ import annotations

from abcxauto.trade_plan import (
    ActiveTradePlan,
    clear_trade_plan,
    format_open_risk_line,
    load_flat_streak,
    load_trade_plan,
    maybe_close_on_confirmed_flat,
    reconcile_open_risk,
    reset_flat_streak,
    save_trade_plan,
    sync_open_risk,
)


def _pos(symbol="IWM", qty=22.0, avg=295.75, mv=6500.0):
    return {
        "symbol": symbol,
        "secType": "STK",
        "quantity": qty,
        "avgCost": avg,
        "marketValue": mv,
        "conId": 1,
    }


def _stop_tgt(symbol="IWM", stop=293.4, target=300.5):
    return [
        {
            "symbol": symbol,
            "action": "SELL",
            "order_type": "STP",
            "aux_price": stop,
            "order_id": 11,
        },
        {
            "symbol": symbol,
            "action": "SELL",
            "order_type": "LMT",
            "lmt_price": target,
            "order_id": 12,
        },
    ]


def test_reconcile_rebuilds_from_book_when_plan_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    clear_trade_plan()
    reset_flat_streak()
    plan = reconcile_open_risk(
        [_pos()],
        _stop_tgt(),
        existing_plan=None,
        thesis="IWM SMA pullback",
    )
    assert plan is not None
    assert plan.symbol == "IWM"
    assert plan.direction == "LONG"
    assert plan.quantity == 22.0
    assert plan.stop_price == 293.4
    assert plan.target_price == 300.5
    assert plan.entry_price == 295.75
    assert plan.thesis == ""
    assert "OPEN RISK" in format_open_risk_line(plan)
    assert "cycles=" not in format_open_risk_line(plan)


def test_reconcile_refreshes_existing_plan_qty_and_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    existing = ActiveTradePlan(
        symbol="IWM",
        direction="LONG",
        thesis="keep me",
        stop_price=290.0,
        target_price=310.0,
        quantity=10,
        cycles_open=3,
    )
    plan = reconcile_open_risk(
        [_pos(qty=22)],
        _stop_tgt(stop=293.4, target=300.5),
        existing_plan=existing,
    )
    assert plan is not None
    assert plan.quantity == 22.0
    assert plan.stop_price == 293.4
    assert plan.target_price == 300.5
    assert plan.cycles_open == 3
    assert plan.thesis == "keep me"


def test_single_empty_snap_does_not_close_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    clear_trade_plan()
    reset_flat_streak()
    save_trade_plan(
        ActiveTradePlan(symbol="IWM", direction="LONG", stop_price=293.4, quantity=22)
    )
    assert maybe_close_on_confirmed_flat([], needed=2) is False
    assert load_trade_plan() is not None
    assert load_flat_streak() == 1
    assert maybe_close_on_confirmed_flat([], needed=2) is True
    assert load_trade_plan() is None


def test_sync_allow_flat_close_false_keeps_disk_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    clear_trade_plan()
    reset_flat_streak()
    save_trade_plan(
        ActiveTradePlan(symbol="IWM", direction="LONG", stop_price=293.4, quantity=22)
    )
    # Pause/Stop path: empty in-memory book must not wipe disk plan.
    kept = sync_open_risk([], [], allow_flat_close=False)
    assert kept is not None
    assert kept.symbol == "IWM"
    assert load_trade_plan() is not None


def test_sync_rebuild_and_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    clear_trade_plan()
    reset_flat_streak()
    plan = sync_open_risk([_pos()], _stop_tgt(), thesis="rehydrate", bump=False)
    assert plan is not None
    loaded = load_trade_plan()
    assert loaded is not None
    assert loaded.stop_price == 293.4


def test_pause_engine_keeps_plan_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    clear_trade_plan()
    reset_flat_streak()
    save_trade_plan(
        ActiveTradePlan(symbol="IWM", direction="LONG", stop_price=293.4, quantity=22)
    )
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    eng.state.positions = []  # stale empty — must not wipe
    eng.state.open_orders = []
    eng.state.connected = True
    eng.worker = type("W", (), {"is_alive": lambda self: True})()
    eng.pause_engine()
    assert load_trade_plan() is not None
    assert eng.state.trade_plan is not None
    assert eng.state.trade_plan.get("symbol") == "IWM"


def _judgment_world(**kwargs):
    from abcxauto.world_state import WorldState

    base = dict(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[_pos()],
        open_orders=[],
        opportunities=[{"symbol": "QQQ", "bias": "LONG", "score": 0.9}],
        news_items=[],
        risk_posture="aggressive",
        effective_posture="aggressive",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={"n_positions": 1},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def _new_entry_act(symbol: str = "QQQ", card: str = "index momo") -> dict:
    return {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": symbol,
            "quantity": 1,
            "direction": "LONG",
            "stop_price": 90.0,
            "target_price": 110.0,
            "card": card,
        },
        "rationale": "new entry",
    }


def test_new_entry_allowed_when_book_open_and_capacity(monkeypatch):
    """Open book is not a new-entry ban — capacity Fact gates new risk."""
    from abcxauto.agent_loop import gate_ticket

    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    monkeypatch.setattr("abcxauto.lab_playbook.new_risk_card_error", lambda *_a, **_k: "")
    strat, forced = gate_ticket(
        _new_entry_act(),
        _judgment_world(
            capacity={
                "open_count": 1,
                "max_open_positions": 6,
                "slots_left": 5,
                "allows_new_risk": True,
            },
        ),
    )
    assert forced is None, forced
    assert strat == "market_bracket"


def test_new_entry_rejected_on_structure_cooldown(monkeypatch):
    from abcxauto.agent_loop import gate_ticket

    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    strat, forced = gate_ticket(
        _new_entry_act("QQQ"),
        _judgment_world(
            structure_cooldown={"QQQ": "scrape_suspect"},
            capacity={
                "open_count": 1,
                "max_open_positions": 6,
                "slots_left": 5,
                "allows_new_risk": True,
            },
        ),
    )
    assert strat == "blocked"
    assert "cooldown" in str((forced or {}).get("note") or "").lower()


def test_new_entry_rejected_when_capacity_full():
    from abcxauto.agent_loop import gate_ticket

    strat, forced = gate_ticket(
        _new_entry_act(),
        _judgment_world(
            capacity={
                "open_count": 6,
                "max_open_positions": 6,
                "slots_left": 0,
                "allows_new_risk": False,
            },
        ),
    )
    assert strat == "blocked"
    assert "capacity" in str((forced or {}).get("note") or "").lower()


def test_mop_zero_gate_ticket_allows_sixteen_names(tmp_path, monkeypatch):
    """mop 0 = off. 16 names are not a slot refuse on paper or live."""
    from abcxauto.agent_loop import gate_ticket

    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    reset_flat_streak()
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    monkeypatch.setattr("abcxauto.lab_playbook.new_risk_card_error", lambda *_a, **_k: "")
    lots = [{"symbol": f"S{i}", "quantity": 1} for i in range(16)]
    world = _judgment_world(
        positions=lots,
        capacity={
            "open_count": 16,
            "max_open_positions": 0,
            "slots_left": None,
            "allows_new_risk": True,
        },
    )
    for mode, port, paper in (("paper", 7497, True), ("live", 7496, False)):
        monkeypatch.setattr(
            "abcxauto.world_state.get_config",
            lambda mode=mode, port=port, paper=paper: __import__("types").SimpleNamespace(
                trading_mode=mode,
                ibkr_port=port,
                is_paper=paper,
                risk_gates_enabled=True,
                max_open_positions=0,
            ),
        )
        strat, forced = gate_ticket(_new_entry_act(), world)
        assert forced is None, (mode, forced)
        assert strat == "market_bracket"


def test_grok_set_mop_four_gate_ticket_refuses_the_fifth(tmp_path, monkeypatch):
    """A Grok-set mop=4 still refuses the 5th name."""
    from abcxauto.agent_loop import gate_ticket

    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    reset_flat_streak()
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    monkeypatch.setattr("abcxauto.lab_playbook.new_risk_card_error", lambda *_a, **_k: "")
    lots = [{"symbol": f"S{i}", "quantity": 1} for i in range(4)]
    world = _judgment_world(
        positions=lots,
        capacity={
            "open_count": 4,
            "max_open_positions": 4,
            "slots_left": 0,
            "allows_new_risk": False,
        },
    )
    monkeypatch.setattr(
        "abcxauto.world_state.get_config",
        lambda: __import__("types").SimpleNamespace(
            trading_mode="paper",
            ibkr_port=7497,
            is_paper=True,
            risk_gates_enabled=True,
            max_open_positions=4,
        ),
    )
    strat, forced = gate_ticket(_new_entry_act(), world)
    assert strat == "blocked"
    assert "capacity" in str((forced or {}).get("note") or "").lower()


def test_new_entry_rejected_while_flat_streak_unconfirmed(tmp_path, monkeypatch):
    from abcxauto.agent_loop import gate_ticket
    from abcxauto.trade_plan import _save_flat_streak_state

    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    clear_trade_plan()
    _save_flat_streak_state(1, True)
    strat, forced = gate_ticket(
        _new_entry_act(),
        _judgment_world(flat=True, positions=[], portfolio_risk={"n_positions": 0}),
    )
    assert strat == "blocked"
    assert "unconfirmed" in str((forced or {}).get("note") or "").lower()


def test_monitor_paused_does_not_flat_close(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    clear_trade_plan()
    reset_flat_streak()
    save_trade_plan(
        ActiveTradePlan(symbol="IWM", direction="LONG", stop_price=293.4, quantity=22)
    )
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    eng.state.autonomous = False
    eng.state.paused = True
    eng.state.positions = []
    eng.state.open_orders = []
    # Simulate two empty monitor snaps while paused.
    eng._apply(
        "monitor_snapshot",
        {"positions": [], "open_orders": [], "account": {}},
    )
    eng._apply(
        "monitor_snapshot",
        {"positions": [], "open_orders": [], "account": {}},
    )
    assert load_trade_plan() is not None


def test_exit_keeps_plan_when_symbol_still_held(tmp_path, monkeypatch):
    from abcxauto.trade_plan import stk_qty_for_symbol

    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    save_trade_plan(
        ActiveTradePlan(symbol="IWM", direction="LONG", stop_price=293.4, quantity=22)
    )
    positions = [_pos(qty=10)]  # partial exit remnant
    assert abs(stk_qty_for_symbol(positions, "IWM")) >= 1e-9
    # Mimic exit_act gate: do not close while qty remains.
    if abs(stk_qty_for_symbol(positions, "IWM")) < 1e-9:
        from abcxauto.trade_plan import close_trade_plan

        close_trade_plan("exit_act")
    assert load_trade_plan() is not None


def test_prefer_farthest_lmt_as_target():
    orders = [
        {"symbol": "IWM", "action": "SELL", "order_type": "STP", "aux_price": 293.4},
        {"symbol": "IWM", "action": "SELL", "order_type": "LMT", "lmt_price": 298.0},
        {"symbol": "IWM", "action": "SELL", "order_type": "LMT", "lmt_price": 300.5},
    ]
    plan = reconcile_open_risk([_pos()], orders, existing_plan=None)
    assert plan is not None
    assert plan.target_price == 300.5


def test_options_only_closes_stk_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    clear_trade_plan()
    reset_flat_streak()
    save_trade_plan(
        ActiveTradePlan(symbol="IWM", direction="LONG", stop_price=293.4, quantity=22)
    )
    opt = {
        "symbol": "IWM",
        "secType": "OPT",
        "quantity": -1,
        "conId": 99,
    }
    assert maybe_close_on_confirmed_flat([opt], needed=2) is True
    assert load_trade_plan() is None


def test_multi_plan_reconcile_and_migrate(tmp_path, monkeypatch):
    from abcxauto.trade_plan import (
        load_trade_plans,
        reconcile_open_risk_all,
        save_trade_plans,
    )

    monkeypatch.setenv("ABCXAUTO_TRADE_PLAN_PATH", str(tmp_path / "plan.json"))
    monkeypatch.setenv("ABCXAUTO_TRADE_PLANS_PATH", str(tmp_path / "plans.json"))
    clear_trade_plan()
    # Legacy single-file migrate
    save_trade_plan(
        ActiveTradePlan(symbol="SPY", direction="LONG", quantity=8, stop_price=500.0)
    )
    plans = load_trade_plans()
    assert len(plans) >= 1
    assert plans[0].symbol == "SPY"
    # Two STK rows → two plans
    positions = [_pos("SPY", qty=8, avg=500, mv=4000), _pos("QQQ", qty=5, avg=400, mv=2000)]
    orders = _stop_tgt("SPY", stop=495) + [
        {
            "symbol": "QQQ",
            "action": "SELL",
            "order_type": "STP",
            "aux_price": 390,
            "order_id": 21,
        }
    ]
    out = reconcile_open_risk_all(positions, orders, plans)
    assert len(out) == 2
    syms = {p.symbol for p in out}
    assert syms == {"SPY", "QQQ"}
    save_trade_plans(out)
    assert "BOOK" in format_open_risk_line() or "SPY" in format_open_risk_line()


def _csco_stops(n=7, qty=50, start_id=101):
    types = ("STP", "TRAIL")
    return [
        {
            "order_id": start_id + i,
            "symbol": "CSCO",
            "sec_type": "STK",
            "action": "SELL",
            "quantity": qty,
            "order_type": types[i % 2],
        }
        for i in range(n)
    ]


def test_stacked_stop_cancel_ids_csco_keeps_newest_covering():
    from abcxauto.trade_plan import stacked_stop_cancel_ids, working_stop_qty

    pos = [{"symbol": "CSCO", "quantity": 50, "sec_type": "STK"}]
    orders = _csco_stops()
    assert working_stop_qty(orders, "CSCO", "LONG") == 350.0
    ids = stacked_stop_cancel_ids(pos, orders)
    assert sorted(ids) == list(range(101, 107))
    assert 107 not in ids


def test_stacked_stop_cancel_ids_leaves_last_covering():
    from abcxauto.trade_plan import stacked_stop_cancel_ids

    pos = [{"symbol": "CSCO", "quantity": 50, "sec_type": "STK"}]
    orders = _csco_stops(n=1, start_id=107)
    assert stacked_stop_cancel_ids(pos, orders) == []


def test_stacked_stop_cancel_ids_no_covering_keeps_all():
    from abcxauto.trade_plan import stacked_stop_cancel_ids, working_stop_qty

    pos = [{"symbol": "CSCO", "quantity": 50, "sec_type": "STK"}]
    orders = _csco_stops(n=7, qty=10)
    assert working_stop_qty(orders, "CSCO", "LONG") == 70.0
    assert stacked_stop_cancel_ids(pos, orders) == []


def test_stop_qty_fact_stacked_crumbs_are_not_a_match():
    """Five 10-share stops sum to the 50-share lot; no single order covers."""
    from abcxauto.trade_plan import ActiveTradePlan, stop_qty_mismatch_fact

    pos = [{"symbol": "CSCO", "quantity": 50, "sec_type": "STK"}]
    orders = _csco_stops(n=5, qty=10)
    plan = ActiveTradePlan(symbol="CSCO", direction="LONG", quantity=50)
    fact = stop_qty_mismatch_fact(pos, orders, plan)
    assert fact is not None
    assert fact["mismatch"] is True
    assert fact["match"] is False
    assert fact["stop_order_qty"] == 10.0
    assert "single stop" in str(fact.get("note") or "")


def test_stop_qty_fact_stacked_covering_is_per_order_match():
    """Seven 50-share stops sum to 350; each order already covers the lot."""
    from abcxauto.trade_plan import ActiveTradePlan, stop_qty_mismatch_fact

    pos = [{"symbol": "CSCO", "quantity": 50, "sec_type": "STK"}]
    orders = _csco_stops()
    plan = ActiveTradePlan(symbol="CSCO", direction="LONG", quantity=50)
    fact = stop_qty_mismatch_fact(pos, orders, plan)
    assert fact is not None
    assert fact["mismatch"] is False
    assert fact["match"] is True
    assert fact["stop_order_qty"] == 50.0
