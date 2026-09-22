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

    def __init__(self, positions=None, open_orders=None, next_id: int = 900):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.calls: list[tuple[str, dict]] = []
        self.listeners: list = []
        self._next_id = int(next_id)

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

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def cancel_order(self, order_id: int):
        self.calls.append(("cancel_order", {"order_id": int(order_id)}))
        self.open_orders = [
            o for o in self.open_orders if int(o["order_id"]) != int(order_id)
        ]
        return {"success": True, "order_id": int(order_id)}

    async def place_oca(self, **kwargs):
        self.calls.append(("place_oca", kwargs))
        symbol = str(kwargs["symbol"]).upper()
        qty = int(kwargs["quantity"])
        direction = str(kwargs.get("direction") or "LONG").upper()
        exit_action = "SELL" if direction == "LONG" else "BUY"
        group = f"OCA_{symbol}_{self._next_id}"
        stop_id, target_id = self._new_id(), self._new_id()
        self.open_orders.append(
            _order(
                stop_id,
                symbol,
                exit_action,
                qty,
                "STP",
                oca_group=group,
                aux_price=float(kwargs["stop_price"]),
            )
        )
        self.open_orders.append(
            _order(
                target_id,
                symbol,
                exit_action,
                qty,
                "LMT",
                oca_group=group,
                lmt_price=float(kwargs["target_price"]),
            )
        )
        return {
            "success": True,
            "stop_order_id": stop_id,
            "target_order_id": target_id,
            "oca_group": group,
        }

    async def place_stop_order(self, symbol, action, quantity, stop_price):
        self.calls.append(
            (
                "place_stop_order",
                {
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "stop_price": stop_price,
                },
            )
        )
        oid = self._new_id()
        self.open_orders.append(
            _order(
                oid,
                str(symbol).upper(),
                str(action).upper(),
                int(quantity),
                "STP",
                aux_price=float(stop_price),
            )
        )
        return {"success": True, "order_id": oid}

    async def place_market_order(self, **kwargs):
        self.calls.append(("place_market_order", kwargs))
        return {"success": True, "order_id": self._new_id(), "filled": True}

    async def place_limit_order(self, **kwargs):
        self.calls.append(("place_limit_order", kwargs))
        return {"success": True, "order_id": self._new_id()}


def _cancel_ids(gateway: BookGateway) -> list[int]:
    return [kw["order_id"] for name, kw in gateway.calls if name == "cancel_order"]


def _call_names(gateway: BookGateway) -> list[str]:
    return [name for name, _kw in gateway.calls]


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


class TestResizeOversizedExitsAfterTrim:
    """Partial long sale must shrink working stop/target to remaining shares."""

    @pytest.fixture(autouse=True)
    def _disable_risk_gates(self, monkeypatch):
        from abcxauto.config import Config, get_config

        base = get_config()
        monkeypatch.setattr(
            "abcxauto.executor.get_config",
            lambda: Config(
                **{
                    **base.__dict__,
                    "risk_gates_enabled": False,
                    "defined_risk_only": False,
                    "cash_only": False,
                    "daily_loss_limit_pct": 0,
                }
            ),
        )
        monkeypatch.setattr(
            "abcxauto.proposals.get_config",
            lambda: Config(
                **{
                    **base.__dict__,
                    "defined_risk_only": False,
                    "risk_posture": "balanced",
                }
            ),
        )

    @pytest.mark.asyncio
    async def test_long_22_with_stop_target_89_resizes_down_not_a_second_sell(self):
        """AVGO-shaped book: held 22, working SELL 89 STP + SELL 89 LMT."""
        gateway = BookGateway(
            positions=[_stk("AVGO", 22, conId=313130367)],
            open_orders=[
                _order(
                    100,
                    "AVGO",
                    "SELL",
                    89,
                    "STP",
                    oca_group="OCA_AVGO_1",
                    aux_price=350.0,
                ),
                _order(
                    101,
                    "AVGO",
                    "SELL",
                    89,
                    "LMT",
                    oca_group="OCA_AVGO_1",
                    lmt_price=380.0,
                ),
            ],
            next_id=200,
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        await reconciler.sweep_now("AVGO")

        oca_calls = [kw for name, kw in gateway.calls if name == "place_oca"]
        assert len(oca_calls) == 1
        assert oca_calls[0]["quantity"] == 22
        assert oca_calls[0]["direction"] == "LONG"
        assert oca_calls[0]["stop_price"] == 350.0
        assert oca_calls[0]["target_price"] == 380.0

        # Replace-on-place: old 89 legs cancelled only after the new oca landed.
        assert sorted(_cancel_ids(gateway)) == [100, 101]
        assert "place_market_order" not in _call_names(gateway)
        assert "place_limit_order" not in _call_names(gateway)

        working = [
            o
            for o in gateway.open_orders
            if str(o.get("symbol")).upper() == "AVGO"
        ]
        assert len(working) == 2
        assert {o["order_type"] for o in working} == {"STP", "LMT"}
        assert all(int(o["quantity"]) == 22 for o in working)
        assert all(o["action"] == "SELL" for o in working)
        # Remainder still long — never flattened.
        assert gateway.positions == [_stk("AVGO", 22, conId=313130367)]
        assert reconciler.last_unprotected == []
