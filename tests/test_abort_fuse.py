"""KEEP-2: abort fuse cancels resting non-exit working. Mock broker. No 7496."""

from __future__ import annotations

import pytest

from abcxauto.abort_fuse import (
    ACTION_FUSES,
    apply_abort_fuse_cancel,
    maybe_apply_abort_fuse,
    non_exit_working_ids,
    reset_abort_fuse_for_tests,
)
from abcxauto.config import get_config
from abcxauto.pcs_kill_scorecard import window_aggregates
from abcxauto.thin_rth_kill_look import (
    F10_HARD_USD,
    F10_PREFERRED_USD,
    MODE_ABORT,
    MODE_MANAGE,
    MODE_OPEN,
    PCS_CARD,
    REASON_ALLOWLIST,
    REASON_DD,
    REASON_ENTRY_BUDGET,
    REASON_F10,
    REASON_QTY0,
    kill_look_port_ok,
    kill_look_send_block,
    kill_mode,
    skip_look_reason,
)
from tests.test_pcs_kill_scorecard import _base_payload
from tests.test_thin_rth_kill_look import PCS_OPEN, _allow_f10, _kill_on, _pcs_lot


class FakeBroker:
    """Records cancel_order. Never flatten."""

    def __init__(self, orders=None, positions=None, *, fail=False, missing_cancel=False):
        self.orders = [dict(o) for o in (orders or [])]
        self.positions = [dict(p) for p in (positions or [])]
        self.cancelled: list[int] = []
        self.fail = fail
        self.flatten_calls = 0
        if missing_cancel:
            self.cancel_order = None  # type: ignore[assignment]

    async def get_open_orders(self):
        return [dict(o) for o in self.orders]

    async def get_positions(self):
        return [dict(p) for p in self.positions]

    async def cancel_order(self, order_id=None, **kwargs):
        oid = int(order_id if order_id is not None else kwargs.get("order_id"))
        if self.fail:
            return {"error": "tws busy"}
        self.cancelled.append(oid)
        self.orders = [o for o in self.orders if int(o.get("order_id") or 0) != oid]
        return {"success": True, "order_id": oid}

    async def flatten_all(self):
        self.flatten_calls += 1
        return {"success": True}


def _entry(oid: int = 11, symbol: str = "SPY") -> dict:
    return {
        "order_id": oid,
        "symbol": symbol,
        "sec_type": "STK",
        "action": "BUY",
        "quantity": 10,
        "order_type": "LMT",
        "lmt_price": 500.0,
    }


def _last_stop(oid: int = 22, symbol: str = "AAPL", qty: int = 10) -> dict:
    return {
        "order_id": oid,
        "symbol": symbol,
        "sec_type": "STK",
        "action": "SELL",
        "quantity": qty,
        "order_type": "STP",
        "aux_price": 90.0,
    }


def _stk(symbol: str = "AAPL", qty: int = 10) -> dict:
    return {"symbol": symbol, "quantity": qty, "sec_type": "STK"}


def _f10_hard() -> dict:
    return {
        "allow_new_risk": False,
        "preferred_trip": True,
        "reason_code": REASON_F10,
        "note": "hard",
        "projected": 2.1,
    }


def test_hygiene_does_not_soften_f10_or_enable_live():
    assert F10_HARD_USD == 2.0
    assert F10_PREFERRED_USD == 1.0
    assert get_config().ibkr_port != 7496
    assert kill_look_port_ok() is True
    assert ACTION_FUSES == frozenset({"F10", "DD30", "QTY0_STREAK"})


def test_non_exit_ids_keep_last_stop_cancel_entry():
    orders = [_entry(11), _last_stop(22)]
    positions = [_stk()]
    ids = non_exit_working_ids(orders, positions)
    assert 11 in ids
    assert 22 not in ids


@pytest.mark.parametrize("fuse", ["F10", "DD30", "QTY0_STREAK"])
@pytest.mark.asyncio
async def test_abort_fuse_cancels_entry_keeps_last_stop(fuse):
    broker = FakeBroker(
        orders=[_entry(11), _last_stop(22)],
        positions=[_stk()],
    )
    out = await apply_abort_fuse_cancel(broker, fuse=fuse)
    assert out["cancel_all_invoked"] is True
    assert out["working_orders_after_abort"] == 0
    assert broker.cancelled == [11]
    assert broker.flatten_calls == 0
    assert any(int(o["order_id"]) == 22 for o in broker.orders)
    win = window_aggregates(
        [
            _base_payload(
                session_id="pcs-v0-20260906-01",
                f10_ok=fuse != "F10",
                dd_pct=30.0 if fuse == "DD30" else 1.0,
                qty=0 if fuse == "QTY0_STREAK" else 1,
                send="NO_SEND",
                send_reason="NO_SEND:f10"
                if fuse == "F10"
                else ("NO_SEND:dd_fuse" if fuse == "DD30" else "NO_SEND:qty0_streak"),
            )
        ]
        if fuse != "QTY0_STREAK"
        else [
            _base_payload(
                session_id=f"pcs-v0-20260906-{i:02d}",
                qty=0,
                send="NO_SEND",
                send_reason="NO_SEND:qty0_streak",
            )
            for i in range(1, 6)
        ]
    )
    assert win["abort_fuse"] == fuse
    assert win["cancel_all_invoked"] is True
    assert win["working_orders_after_abort"] == 0


