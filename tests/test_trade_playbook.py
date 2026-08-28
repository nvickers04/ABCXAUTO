"""Overlay share + last-stop guard (no clock, tape, or ticket)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import abcxauto.trade_playbook as trade_playbook
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.trade_playbook import (
    OVERLAY_ALREADY_PROTECTED,
    OVERLAY_NO_LONG_STOCK,
    OVERLAY_SHARES_INSUFFICIENT,
    OVERLAY_SHARES_UNSPECIFIED,
    check_overlay_shares,
    long_share_lots,
)
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

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
        "OVERLAY_ALREADY_PROTECTED",
        "OVERLAY_NO_LONG_STOCK",
        "OVERLAY_SHARES_INSUFFICIENT",
        "OVERLAY_SHARES_UNSPECIFIED",
        "check_overlay_shares",
        "long_share_lots",
    }


def test_system_prompt_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def _pypl_long(qty: float = 50) -> dict:
    return {"symbol": "PYPL", "secType": "STK", "quantity": qty}


def _last_stop(symbol: str = "PYPL", qty: float = 50, stop: float = 52.61) -> dict:
    return {
        "order_id": 9,
        "symbol": symbol,
        "secType": "STK",
        "action": "SELL",
        "quantity": qty,
        "order_type": "STP",
        "stop_price": stop,
    }


def _pp_params(symbol: str = "PYPL", shares: float = 50) -> dict:
    return {
        "symbol": symbol,
        "expiration": "20260918",
        "strike": 50.0,
        "shares": shares,
    }


def test_pypl_last_stop_refuses_protective_put_sample():
    """Virgin protective_put on a last-stop-covered lot does not reduce risk."""
    book = [_pypl_long(50)]
    stop = [_last_stop("PYPL", 50, 52.61)]
    ok, code, msg = check_overlay_shares(
        "protective_put", _pp_params(), book, stop
    )
    assert ok is False
    assert code == OVERLAY_ALREADY_PROTECTED
    assert "PYPL" in msg
    assert "last-stop" in msg


@pytest.mark.parametrize("strat", ("protective_put", "covered_call", "collar"))
def test_last_stop_refuses_share_overlays(strat):
    params = _pp_params()
    if strat == "collar":
        params = {
            "symbol": "PYPL",
            "expiration": "20260918",
            "put_strike": 50.0,
            "call_strike": 60.0,
            "shares": 50,
        }
    ok, code, _ = check_overlay_shares(
        strat, params, [_pypl_long(50)], [_last_stop()]
    )
    assert ok is False
    assert code == OVERLAY_ALREADY_PROTECTED


def test_cash_secured_put_on_protected_lot_is_refused():
    ok, code, _ = check_overlay_shares(
        "cash_secured_put",
        {"symbol": "PYPL", "expiration": "20260918", "strike": 50.0, "contracts": 1},
        [_pypl_long(50)],
        [_last_stop()],
    )
    assert ok is False
    assert code == OVERLAY_ALREADY_PROTECTED


def test_unprotected_pypl_still_accepts_protective_put():
    ok, code, msg = check_overlay_shares(
        "protective_put", _pp_params(), [_pypl_long(50)], []
    )
    assert ok is True
    assert code == "ok"
    assert msg == "shares ok"


def test_crumb_stop_is_not_a_covering_last_stop_for_overlay():
    ok, code, _ = check_overlay_shares(
        "protective_put",
        _pp_params(),
        [_pypl_long(50)],
        [_last_stop("PYPL", 1, 52.61)],
    )
    assert ok is True
    assert code == "ok"


def test_csp_on_a_different_name_is_not_refused():
    """Defined-risk new-risk on another name is not this overlay gate."""
    ok, code, msg = check_overlay_shares(
        "cash_secured_put",
        {"symbol": "AAPL", "expiration": "20260918", "strike": 200.0, "contracts": 1},
        [_pypl_long(50)],
        [_last_stop()],
    )
    assert ok is True
    assert code == "ok"
    assert msg == "n/a"


def test_vertical_on_another_name_is_not_overlay_law():
    ok, code, msg = check_overlay_shares(
        "vertical_spread",
        {"symbol": "AAPL", "quantity": 1},
        [_pypl_long(50)],
        [_last_stop()],
    )
    assert ok is True
    assert code == "ok"
    assert msg == "n/a"


def _stub_overlay_send(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)
    monkeypatch.setattr("abcxauto.lab_playbook.live_new_risk_allowed", lambda: True)
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
    return sent


def _world(**kw):
    from abcxauto.world_state import WorldState

    fields = dict(
        cycle=1,
        session_status="regular",
        flat=False,
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


def _pp_ticket() -> dict:
    return {
        "action": "protective_put",
        "strategy": "protective_put",
        "params": _pp_params(),
        "rationale": "overlay sample",
    }


@pytest.mark.asyncio
async def test_execute_ticket_refuses_protective_put_on_pypl_last_stop(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    sent = _stub_overlay_send(monkeypatch)
    pos = [_pypl_long(50)]
    stop = [_last_stop("PYPL", 50, 52.61)]
    result = await execute_ticket(
        _pp_ticket(),
        object(),
        _world(positions=pos, open_orders=stop, flat=False),
        {
            "account": {"netliquidation": 100_000.0},
            "positions": pos,
            "open_orders": stop,
        },
    )
    assert result.get("status") == "blocked"
    assert result.get("reason_code") == OVERLAY_ALREADY_PROTECTED
    assert "last-stop" in str(result.get("note") or "")
    assert sent == []


@pytest.mark.asyncio
async def test_execute_ticket_accepts_protective_put_on_unprotected_pypl(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    sent = _stub_overlay_send(monkeypatch)
    pos = [_pypl_long(50)]
    result = await execute_ticket(
        _pp_ticket(),
        object(),
        _world(
            positions=pos,
            open_orders=[],
            flat=False,
            needs_protection=True,
            unprotected=["PYPL"],
        ),
        {
            "account": {"netliquidation": 100_000.0},
            "positions": pos,
            "open_orders": [],
        },
    )
    assert result.get("status") == "ok"
    assert sent
    assert sent[0]["strategy"] == "protective_put"


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
