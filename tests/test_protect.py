"""Clerk must not invent omitted ticket fields; last-stop cover stays locked."""

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


_TICKET_OWNED = (
    "stop_price",
    "target_price",
    "entry_price",
    "price_hint",
    "quantity",
)


def _cfg(**kw):
    base = dict(max_risk_per_trade_pct=1.0, max_position_pct=20.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _assert_omitted(params: dict, *keys: str) -> None:
    want = keys or _TICKET_OWNED
    for key in want:
        assert params.get(key) in (None, ""), key


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
    snapshot = dict(act["params"])
    filled = fill_missing_protection(
        act, quote_last=500.0, equity=100_000.0, posture="balanced", cfg=_cfg()
    )
    p = act["params"]
    assert filled == []
    assert "_protection_filled" not in act
    _assert_omitted(p)
    # 1% balanced band around last would have been 495 / 505 — must not appear.
    assert p.get("stop_price") != 495.0
    assert p.get("target_price") != 505.0
    assert p == snapshot
    ok, _code, msg = check_live_geometry(
        "market_bracket", p, quote_last=500.0, posture="balanced"
    )
    assert ok is False
    assert "required" in msg


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
    assert filled == []
    _assert_omitted(p, "entry_price", "price_hint")


def test_fill_short_sides():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SPY", "direction": "SHORT"},
    }
    filled = fill_missing_protection(
        act, quote_last=500.0, equity=100_000.0, posture="balanced", cfg=_cfg()
    )
    p = act["params"]
    assert filled == []
    _assert_omitted(p)
    assert p.get("stop_price") != 505.0
    assert p.get("target_price") != 495.0


def test_fill_noop_without_quote():
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {"symbol": "SPY", "direction": "LONG"},
    }
    assert fill_missing_protection(act, quote_last=None, equity=100_000.0) == []
    _assert_omitted(act["params"])


def test_oca_qty_from_open_stock():
    act = {
        "action": "oca",
        "strategy": "oca",
        "params": {"symbol": "IWM", "direction": "LONG"},
    }
    filled = fill_missing_protection(
        act,
        quote_last=200.0,
        equity=50_000.0,
        posture="balanced",
        cfg=_cfg(),
        positions=[{"symbol": "IWM", "quantity": 12, "sec_type": "STK"}],
    )
    assert filled == []
    assert act["params"].get("quantity") in (None, "")
    _assert_omitted(act["params"])


def test_fill_does_not_invent_entry_or_price_hint_from_quote():
    act = {
        "action": "bracket",
        "strategy": "bracket",
        "params": {"symbol": "SPY", "direction": "LONG", "quantity": 3},
    }
    filled = fill_missing_protection(
        act, quote_last=500.0, equity=100_000.0, posture="balanced", cfg=_cfg()
    )
    assert filled == []
    _assert_omitted(act["params"], "stop_price", "target_price", "entry_price", "price_hint")
    assert act["params"]["quantity"] == 3


@pytest.mark.asyncio
async def test_execute_ticket_refuses_thin_idea(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    # A promoted naked entry becomes new risk; the card gate has its own suite.
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
    assert result.get("status") == "blocked"
    assert sent == []
    p = act.get("params") or {}
    _assert_omitted(p)
    assert p.get("stop_price") != 495.0
    assert p.get("quantity") not in (1, 2, 20, 40)


def _stub_thin_send(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            is_paper=True,
            trading_mode="paper",
            max_risk_per_trade_pct=1.0,
            max_position_pct=20.0,
        ),
    )
    return sent


