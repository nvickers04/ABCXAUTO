"""Overlay share guard: clerk inventory only (no clock, tape, or ticket)."""

from __future__ import annotations

import abcxauto.trade_playbook as trade_playbook
from abcxauto.trade_playbook import (
    OVERLAY_NO_LONG_STOCK,
    OVERLAY_SHARES_INSUFFICIENT,
    OVERLAY_SHARES_UNSPECIFIED,
    check_overlay_shares,
    long_share_lots,
)

_DEAD_LAW = (
    "format_trade_playbook",
    "world_hints_from_world",
    "max_long_shares",
    "_PLAYBOOK",
    "set_wake",
    "next_look",
    "schedule_look",
    "focus_tickers",
    "ticker_dump",
    "send_hint",
)


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


def test_untyped_lot_is_not_painted_as_stock():
    lots = long_share_lots(
        [
            {"symbol": "IWM", "quantity": 100},
            {"symbol": "QQQ", "secType": "OPT", "quantity": 10},
            {"symbol": "SPY", "secType": "STK", "quantity": 50},
            {"symbol": "DIA", "secType": "STK", "quantity": float("nan")},
        ]
    )
    assert lots == {"SPY": 50.0}

    ok, code, _ = check_overlay_shares(
        "covered_call",
        {"symbol": "IWM", "shares": 100},
        [{"symbol": "IWM", "quantity": 100}],
    )
    assert ok is False
    assert code == OVERLAY_NO_LONG_STOCK


def test_overlay_does_not_invent_shares():
    params = {"symbol": "IWM"}
    ok, code, msg = check_overlay_shares(
        "covered_call",
        params,
        [{"symbol": "IWM", "secType": "STK", "quantity": 200}],
    )
    assert ok is False
    assert code == OVERLAY_SHARES_UNSPECIFIED
    assert params == {"symbol": "IWM"}
    assert "not invented" in msg


def test_overlay_unreadable_shares_fail_closed():
    book = [{"symbol": "IWM", "secType": "STK", "quantity": 200}]
    for raw in ("x", float("nan"), float("inf"), 0, -100, True):
        params = {"symbol": "IWM", "shares": raw}
        ok, code, _ = check_overlay_shares("collar", params, book)
        assert ok is False, raw
        assert code == OVERLAY_SHARES_UNSPECIFIED, raw
        assert params["shares"] is raw


def test_overlay_does_not_write_ticket_fields():
    params = {"symbol": "IWM", "shares": 100, "strike": 300}
    ok, code, _ = check_overlay_shares(
        "protective_put",
        params,
        [{"symbol": "IWM", "secType": "STK", "quantity": 100}],
    )
    assert ok is True
    assert code == "ok"
    assert params == {"symbol": "IWM", "shares": 100, "strike": 300}


def test_non_overlay_is_not_playbook_law():
    params = {"symbol": "QQQ", "quantity": 1}
    ok, code, msg = check_overlay_shares("market_bracket", params, [])
    assert ok is True
    assert code == "ok"
    assert msg == "n/a"
    assert params == {"symbol": "QQQ", "quantity": 1}


def test_leftover_playbook_law_is_gone():
    for name in _DEAD_LAW:
        assert not hasattr(trade_playbook, name), name
    assert set(trade_playbook.__all__) == {
        "OVERLAY_NO_LONG_STOCK",
        "OVERLAY_SHARES_INSUFFICIENT",
        "OVERLAY_SHARES_UNSPECIFIED",
        "check_overlay_shares",
        "long_share_lots",
    }


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
