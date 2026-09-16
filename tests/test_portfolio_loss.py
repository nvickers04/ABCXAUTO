"""Portfolio aggregate defined-max-loss math. Display only. No TWS."""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock

import pytest

from abcxauto.config import Config, get_config, load_risk_settings
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.portfolio_loss import (
    defined_max_loss_usd,
    is_working_exit_or_last_stop,
    live_portfolio_usd_check,
    portfolio_usd_check,
)
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


def _assert_no_cap_keys(blob: dict | None) -> None:
    if not isinstance(blob, dict):
        return
    assert "portfolio_cap_usd" not in blob
    assert "portfolio_usd_refused" not in blob


def test_hygiene_f10_port_prompt_lock():
    assert F10_HARD_USD == 15.0
    assert get_config().ibkr_port != 7496
    assert Config().ibkr_port == 7497
    assert get_config().trading_mode == "paper"
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert not hasattr(get_config(), "portfolio_cap_usd")


def test_allowlist_refuses_incomplete_geom_and_undefined_stk(monkeypatch):
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


def test_open_400_working_200_candidate_300_sums_exposure():
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[_working(200, oid=11)],
        candidate=_vertical(300),
    )
    assert check["portfolio_max_loss_usd"] == pytest.approx(900.0)
    assert check["allow"] is True


def test_closer_skips_exposure_sum():
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[_working(200, oid=11)],
        candidate=_closer(),
    )
    assert check["closer"] is True
    assert check["allow"] is True
    assert check["portfolio_max_loss_usd"] is None


def test_unprotected_last_stop_skips_exposure_sum():
    check = portfolio_usd_check(
        open_lots=[_lot(800)],
        working=[_working(200, oid=11)],
        candidate=_last_stop(),
    )
    assert check["closer"] is True
    assert check["allow"] is True


def test_unreadable_candidate_max_loss_marks_unreadable():
    ticket = {
        "strategy": "vertical_spread",
        "params": {"symbol": "SPY", "quantity": 1, "card": "pcs-skew"},
    }
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[],
        candidate=ticket,
    )
    assert defined_max_loss_usd(ticket) is None
    assert check["unreadable"] is True
    assert check["allow"] is True


def test_unreadable_open_lot_partial_sum_still_reports_candidate():
    lot = {"symbol": "SPY", "quantity": 1, "marketValue": 400.0, "mid": 1.25}
    assert defined_max_loss_usd(lot) is None
    check = portfolio_usd_check(
        open_lots=[lot],
        working=[],
        candidate=_vertical(300),
    )
    assert check["unreadable"] is True
    assert check["open_usd"] == pytest.approx(0.0)
    assert check["portfolio_max_loss_usd"] == pytest.approx(300.0)
    assert check["allow"] is True


def test_closer_passes_when_open_max_loss_unreadable():
    lot = {"symbol": "SPY", "quantity": 1, "marketValue": 400.0}
    check = portfolio_usd_check(
        open_lots=[lot],
        working=[],
        candidate=_closer(),
    )
    assert check["allow"] is True


def test_working_exit_oid_not_counted():
    exit_row = _working(999, oid=88, role="exit")
    assert is_working_exit_or_last_stop(exit_row) is True
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[exit_row],
        candidate=_vertical(300),
    )
    assert check["working_usd"] == pytest.approx(0.0)
    assert check["portfolio_max_loss_usd"] == pytest.approx(700.0)
    assert check["allow"] is True


def test_working_last_stop_oid_not_counted():
    stop = _working(999, oid=9, last_stop=True, order_type="STP")
    check = portfolio_usd_check(
        open_lots=[_lot(400)],
        working=[stop],
        candidate=_vertical(300),
    )
    assert check["working_usd"] == pytest.approx(0.0)
    assert check["portfolio_max_loss_usd"] == pytest.approx(700.0)
    assert check["allow"] is True


def test_structure_width_minus_credit_preferred_over_explicit():
    ticket = _vertical(max_loss=50.0, long_strike=500.0, short_strike=505.0, limit_price=1.25)
    assert defined_max_loss_usd(ticket) == pytest.approx(375.0)


def test_mid_is_not_max_loss_evidence():
    row = {"symbol": "SPY", "quantity": 1, "mid": 4.0, "marketValue": 400.0, "last": 3.9}
    assert defined_max_loss_usd(row) is None


