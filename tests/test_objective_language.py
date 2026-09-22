"""Book/wake text: no Controls lecture, no old scan headers."""

from __future__ import annotations

from abcxauto.config import get_config
from abcxauto.opportunity_scan import metrics_for_symbol


def test_metrics_no_advice_note():
    candles = [{"c": 100.0 + i * 0.1} for i in range(60)]
    idea = metrics_for_symbol(candles, "SPY")
    assert idea is not None
    assert "score" not in idea
    assert idea.get("source") == "mda"
    assert "uptrend support" not in str(idea).lower()


def test_book_facts_have_no_controls_lecture(monkeypatch):
    from abcxauto.brain import _book_payload
    from abcxauto.world_state import WorldState

    get_config.cache_clear()

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
        opportunities=[
            {
                "symbol": "QQQ",
                "source": "mda",
                "freshness": "delayed",
                "mda_last": 100.0,
                "dist20": 0.0,
                "ret5": 0.0,
            }
        ],
        news_items=[],
        risk_posture="aggressive",
        effective_posture="aggressive",
        gates={},
        envelope={},
        regime={
            "trend_bias": "bullish",
            "feature_mix_bias": "bullish",
            "session_phase": "mid",
            "vol_proxy": "normal",
        },
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    blob = _book_payload(world)
    from tests.conftest import assert_no_cycle_keys

    assert_no_cycle_keys(blob.get("world") if isinstance(blob.get("world"), dict) else {})
    prompt = "\n".join(str(blob.get(k) or "") for k in ("world", "day"))
    assert "controls" not in blob
    assert "CONTROLS" not in prompt
    assert "mandate_summary" not in str(blob)
    assert "MANDATE" not in prompt
    assert "scan_tape" not in prompt.lower()
    assert "SCAN TAPE" not in prompt
    assert "prefer manage" not in prompt.lower()
    assert "prefer acting" not in prompt.lower()
    assert "floor" not in blob
    assert "operator_card" not in blob
    assert "levers" not in blob
    assert "last_look" not in blob
    assert "IBKR" in prompt
    assert "idle_streak" not in prompt


def test_book_payload_keeps_zero_cash():
    """TotalCashValue 0 is leftover $0, not NL minus deployed."""
    from abcxauto.brain import _book_payload
    from abcxauto.world_state import WorldState

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=10000.0,
        daily_pnl=0.0,
        positions=[
            {
                "symbol": "AVGO",
                "sec_type": "STK",
                "quantity": 10,
                "market_price": 300.0,
                "avg_cost": 300.0,
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
        portfolio_risk={
            "capital_liquidity": {
                "total_cash": 0.0,
                "cash_pct_nl": 0.0,
                "deployed_long_pct_nl": 30.0,
            }
        },
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    blob = _book_payload(world)
    line = str(blob.get("allocation_line") or "")
    assert "leftover $0" in line
    assert "cash=0" in line
    assert "cash=70" not in line


def test_day_facts_surface_portfolio_risk():
    """Clerk day_facts ships portfolio_risk % of NL (not brain._book_facts)."""
    from abcxauto.world_state import WorldState, day_facts

    port = {
        "n_positions": 1,
        "top_symbol": "QQQ",
        "top_concentration_pct": 13.51,
        "exposure": {
            "top_symbol": "QQQ",
            "top_concentration_pct": 13.51,
            "symbols": [{"symbol": "QQQ", "pct_nl": 13.51}],
        },
        "capital_liquidity": {
            "total_cash": 32000.0,
            "cash_pct_nl": 86.49,
            "deployed_long_pct_nl": 13.51,
        },
    }
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=False,
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
        portfolio_risk=port,
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    day = day_facts(world, {})
    assert day["portfolio_risk"]["top_concentration_pct"] == 13.51
    assert day["exposure"]["top_concentration_pct"] == 13.51
    assert day["exposure"]["symbols"][0]["pct_nl"] == 13.51
    assert day["capital_liquidity"]["cash_pct_nl"] == 86.49
    assert day["capital_liquidity"]["deployed_long_pct_nl"] == 13.51


def test_format_wake_includes_portfolio_pct_nl():
    from abcxauto.world_state import format_wake

    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "names": 1,
            "lots": 1,
            "nl": 10_000.0,
            "daily_pnl": -100.0,
            "daily_pnl_pct_of_nl": -1.0,
            "capital_liquidity": {
                "cash_pct_nl": 80.0,
                "deployed_long_pct_nl": 20.0,
            },
            "exposure": {
                "top_symbol": "QQQ",
                "top_concentration_pct": 20.0,
            },
            "capacity": {"open_count": 1, "max_open_positions": 0},
            "open_lots": ["QQQ STK long 10"],
        },
    )
    # Leftover / deployed share allocation_line wording (no "% NL" suffix).
    assert "cash=80.0%" in text
    assert "deployed=20.0%" in text
    assert "top QQQ=20.0% NL" in text


def _day_world(**kwargs):
    from abcxauto.world_state import WorldState

    fields = dict(
        cycle=1,
        session_status="regular",
        flat=False,
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
    fields.update(kwargs)
    return WorldState(**fields)


def test_day_facts_rebuilds_capital_liquidity_when_bag_missing():
    """Positions + NL + real cash still stamp cash_pct / deployed when the risk bag is empty."""
    from abcxauto.world_state import day_facts

    world = _day_world(
        positions=[
            {
                "symbol": "QQQ",
                "quantity": 10,
                "marketValue": 5000,
                "secType": "STK",
            }
        ],
        portfolio_risk={},
        book={"capital_liquidity": {"total_cash": 32000.0}},
    )
    day = day_facts(world, {})
    cap = day["capital_liquidity"]
    assert cap["cash_pct_nl"] == 86.49
    assert cap["deployed_long_pct_nl"] == 13.51
    assert cap["total_cash"] == 32000.0


def test_day_facts_does_not_invent_cash_pct_when_cash_missing():
    from abcxauto.world_state import day_facts

    world = _day_world(
        positions=[
            {
                "symbol": "QQQ",
                "quantity": 10,
                "marketValue": 5000,
                "secType": "STK",
            }
        ],
        portfolio_risk={},
        book={},
    )
    day = day_facts(world, {})
    cap = day.get("capital_liquidity") or {}
    assert cap.get("cash_pct_nl") is None
    assert cap.get("total_cash") is None
    assert cap.get("deployed_long_pct_nl") == 13.51


