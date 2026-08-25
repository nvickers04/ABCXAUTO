"""Fill-driven protection reconciliation — the sub-poll half of the fix.

The monitor snapshot runs about every 31 s. On 2026-08-13 a stacked trailing
stop filled at 15:56:22 and a second one filled at 15:58:00 against a position
that no longer existed: the naked window opened and closed between polls, so a
per-snapshot sweep alone would have been too slow to matter.

This listens to IBKR order status instead. When an order reports ``Filled`` the
symbol is swept within a second or two, and again a few seconds later because
the position ledger can lag its own fill. The sweep itself is
``executor.cancel_orphaned_protection``, which only ever cancels protection on
a contract it can prove is flat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Set

from abcxauto.broker.order_types import is_stop_order

logger = logging.getLogger(__name__)

_SETTLE_S = 1.0
_RETRY_S = 5.0
# Same slack as trade_plan stacked-stop cover and the protection report.
_COVER_QTY_SLACK = 0.51
_FILL_SIDES = {"BUY": "BUY", "BOT": "BUY", "SELL": "SELL", "SLD": "SELL"}


def _qty(row: Any) -> float:
    if not isinstance(row, dict):
        return 0.0
    raw = row.get("quantity")
    if raw is None:
        raw = row.get("position")
    if raw is None:
        raw = row.get("totalQuantity")
    if raw is None:
        raw = row.get("filled")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _sec_bucket(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    sec = str(
        row.get("sec_type") or row.get("secType") or row.get("sec") or "STK"
    ).upper()
    if sec in ("", "STK", "ETF"):
        return "STK"
    return sec


def _fill_side(raw: Any) -> str:
    """BUY/SELL, including IBKR fill aliases BOT/SLD. Unknown stays empty."""
    return _FILL_SIDES.get(str(raw or "").strip().upper(), "")


def last_stop_covers_lot(position: Any, orders: Any) -> bool:
    """True only when a working exit-side stop covers held STK qty.

    A 1-share STP on a 10-share lot is not a last-stop. A BUY stop on a long
    (or a SELL stop on a short) is the wrong side and does not cover.
    """
    if not isinstance(position, dict):
        return False
    if _sec_bucket(position) != "STK":
        return False
    held = _qty(position)
    if abs(held) < 1e-9:
        return False
    symbol = str(position.get("symbol") or "").upper()
    if not symbol:
        return False
    want = "SELL" if held > 0 else "BUY"
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        if str(order.get("symbol") or "").upper() != symbol:
            continue
        if _sec_bucket(order) != "STK":
            continue
        otype = str(order.get("order_type") or order.get("orderType") or "")
        if not is_stop_order(otype):
            continue
        if _fill_side(order.get("action") or order.get("side")) != want:
            continue
        if abs(_qty(order)) + 1e-9 >= abs(held) - _COVER_QTY_SLACK:
            return True
    return False


def uncovered_stk_symbols(
    positions: Any,
    orders: Any,
    *,
    symbol: str = "",
) -> list[str]:
    """STK lots whose last-stop does not cover held qty (wrong side or short)."""
    want = str(symbol or "").strip().upper()
    seen: set[str] = set()
    out: list[str] = []
    for position in positions or []:
        if not isinstance(position, dict):
            continue
        if _sec_bucket(position) != "STK":
            continue
        if abs(_qty(position)) < 1e-9:
            continue
        sym = str(position.get("symbol") or "").upper()
        if not sym or (want and sym != want) or sym in seen:
            continue
        if last_stop_covers_lot(position, orders):
            continue
        seen.add(sym)
        out.append(sym)
    return out


class ProtectionReconciler:
    """Cancels orphaned protection on the fill that orphaned it."""

    def __init__(
        self,
        connector: Any,
        *,
        settle_s: float = _SETTLE_S,
        retry_s: float = _RETRY_S,
    ) -> None:
        self.connector = connector
        self.settle_s = max(0.0, float(settle_s))
        self.retry_s = max(0.0, float(retry_s))
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: Set[str] = set()
        self._tasks: Set[asyncio.Task] = set()
        self._started = False
        self.last_unprotected: list[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Register the order-status listener. False when the connector has none."""
        if self._started:
            return True
        register = getattr(self.connector, "register_order_status_listener", None)
        if not callable(register):
            return False
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        register(self._on_order_status)
        self._started = True
        logger.info("Protection reconciler armed (fill -> orphan sweep)")
        return True

    def stop(self) -> None:
        if self._started:
            unregister = getattr(
                self.connector, "unregister_order_status_listener", None
            )
            if callable(unregister):
                try:
                    unregister(self._on_order_status)
                except Exception:
                    logger.debug("reconciler unregister failed", exc_info=True)
        self._started = False
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._pending.clear()
        self.last_unprotected = []

    @property
    def running(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # Event path
    # ------------------------------------------------------------------

    def _on_order_status(self, event: Dict[str, Any]) -> None:
        """Sync listener: a completed fill may have just orphaned protection."""
        if not isinstance(event, dict):
            return
        if str(event.get("status") or "").strip().lower() != "filled":
            return
        symbol = str(event.get("symbol") or "").strip().upper()
        if not symbol:
            return
        self.schedule(
            symbol,
            action=event.get("action") or event.get("side") or "",
        )

    def schedule(self, symbol: str, *, action: str = "") -> None:
        """Queue one debounced sweep for ``symbol`` on the reconciler loop.

        ``action`` is the side of the fill that triggered this. A BUY fill can
        only orphan BUY-side protection (a short being covered); it must never
        reach the SELL-side protection of the long it may have just opened,
        because the new lot can arrive in the ledger after its own fill.
        BOT/SLD are the same sides as BUY/SELL; anything else stays unknown
        and sweeps both (a missing action on a flat book must still cancel).
        """
        symbol = str(symbol or "").strip().upper()
        if not symbol or symbol in self._pending:
            return
        side = _fill_side(action)
        actions = {side} if side in ("BUY", "SELL") else None
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug("reconciler has no loop; skipping sweep for %s", symbol)
            return
        self._pending.add(symbol)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            coro = self._sweep_later(symbol, actions)
            if running is loop:
                self._track(loop.create_task(coro))
            else:
                asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            self._pending.discard(symbol)
            logger.warning("reconciler could not schedule sweep for %s: %s", symbol, e)

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sweep_later(self, symbol: str, actions: Any = None) -> None:
        try:
            for delay in (self.settle_s, self.retry_s):
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.sweep_now(symbol, actions=actions)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("orphan sweep for %s failed", symbol)
        finally:
            self._pending.discard(symbol)

    async def sweep_now(self, symbol: str, *, actions: Any = None) -> list[int]:
        """One immediate sweep of ``symbol``. Returns cancelled order ids."""
        from abcxauto.executor import cancel_orphaned_protection

        symbol = str(symbol or "").strip().upper()
        cancelled = await cancel_orphaned_protection(
            self.connector,
            symbols={symbol},
            actions=actions,
        )
        if cancelled:
            logger.warning(
                "orphan-protection: cancelled %s on flat %s after fill",
                cancelled, symbol,
            )
        await self._note_uncovered_lots(symbol)
        return list(cancelled or [])

    async def _note_uncovered_lots(self, symbol: str) -> None:
        """A leftover lot is unprotected unless its last-stop covers held qty."""
        self.last_unprotected = []
        try:
            positions = await self.connector.get_positions()
            open_orders = await self.connector.get_open_orders()
        except Exception:
            logger.debug("reconciler cover-check skipped for %s", symbol, exc_info=True)
            return
        if positions is None or open_orders is None:
            return
        uncovered = uncovered_stk_symbols(positions, open_orders, symbol=symbol)
        self.last_unprotected = uncovered
        if uncovered:
            logger.warning(
                "last-stop does not cover lot qty on %s after fill (not protected)",
                ",".join(uncovered),
            )
