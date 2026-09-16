"""last_turn must persist working tickets so a TWS bounce is not fake-flat."""

from __future__ import annotations

import json

from abcxauto import think_stream as ts


def _bag_order(oid: int = 20121) -> dict:
    return {
        "order_id": oid,
        "symbol": "SPY",
        "secType": "BAG",
        "orderType": "LMT",
        "action": "SELL",
        "lmtPrice": 0.98,
        "totalQuantity": 1,
        "comboLegs": [{}, {}],
    }


def test_write_last_turn_keeps_a_resting_bag_unflat(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "strat": "vertical_spread",
        "rationale": "credit 0.98",
        "sends": 1,
        "tool_trace": ["send"],
        "open_orders": [_bag_order()],
        "positions": [],
        "world_state": {"flat": True, "net_liquidation": 34000, "open_lots": []},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["flat"] is False
    assert last["open_lots"] == []
    assert last["working_orders"]
    assert last["working_orders"][0]["order_id"] == 20121
    assert last["working_orders"][0]["role"] == "entry"
    brief = json.loads((tmp_path / "desk_brief.json").read_text(encoding="utf-8"))
    assert brief["working_orders"][0]["order_id"] == 20121


def test_write_last_turn_after_send_persists_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn_after_send(
        strat="vertical_spread",
        sends=1,
        positions=[],
        orders=[_bag_order()],
        rationale="placed",
        tool_trace=["send"],
        net_liquidation=34000,
    )
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["flat"] is False
    assert last["working_orders"][0]["order_id"] == 20121


def test_seed_snap_restores_working_orders_when_book_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "strat": "vertical_spread",
        "rationale": "credit 0.98",
        "sends": 1,
        "tool_trace": ["send"],
        "open_orders": [_bag_order()],
        "world_state": {"flat": True, "net_liquidation": 34000},
    })
    snap: dict = {}
    ts.seed_snap_from_last_turn(snap)
    assert snap["open_orders"]
    assert snap["open_orders"][0]["order_id"] == 20121
    assert snap["working_orders"][0]["order_id"] == 20121


def test_unreliable_book_keeps_prior_working_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn({
        "strat": "vertical_spread",
        "rationale": "working",
        "sends": 1,
        "open_orders": [_bag_order()],
        "world_state": {"flat": False, "net_liquidation": 34000},
    })
    ts.write_last_turn({
        "strat": "skipped",
        "rationale": "skipped_grok: book_unreliable",
        "validation": "skipped_grok: book_unreliable",
        "book_unreliable": True,
        "positions": [],
        "open_orders": [],
        "world_state": {
            "flat": True,
            "net_liquidation": 0,
            "gates": {"book_unreliable": True},
        },
        "reality_pulse": {"data_freshness": {"ibkr_connected": False}},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["flat"] is False
    assert last["working_orders"][0]["order_id"] == 20121


def test_brain_fallback_flat_sees_orders():
    from abcxauto.world_state import book_is_flat

    assert book_is_flat([], [_bag_order()]) is False
    assert book_is_flat([], []) is True


def test_write_last_turn_fill_lag_does_not_fake_flat(tmp_path, monkeypatch):
    """Filled ticket + empty positions must not overwrite a working last_turn."""
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts._run = {"run_id": "r1", "pid": 1}
    ts.write_last_turn_after_send(
        strat="vertical_spread",
        sends=1,
        positions=[],
        orders=[_bag_order(19875)],
        rationale="placed",
        tool_trace=["send"],
        net_liquidation=34047.64,
    )
    fill = {
        "side": "BOT",
        "symbol": "SPY",
        "conId": 28812380,
        "order_id": 19875,
        "ts": "2099-01-01T00:00:00+00:00",
        "quantity": 6.0,
        "price": 5.08,
    }
    ts.write_last_turn({
        "strat": "vertical_spread",
        "rationale": "filled",
        "sends": 6,
        "send_calls": 6,
        "tool_trace": ["send", "book"],
        "positions": [],
        "open_orders": [],
        "fills": [fill],
        "world_state": {"flat": True, "net_liquidation": 34047.64},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["flat"] is False
    assert last["working_orders"] == []