@pytest.mark.asyncio
async def test_fuse_none_does_not_cancel():
    broker = FakeBroker(orders=[_entry(11)])
    out = await maybe_apply_abort_fuse(broker, abort_fuse="none")
    assert out["cancel_all_invoked"] is False
    assert broker.cancelled == []
    assert out["working_orders_after_abort"] is None


@pytest.mark.asyncio
async def test_entry_budget_abort_does_not_cancel():
    """Spent look is MODE_ABORT but not a named scorecard fuse."""
    broker = FakeBroker(orders=[_entry(11)])
    out = await maybe_apply_abort_fuse(
        broker, abort_fuse="none", f10=_allow_f10()
    )
    assert out["cancel_all_invoked"] is False
    assert broker.cancelled == []


@pytest.mark.asyncio
async def test_live_f10_hard_cancels():
    broker = FakeBroker(orders=[_entry(11)])
    out = await maybe_apply_abort_fuse(broker, abort_fuse=None, f10=_f10_hard())
    assert out["abort_fuse"] == "F10"
    assert out["cancel_all_invoked"] is True
    assert out["working_orders_after_abort"] == 0
    assert broker.cancelled == [11]


@pytest.mark.asyncio
async def test_cancel_fail_logged_fail_closed():
    broker = FakeBroker(orders=[_entry(11)], fail=True)
    out = await apply_abort_fuse_cancel(broker, fuse="F10")
    assert out["cancel_all_invoked"] is True
    assert out["working_orders_after_abort"] == 1
    assert out["errors"]
    assert broker.orders[0]["order_id"] == 11


@pytest.mark.asyncio
async def test_unreadable_book_fail_closed():
    class Blind:
        async def get_open_orders(self):
            raise RuntimeError("disconnected")

    out = await apply_abort_fuse_cancel(Blind(), fuse="DD30")
    assert out["cancel_all_invoked"] is True
    assert out["working_orders_after_abort"] is None
    assert out["errors"]


@pytest.mark.pcs_kill_look
def test_scorecard_fuse_stops_non_exit_look_exits_still_manage(monkeypatch):
    _kill_on(monkeypatch)
    f10 = _allow_f10()
    assert (
        kill_mode("regular", positions=[], f10=f10, abort_fuse="DD30") == MODE_ABORT
    )
    assert skip_look_reason(
        "regular", positions=[], f10=f10, abort_fuse="DD30"
    ) == REASON_DD
    assert skip_look_reason(
        "regular", positions=[], f10=f10, abort_fuse="QTY0_STREAK"
    ) == REASON_QTY0
    assert skip_look_reason(
        "regular", positions=[], f10=f10, abort_fuse="F10"
    ) == REASON_F10
    # Open lot: MANAGE so exits still look.
    assert (
        kill_mode(
            "regular",
            positions=[_pcs_lot()],
            f10=f10,
            abort_fuse="DD30",
        )
        == MODE_MANAGE
    )
    assert (
        skip_look_reason(
            "regular",
            positions=[_pcs_lot()],
            f10=f10,
            abort_fuse="DD30",
        )
        == ""
    )
    # Unprotected last-stop still looks.
    assert (
        skip_look_reason(
            "regular",
            positions=[],
            f10=f10,
            abort_fuse="F10",
            unprotected=True,
        )
        == ""
    )
    assert (
        kill_mode("regular", positions=[], f10=f10, abort_fuse="none")
        == MODE_OPEN
    )


@pytest.mark.pcs_kill_look
def test_exits_never_blocked_on_abort_fuse(monkeypatch):
    _kill_on(monkeypatch)
    f10 = _allow_f10()
    close = {
        "strategy": "vertical_spread",
        "params": {**PCS_OPEN, "closing_position": True},
        "card": PCS_CARD,
    }
    assert (
        kill_look_send_block(
            close, session="regular", f10=f10, abort_fuse="F10"
        )
        is None
    )
    assert (
        kill_look_send_block(
            close, session="regular", f10=f10, abort_fuse="DD30"
        )
        is None
    )
    open_act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
        "card": PCS_CARD,
    }
    blocked = kill_look_send_block(
        open_act, session="regular", f10=f10, abort_fuse="DD30"
    )
    assert blocked is not None
    assert blocked["reason_code"] == REASON_DD
    blocked_f10 = kill_look_send_block(
        open_act, session="regular", f10=f10, abort_fuse="F10"
    )
    assert blocked_f10 is not None
    assert blocked_f10["reason_code"] == REASON_F10
    assert blocked_f10["reason_code"] != REASON_ALLOWLIST


@pytest.mark.asyncio
async def test_maybe_apply_does_not_flatten():
    broker = FakeBroker(orders=[_entry(7)])
    await maybe_apply_abort_fuse(broker, abort_fuse="F10")
    assert broker.flatten_calls == 0
    assert 7 in broker.cancelled
