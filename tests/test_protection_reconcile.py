"""Protection must not outlive the position it protects.

Two incidents drive this file.

2026-08-13 CSCO: ten ``trailing_stop`` sends stacked on one 50-share lot, one
trail filled and went flat, and a second trail then filled again and left the
account short 50 with no covering stop for ~17 seconds. Two defects — exits
that accumulate, and exits that keep working on a flat book.

2026-08-20 NVDA: the lot was closed by a separate ``market_order``, so the OCA
legs never saw a sibling fill and kept working on a flat NVDA for 57 seconds.
The stop happened to sit 1.2% away; that was luck, not design.
"""

from __future__ import annotations

import asyncio

import pytest

from abcxauto.executor import cancel_orphaned_protection, execute_proposal
from abcxauto.protect import (
    last_stop_block_reason,
    order_covers_open_lot,
    orphaned_protection_ids,
    orphaned_protection_rows,
    protective_role,
)
from abcxauto.proposals import validate_proposal

RATIONALE = "regression: protection must not outlive its position"


@pytest.fixture(autouse=True)
def _disable_risk_gates(monkeypatch):
    """Capital gates have their own suite; these cases are about the book."""
    from abcxauto.config import Config, get_config

    base = get_config()
    monkeypatch.setattr(
        "abcxauto.executor.get_config",
        lambda: Config(**{**base.__dict__, "risk_gates_enabled": False}),
    )
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: Config(
            **{**base.__dict__, "defined_risk_only": False, "risk_posture": "balanced"}
        ),
    )


def _stk(symbol: str, qty: int, con_id: int | None = None) -> dict:
    row = {"symbol": symbol, "quantity": qty, "sec_type": "STK"}
    if con_id is not None:
        row["conId"] = con_id
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
    """Fake IBKR that keeps a working-order book, so sequences are real."""

    connected = True

    def __init__(self, positions=None, open_orders=None, next_id: int = 900):
        self.positions = list(positions or [])
        self.open_orders = list(open_orders or [])
        self.calls: list[tuple[str, dict]] = []
        self._next_id = next_id

    # --- reads -------------------------------------------------------
    async def get_positions(self):
        return [dict(p) for p in self.positions]

    async def get_open_orders(self):
        return [dict(o) for o in self.open_orders]

    async def get_account_summary(self):
        return {"netliquidation": 100_000.0, "dailypnl": 0.0}

    # --- listener seam used by the reconciler ------------------------
    def register_order_status_listener(self, cb):
        self.listeners = getattr(self, "listeners", [])
        self.listeners.append(cb)

    def unregister_order_status_listener(self, cb):
        if cb in getattr(self, "listeners", []):
            self.listeners.remove(cb)

    # --- writes ------------------------------------------------------
    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def cancel_order(self, order_id: int):
        self.calls.append(("cancel_order", {"order_id": int(order_id)}))
        before = len(self.open_orders)
        self.open_orders = [
            o for o in self.open_orders if int(o["order_id"]) != int(order_id)
        ]
        if len(self.open_orders) == before:
            return {"error": f"Order {order_id} not found"}
        return {"success": True, "order_id": int(order_id)}

    async def place_trailing_stop(self, **kwargs):
        self.calls.append(("place_trailing_stop", kwargs))
        oid = self._new_id()
        direction = str(kwargs.get("direction") or "LONG").upper()
        self.open_orders.append(
            _order(
                oid,
                str(kwargs["symbol"]).upper(),
                "SELL" if direction == "LONG" else "BUY",
                int(kwargs["quantity"]),
                "TRAIL",
            )
        )
        return {"success": True, "order_id": oid}

    async def place_oca(self, **kwargs):
        self.calls.append(("place_oca", kwargs))
        symbol = str(kwargs["symbol"]).upper()
        qty = int(kwargs["quantity"])
        direction = str(kwargs.get("direction") or "LONG").upper()
        exit_action = "SELL" if direction == "LONG" else "BUY"
        group = f"OCA_{symbol}_{self._next_id}"
        stop_id, target_id = self._new_id(), self._new_id()
        self.open_orders.append(
            _order(stop_id, symbol, exit_action, qty, "STP", oca_group=group)
        )
        self.open_orders.append(
            _order(target_id, symbol, exit_action, qty, "LMT", oca_group=group)
        )
        return {
            "success": True,
            "stop_order_id": stop_id,
            "target_order_id": target_id,
            "oca_group": group,
        }

    async def place_market_order(self, **kwargs):
        """Fills immediately and flattens the lot — and touches no other order."""
        self.calls.append(("place_market_order", kwargs))
        symbol = str(kwargs["symbol"]).upper()
        qty = int(kwargs["quantity"])
        signed = -qty if str(kwargs.get("action")).upper() == "SELL" else qty
        for p in self.positions:
            if str(p.get("symbol")).upper() == symbol and str(
                p.get("sec_type", "STK")
            ).upper().startswith("STK"):
                p["quantity"] = int(p.get("quantity") or 0) + signed
        self.positions = [p for p in self.positions if int(p.get("quantity") or 0) != 0]
        return {"success": True, "order_id": self._new_id(), "filled": True}

    # --- helpers -----------------------------------------------------
    def fill(self, order_id: int) -> dict:
        """Simulate an exit order filling: lot shrinks, that order leaves."""
        order = next(
            o for o in self.open_orders if int(o["order_id"]) == int(order_id)
        )
        signed = -int(order["quantity"]) if order["action"] == "SELL" else int(
            order["quantity"]
        )
        symbol = str(order["symbol"]).upper()
        found = False
        for p in self.positions:
            if str(p.get("symbol")).upper() == symbol:
                p["quantity"] = int(p.get("quantity") or 0) + signed
                found = True
        if not found:
            self.positions.append(_stk(symbol, signed))
        self.positions = [p for p in self.positions if int(p.get("quantity") or 0) != 0]
        self.open_orders = [
            o for o in self.open_orders if int(o["order_id"]) != int(order_id)
        ]
        return {"status": "Filled", "symbol": symbol, "order_id": int(order_id)}

    def working_sells(self, symbol: str) -> list[int]:
        return [
            int(o["order_id"])
            for o in self.open_orders
            if str(o["symbol"]).upper() == symbol.upper() and o["action"] == "SELL"
        ]