def _flat_world(**kw):
    from abcxauto.world_state import WorldState

    fields = dict(
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
    fields.update(kw)
    return WorldState(**fields)


@pytest.mark.asyncio
async def test_execute_ticket_refuses_omitted_qty_when_size_would_fit(monkeypatch):
    """Old fill would size qty from |last-stop| and 1% NL. Clerk must not."""
    from abcxauto.agent_loop import execute_ticket

    sent = _stub_thin_send(monkeypatch)
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "card": "flush bounce",
            "symbol": "SPY",
            "direction": "LONG",
            "stop_price": 490.0,
            "target_price": 520.0,
        },
    }
    result = await execute_ticket(
        act,
        object(),
        _flat_world(),
        {
            "account": {"netliquidation": 100_000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"SPY": 500.0},
        },
    )
    assert result.get("status") == "blocked"
    assert sent == []
    p = act.get("params") or {}
    assert p["stop_price"] == 490.0
    assert p["target_price"] == 520.0
    assert p.get("quantity") in (None, "")
    assert p.get("price_hint") in (None, "")


@pytest.mark.asyncio
async def test_execute_ticket_refuses_omitted_stop_even_with_session_low(monkeypatch):
    """Session low / 1% band must not complete a thin stop. Send is refused."""
    from abcxauto.agent_loop import execute_ticket

    sent = _stub_thin_send(monkeypatch)
    act = {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "card": "flush bounce",
            "symbol": "SNDK",
            "direction": "LONG",
            "quantity": 10,
        },
    }
    result = await execute_ticket(
        act,
        object(),
        _flat_world(net_liquidation=37000.0),
        {
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
                }
            },
        },
    )
    assert result.get("status") in ("blocked", "rejected")
    assert sent == []
    assert "stop_price" in str(result.get("error") or result.get("note") or "")
    p = act.get("params") or {}
    assert p["quantity"] == 10
    assert p.get("stop_price") not in (88.0, 90.59)  # session low / ~1% band
    assert p.get("target_price") not in (93.0, 92.41)
    _assert_omitted(p, "stop_price", "target_price", "entry_price", "price_hint")


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
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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


def test_fill_does_not_invent_from_today_session_low():
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
    assert filled == []
    _assert_omitted(p)
    assert p.get("stop_price") != 88.0
    assert p.get("target_price") != 93.0
    ok, _code, msg = check_live_geometry(
        "market_bracket",
        p,
        quote_last=91.5,
        posture="balanced",
        session={"low": 88.0, "high": 92.0, "today": True},
    )
    assert ok is False
    assert "required" in msg


def test_fill_ignores_prior_day_session():
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
        session={"low": 88.0, "high": 92.0, "retrace_30": 93.0, "today": False},
    )
    p = act["params"]
    assert filled == []
    _assert_omitted(p)
    assert p.get("stop_price") != 88.0
    assert p.get("target_price") != 93.0


def test_size_if_stop_is_knob_math_not_a_ticket():
    from abcxauto.protect import size_if_stop

    cfg = _cfg(max_risk_per_trade_pct=1.0, max_position_pct=50.0)
    out = size_if_stop(last=91.5, stop=88.0, equity=37000.0, cfg=cfg)
    assert out["qty"] == 105
    assert out["stop"] == 88.0
    assert out["risk_per_share"] == pytest.approx(3.5)
    assert size_if_stop(last=91.5, stop=91.5, equity=37000.0, cfg=cfg) == {}




