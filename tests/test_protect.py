"""Clerk completes missing protection; never rewrites Grok's prices."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.protect import (
    fill_missing_protection,
    last_stop_block_reason,
    order_covers_open_lot,
    orphaned_protection_ids,
    promote_naked_entry,
)
from abcxauto.structure_grade import check_live_geometry


def _cfg(**kw):
    base = dict(max_risk_per_trade_pct=1.0, max_position_pct=20.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_promote_opening_market_order():
    act = {
        "action": "market_order",
        "strategy": "market_order",
        "params": {"symbol": "SPY", "action": "BUY", "quantity": 5},
    }
    assert promote_naked_entry(act, []) is True
    assert act["strategy"] == "market_bracket"
    assert act["params"]["direction"] == "LONG"


def test_promote_skips_closing_market_order():
    act = {
        "action": "market_order",
        "strategy": "market_order",
        "params": {
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 5,
            "closing_position": True,
            "conId": 1,
        },
    }
    assert promote_naked_entry(act, []) is False
    assert act["strategy"] == "market_order"


def test_promote_needs_symbol_and_side():
    act = {"action": "market_order", "strategy": "market_order", "params": {}}
    assert promote_naked_entry(act, []) is False


def test_fill_thin_long_bracket():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SPY", "direction": "LONG"},
    }
    filled = fill_missing_protection(
        act, quote_last=500.0, equity=100_000.0, posture="balanced", cfg=_cfg()
    )
    p = act["params"]
    assert "stop_price" in filled
    assert "target_price" in filled
    assert "quantity" in filled
    assert p["stop_price"] < 500.0 < p["target_price"]
    assert p["quantity"] >= 1
    ok, code, _ = check_live_geometry(
        "market_bracket", p, quote_last=500.0, posture="balanced"
    )
    assert ok is True
    assert code == "ok"


def test_fill_does_not_rewrite_grok_prices():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "SPY",
            "direction": "LONG",
            "stop_price": 490.0,
            "target_price": 520.0,
            "quantity": 7,
        },
    }
    filled = fill_missing_protection(
        act, quote_last=500.0, equity=100_000.0, posture="balanced", cfg=_cfg()
    )
    p = act["params"]
    assert p["stop_price"] == 490.0
    assert p["target_price"] == 520.0
    assert p["quantity"] == 7
    assert "stop_price" not in filled
    assert "target_price" not in filled
    assert "quantity" not in filled


def test_fill_short_sides():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SPY", "direction": "SHORT"},
    }
    fill_missing_protection(
        act, quote_last=500.0, equity=100_000.0, posture="balanced", cfg=_cfg()
    )
    p = act["params"]
    assert p["target_price"] < 500.0 < p["stop_price"]


def test_fill_noop_without_quote():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SPY", "direction": "LONG"},
    }
    assert fill_missing_protection(act, quote_last=None, equity=100_000.0) == []
    assert "stop_price" not in act["params"]


def test_oca_qty_from_open_stock():
    act = {
        "action": "oca",
        "strategy": "oca",
        "params": {"symbol": "IWM", "direction": "LONG"},
    }
    fill_missing_protection(
        act,
        quote_last=200.0,
        equity=50_000.0,
        posture="balanced",
        cfg=_cfg(),
        positions=[{"symbol": "IWM", "quantity": 12, "sec_type": "STK"}],
    )
    assert act["params"]["quantity"] == 12


@pytest.mark.asyncio
async def test_execute_ticket_completes_thin_idea(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    # A promoted naked entry becomes new risk; the card gate has its own suite.
    monkeypatch.setattr("abcxauto.lab_playbook.new_risk_card_error", lambda *_a, **_k: "")
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            is_paper=True,
            trading_mode="paper",
            max_risk_per_trade_pct=1.0,
            max_position_pct=20.0,
        ),
    )
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
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
    act = {
        "action": "market_order",
        "strategy": "market_order",
        "params": {"symbol": "SPY", "action": "BUY"},
    }
    snap = {
        "account": {"netliquidation": 100_000.0},
        "positions": [],
        "open_orders": [],
        "spy_quote": {"last": 500.0},
        "ibkr_live_quotes": {"SPY": 500.0},
    }
    result = await execute_ticket(act, object(), world, snap)
    assert result.get("status") == "ok"
    assert sent
    ticket = sent[0]
    assert ticket["strategy"] == "market_bracket"
    p = ticket["params"]
    assert p["stop_price"] < 500.0 < p["target_price"]
    assert int(p["quantity"]) >= 1


# ---------------------------------------------------------------------------
# last-stop: exit-side qty, not "any other stop"
# ---------------------------------------------------------------------------


def _stk(symbol: str, qty: float, **extra) -> dict:
    row = {"symbol": symbol, "quantity": qty, "sec_type": "STK"}
    row.update(extra)
    return row


def _order(oid: int, symbol: str, action: str, qty: float, otype: str, **extra) -> dict:
    row = {
        "order_id": oid,
        "symbol": symbol,
        "sec_type": "STK",
        "action": action,
        "quantity": qty,
        "order_type": otype,
    }
    row.update(extra)
    return row


def test_crumb_stop_is_not_a_replacement_last_stop():
    """A 1-share STP must not unlock cancel of the 10-share last-stop."""
    orders = [
        _order(9, "AAPL", "SELL", 10, "STP"),
        _order(10, "AAPL", "SELL", 1, "STP"),
    ]
    lot = [_stk("AAPL", 10)]
    reason = last_stop_block_reason(9, orders, lot)
    assert reason and "only working stop" in reason
    assert last_stop_block_reason(10, orders, lot) is None


def test_wrong_side_stop_is_not_a_replacement_last_stop():
    """A BUY stop on a long is not cover — cancelling the SELL last-stop is refused."""
    orders = [
        _order(9, "AAPL", "SELL", 10, "STP"),
        _order(10, "AAPL", "BUY", 10, "STP"),
    ]
    lot = [_stk("AAPL", 10)]
    assert last_stop_block_reason(9, orders, lot)
    assert last_stop_block_reason(10, orders, lot) is None
    assert last_stop_block_reason(
        9, [_order(9, "AAPL", "BUY", 10, "STP")], lot
    ) is None


def test_wrong_side_leftover_after_flatten_flip_is_not_a_last_stop():
    """Long flattened through, now short: leftover SELL may be cancelled.

    The orphan sweep still leaves it (same contract is live). The last-stop
    gate must not treat it as load-bearing cover — it is a naked add.
    """
    leftover = [_order(9, "AAPL", "SELL", 10, "STP")]
    short = [_stk("AAPL", -10)]
    assert last_stop_block_reason(9, leftover, short) is None
    assert order_covers_open_lot(leftover[0], short) is True
    assert orphaned_protection_ids(short, leftover) == []


def test_covering_sell_stop_still_blocks_on_a_live_long():
    orders = [_order(9, "AAPL", "SELL", 10, "STP")]
    assert last_stop_block_reason(9, orders, [_stk("AAPL", 10)])
    assert order_covers_open_lot(orders[0], [_stk("AAPL", 10)]) is True
    assert orphaned_protection_ids([_stk("AAPL", 10)], orders) == []


def test_second_covering_stop_allows_cancel():
    orders = [
        _order(9, "AAPL", "SELL", 10, "STP"),
        _order(10, "AAPL", "SELL", 10, "TRAIL"),
    ]
    assert last_stop_block_reason(9, orders, [_stk("AAPL", 10)]) is None


def test_flat_book_releases_last_stop():
    orders = [_order(9, "AAPL", "SELL", 10, "STP")]
    assert last_stop_block_reason(9, orders, []) is None


def test_cover_qty_slack_matches_the_book():
    lot = [_stk("AAPL", 10)]
    assert last_stop_block_reason(9, [_order(9, "AAPL", "SELL", 9.49, "STP")], lot)
    assert last_stop_block_reason(9, [_order(9, "AAPL", "SELL", 9.48, "STP")], lot) is None


def test_sld_alias_is_exit_side_for_a_long():
    orders = [_order(9, "AAPL", "SLD", 10, "STP")]
    assert last_stop_block_reason(9, orders, [_stk("AAPL", 10)])


def test_covering_buy_stop_blocks_on_a_live_short():
    orders = [_order(9, "TSLA", "BUY", 10, "STP")]
    assert last_stop_block_reason(9, orders, [_stk("TSLA", -10)])
    assert order_covers_open_lot(orders[0], [_stk("TSLA", -10)]) is True


def test_bot_alias_is_exit_side_for_a_short():
    orders = [_order(9, "TSLA", "BOT", 10, "STP")]
    assert last_stop_block_reason(9, orders, [_stk("TSLA", -10)])


@pytest.mark.asyncio
async def test_execute_ticket_does_not_fill_omitted_fields_from_hunt_sketch(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            is_paper=True,
            trading_mode="paper",
            max_risk_per_trade_pct=1.0,
            max_position_pct=20.0,
        ),
    )
    world = WorldState(
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
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"card": "flush bounce"},
        "rationale": "",
    }
    snap = {
        "account": {"netliquidation": 37000.0},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {"SNDK": 91.5},
        "session_range": {
            "SNDK": {
                "today": True,
                "low": 88.0,
                "high": 92.0,
                "last": 91.5,
                "retrace_30": 93.0,
                "ticket": {
                    "strategy": "market_bracket",
                    "card": "flush bounce",
                    "direction": "LONG",
                    "stop_price": 88.0,
                    "target_price": 93.0,
                    "quantity": 10,
                },
            }
        },
    }
    result = await execute_ticket(act, object(), world, snap)
    assert result.get("status") == "blocked"
    assert "params.symbol" in str(result.get("note") or "")
    assert sent == []
    p = act.get("params") or {}
    assert p.get("card") == "flush bounce"
    for key in ("symbol", "direction", "stop_price", "target_price", "quantity"):
        assert p.get(key) in (None, "")
    assert "_hunt_sketch" not in act


def test_fill_uses_today_session_low_and_retrace():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SNDK", "direction": "LONG"},
    }
    filled = fill_missing_protection(
        act,
        quote_last=91.5,
        equity=37000.0,
        posture="balanced",
        cfg=_cfg(),
        session={
            "low": 88.0,
            "high": 92.0,
            "retrace_30": 93.0,
            "today": True,
        },
    )
    p = act["params"]
    assert "stop_price" in filled
    assert "target_price" in filled
    assert p["stop_price"] == 88.0
    assert p["target_price"] == 93.0
    ok, code, _ = check_live_geometry(
        "market_bracket",
        p,
        quote_last=91.5,
        posture="balanced",
        session={"low": 88.0, "high": 92.0, "today": True},
    )
    assert ok is True
    assert code == "ok"


def test_fill_ignores_prior_day_session():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SNDK", "direction": "LONG"},
    }
    fill_missing_protection(
        act,
        quote_last=91.5,
        equity=37000.0,
        posture="balanced",
        cfg=_cfg(),
        session={"low": 88.0, "high": 92.0, "retrace_30": 93.0, "today": False},
    )
    p = act["params"]
    assert p["stop_price"] != 88.0
    assert p["target_price"] != 93.0


def test_size_if_stop_is_knob_math_not_a_ticket():
    from abcxauto.protect import size_if_stop

    cfg = _cfg(max_risk_per_trade_pct=1.0, max_position_pct=50.0)
    out = size_if_stop(last=91.5, stop=88.0, equity=37000.0, cfg=cfg)
    assert out["qty"] == 105
    assert out["stop"] == 88.0
    assert out["risk_per_share"] == pytest.approx(3.5)
    assert size_if_stop(last=91.5, stop=91.5, equity=37000.0, cfg=cfg) == {}


def _save_gap_card():
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)


def test_gap_card_does_not_invent_a_percent_stop():
    _save_gap_card()
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SNDK", "direction": "LONG"},
    }
    filled = fill_missing_protection(
        act,
        quote_last=91.5,
        equity=37000.0,
        posture="balanced",
        cfg=_cfg(),
    )
    assert "stop_price" not in filled
    assert "target_price" not in filled
    assert act["params"].get("stop_price") in (None, "")


@pytest.mark.asyncio
async def test_execute_ticket_without_session_blocks_gap_card(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    _save_gap_card()
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    world = WorldState(
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
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"card": "flush bounce"},
        "rationale": "",
    }
    result = await execute_ticket(
        act,
        object(),
        world,
        {"account": {"netliquidation": 37000.0}, "positions": [], "open_orders": []},
    )
    assert result.get("status") == "blocked"
    assert "candles" in str(result.get("note") or "")
    assert sent == []

    act2 = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "card": "flush bounce",
            "symbol": "SNDK",
            "direction": "LONG",
            "stop_price": 88.0,
            "target_price": 93.0,
            "quantity": 10,
        },
        "rationale": "card=flush bounce SNDK",
    }
    result2 = await execute_ticket(
        act2,
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"SNDK": 91.5},
        },
    )
    assert result2.get("status") == "ok"
    assert sent and sent[0]["params"]["stop_price"] == 88.0


@pytest.mark.asyncio
async def test_execute_ticket_blocks_when_last_is_on_opening_low(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    _save_gap_card()
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    world = WorldState(
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
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "card": "flush bounce",
            "symbol": "SNDK",
            "direction": "LONG",
            "stop_price": 88.0,
            "target_price": 93.0,
            "quantity": 10,
        },
        "rationale": "card=flush bounce SNDK",
    }
    result = await execute_ticket(
        act,
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"SNDK": 88.0},
            "session_range": {
                "SNDK": {
                    "today": True,
                    "low": 88.0,
                    "last": 88.0,
                    "above_low": False,
                    "retrace_30": 93.0,
                }
            },
        },
    )
    assert result.get("status") == "blocked"
    assert "opening low" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_blocks_when_gap_is_under_the_card_floor(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "when_on": "mega/large ≥6% earnings-miss gap",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    world = WorldState(
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
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "card": "flush bounce",
            "symbol": "MU",
            "direction": "LONG",
            "stop_price": 900.0,
            "target_price": 920.0,
            "quantity": 5,
        },
        "rationale": "card=flush bounce MU",
    }
    result = await execute_ticket(
        act,
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"MU": 910.0},
            "session_range": {
                "MU": {
                    "today": True,
                    "low": 900.0,
                    "last": 910.0,
                    "above_low": True,
                    "open_gap_pct": -3.3,
                    "retrace_30": 920.0,
                }
            },
        },
    )
    assert result.get("status") == "blocked"
    assert "gap under 6%" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_blocks_when_last_is_under_card_price_floor(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "scan": "most_active + top_losers; mega/large only; skip levered ETFs and sub-$15 names",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    world = WorldState(
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
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "card": "flush bounce",
                "symbol": "AAOI",
                "direction": "LONG",
                "stop_price": 11.0,
                "target_price": 14.0,
                "quantity": 100,
            },
            "rationale": "card=flush bounce AAOI",
        },
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"AAOI": 12.0},
            "session_range": {
                "AAOI": {
                    "today": True,
                    "low": 11.0,
                    "last": 12.0,
                    "above_low": True,
                    "open_gap_pct": -8.0,
                }
            },
        },
    )
    assert result.get("status") == "blocked"
    assert "last under $15" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_blocks_wide_spread_on_tight_spread_card(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "review": "do not re-enter that name the same session",
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "when_on": "tight live spread, hold above the opening low",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    world = WorldState(
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
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "card": "flush bounce",
            "symbol": "SNDK",
            "direction": "LONG",
            "stop_price": 88.0,
            "target_price": 93.0,
            "quantity": 10,
        },
        "rationale": "card=flush bounce SNDK",
    }
    snap = {
        "account": {"netliquidation": 37000.0},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {"SNDK": 91.5},
        "session_range": {
            "SNDK": {
                "today": True,
                "low": 88.0,
                "last": 91.5,
                "above_low": True,
                "bid": 89.0,
                "ask": 93.0,
                "spread": 4.0,
                "open_gap_pct": -6.5,
            }
        },
    }
    result = await execute_ticket(act, object(), world, snap)
    assert result.get("status") == "blocked"
    assert "spread wider than the stop" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_does_not_apply_hunt_hold_to_manage(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    _save_gap_card()
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[{"symbol": "SNDK", "sec_type": "STK", "quantity": 10, "conId": 1}],
        open_orders=[{"order_id": 42, "symbol": "SNDK"}],
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
    result = await execute_ticket(
        {
            "action": "cancel",
            "strategy": "cancel",
            "params": {"order_id": 42},
            "rationale": "cancel child",
        },
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": world.positions,
            "open_orders": world.open_orders,
            "session_range": {
                "SNDK": {
                    "today": True,
                    "low": 88.0,
                    "last": 88.0,
                    "above_low": False,
                    "open_gap_pct": -6.5,
                }
            },
        },
    )
    assert "opening low" not in str(result.get("note") or "")
    assert "gap under" not in str(result.get("note") or "")


@pytest.mark.asyncio
async def test_execute_ticket_uses_scan_hit_last_when_quote_map_misses(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            is_paper=True,
            trading_mode="paper",
            max_risk_per_trade_pct=1.0,
            max_position_pct=20.0,
        ),
    )
    world = WorldState(
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
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "card": "flush bounce",
                "symbol": "SNDK",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
                "quantity": 10,
            },
            "rationale": "card=flush bounce SNDK",
        },
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {},
            "scan_hits": {
                "quoted": 1,
                "rows": [{"symbol": "SNDK", "last": 91.5}],
            },
            "session_range": {
                "SNDK": {
                    "today": True,
                    "low": 88.0,
                    "high": 92.0,
                    "last": 91.5,
                    "retrace_30": 93.0,
                    "ticket": {
                        "strategy": "market_bracket",
                        "card": "flush bounce",
                        "direction": "LONG",
                        "stop_price": 88.0,
                        "target_price": 93.0,
                        "quantity": 10,
                    },
                }
            },
        },
    )
    assert result.get("status") == "ok"
    assert sent
    assert sent[0]["params"]["symbol"] == "SNDK"
    assert sent[0]["_quote_last"] == 91.5


@pytest.mark.asyncio
async def test_execute_ticket_blocks_no_add_on_an_open_name(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. One name, no add.",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[{"symbol": "SNDK", "sec_type": "STK", "quantity": 10, "conId": 1}],
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
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "card": "flush bounce",
                "symbol": "SNDK",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
                "quantity": 5,
            },
            "rationale": "add",
        },
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": world.positions,
            "open_orders": [],
            "ibkr_live_quotes": {"SNDK": 91.5},
            "session_range": {
                "SNDK": {
                    "today": True,
                    "low": 88.0,
                    "last": 91.5,
                    "above_low": True,
                    "open_gap_pct": -6.5,
                }
            },
        },
    )
    assert result.get("status") == "blocked"
    assert "no add SNDK" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_blocks_new_risk_on_a_price_hint(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "bounce",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    world = WorldState(
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
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "card": "flush bounce",
                "symbol": "SNDK",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
                "quantity": 10,
                "price_hint": 91.5,
            },
            "rationale": "hint only",
        },
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {},
        },
    )
    assert result.get("status") == "blocked"
    assert "IBKR live last" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_blocks_when_one_share_blows_card_risk(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.world_state import WorldState

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "bounce",
                            "shape": "LONG STK. Qty so dollar risk ≤1% NL.",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            is_paper=True,
            trading_mode="paper",
            max_risk_per_trade_pct=1.0,
            max_position_pct=20.0,
        ),
    )
    world = WorldState(
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
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "card": "flush bounce",
                "symbol": "BKNG",
                "direction": "LONG",
                "stop_price": 100.0,
                "target_price": 520.0,
            },
            "rationale": "card=flush bounce BKNG",
        },
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"BKNG": 500.0},
        },
    )
    assert result.get("status") == "blocked"
    assert "size won't fit" in str(result.get("note") or "")
    assert sent == []