def _cancel_ids(gateway: BookGateway) -> list[int]:
    return [kw["order_id"] for name, kw in gateway.calls if name == "cancel_order"]


# ---------------------------------------------------------------------------
# "Is this order still covering something?" — the test that must be safe
# ---------------------------------------------------------------------------


class TestStillCoveringIsConservative:
    def test_stop_on_a_live_lot_still_covers(self):
        order = _order(1, "CSCO", "SELL", 50, "STP")
        assert order_covers_open_lot(order, [_stk("CSCO", 50)]) is True
        assert orphaned_protection_ids([_stk("CSCO", 50)], [order]) == []

    def test_partial_close_leaves_protection_alone(self):
        """40-share stop over a 15-share remainder is oversized, not orphaned."""
        order = _order(1, "NVDA", "SELL", 40, "STP")
        assert orphaned_protection_ids([_stk("NVDA", 15)], [order]) == []

    def test_unreadable_book_covers_everything(self):
        order = _order(1, "CSCO", "SELL", 50, "STP")
        assert order_covers_open_lot(order, None) is True
        assert orphaned_protection_ids(None, [order]) == []

    def test_unidentifiable_lot_freezes_the_whole_sweep(self):
        """A lot we cannot fingerprint means we cannot prove anything is flat."""
        mystery = {"quantity": 3, "sec_type": "BAG", "conId": 77}
        order = _order(1, "CSCO", "SELL", 50, "STP")
        assert order_covers_open_lot(order, [mystery]) is True
        assert orphaned_protection_ids([mystery], [order]) == []

    def test_combo_order_is_never_swept(self):
        bag = {
            "order_id": 5, "symbol": "SPY", "sec_type": "BAG",
            "action": "SELL", "quantity": 1, "order_type": "LMT",
            "oca_group": "OCA_SPY_1",
        }
        assert order_covers_open_lot(bag, []) is True
        assert orphaned_protection_ids([], [bag]) == []

    def test_matching_conid_covers_even_when_symbols_disagree(self):
        order = _order(1, "CSCO", "SELL", 50, "STP", conId=4242)
        lot = {"symbol": "CSCO.OLD", "quantity": 50, "sec_type": "STK", "conId": 4242}
        assert order_covers_open_lot(order, [lot]) is True

    def test_option_lot_does_not_cover_a_stock_stop(self):
        """A stock stop with only options open would sell shares we do not own."""
        calls = {
            "symbol": "NVDA", "quantity": 2, "sec_type": "OPT",
            "strike": 120.0, "right": "C", "expiration": "20260918",
        }
        stock_stop = _order(1, "NVDA", "SELL", 40, "STP")
        assert order_covers_open_lot(stock_stop, [calls]) is False

    def test_matching_option_leg_covers_its_stop(self):
        calls = {
            "symbol": "NVDA", "quantity": 2, "sec_type": "OPT",
            "strike": 120.0, "right": "C", "expiration": "20260918",
        }
        leg_stop = {
            "order_id": 1, "symbol": "NVDA", "sec_type": "OPT", "action": "SELL",
            "quantity": 2, "order_type": "STP", "strike": 120.0, "right": "C",
            "expiration": "20260918",
        }
        assert order_covers_open_lot(leg_stop, [calls]) is True

    def test_child_of_a_working_parent_is_not_orphaned(self):
        """A hand-placed TWS bracket is flat until the entry fills."""
        entry = _order(700, "SPY", "BUY", 100, "LMT")
        child_stop = _order(701, "SPY", "SELL", 100, "STP", parent_id=700)
        child_target = _order(702, "SPY", "SELL", 100, "LMT", parent_id=700)
        assert orphaned_protection_ids([], [entry, child_stop, child_target]) == []

    def test_child_of_a_filled_parent_is_orphaned(self):
        """Parent gone from the book, position flat: nothing left to protect."""
        child_stop = _order(701, "SPY", "SELL", 100, "STP", parent_id=700)
        assert orphaned_protection_ids([], [child_stop]) == [701]

    def test_option_order_without_a_fingerprint_is_left_alone(self):
        vague = {
            "order_id": 1, "symbol": "NVDA", "sec_type": "OPT",
            "action": "SELL", "quantity": 2, "order_type": "STP",
        }
        assert order_covers_open_lot(vague, []) is True