@pytest.mark.asyncio
async def test_opening_market_bracket_needs_a_real_card_then_reaches_geometry(monkeypatch):
    """No card / unknown card refuse at the label. A real name reaches geometry."""
    from abcxauto.agent_loop import execute_ticket

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            is_paper=True,
            trading_mode="paper",
            max_risk_per_trade_pct=1.0,
            max_position_pct=20.0,
        ),
    )
    world = _flat_world(net_liquidation=37000.0)
    snap = {
        "account": {"netliquidation": 37000.0},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {"SNDK": 91.5},
    }
    complete = {
        "symbol": "SNDK",
        "direction": "LONG",
        "stop_price": 88.0,
        "target_price": 93.0,
        "quantity": 10,
    }
    missing = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": dict(complete),
            "rationale": "no card",
        },
        object(),
        world,
        snap,
    )
    assert missing.get("status") == "blocked"
    assert "params.card" in str(missing.get("note") or "")
    assert sent == []

    unknown = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {**complete, "card": "moon shot"},
            "rationale": "unknown card",
        },
        object(),
        world,
        snap,
    )
    assert unknown.get("status") == "ok"
    assert sent and sent[0]["params"]["card"] == "moon shot"
    sent.clear()

    thin = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "card": "flush bounce",
                "symbol": "SNDK",
                "direction": "LONG",
                "quantity": 10,
            },
            "rationale": "card but no stop",
        },
        object(),
        world,
        snap,
    )
    assert thin.get("status") in ("blocked", "rejected")
    blob = str(thin)
    assert "params.card" not in blob
    assert "playbook card" not in blob.lower()
    assert "required" in blob.lower() or "stop" in blob.lower()
    assert sent == []

    ok = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {**complete, "card": "flush bounce"},
            "rationale": "card=flush bounce SNDK",
        },
        object(),
        world,
        snap,
    )
    assert ok.get("status") == "ok"
    assert sent and sent[0]["params"]["card"] == "flush bounce"
    assert sent[0]["params"]["stop_price"] == 88.0


