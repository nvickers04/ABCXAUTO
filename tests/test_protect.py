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