class TestProtectiveShape:
    def test_stops_and_trails_are_protection(self):
        for otype in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT"):
            assert protective_role(_order(1, "SPY", "SELL", 10, otype)) == "stop"

    def test_unattached_limit_is_not_protection(self):
        """A resting LMT with no lineage may be an entry — never swept."""
        lonely = _order(1, "SPY", "SELL", 10, "LMT")
        assert protective_role(lonely) == ""
        assert orphaned_protection_ids([], [lonely]) == []

    def test_oca_or_bracket_limit_is_protection(self):
        assert protective_role(
            _order(1, "SPY", "SELL", 10, "LMT", oca_group="OCA_SPY_1")
        ) == "bracket_leg"
        assert protective_role(
            _order(2, "SPY", "SELL", 10, "LMT", parent_id=41)
        ) == "bracket_leg"

    def test_market_order_is_not_protection(self):
        assert protective_role(_order(1, "SPY", "SELL", 10, "MKT")) == ""


class TestLastStopRuleIsShared:
    def test_only_stop_on_a_live_lot_is_blocked(self):
        orders = [_order(9, "AAPL", "SELL", 10, "STP")]
        reason = last_stop_block_reason(9, orders, [_stk("AAPL", 10)])
        assert reason and "only working stop" in reason

    def test_redundant_stop_is_allowed(self):
        orders = [
            _order(9, "AAPL", "SELL", 10, "STP"),
            _order(10, "AAPL", "SELL", 10, "TRAIL"),
        ]
        assert last_stop_block_reason(9, orders, [_stk("AAPL", 10)]) is None

    def test_flat_lot_releases_the_rule(self):
        orders = [_order(9, "AAPL", "SELL", 10, "STP")]
        assert last_stop_block_reason(9, orders, []) is None

    def test_unknown_order_is_the_gateways_problem(self):
        assert last_stop_block_reason(404, [], [_stk("AAPL", 10)]) is None


