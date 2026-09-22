"""F10 session cost prefers look_ledger.session_spend when present."""

from __future__ import annotations

from abcxauto.thin_rth_kill_look import (
    EST_THIS_LOOK_USD,
    F10_HARD_USD,
    F10_PREFERRED_USD,
    PCS_CARD,
    REASON_F10,
    REASON_MODEL_COST,
    f10_gate,
    kill_look_send_block,
    session_model_cost_usd,
    skip_look_reason,
)

PCS_OPEN = {
    "symbol": "SPY",
    "expiration": "20260918",
    "long_strike": 500.0,
    "short_strike": 505.0,
    "right": "P",
    "quantity": 1,
    "limit_price": 0.85,
    "card": PCS_CARD,
}


def _kill_on(monkeypatch) -> None:
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1")


def test_ledger_hard_spend_blocks_new_risk(monkeypatch):
    """{usd:15.5, unknown:False, hard:True} refuses new risk; looks still run."""
    _kill_on(monkeypatch)
    monkeypatch.setattr(
        "abcxauto.look_ledger.session_spend",
        lambda day, *, path=None: {
            "usd": 15.5,
            "unknown": False,
            "preferred": True,
            "hard": True,
        },
    )
    cost = session_model_cost_usd()
    assert cost == 15.5
    assert F10_HARD_USD == 15.0
    assert F10_PREFERRED_USD == 10.0
    gate = f10_gate(cost, est_this_look=EST_THIS_LOOK_USD, window_cost=0.0)
    assert gate["allow_new_risk"] is False
    assert gate["reason_code"] == REASON_F10
    assert skip_look_reason("regular", positions=[], f10=gate) == ""
    act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    blocked = kill_look_send_block(act, session="regular", f10=gate)
    assert blocked is not None
    assert blocked["reason_code"] == REASON_F10
    close = {
        "strategy": "vertical_spread",
        "params": {**PCS_OPEN, "closing_position": True},
        "card": PCS_CARD,
    }
    assert kill_look_send_block(close, session="regular", f10=gate) is None


def test_ledger_unknown_spend_fail_closed_not_zero(monkeypatch):
    """{usd:None, unknown:True, hard:True} is unreadable — not a $0 session."""
    _kill_on(monkeypatch)
    monkeypatch.setattr(
        "abcxauto.look_ledger.session_spend",
        lambda day, *, path=None: {
            "usd": None,
            "unknown": True,
            "preferred": False,
            "hard": True,
        },
    )
    cost = session_model_cost_usd()
    assert cost is None
    assert cost != 0
    assert cost != 0.0
    gate = f10_gate(cost, est_this_look=EST_THIS_LOOK_USD, window_cost=0.0)
    assert gate["allow_new_risk"] is False
    assert gate["reason_code"] == REASON_MODEL_COST
    assert gate["projected"] is None
    assert skip_look_reason("regular", positions=[], f10=gate) == ""
    act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    blocked = kill_look_send_block(act, session="regular", f10=gate)
    assert blocked is not None
    assert blocked["reason_code"] == REASON_MODEL_COST