@pytest.mark.asyncio
async def test_close_and_cancel_without_card_still_send(monkeypatch):
    """Exits / cancel / flatten-style closes do not need params.card."""
    from abcxauto.agent_loop import execute_ticket

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    world = _flat_world(
        net_liquidation=37000.0,
        flat=False,
        positions=[{"symbol": "NKE", "sec_type": "STK", "quantity": 70, "conId": 9}],
        open_orders=[{"order_id": 42, "symbol": "NKE"}],
    )
    snap = {
        "account": {"netliquidation": 37000.0},
        "positions": world.positions,
        "open_orders": world.open_orders,
        "ibkr_live_quotes": {"NKE": 91.5},
    }
    cancel = await execute_ticket(
        {
            "action": "cancel_order",
            "strategy": "cancel_order",
            "params": {"order_id": 42},
            "rationale": "cancel child",
        },
        object(),
        world,
        snap,
    )
    assert cancel.get("status") == "ok"
    assert "params.card" not in str(cancel.get("note") or "")
    assert sent and sent[0]["strategy"] == "cancel_order"

    sent.clear()
    close = await execute_ticket(
        {
            "action": "market_order",
            "strategy": "market_order",
            "params": {
                "symbol": "NKE",
                "action": "SELL",
                "quantity": 70,
                "closing_position": True,
                "conId": 9,
            },
            "rationale": "flatten-style close",
        },
        object(),
        world,
        snap,
    )
    assert close.get("status") == "ok"
    assert "params.card" not in str(close.get("note") or "")
    assert sent and sent[0]["strategy"] == "market_order"
    assert sent[0]["params"].get("card") in (None, "")

    sent.clear()
    nke = await execute_ticket(
        {
            "action": "market_order",
            "strategy": "market_order",
            "params": {"symbol": "NKE", "action": "SELL", "closing_position": True},
            "rationale": "symbol-only close",
        },
        object(),
        world,
        snap,
    )
    assert nke.get("status") in ("blocked", "validated_block")
    blob = str(nke)
    assert "params.card" not in blob
    assert "conId" in blob or "conid" in blob.lower()
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_without_session_does_not_invent_a_candles_gate(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
    assert "params.symbol" in str(result.get("note") or "")
    assert "candles" not in str(result.get("note") or "").lower()
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
async def test_session_card_send_before_open_print_is_refused(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    world = WorldState(
        cycle=1,
        session_status="premarket",
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
    snap_base = {
        "account": {"netliquidation": 37000.0},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {"SNDK": 91.5},
    }

    def _ticket():
        return {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": dict(act["params"]),
            "rationale": act["rationale"],
        }

    result = await execute_ticket(_ticket(), object(), world, snap_base)
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == "opening_print"
    note = str(result.get("note") or "")
    assert "opening print" in note.lower()
    assert "candles" not in note.lower()
    assert "hold" not in note.lower()
    assert sent == []

    prior = await execute_ticket(
        _ticket(),
        object(),
        world,
        {
            **snap_base,
            "session_range": {
                "SNDK": {"today": False, "low": 88.0, "open": 90.0, "last": 91.5}
            },
        },
    )
    assert prior.get("status") == "blocked"
    assert prior.get("reason_code") == "opening_print"
    assert sent == []

    ok = await execute_ticket(
        _ticket(),
        object(),
        world,
        {
            **snap_base,
            "session_range": {
                "SNDK": {
                    "today": True,
                    "low": 88.0,
                    "open": 90.0,
                    "last": 91.5,
                    "above_low": False,
                }
            },
        },
    )
    assert ok.get("status") == "ok"
    assert sent and sent[0]["params"]["stop_price"] == 88.0


@pytest.mark.asyncio
async def test_non_session_card_may_send_in_premarket(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    world = WorldState(
        cycle=1,
        session_status="premarket",
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
                "card": "generic STK market bracket",
                "symbol": "SNDK",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
                "quantity": 10,
            },
        },
        object(),
        world,
        {
            "account": {"netliquidation": 37000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"SNDK": 91.5},
        },
    )
    assert result.get("status") == "ok"
    assert sent


@pytest.mark.asyncio
async def test_execute_ticket_does_not_invent_a_hold_above_open_gate(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
            "ibkr_live_quotes": {"SNDK": 91.5},
            "session_range": {
                "SNDK": {
                    "today": True,
                    "low": 88.0,
                    "last": 91.5,
                    "above_low": False,
                    "above_open": False,
                    "retrace_30": 93.0,
                }
            },
        },
    )
    assert result.get("status") == "ok"
    assert "opening low" not in str(result.get("note") or "")
    assert sent and sent[0]["params"]["stop_price"] == 88.0


@pytest.mark.asyncio
async def test_execute_ticket_does_not_invent_a_gap_floor_gate(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
    assert result.get("status") == "ok"
    assert "gap under" not in str(result.get("note") or "")
    assert sent and sent[0]["params"]["symbol"] == "MU"


@pytest.mark.asyncio
async def test_execute_ticket_does_not_invent_a_card_price_floor_gate(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
    assert result.get("status") == "ok"
    assert "last under" not in str(result.get("note") or "")
    assert sent and sent[0]["params"]["symbol"] == "AAOI"


@pytest.mark.asyncio
async def test_execute_ticket_does_not_invent_a_tight_spread_gate(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
    assert result.get("status") == "ok"
    assert "spread wider" not in str(result.get("note") or "")
    assert sent and sent[0]["params"]["symbol"] == "SNDK"


@pytest.mark.asyncio
async def test_execute_ticket_does_not_apply_hunt_hold_to_manage(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

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
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
async def test_execute_ticket_does_not_invent_a_no_add_gate(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
    assert result.get("status") == "ok"
    assert "no add" not in str(result.get("note") or "")
    assert sent and sent[0]["params"]["symbol"] == "SNDK"


@pytest.mark.asyncio
async def test_execute_ticket_blocks_new_risk_on_a_price_hint(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
    assert result.get("reason_code") == "stale_or_invented_number"
    assert "91.5" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_blocks_when_one_share_blows_card_risk(monkeypatch):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
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
    assert "quantity or size_pct_nl" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_size_pct_nl_is_not_a_card_one_pct_refuse(monkeypatch):
    """A card note of 1% is not a refuse. Grok's % of NL sizes the ticket."""
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.world_state import WorldState

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr(
        "abcxauto.agent_loop.get_config",
        lambda: SimpleNamespace(
            is_paper=True,
            trading_mode="paper",
            max_risk_per_trade_pct=25.0,
            max_position_pct=25.0,
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
                "stop_price": 490.0,
                "target_price": 520.0,
                "size_pct_nl": 5.0,
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
    assert result.get("status") == "ok", result
    assert sent
    assert sent[0]["params"]["quantity"] == 3
    assert sent[0]["params"]["size_pct_nl"] == 5.0