# ---------------------------------------------------------------------------
# Incident 1 — 2026-08-13 CSCO
# ---------------------------------------------------------------------------


class TestCscoStacking:
    """Repeated trailing_stop on one lot must not accumulate SELL orders."""

    def _csco_book(self) -> BookGateway:
        return BookGateway(
            positions=[_stk("CSCO", 50, con_id=4242)],
            open_orders=[
                _order(205, "CSCO", "SELL", 50, "STP", oca_group="BRK_CSCO", parent_id=204),
                _order(206, "CSCO", "SELL", 50, "LMT", oca_group="BRK_CSCO", parent_id=204),
            ],
        )

    @pytest.mark.asyncio
    async def test_ten_trailing_stops_leave_one_working_stop(self):
        gateway = self._csco_book()
        payload = {
            "symbol": "CSCO", "quantity": 50, "direction": "LONG",
            "trail_percent": 2.0,
        }
        for _ in range(10):
            proposal = validate_proposal("trailing_stop", payload, RATIONALE)
            result = await execute_proposal(proposal, gateway)
            assert result["success"] is True
            stops = [
                o for o in gateway.open_orders
                if o["order_type"] in ("STP", "TRAIL")
            ]
            assert len(stops) == 1, gateway.open_orders

        # The bracket take-profit is Grok's geometry: a trail does not replace it.
        assert sorted(gateway.working_sells("CSCO")) == [206, gateway._next_id]

    @pytest.mark.asyncio
    async def test_new_oca_replaces_the_whole_prior_structure(self):
        gateway = self._csco_book()
        payload = {
            "symbol": "CSCO", "quantity": 50, "direction": "LONG",
            "stop_price": 108.0, "target_price": 118.0, "price_hint": 113.0,
        }
        proposal = validate_proposal("oca", payload, RATIONALE)
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        assert sorted(_cancel_ids(gateway)) == [205, 206]
        assert sorted(gateway.working_sells("CSCO")) == [
            result["stop_order_id"], result["target_order_id"]
        ]

    def test_flat_book_with_stale_sells_is_all_orphan(self):
        """15:56:46: no CSCO lot, eleven SELL-50 orders still working."""
        orders = [
            _order(205, "CSCO", "SELL", 50, "STP", oca_group="BRK_CSCO"),
            _order(206, "CSCO", "SELL", 50, "LMT", oca_group="BRK_CSCO"),
        ] + [
            _order(oid, "CSCO", "SELL", 50, "TRAIL")
            for oid in (214, 230, 238, 244, 250, 256, 258, 260, 262)
        ]
        rows = orphaned_protection_rows([], orders)
        assert [r["order_id"] for r in rows] == [o["order_id"] for o in orders]
        assert all(r["symbol"] == "CSCO" for r in rows)

    @pytest.mark.asyncio
    async def test_trail_fill_leaves_nothing_working_on_a_flat_book(self):
        """The whole incident: stack, fill, then no SELL may survive flat."""
        gateway = self._csco_book()
        payload = {
            "symbol": "CSCO", "quantity": 50, "direction": "LONG",
            "trail_percent": 2.0,
        }
        for _ in range(10):
            proposal = validate_proposal("trailing_stop", payload, RATIONALE)
            await execute_proposal(proposal, gateway)

        trail_id = max(gateway.working_sells("CSCO"))
        gateway.fill(trail_id)
        assert gateway.positions == []

        cancelled = await cancel_orphaned_protection(gateway)
        assert cancelled == [206]
        assert gateway.working_sells("CSCO") == []


# ---------------------------------------------------------------------------
# Incident 2 — 2026-08-20 NVDA
# ---------------------------------------------------------------------------


