"""Mega-worker: capacity streams, merge, frequency Fact."""

from __future__ import annotations

from types import SimpleNamespace

from abcxauto.mega_worker import (
    capacity_allows_new_risk,
    merge_send_queue,
    safety_facts_broken,
    select_streams,
)


def _world(**kwargs):
    base = dict(
        needs_protection=False,
        unprotected=[],
        trade_plan={"symbol": "SPY", "direction": "LONG"},
        trade_plans=[{"symbol": "SPY"}],
        positions=[{"symbol": "SPY", "quantity": 8}],
        capacity={
            "open_count": 1,
            "max_open_positions": 6,
            "slots_left": 5,
            "allows_new_risk": True,
        },
        stop_qty_fact=None,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_capacity_allows_and_blocks():
    assert capacity_allows_new_risk(_world()) is True
    assert (
        capacity_allows_new_risk(
            _world(
                capacity={
                    "open_count": 6,
                    "max_open_positions": 6,
                    "slots_left": 0,
                    "allows_new_risk": False,
                }
            )
        )
        is False
    )


def test_safety_facts_prefer_open_risk():
    w = _world(needs_protection=True, unprotected=["SPY"])
    assert safety_facts_broken(w) is True
    streams = select_streams(
        {"stance": "hunt", "intent": {"kind": "hunt"}},
        w,
        needs_prot=True,
        cfg=SimpleNamespace(
            control_budget_pct=90,
            control_frequency_pct=90,
            control_deliberation_pct=80,
            control_rotation_pct=50,
        ),
    )
    assert streams == ["open_risk"]


def test_select_streams_dual_under_budget():
    w = _world()
    cfg = SimpleNamespace(
        control_budget_pct=80,
        control_frequency_pct=70,
        control_deliberation_pct=70,
        control_rotation_pct=50,
    )
    streams = select_streams(
        {
            "stance": "manage",
            "intent": {"kind": "manage"},
            "secondary_intent": {"kind": "hunt", "symbol": "QQQ"},
        },
        w,
        cfg=cfg,
    )
    assert "open_risk" in streams
    assert "new_risk" in streams or "escapade" in streams


def test_merge_open_risk_wins_on_safety():
    w = _world(
        stop_qty_fact={"mismatch": True, "symbol": "SPY"},
    )
    merged = merge_send_queue(
        [
            {
                "_stream": "new_risk",
                "strategy": "market_bracket",
                "action": "market_bracket",
            },
            {
                "_stream": "open_risk",
                "strategy": "modify_stop",
                "action": "modify_stop",
            },
        ],
        world=w,
        judgment={"stance": "hunt"},
    )
    assert merged is not None
    assert merged.get("_stream") == "open_risk"
    assert merged.get("strategy") == "modify_stop"


def test_idle_single_stream_no_escapade():
    streams = select_streams(
        {"stance": "idle", "intent": {"kind": "idle"}},
        _world(trade_plan=None, trade_plans=[], positions=[]),
        cfg=SimpleNamespace(
            control_budget_pct=90,
            control_frequency_pct=90,
            control_deliberation_pct=90,
            control_rotation_pct=50,
        ),
    )
    assert streams == ["open_risk"]


def test_rotation_thin_cash_keeps_open_risk_and_suffix():
    from abcxauto.mega_worker import stream_act_prompt_suffix

    w = _world(
        portfolio_risk={
            "capital_liquidity": {
                "cash_pct_nl": 5.0,
                "deployed_long_pct_nl": 94.0,
                "cash_thin": True,
            }
        }
    )
    cfg = SimpleNamespace(
        control_budget_pct=50,
        control_frequency_pct=50,
        control_deliberation_pct=50,
        control_rotation_pct=90,
    )
    streams = select_streams(
        {"stance": "manage", "intent": {"kind": "manage"}},
        w,
        cfg=cfg,
    )
    assert "open_risk" in streams
    suffix = stream_act_prompt_suffix("open_risk", world=w, cfg=cfg)
    assert "capital_rotation" in suffix or "free cash" in suffix.lower()
