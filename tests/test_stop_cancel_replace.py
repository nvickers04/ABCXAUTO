"""Cancelled last-stop must be replaced; a resting LMT is not protection.

IBKR can cancel a working STP (10326 / ApiCancelled) while the STK lot and its
LMT target remain. The fill-driven reconciler used to ignore every status
except Filled, so the book stayed naked until the next poll — or longer.

Process start must also cover an already-cancelled stop when a finite price is
on record (journal / last dispatch). No price → log unprotected; never invent.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from abcxauto.protect_reconciler import ProtectionReconciler


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
        self.calls: list[tuple[str, object]] = []
        self.listeners: list = []
        self._next_id = next_id
        self._bracket_groups: dict = {}

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

    async def place_stop_order(
        self, symbol: str, action: str, quantity: int, stop_price: float
    ):
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
        oid = self._next_id
        self._next_id += 1
        row = _order(
            oid, symbol, action, quantity, "STP", aux_price=float(stop_price)
        )
        self.open_orders.append(row)
        return {"success": True, "order_id": oid, "symbol": symbol}

    async def place_market_order(self, *args, **kwargs):
        self.calls.append(("place_market_order", {"args": args, "kwargs": kwargs}))
        return {"success": True, "order_id": self._next_id}


def _place_stops(gateway: BookGateway) -> list[dict]:
    return [kw for name, kw in gateway.calls if name == "place_stop_order"]


def _cancel_ids(gateway: BookGateway) -> list[int]:
    return [kw["order_id"] for name, kw in gateway.calls if name == "cancel_order"]


async def _drain(reconciler: ProtectionReconciler) -> None:
    for _ in range(50):
        if (
            not reconciler._pending
            and not reconciler._pending_replace
            and not reconciler._startup_cover_pending
            and not reconciler._tasks
        ):
            break
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _no_journal_startup_price(monkeypatch):
    """Cancel-path tests must not pick up a live journal stop for the symbol."""
    monkeypatch.setattr(
        "abcxauto.protect_reconciler.known_stop_price_for_symbol",
        lambda symbol, connector=None: None,
    )


class TestStopCancelReplace:
    @pytest.mark.asyncio
    async def test_cancelled_stp_places_one_bare_gtc_stop(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(50, "AAPL", "SELL", 10, "LMT", limit_price=200.0)],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        reconciler._on_order_status(
            {
                "status": "Cancelled",
                "symbol": "AAPL",
                "order_type": "STP",
                "action": "SELL",
                "order_id": 49,
                "aux_price": 175.5,
            }
        )
        await _drain(reconciler)
        placed = _place_stops(gateway)
        assert len(placed) == 1
        assert placed[0] == {
            "symbol": "AAPL",
            "action": "SELL",
            "quantity": 10,
            "stop_price": 175.5,
        }
        assert "ocaGroup" not in placed[0]
        assert "oca_group" not in placed[0]
        assert _cancel_ids(gateway) == []
        assert not any(name == "place_market_order" for name, _ in gateway.calls)
        # Target LMT left working.
        assert any(int(o["order_id"]) == 50 for o in gateway.open_orders)
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_apicancelled_trail_replaces_for_held_qty(self):
        gateway = BookGateway(
            positions=[_stk("NVDA", 40)],
            open_orders=[],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        reconciler._on_order_status(
            {
                "status": "ApiCancelled",
                "symbol": "NVDA",
                "order_type": "TRAIL",
                "action": "SELL",
                "order_id": 10326,
                "stop_price": 110.0,
            }
        )
        await _drain(reconciler)
        placed = _place_stops(gateway)
        assert len(placed) == 1
        assert placed[0]["quantity"] == 40
        assert placed[0]["stop_price"] == 110.0
        assert placed[0]["action"] == "SELL"
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_resting_lmt_alone_is_not_cover(self):
        """LMT target remains after STP cancel — still needs a stop."""
        gateway = BookGateway(
            positions=[_stk("TSLA", -5)],
            open_orders=[_order(7, "TSLA", "BUY", 5, "LMT", limit_price=200.0)],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        reconciler._on_order_status(
            {
                "status": "cancelled",
                "symbol": "TSLA",
                "order_type": "STP LMT",
                "action": "BUY",
                "order_id": 6,
                "aux_price": 250.0,
            }
        )
        await _drain(reconciler)
        placed = _place_stops(gateway)
        assert len(placed) == 1
        assert placed[0]["action"] == "BUY"
        assert placed[0]["quantity"] == 5
        assert int(gateway.open_orders[0]["order_id"]) == 7  # LMT untouched
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_does_not_double_place_when_another_stop_covers(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(2, "AAPL", "SELL", 10, "STP", aux_price=170.0)],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        reconciler._on_order_status(
            {
                "status": "Cancelled",
                "symbol": "AAPL",
                "order_type": "STP",
                "action": "SELL",
                "order_id": 1,
                "aux_price": 175.0,
            }
        )
        await _drain(reconciler)
        assert _place_stops(gateway) == []
        assert reconciler.last_unprotected == []
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_cancelled_lmt_does_not_place_a_stop(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(1, "AAPL", "SELL", 10, "STP", aux_price=175.0)],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        reconciler._on_order_status(
            {
                "status": "Cancelled",
                "symbol": "AAPL",
                "order_type": "LMT",
                "action": "SELL",
                "order_id": 2,
                "limit_price": 200.0,
            }
        )
        await _drain(reconciler)
        assert _place_stops(gateway) == []
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_no_price_logs_uncovered_and_does_not_invent(self, caplog):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(50, "AAPL", "SELL", 10, "LMT", limit_price=200.0)],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        with caplog.at_level(logging.WARNING):
            reconciler._on_order_status(
                {
                    "status": "Cancelled",
                    "symbol": "AAPL",
                    "order_type": "STP",
                    "action": "SELL",
                    "order_id": 49,
                }
            )
            await _drain(reconciler)
        assert _place_stops(gateway) == []
        assert reconciler.last_unprotected == ["AAPL"]
        assert any(
            "last-stop does not cover lot qty on AAPL" in r.message for r in caplog.records
        )
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_non_positive_price_is_not_invented(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        for bad in (0, -5.0, float("nan"), float("inf")):
            gateway.calls.clear()
            reconciler.last_unprotected = []
            reconciler._on_order_status(
                {
                    "status": "Cancelled",
                    "symbol": "AAPL",
                    "order_type": "STP",
                    "action": "SELL",
                    "order_id": 49,
                    "aux_price": bad,
                }
            )
            await _drain(reconciler)
            assert _place_stops(gateway) == []
            assert reconciler.last_unprotected == ["AAPL"]
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_flat_book_after_cancel_places_nothing(self):
        gateway = BookGateway(positions=[], open_orders=[])
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        reconciler._on_order_status(
            {
                "status": "Cancelled",
                "symbol": "AAPL",
                "order_type": "STP",
                "action": "SELL",
                "order_id": 49,
                "aux_price": 175.0,
            }
        )
        await _drain(reconciler)
        assert _place_stops(gateway) == []
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_submitted_still_ignored(self):
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        gateway.calls.clear()
        reconciler._on_order_status(
            {
                "status": "Submitted",
                "symbol": "AAPL",
                "order_type": "STP",
                "action": "SELL",
                "order_id": 49,
                "aux_price": 175.0,
            }
        )
        await asyncio.sleep(0.02)
        assert _place_stops(gateway) == []
        assert reconciler._pending_replace == set()
        reconciler.stop()


class TestStartupCover:
    @pytest.mark.asyncio
    async def test_startup_places_bare_stop_at_known_aux(self, monkeypatch):
        gateway = BookGateway(
            positions=[_stk("AVGO", 87)],
            open_orders=[_order(50, "AVGO", "SELL", 87, "LMT", limit_price=400.0)],
        )
        monkeypatch.setattr(
            "abcxauto.protect_reconciler.known_stop_price_for_symbol",
            lambda symbol, connector=None: 356.21 if symbol == "AVGO" else None,
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        placed = _place_stops(gateway)
        assert len(placed) == 1
        assert placed[0] == {
            "symbol": "AVGO",
            "action": "SELL",
            "quantity": 87,
            "stop_price": 356.21,
        }
        assert "ocaGroup" not in placed[0]
        assert not any(name == "place_market_order" for name, _ in gateway.calls)
        assert reconciler.last_unprotected == []
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_startup_no_price_logs_unprotected_does_not_invent(self, caplog):
        gateway = BookGateway(
            positions=[_stk("AVGO", 87)],
            open_orders=[],
        )
        # autouse fixture already returns None for known_stop_price_for_symbol
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        with caplog.at_level(logging.WARNING):
            reconciler.start()
            await _drain(reconciler)
        assert _place_stops(gateway) == []
        assert reconciler.last_unprotected == ["AVGO"]
        assert any(
            "last-stop does not cover lot qty on AVGO at startup" in r.message
            for r in caplog.records
        )
        assert not any(name == "place_market_order" for name, _ in gateway.calls)
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_startup_uses_in_process_last_dispatch_stop(self, monkeypatch):
        """orders.last_known_stop_price (bracket group) beats an empty journal stub."""
        from types import SimpleNamespace

        from abcxauto.broker.orders import last_known_stop_price

        gateway = BookGateway(
            positions=[_stk("AVGO", 10)],
            open_orders=[],
        )
        gateway._bracket_groups = {
            "BRK_AVGO_1": SimpleNamespace(symbol="AVGO", stop_price=356.21),
        }
        assert last_known_stop_price(gateway, "AVGO") == 356.21

        def _known(symbol, connector=None):
            from abcxauto.broker.orders import last_known_stop_price as _lk

            if connector is not None:
                px = _lk(connector, symbol)
                if px is not None:
                    return px
            return None

        monkeypatch.setattr(
            "abcxauto.protect_reconciler.known_stop_price_for_symbol",
            _known,
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        await _drain(reconciler)
        placed = _place_stops(gateway)
        assert len(placed) == 1
        assert placed[0]["stop_price"] == 356.21
        assert placed[0]["quantity"] == 10
        reconciler.stop()