class TestNvdaOrphanedLegs:
    def _nvda_book(self) -> BookGateway:
        return BookGateway(
            positions=[_stk("NVDA", 40, con_id=4391)],
            open_orders=[
                _order(4279, "NVDA", "SELL", 40, "STP", oca_group="OCA_NVDA_1"),
                _order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_NVDA_1"),
            ],
        )

    @pytest.mark.asyncio
    async def test_market_order_close_leaves_no_working_legs(self):
        gateway = self._nvda_book()
        proposal = validate_proposal(
            "market_order",
            {"symbol": "NVDA", "action": "SELL", "quantity": 40,
             "closing_position": True},
            RATIONALE,
        )
        result = await execute_proposal(proposal, gateway)
        assert result["success"] is True
        # The exit was not an OCA leg, so IBKR cancels nothing.
        assert gateway.working_sells("NVDA") == [4279, 4280]

        cancelled = await cancel_orphaned_protection(gateway)
        assert sorted(cancelled) == [4279, 4280]
        assert gateway.working_sells("NVDA") == []
        assert not any(
            "not found" in str(c) for c in gateway.calls
        )

    @pytest.mark.asyncio
    async def test_fill_event_sweeps_within_the_poll_window(self):
        """The naked window closed between two 31 s polls — so listen to fills."""
        from abcxauto.protect_reconciler import ProtectionReconciler

        gateway = self._nvda_book()
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        assert reconciler.start() is True
        assert gateway.listeners == [reconciler._on_order_status]

        gateway.positions = []  # the market_order fill flattened NVDA
        for cb in gateway.listeners:
            cb({"status": "Filled", "symbol": "NVDA", "order_id": 4999})

        for _ in range(50):
            if not reconciler._pending:
                break
            await asyncio.sleep(0.01)

        assert sorted(_cancel_ids(gateway)) == [4279, 4280]
        assert gateway.working_sells("NVDA") == []
        reconciler.stop()
        assert gateway.listeners == []

    @pytest.mark.asyncio
    async def test_fill_event_on_a_live_lot_cancels_nothing(self):
        from abcxauto.protect_reconciler import ProtectionReconciler

        gateway = self._nvda_book()
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        reconciler._on_order_status(
            {"status": "Filled", "symbol": "NVDA", "order_id": 4999}
        )
        for _ in range(50):
            if not reconciler._pending:
                break
            await asyncio.sleep(0.01)
        assert _cancel_ids(gateway) == []
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_buy_fill_cannot_touch_sell_side_protection(self):
        """An entry fill must not cancel the protection placed against it.

        The lot can reach the ledger after its own fill, so a BUY fill sweeping
        SELL stops would strip a brand-new bracket in the gap.
        """
        from abcxauto.protect_reconciler import ProtectionReconciler

        gateway = BookGateway(
            positions=[],  # the new long has not landed in the ledger yet
            open_orders=[
                _order(4279, "NVDA", "SELL", 40, "STP", oca_group="OCA_NVDA_2"),
                _order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_NVDA_2"),
            ],
        )
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        reconciler._on_order_status(
            {"status": "Filled", "symbol": "NVDA", "action": "BUY", "order_id": 4278}
        )
        for _ in range(50):
            if not reconciler._pending:
                break
            await asyncio.sleep(0.01)
        assert _cancel_ids(gateway) == []
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_sell_fill_sweeps_the_sell_side_it_orphaned(self):
        from abcxauto.protect_reconciler import ProtectionReconciler

        gateway = self._nvda_book()
        gateway.positions = []
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        reconciler._on_order_status(
            {"status": "Filled", "symbol": "NVDA", "action": "SELL", "order_id": 4278}
        )
        for _ in range(50):
            if not reconciler._pending:
                break
            await asyncio.sleep(0.01)
        assert sorted(_cancel_ids(gateway)) == [4279, 4280]
        reconciler.stop()

    @pytest.mark.asyncio
    async def test_non_fill_status_does_not_sweep(self):
        from abcxauto.protect_reconciler import ProtectionReconciler

        gateway = self._nvda_book()
        gateway.positions = []
        reconciler = ProtectionReconciler(gateway, settle_s=0.0, retry_s=0.0)
        reconciler.start()
        reconciler._on_order_status({"status": "Submitted", "symbol": "NVDA"})
        await asyncio.sleep(0.02)
        assert _cancel_ids(gateway) == []
        reconciler.stop()


