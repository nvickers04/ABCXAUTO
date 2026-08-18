"""Trade-type playbook: overlay share guard (no stance allowlist)."""

from __future__ import annotations

from abcxauto.trade_playbook import (
    OVERLAY_NO_LONG_STOCK,
    OVERLAY_SHARES_INSUFFICIENT,
    check_overlay_shares,
    long_share_lots,
    world_hints_from_world,
)
from abcxauto.world_state import WorldState


def _world(**kwargs) -> WorldState:
    base = dict(
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
    )
    base.update(kwargs)
    return WorldState(**base)


def test_share_lot_guard_rejects_without_stock():
    ok, code, msg = check_overlay_shares(
        "covered_call",
        {"symbol": "IWM", "shares": 100, "strike": 300, "expiration": "20260821"},
        [{"symbol": "IWM", "secType": "STK", "quantity": 22}],
    )
    assert ok is False
    assert code == OVERLAY_SHARES_INSUFFICIENT
    assert "22" in msg

    ok2, code2, _ = check_overlay_shares(
        "covered_call",
        {"symbol": "QQQ", "shares": 100},
        [{"symbol": "IWM", "secType": "STK", "quantity": 200}],
    )
    assert ok2 is False
    assert code2 == OVERLAY_NO_LONG_STOCK

    ok3, code3, _ = check_overlay_shares(
        "collar",
        {"symbol": "IWM", "shares": 100, "put_strike": 290, "call_strike": 310},
        [{"symbol": "IWM", "secType": "STK", "quantity": 100}],
    )
    assert ok3 is True
    assert code3 == "ok"


def test_long_share_lots_ignores_shorts():
    lots = long_share_lots(
        [
            {"symbol": "IWM", "secType": "STK", "quantity": 100},
            {"symbol": "QQQ", "secType": "STK", "quantity": -50},
        ]
    )
    assert lots == {"IWM": 100.0}


def test_strategy_diversity_observe_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "j.db"))
    from abcxauto.memory import reset_journal

    j = reset_journal(path=str(tmp_path / "j.db"), enabled=True)
    j.record_decision(cycle=1, action="market_bracket", strategy="market_bracket", rationale="a")
    j.record_decision(cycle=2, action="hold", strategy="hold", rationale="b")
    j.record_decision(cycle=3, action="covered_call", strategy="covered_call", rationale="c")
    j.record_decision(cycle=4, action="blocked", strategy="blocked", rationale="d")
    div = j.strategy_diversity(limit=40)
    assert div["n_distinct"] == 2
    assert set(div["strategies"]) == {"market_bracket", "covered_call"}
    assert div["n_decisions"] == 4


def test_world_hints_from_world():
    world = _world(
        flat=False,
        positions=[{"symbol": "IWM", "secType": "STK", "quantity": 100}],
        trade_plan={"symbol": "IWM"},
    )
    hints = world_hints_from_world(world)
    assert hints["long_lots"]["IWM"] == 100.0
    assert hints["has_trade_plan"] is True
