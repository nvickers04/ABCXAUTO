"""Portfolio USD math is display-only. KEEP-5A refuse is deleted. No TWS."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from abcxauto.config import Config, get_config, update_risk_config
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.portfolio_loss import (
    PORTFOLIO_CAP_USD_DEFAULT,
    REASON_PORTFOLIO_USD,
    REASON_PORTFOLIO_USD_CAP,
    REASON_PORTFOLIO_USD_UNREADABLE,
    defined_max_loss_usd,
    is_working_exit_or_last_stop,
    live_portfolio_usd_check,
    portfolio_usd_check,
    portfolio_usd_place_block,
    resolve_portfolio_cap_usd,
)
from abcxauto.self_tune import OPERATOR_DISK_KEYS, apply_self_tune
from abcxauto.memory import get_journal
from abcxauto.send_preview import (
    bind_place_token,
    collect_would_refuse,
    preview_ticket,
    reset_preview_state,
)
from abcxauto.thin_rth_kill_look import (
    F10_HARD_USD,
    MODE_OPEN,
    REASON_ALLOWLIST,
    pcs_send_ok,
)
from abcxauto.world_state import WorldState
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK


def setup_function():
    reset_preview_state()


def teardown_function():
    reset_preview_state()


def _lot(max_loss: float | None = 400.0, **extra) -> dict:
    row = {"symbol": "SPY", "quantity": 1, "secType": "OPT"}
    if max_loss is not None:
        row["max_loss"] = max_loss
    row.update(extra)
    return row


def _working(max_loss: float = 200.0, *, oid: int = 11, **extra) -> dict:
    row = {"order_id": oid, "symbol": "QQQ", "quantity": 1, "max_loss": max_loss}
    row.update(extra)
    return row


def _vertical(max_loss: float | None = 300.0, **overrides) -> dict:
    params = {
        "symbol": "IWM",
        "expiration": "20260718",
        "long_strike": 200.0,
        "short_strike": 205.0,
        "right": "P",
        "quantity": 1,
        "limit_price": 2.0,
        "card": "pcs-skew",
    }
    params.update(overrides)
    act = {
        "strategy": "vertical_spread",
        "action": "vertical_spread",
        "params": params,
        "rationale": "portfolio-usd-display",
        "card": params.get("card") or "pcs-skew",
        "_desk_session": "regular",
    }
    if max_loss is not None:
        act["max_loss"] = max_loss
    return act


def _closer(**overrides) -> dict:
    params = {"symbol": "SPY", "quantity": 1, "closing_position": True}
    params.update(overrides)
    return {
        "strategy": "close_option",
        "params": params,
        "rationale": "exit",
        "_desk_session": "regular",
    }


def _last_stop() -> dict:
    return {
        "strategy": "stop_order",
        "params": {
            "symbol": "SPY",
            "quantity": 10,
            "stop_price": 490.0,
            "last_stop": True,
        },
        "rationale": "unprotected last-stop",
        "_desk_session": "regular",
    }


def _world(**kwargs) -> WorldState:
    base = dict(
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
        capacity={
            "open_count": 0,
            "max_open_positions": 0,
            "slots_left": 0,
            "allows_new_risk": True,
        },
    )
    base.update(kwargs)
    return WorldState(**base)


def _connector() -> MagicMock:
    connector = MagicMock()
    connector.connected = True
    connector.place_order = MagicMock()
    connector.place_vertical_spread = MagicMock()
    return connector


def _no_usd_refuse(blob: dict | None) -> None:
    reasons = []
    if isinstance(blob, dict):
        reasons.extend(blob.get("would_refuse") or [])
        reasons.append(blob.get("reason_code") or "")
        reasons.append(blob.get("note") or "")
        reasons.append(blob.get("reason") or "")
    text = " ".join(str(r) for r in reasons)
    assert "portfolio_usd" not in text
    assert REASON_PORTFOLIO_USD not in text
    assert REASON_PORTFOLIO_USD_UNREADABLE not in text
    assert REASON_PORTFOLIO_USD_CAP not in text
    if isinstance(blob, dict):
        assert blob.get("portfolio_usd_refused") is not True


def test_hygiene_f10_port_prompt_lock():
    assert F10_HARD_USD == 2.0
    assert get_config().ibkr_port != 7496
    assert Config().ibkr_port == 7497
    assert get_config().trading_mode == "paper"
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert PORTFOLIO_CAP_USD_DEFAULT == 800.0
    assert get_config().portfolio_cap_usd == 800.0


def test_allowlist_still_refuses_non_pcs_skew(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PCS_KILL_LOOK", "1")
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.live_f10_gate",
        lambda: {
            "allow_new_risk": True,
            "preferred_trip": False,
            "reason_code": "",
            "note": "",
        },
    )
    ok, why = pcs_send_ok(
        "vertical_spread",
        {"symbol": "SPY", "right": "P", "card": "other"},
        "other",
        mode=MODE_OPEN,
    )
    assert ok is False
    assert why == REASON_ALLOWLIST
    ok2, why2 = pcs_send_ok(
        "market_bracket",
        {"symbol": "SPY", "card": "x"},
        "x",
        mode=MODE_OPEN,
    )
    assert ok2 is False
    assert why2 == REASON_ALLOWLIST
    reasons = collect_would_refuse(
        {
            "strategy": "market_bracket",
            "params": {
                "symbol": "SPY",
                "quantity": 1,
                "direction": "LONG",
                "entry_price": 500.0,
                "stop_price": 495.0,
                "target_price": 510.0,
                "card": "other",
            },
            "card": "other",
            "_desk_session": "regular",
        }
    )
    assert any(REASON_ALLOWLIST in str(r) for r in reasons)


def test_module_has_no_ibkr_import():
    import abcxauto.portfolio_loss as mod

    src = inspect.getsource(mod)
    assert "ib_insync" not in src
    assert "abcxauto.broker" not in src
    assert "from ib_" not in src


def test_cap_unset_is_display_default_not_a_gate():
    out = resolve_portfolio_cap_usd(present=False)
    assert out["ok"] is True
    assert out["cap"] == 800.0
    assert out["defaulted"] is True
    assert out["unreadable"] is False


def test_cap_garbage_is_unreadable_not_a_refuse():
    for raw in ("nope", "nan", "inf", "", None, -1, object()):
        out = resolve_portfolio_cap_usd(raw, present=True)
        assert out["ok"] is False
        assert out["unreadable"] is True
        assert out["cap"] is None


def test_open_400_working_200_candidate_300_does_not_refuse():
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[_working(200, oid=11)],
        candidate=_vertical(300),
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert check["portfolio_max_loss_usd"] == pytest.approx(900.0)
    assert check["portfolio_cap_usd"] == 800.0
    assert check["portfolio_usd_refused"] is False
    assert check["allow"] is True
    assert check.get("reason_code") != REASON_PORTFOLIO_USD


def test_closer_at_or_over_cap_is_allowed():
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[_working(200, oid=11)],
        candidate=_closer(),
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert check["closer"] is True
    assert check["portfolio_usd_refused"] is False
    assert check["allow"] is True


def test_unprotected_last_stop_allowed_over_cap():
    check = portfolio_usd_check(
        open_lots=[_lot(800)],
        working=[_working(200, oid=11)],
        candidate=_last_stop(),
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert check["closer"] is True
    assert check["portfolio_usd_refused"] is False
    assert check["allow"] is True


def test_unreadable_candidate_max_loss_does_not_refuse():
    ticket = {
        "strategy": "vertical_spread",
        "params": {"symbol": "SPY", "quantity": 1, "card": "pcs-skew"},
    }
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[],
        candidate=ticket,
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert defined_max_loss_usd(ticket) is None
    assert check["unreadable"] is True
    assert check["portfolio_usd_refused"] is False
    assert check["allow"] is True
    assert check.get("reason_code") != REASON_PORTFOLIO_USD_UNREADABLE


def test_unreadable_open_lot_does_not_refuse_or_invent_zero():
    lot = {"symbol": "SPY", "quantity": 1, "marketValue": 400.0, "mid": 1.25}
    assert defined_max_loss_usd(lot) is None
    check = portfolio_usd_check(
        open_lots=[lot],
        working=[],
        candidate=_vertical(300),
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert check["unreadable"] is True
    assert check["open_usd"] is None
    assert check["portfolio_usd_refused"] is False
    assert check["allow"] is True


def test_closer_passes_when_open_max_loss_unreadable():
    lot = {"symbol": "SPY", "quantity": 1, "marketValue": 400.0}
    check = portfolio_usd_check(
        open_lots=[lot],
        working=[],
        candidate=_closer(),
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert check["allow"] is True
    assert check["portfolio_usd_refused"] is False


def test_working_exit_oid_not_counted():
    exit_row = _working(999, oid=88, role="exit")
    assert is_working_exit_or_last_stop(exit_row) is True
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[exit_row],
        candidate=_vertical(300),
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert check["working_usd"] == pytest.approx(0.0)
    assert check["portfolio_max_loss_usd"] == pytest.approx(700.0)
    assert check["portfolio_usd_refused"] is False
    assert check["allow"] is True


def test_working_last_stop_oid_not_counted():
    stop = _working(999, oid=9, last_stop=True, order_type="STP")
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[stop],
        candidate=_vertical(300),
        portfolio_cap_usd=800,
        cap_present=True,
    )
    assert check["working_usd"] == pytest.approx(0.0)
    assert check["portfolio_max_loss_usd"] == pytest.approx(700.0)
    assert check["allow"] is True


def test_structure_width_minus_credit_preferred_over_explicit():
    ticket = _vertical(max_loss=50.0, long_strike=500.0, short_strike=505.0, limit_price=1.25)
    # width 5 − credit 1.25 = 3.75 × 100 = 375
    assert defined_max_loss_usd(ticket) == pytest.approx(375.0)


def test_mid_is_not_max_loss_evidence():
    row = {"symbol": "SPY", "quantity": 1, "mid": 4.0, "marketValue": 400.0, "last": 3.9}
    assert defined_max_loss_usd(row) is None


def test_garbage_cap_does_not_refuse_new_risk_or_closer():
    bad = portfolio_usd_check(
        open_lots=[],
        working=[],
        candidate=_vertical(300),
        portfolio_cap_usd="nope",
        cap_present=True,
    )
    assert bad["unreadable"] is True
    assert bad["portfolio_usd_refused"] is False
    assert bad["allow"] is True
    assert bad.get("reason_code") != REASON_PORTFOLIO_USD_CAP
    close = portfolio_usd_check(
        open_lots=[],
        working=[],
        candidate=_closer(),
        portfolio_cap_usd="nope",
        cap_present=True,
    )
    assert close["allow"] is True
    assert close["portfolio_usd_refused"] is False


def test_self_tune_cannot_persist_portfolio_cap():
    assert "portfolio_cap_usd" in OPERATOR_DISK_KEYS
    before = get_config().portfolio_cap_usd
    out = apply_self_tune({"portfolio_cap_usd": 2000}, persist=True)
    rejected = out.get("rejected") or {}
    assert "portfolio_cap_usd" in rejected
    assert "operator disk" in rejected["portfolio_cap_usd"]
    assert get_config().portfolio_cap_usd == before
    assert get_config().portfolio_cap_usd == 800.0


def test_operator_disk_may_lower_not_raise():
    update_risk_config(portfolio_cap_usd=500, persist=True)
    assert get_config().portfolio_cap_usd == 500.0
    update_risk_config(portfolio_cap_usd=2000, persist=True)
    assert get_config().portfolio_cap_usd == 800.0


def test_preview_does_not_refuse_on_portfolio_usd_alone(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *a, **k: None,
    )
    ticket = _vertical(300)
    snap = {
        "account": {"netliquidation": 100000},
        "positions": [_lot(400)],
        "open_orders": [_working(200)],
    }
    reasons = collect_would_refuse(ticket, snap=snap)
    assert not any("portfolio_usd" in str(r) for r in reasons)
    assert not any(REASON_PORTFOLIO_USD in str(r) for r in reasons)
    out = preview_ticket(ticket, snap=snap)
    _no_usd_refuse(out)
    assert not any("portfolio_usd" in str(r) for r in (out.get("would_refuse") or []))
    row = get_journal().get_send_preview(out["preview_id"])
    if row is not None:
        assert row.get("portfolio_usd_refused") is not True


def test_preview_closer_over_cap_passes():
    world = _world(positions=[_lot(800)])
    snap = {"account": {"netliquidation": 100000}, "positions": [_lot(800)]}
    out = preview_ticket(_closer(), world=world, snap=snap)
    assert out["portfolio_usd_refused"] is not True
    assert not any("portfolio_usd" in str(r) for r in (out.get("would_refuse") or []))


def test_place_block_helper_never_blocks():
    ticket = _vertical(300)
    ticket["_live_positions"] = [_lot(400)]
    ticket["_open_orders"] = [_working(200)]
    assert portfolio_usd_place_block(ticket) is None
    assert ticket.get("portfolio_usd_refused") is not True


@pytest.mark.asyncio
async def test_over_cap_place_reaches_execute(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(True)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    ticket = _vertical(300)
    ticket["_live_positions"] = [_lot(400)]
    ticket["_open_orders"] = [_working(200)]
    bind_place_token(ticket)
    result = await send_action(ticket, _connector())
    assert dispatched == [True]
    assert result["status"] == "ok"
    _no_usd_refuse(result)


@pytest.mark.asyncio
async def test_closer_place_over_cap_reaches_execute(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(True)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    ticket = _closer()
    ticket["_live_positions"] = [_lot(800)]
    ticket["_open_orders"] = [_working(200)]
    result = await send_action(ticket, _connector())
    assert result["status"] == "ok"
    assert dispatched == [True]


@pytest.mark.asyncio
async def test_unreadable_candidate_place_reaches_execute(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(True)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    ticket = {
        "strategy": "vertical_spread",
        "params": {"symbol": "SPY", "quantity": 1, "card": "pcs-skew"},
        "card": "pcs-skew",
        "_desk_session": "regular",
    }
    bind_place_token(ticket)
    result = await send_action(ticket, _connector())
    assert dispatched == [True]
    assert result["status"] == "ok"
    _no_usd_refuse(result)


def test_live_check_uses_fixture_book_without_refuse():
    ticket = _vertical(300)
    ticket["_live_positions"] = [_lot(400)]
    ticket["_open_orders"] = [_working(200)]
    check = live_portfolio_usd_check(ticket, portfolio_cap_usd=800, cap_present=True)
    assert check["portfolio_usd_refused"] is False
    assert check["allow"] is True
    assert check["portfolio_max_loss_usd"] == pytest.approx(900.0)