# ---------------------------------------------------------------------------
# The sweep must never become the bigger bug
# ---------------------------------------------------------------------------


class TestSweepSafety:
    @pytest.mark.asyncio
    async def test_live_lot_sees_no_cancels_at_all(self):
        gateway = BookGateway(
            positions=[_stk("CSCO", 50)],
            open_orders=[
                _order(1, "CSCO", "SELL", 50, "STP"),
                _order(2, "CSCO", "SELL", 50, "LMT", oca_group="OCA_CSCO_1"),
            ],
        )
        assert await cancel_orphaned_protection(gateway) == []
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_sweep_refuses_an_id_the_last_stop_rule_blocks(self, monkeypatch):
        """Belt and braces: if the two book views disagree, keep the stop."""
        gateway = BookGateway(
            positions=[_stk("AAPL", 10)],
            open_orders=[_order(9, "AAPL", "SELL", 10, "STP")],
        )
        monkeypatch.setattr(
            "abcxauto.protect.orphaned_protection_ids",
            lambda *a, **k: [9],
        )
        assert await cancel_orphaned_protection(gateway) == []
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_unreadable_book_fails_closed(self):
        class Blind(BookGateway):
            async def get_positions(self):
                raise RuntimeError("TWS 7497 refused")

        gateway = Blind(open_orders=[_order(1, "CSCO", "SELL", 50, "STP")])
        assert await cancel_orphaned_protection(gateway) == []
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_empty_ledger_on_a_dead_socket_is_not_flat(self):
        """get_positions() returns [] when disconnected — never believe it."""
        gateway = BookGateway(
            positions=[],
            open_orders=[_order(1, "CSCO", "SELL", 50, "STP")],
        )
        gateway.connected = False
        assert await cancel_orphaned_protection(gateway) == []
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_empty_ledger_without_a_readable_account_is_not_flat(self):
        """Portfolio and account arrive together; no NetLiq means no evidence."""

        class Stalled(BookGateway):
            async def get_account_summary(self):
                return {"error": "Not connected"}

        gateway = Stalled(
            positions=[],
            open_orders=[_order(1, "CSCO", "SELL", 50, "STP")],
        )
        assert await cancel_orphaned_protection(gateway) == []
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_other_lots_present_needs_no_corroboration(self):
        """A populated ledger that simply lacks CSCO is evidence on its own."""

        class NoAccount(BookGateway):
            async def get_account_summary(self):
                raise AssertionError("must not need the account when lots exist")

        gateway = NoAccount(
            positions=[_stk("NVDA", 40)],
            open_orders=[_order(1, "CSCO", "SELL", 50, "STP")],
        )
        assert await cancel_orphaned_protection(gateway) == [1]

    @pytest.mark.asyncio
    async def test_lot_that_lands_between_reads_keeps_its_protection(self):
        """The reread is the guard: a lot can reach the ledger after its fill."""

        class LaggingLedger(BookGateway):
            reads = 0

            async def get_positions(self):
                LaggingLedger.reads += 1
                if LaggingLedger.reads == 1:
                    return []
                return [_stk("CSCO", 50)]

        gateway = LaggingLedger(
            open_orders=[_order(1, "CSCO", "SELL", 50, "STP")],
        )
        assert await cancel_orphaned_protection(gateway) == []
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_cycle_skips_the_sweep_when_the_book_is_unreliable(self):
        from abcxauto.agent_loop import _reconcile_protection_after_snap

        gateway = BookGateway(
            positions=[],
            open_orders=[_order(1, "CSCO", "SELL", 50, "STP")],
        )
        snap = {
            "positions": [],
            "open_orders": list(gateway.open_orders),
            "account": {},
            "book_unreliable": True,
        }
        await _reconcile_protection_after_snap(gateway, snap)
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_symbol_scope_leaves_other_names_alone(self):
        gateway = BookGateway(
            positions=[],
            open_orders=[
                _order(1, "CSCO", "SELL", 50, "STP"),
                _order(2, "NVDA", "SELL", 40, "STP"),
            ],
        )
        cancelled = await cancel_orphaned_protection(gateway, symbols={"NVDA"})
        assert cancelled == [2]
        assert gateway.working_sells("CSCO") == [1]

    @pytest.mark.asyncio
    async def test_sweep_does_not_flatten_or_place_anything(self):
        gateway = BookGateway(
            positions=[],
            open_orders=[_order(1, "CSCO", "SELL", 50, "STP")],
        )
        await cancel_orphaned_protection(gateway)
        assert {name for name, _kw in gateway.calls} == {"cancel_order"}