def test_legacy_portfolio_cap_key_in_risk_file_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "risk_settings.json"
    path.write_text(
        json.dumps({"portfolio_cap_usd": 500, "daily_loss_limit_pct": 25}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("abcxauto.config._risk_settings_path", lambda: path)
    loaded = load_risk_settings(path)
    assert "portfolio_cap_usd" not in loaded
    assert loaded["daily_loss_limit_pct"] == 25.0
    cfg = get_config()
    assert not hasattr(cfg, "portfolio_cap_usd")


def test_preview_reports_exposure_without_cap_keys(monkeypatch):
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
    out = preview_ticket(ticket, snap=snap)
    _assert_no_cap_keys(out)
    assert out["portfolio_max_loss_usd"] == pytest.approx(900.0)
    assert not any("portfolio_usd" in str(r) for r in (out.get("would_refuse") or []))


def test_preview_closer_has_no_exposure_keys():
    world = _world(positions=[_lot(800)])
    snap = {"account": {"netliquidation": 100000}, "positions": [_lot(800)]}
    out = preview_ticket(_closer(), world=world, snap=snap)
    _assert_no_cap_keys(out)
    assert not any("portfolio_usd" in str(r) for r in (out.get("would_refuse") or []))


@pytest.mark.asyncio
async def test_high_aggregate_exposure_place_reaches_execute(monkeypatch):
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
    _assert_no_cap_keys(result)


@pytest.mark.asyncio
async def test_closer_place_reaches_execute(monkeypatch):
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
    _assert_no_cap_keys(result)


def test_live_check_uses_fixture_book():
    ticket = _vertical(300)
    ticket["_live_positions"] = [_lot(400)]
    ticket["_open_orders"] = [_working(200)]
    check = live_portfolio_usd_check(ticket)
    assert check["allow"] is True
    assert check["portfolio_max_loss_usd"] == pytest.approx(900.0)


def _spy_call_vertical_legs(*, qty: int = 6) -> list[dict]:
    return [
        {
            "symbol": "SPY",
            "secType": "OPT",
            "right": "C",
            "expiration": "20260925",
            "strike": 755.0,
            "quantity": qty,
            "avg_cost": 850.0,
        },
        {
            "symbol": "SPY",
            "secType": "OPT",
            "right": "C",
            "expiration": "20260925",
            "strike": 765.0,
            "quantity": -qty,
            "avg_cost": 420.0,
        },
    ]


def test_opt_legs_pair_into_vertical_max_loss():
    legs = _spy_call_vertical_legs(qty=6)
    check = portfolio_usd_check(
        open_lots=legs,
        working=[],
        candidate=_vertical(
            3100.0,
            symbol="NOK",
            long_strike=10.0,
            short_strike=10.5,
            quantity=100,
            limit_price=0.19,
        ),
    )
    assert check["open_usd"] == pytest.approx(2580.0)
    assert check["candidate_usd"] == pytest.approx(3100.0)
    assert check["portfolio_max_loss_usd"] == pytest.approx(5680.0)
    assert check["allow"] is True


def test_bag_working_order_without_strikes_does_not_null_total():
    legs = _spy_call_vertical_legs(qty=6)
    bag_entry = {
        "order_id": 20034,
        "symbol": "SPY",
        "secType": "BAG",
        "order_type": "LMT",
        "action": "SELL",
        "quantity": 6,
        "lmt_price": 5.24,
        "comboLegs": [{"conId": 1, "ratio": 1}, {"conId": 2, "ratio": 1}],
    }
    check = portfolio_usd_check(
        open_lots=legs,
        working=[bag_entry],
        candidate=_vertical(
            3100.0,
            symbol="NOK",
            long_strike=10.0,
            short_strike=10.5,
            quantity=100,
            limit_price=0.19,
        ),
    )
    assert check["portfolio_max_loss_usd"] == pytest.approx(5680.0)
    assert check["unreadable"] is True


def test_bag_row_with_strike_geometry_counts():
    bag = {
        "symbol": "SPY",
        "secType": "BAG",
        "quantity": 2,
        "long_strike": 755.0,
        "short_strike": 765.0,
        "limit_price": 4.5,
    }
    loss = defined_max_loss_usd(bag)
    assert loss == pytest.approx(1100.0)
