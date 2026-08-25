"""Last-stop / covering exits must cover lot qty on the exit side.

The fill-driven reconciler used to treat any same-symbol stop as a cover.
A 1-share STP on a 10-share lot is not protected, and a BUY stop on a long
is the wrong side. IBKR fill aliases BOT/SLD are BUY/SELL — a BOT fill must
not sweep SELL-side protection of a long that has not landed in the ledger.
"""

from __future__ import annotations

import asyncio

import pytest

from abcxauto.protect_reconciler import (
    ProtectionReconciler,
    last_stop_covers_lot,
    uncovered_stk_symbols,
)


def _stk(symbol: str, qty: int, **extra) -> dict:
    row = {"symbol": symbol, "quantity": qty, "sec_type": "STK"}
    row.update(extra)
    return row


def _order(oid: int, symbol: str, action: str, qty: int, otype: str, **extra) -> dict:
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


class BookGateway:
    connected = True

    def __init__(self, positions=None, open_orders=None):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.calls: list[tuple[str, dict]] = []
        self.listeners: list = []

    async def get_positions(self):
        return [dict(p) for p in self.positions]

    async def get_open_orders(self):
        return [dict(o) for o in self.open_orders]

    async def get_account_summary(self):
        return {"netliquidation": 100_000.0, "dailypnl": 0.0}

    def register_order_status_listener(self, cb):
        self.listeners.append(cb)

    def unregister_order_status_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    async def cancel_order(self, order_id: int):
        self.calls.append(("cancel_order", {"order_id": int(order_id)}))
        self.open_orders = [
            o for o in self.open_orders if int(o["order_id"]) != int(order_id)
        ]
        return {"success": True, "order_id": int(order_id)}


def _cancel_ids(gateway: BookGateway) -> list[int]:
    return [kw["order_id"] for name, kw in gateway.calls if name == "cancel_order"]


async def _drain(reconciler: ProtectionReconciler) -> None:
    for _ in range(50):
        if not reconciler._pending:
            break
        await asyncio.sleep(0.01)


class TestLastStopCoversLotQty:
    def test_one_share_stop_does_not_cover_ten_share_lot(self):
        lot = _stk("AAPL", 10)
        stop = _order(1, "AAPL", "SELL", 1, "STP")
        assert last_stop_covers_lot(lot, [stop]) is False
        assert uncovered_stk_symbols([lot], [stop]) == ["AAPL"]

    def test_covering_sell_stop_protects_a_long(self):
        lot = _stk("AAPL", 10)
        stop = _order(1, "AAPL", "SELL", 10, "STP")
        assert last_stop_covers_lot(lot, [stop]) is True
        assert uncovered_stk_symbols([lot], [stop]) == []

    def test_buy_stop_on_a_long_is_wrong_side(self):
        lot = _stk("AAPL", 10)
        stop = _order(1, "AAPL", "BUY", 10, "STP")
        assert last_stop_covers_lot(lot, [stop]) is False
        assert uncovered_stk_symbols([lot], [stop]) == ["AAPL"]

    def test_sell_stop_on_a_short_is_wrong_side(self):
        lot = _stk("TSLA", -10)
        stop = _order(1, "TSLA", "SELL", 10, "STP")
        assert last_stop_covers_lot(lot, [stop]) is False

    def test_covering_buy_stop_protects_a_short(self):
        lot = _stk("TSLA", -10)
        stop = _order(1, "TSLA", "BUY", 10, "STP")
        assert last_stop_covers_lot(lot, [stop]) is True

    def test_oversized_stop_still_covers(self):
        assert last_stop_covers_lot(
            _stk("NVDA", 15), [_order(1, "NVDA", "SELL", 40, "TRAIL")]
        ) is True

    def test_crumb_slack_matches_the_book(self):
        lot = _stk("AAPL", 10)
        assert last_stop_covers_lot(lot, [_order(1, "AAPL", "SELL", 9.49, "STP")]) is True
        assert last_stop_covers_lot(lot, [_order(1, "AAPL", "SELL", 9.48, "STP")]) is False

    def test_option_lot_is_not_a_last_stop_question(self):
        opt = {
            "symbol": "NVDA", "quantity": 2, "sec_type": "OPT",
            "strike": 120.0, "right": "C", "expiration": "20260918",
        }
        assert last_stop_covers_lot(opt, [_order(1, "NVDA", "SELL", 2, "STP")]) is False
        assert uncovered_stk_symbols([opt], [_order(1, "NVDA", "SELL", 2, "STP")]) == []


class TestSweepReconcilesCoverQty:
    @pytest.mark.asyncio
    async def test_undersized_stop_is_not_reconciled_as_protected(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(9, "AAPL", "SELL", 1, "STP")],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        cancelled = await reconciler.sweep_now("AAPL")
        assert cancelled == []
        assert _cancel_ids(gateway) == []
        assert reconciler.last_unprotected == ["AAPL"]

    @pytest.mark.asyncio
    async def test_covering_stop_is_reconciled_as_protected(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(9, "AAPL", "SELL", 10, "STP")],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        await reconciler.sweep_now("AAPL")
        assert reconciler.last_unprotected == []
        assert _cancel_ids(gateway) == []

    @pytest.mark.asyncio
    async def test_wrong_side_stop_is_not_reconciled_as_protected(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(9, "AAPL", "BUY", 10, "STP")],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        await reconciler.sweep_now("AAPL")
        assert reconciler.last_unprotected == ["AAPL"]
        assert _cancel_ids(gateway) == []


class TestFillSideAliases:
    @pytest.mark.asyncio
    async def test_bot_fill_cannot_touch_sell_side_protection(self):
        """BOT is a BUY fill. The new long may still be missing from the ledger."""
        gateway = BookGateway(
            positions=[],
            open_orders=[
                _order(4279, "NVDA", "SELL", 40, "STP", oca_group="OCA_NVDA_2"),
                _order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_NVDA_2"),
            ],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        reconciler._on_order_status(
            {"status": "Filled", "symbol": "NVDA", "side": "BOT", "order_id": 4278}
        )
        await _drain(reconciler)
        assert _cancel_ids(gateway) == []
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_sld_fill_sweeps_the_sell_side_it_orphaned(self):
        gateway = BookGateway(
            positions=[],
            open_orders=[
                _order(4279, "NVDA", "SELL", 40, "STP", oca_group="OCA_NVDA_1"),
                _order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_NVDA_1"),
            ],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        reconciler._on_order_status(
            {"status": "Filled", "symbol": "NVDA", "side": "SLD", "order_id": 4278}
        )
        await _drain(reconciler)
        assert sorted(_cancel_ids(gateway)) == [4279, 4280]
        reconciler.stop()