class TestBackstopWiring:
    """Both poll paths sweep, so a missed fill event is late, not permanent."""

    @pytest.mark.asyncio
    async def test_monitor_poll_sweeps_and_refreshes_the_report(self):
        from abcxauto.monitor import PortfolioMonitor

        class Session:
            supports_agent_review = False

            def emit(self, *_a, **_k):
                pass

        gateway = BookGateway(
            positions=[],
            open_orders=[
                _order(4279, "NVDA", "SELL", 40, "STP", oca_group="OCA_NVDA_1"),
                _order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_NVDA_1"),
            ],
        )
        monitor = PortfolioMonitor(Session(), gateway)
        await monitor._tick()

        assert sorted(_cancel_ids(gateway)) == [4279, 4280]
        assert monitor.latest["protection"]["orphaned_protection"] == []
        assert monitor.latest["open_orders"] == []

    @pytest.mark.asyncio
    async def test_cycle_snap_sweeps_before_grok_reads_the_book(self):
        from abcxauto.agent_loop import _reconcile_protection_after_snap
        from abcxauto.monitor import build_protection_report

        gateway = BookGateway(
            positions=[],
            open_orders=[_order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_1")],
        )
        snap = {
            "positions": [],
            "open_orders": list(gateway.open_orders),
            "account": {},
            "protection": build_protection_report([], gateway.open_orders),
        }
        await _reconcile_protection_after_snap(gateway, snap)

        assert _cancel_ids(gateway) == [4280]
        assert snap["open_orders"] == []
        assert snap["protection"]["orphaned_protection"] == []

    @pytest.mark.asyncio
    async def test_cycle_snap_collapses_stacks_and_orphans_together(self):
        """Two stacked stops on a live lot, plus a stale exit on a flat name."""
        from abcxauto.agent_loop import _reconcile_protection_after_snap
        from abcxauto.monitor import build_protection_report

        orders = [
            _order(11, "CSCO", "SELL", 50, "STP"),
            _order(12, "CSCO", "SELL", 50, "TRAIL"),
            _order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_1"),
        ]
        gateway = BookGateway(positions=[_stk("CSCO", 50)], open_orders=orders)
        snap = {
            "positions": [_stk("CSCO", 50)],
            "open_orders": list(orders),
            "account": {},
            "protection": build_protection_report([_stk("CSCO", 50)], orders),
        }
        await _reconcile_protection_after_snap(gateway, snap)

        assert sorted(_cancel_ids(gateway)) == [11, 4280]
        assert [o["order_id"] for o in snap["open_orders"]] == [12]


class TestProtectionReportFact:
    def test_report_names_the_orphans(self):
        from abcxauto.monitor import build_protection_report

        report = build_protection_report(
            [],
            [_order(4280, "NVDA", "SELL", 40, "LMT", oca_group="OCA_NVDA_1")],
        )
        assert report["unprotected_symbols"] == []
        assert [r["order_id"] for r in report["orphaned_protection"]] == [4280]

    def test_covered_book_reports_no_orphans(self):
        from abcxauto.monitor import build_protection_report

        report = build_protection_report(
            [_stk("NVDA", 40)],
            [_order(4279, "NVDA", "SELL", 40, "STP")],
        )
        assert report["orphaned_protection"] == []
