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

When a stop reports ``Cancelled`` / ``ApiCancelled`` (IBKR 10326 and friends)
and an open STK lot would be left without a covering last-stop, one bare GTC
STP is placed at the cancelled stop's aux price. A resting LMT target is not
protection and is left alone.

On process start the same cover runs once for open STK lots that already have
no working last-stop: a finite stop price is taken from the in-process last
dispatch / bracket group or the journal. No price on record → log unprotected;
never invent a price; never market-sell.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

_SETTLE_S = 1.0
_RETRY_S = 5.0
# Same slack as trade_plan stacked-stop cover and the protection report.
_COVER_QTY_SLACK = 0.51
_FILL_SIDES = {"BUY": "BUY", "BOT": "BUY", "SELL": "SELL", "SLD": "SELL"}
_STOP_TYPES = frozenset({"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"})
_CANCEL_STATUSES = frozenset({"cancelled", "apicancelled"})
_PRICE_KEYS = ("aux_price", "stop_price", "auxPrice", "stopPrice", "stop")


def _qty(row: Any) -> float:
    if not isinstance(row, dict):
        return 0.0
    raw = row.get("quantity")
    if raw is None:
        raw = row.get("position")
    if raw is None:
        raw = row.get("totalQuantity")
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
        otype = str(order.get("order_type") or order.get("orderType") or "").upper()
        if otype not in _STOP_TYPES:
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


def _finite_stop_price(raw: Any) -> Optional[float]:
    """Finite positive stop/aux price, else None (never invent)."""
    if raw is None or raw == "":
        return None
    try:
        px = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isfinite(px) and px > 0.0:
        return px
    return None


def _price_from_mapping(row: Any) -> Optional[float]:
    if not isinstance(row, dict):
        return None
    for key in _PRICE_KEYS:
        px = _finite_stop_price(row.get(key))
        if px is not None:
            return px
    return None


def _event_stop_price(event: Any) -> Optional[float]:
    """Finite positive aux/stop price from a cancel event, else None (never invent)."""
    return _price_from_mapping(event)


def _journal_stop_price(symbol: str) -> Optional[float]:
    """Most recent finite stop for ``symbol`` from dispatches, then proposals."""
    want = str(symbol or "").strip().upper()
    if not want:
        return None
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
    except Exception:
        logger.debug("known-stop journal import failed", exc_info=True)
        return None

    try:
        for row in journal.recent_dispatches(limit=80) or []:
            if not isinstance(row, dict):
                continue
            result = row.get("result")
            if not isinstance(result, dict):
                continue
            if str(result.get("symbol") or "").upper() != want:
                continue
            px = _price_from_mapping(result)
            if px is not None:
                return px
    except Exception:
        logger.debug("known-stop dispatch lookup failed for %s", want, exc_info=True)

    try:
        for row in journal.recent_proposals(limit=80) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol") or "").upper() != want:
                continue
            params = row.get("params")
            if params is None:
                raw = row.get("params_json")
                if isinstance(raw, str) and raw.strip():
                    try:
                        params = json.loads(raw)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        params = None
            px = _price_from_mapping(params)
            if px is not None:
                return px
    except Exception:
        logger.debug("known-stop proposal lookup failed for %s", want, exc_info=True)
    return None


def known_stop_price_for_symbol(
    symbol: str, connector: Any = None
) -> Optional[float]:
    """Finite stop on record for ``symbol``: last dispatch, then journal. Never invent."""
    want = str(symbol or "").strip().upper()
    if not want:
        return None
    if connector is not None:
        try:
            from abcxauto.broker.orders import last_known_stop_price

            px = last_known_stop_price(connector, want)
            if px is not None:
                return px
        except Exception:
            logger.debug(
                "in-process last-dispatch stop lookup failed for %s",
                want,
                exc_info=True,
            )
    return _journal_stop_price(want)


def _stk_lot_for_symbol(positions: Any, symbol: str) -> Optional[dict]:
    want = str(symbol or "").strip().upper()
    if not want:
        return None
    for position in positions or []:
        if not isinstance(position, dict):
            continue
        if _sec_bucket(position) != "STK":
            continue
        if str(position.get("symbol") or "").upper() != want:
            continue
        if abs(_qty(position)) < 1e-9:
            continue
        return position
    return None


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
        self._pending_replace: Set[str] = set()
        self._startup_cover_pending = False
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
        self._schedule_startup_cover()
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
        self._pending_replace.clear()
        self._startup_cover_pending = False
        self.last_unprotected = []

    @property
    def running(self) -> bool:
        return self._started

    # ------------------------------------------------------------------
    # Event path
    # ------------------------------------------------------------------

    def _on_order_status(self, event: Dict[str, Any]) -> None:
        """Sync listener: fills may orphan protection; cancelled stops may uncover."""
        if not isinstance(event, dict):
            return
        status = str(event.get("status") or "").strip().lower()
        symbol = str(event.get("symbol") or "").strip().upper()
        if not symbol:
            return
        if status == "filled":
            self.schedule(
                symbol,
                action=event.get("action") or event.get("side") or "",
            )
            return
        if status in _CANCEL_STATUSES:
            self._schedule_stop_replace(event)

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

    def _schedule_stop_replace(self, event: Dict[str, Any]) -> None:
        """Queue one bare GTC stop if a cancelled stop left an STK lot uncovered."""
        otype = str(event.get("order_type") or event.get("orderType") or "").upper()
        if otype not in _STOP_TYPES:
            return
        symbol = str(event.get("symbol") or "").strip().upper()
        if not symbol or symbol in self._pending_replace:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug(
                "reconciler has no loop; skipping stop-replace for %s", symbol
            )
            return
        self._pending_replace.add(symbol)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            coro = self._replace_cancelled_stop(symbol, dict(event))
            if running is loop:
                self._track(loop.create_task(coro))
            else:
                asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            self._pending_replace.discard(symbol)
            logger.warning(
                "reconciler could not schedule stop-replace for %s: %s", symbol, e
            )

    def _schedule_startup_cover(self) -> None:
        """Once per arm: cover open STK lots whose last-stop is already gone."""
        if self._startup_cover_pending:
            return
        place = getattr(self.connector, "place_stop_order", None)
        if not callable(place):
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.debug("reconciler has no loop; skipping startup cover")
            return
        self._startup_cover_pending = True
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            coro = self.cover_unprotected_at_startup()
            if running is loop:
                self._track(loop.create_task(coro))
            else:
                asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:
            self._startup_cover_pending = False
            logger.warning("reconciler could not schedule startup cover: %s", e)

    async def cover_unprotected_at_startup(self) -> list[str]:
        """Place bare GTC stops for uncovered STK lots when a known price exists.

        Called from ``start``. Returns symbols left unprotected (no finite
        price on record, or place failed). Never invents a price; never
        market-sells.
        """
        left: list[str] = []
        try:
            if self.settle_s > 0:
                await asyncio.sleep(self.settle_s)
            try:
                positions = await self.connector.get_positions()
                open_orders = await self.connector.get_open_orders()
            except Exception:
                logger.debug("startup cover book read failed", exc_info=True)
                return left
            if positions is None or open_orders is None:
                return left
            uncovered = uncovered_stk_symbols(positions, open_orders)
            if not uncovered:
                self.last_unprotected = []
                await self._resize_held_exits(positions)
                return left
            live_orders = list(open_orders)
            still: list[str] = []
            for symbol in uncovered:
                lot = _stk_lot_for_symbol(positions, symbol)
                if lot is None:
                    continue
                if last_stop_covers_lot(lot, live_orders):
                    continue
                stop_price = known_stop_price_for_symbol(
                    symbol, connector=self.connector
                )
                if stop_price is None:
                    still.append(symbol)
                    continue
                ok = await self._place_bare_gtc_stop(
                    lot, stop_price, reason="startup"
                )
                if ok:
                    # Reflect the resting replacement for later symbols.
                    live_orders.append(
                        {
                            "symbol": symbol,
                            "sec_type": "STK",
                            "action": "SELL" if _qty(lot) > 0 else "BUY",
                            "quantity": abs(int(_qty(lot))),
                            "order_type": "STP",
                            "aux_price": stop_price,
                        }
                    )
                else:
                    still.append(symbol)
            self.last_unprotected = still
            left = still
            if still:
                logger.warning(
                    "last-stop does not cover lot qty on %s at startup "
                    "(not protected; no finite stop on record)",
                    ",".join(still),
                )
            return left
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("startup cover failed")
            return left
        finally:
            self._startup_cover_pending = False

    async def _place_bare_gtc_stop(
        self, lot: dict, stop_price: float, *, reason: str
    ) -> bool:
        """Place one bare GTC STP for held qty. True when place claims success."""
        symbol = str(lot.get("symbol") or "").upper()
        held = _qty(lot)
        qty = abs(int(held))
        if qty <= 0 or not symbol:
            return False
        action = "SELL" if held > 0 else "BUY"
        place = getattr(self.connector, "place_stop_order", None)
        if not callable(place):
            return False
        try:
            placed = await place(symbol, action, qty, stop_price)
        except Exception:
            logger.exception(
                "bare GTC stop %s failed for %s", reason, symbol
            )
            return False
        if isinstance(placed, dict) and (
            placed.get("success") or placed.get("order_id")
        ):
            logger.warning(
                "last-stop missing on %s (%s); placed bare GTC STP "
                "action=%s qty=%s @ %s (no oca)",
                symbol,
                reason,
                action,
                qty,
                stop_price,
            )
            self.last_unprotected = [
                s for s in self.last_unprotected if s != symbol
            ]
            return True
        return False

    async def _replace_cancelled_stop(
        self, symbol: str, event: Dict[str, Any]
    ) -> None:
        """Place one bare GTC STP for held qty when the last covering stop died."""
        try:
            if self.settle_s > 0:
                await asyncio.sleep(self.settle_s)
            try:
                positions = await self.connector.get_positions()
                open_orders = await self.connector.get_open_orders()
            except Exception:
                logger.debug(
                    "reconciler stop-replace book read failed for %s",
                    symbol,
                    exc_info=True,
                )
                return
            if positions is None or open_orders is None:
                return
            lot = _stk_lot_for_symbol(positions, symbol)
            if lot is None:
                return
            if last_stop_covers_lot(lot, open_orders):
                return
            stop_price = _event_stop_price(event)
            if stop_price is None:
                self.last_unprotected = uncovered_stk_symbols(
                    positions, open_orders, symbol=symbol
                )
                if self.last_unprotected:
                    logger.warning(
                        "last-stop does not cover lot qty on %s after fill "
                        "(not protected)",
                        ",".join(self.last_unprotected),
                    )
                return
            ok = await self._place_bare_gtc_stop(
                lot, stop_price, reason="after cancel"
            )
            if not ok:
                await self._note_uncovered_lots(symbol)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stop-replace for %s failed", symbol)
        finally:
            self._pending_replace.discard(symbol)

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
        from abcxauto.protect import begin_look_protection_budget
        from abcxauto.executor import cancel_orphaned_protection

        symbol = str(symbol or "").strip().upper()
        begin_look_protection_budget(reset=True)
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
        await self._resize_oversized_exits(symbol)
        await self._note_uncovered_lots(symbol)
        return list(cancelled or [])

    async def _resize_held_exits(self, positions: Any) -> None:
        """Startup: shrink stop/target qty that still matches the pre-trim lot."""
        seen: set[str] = set()
        for pos in positions or []:
            if not isinstance(pos, dict):
                continue
            sym = str(pos.get("symbol") or "").strip().upper()
            sec = str(pos.get("secType") or pos.get("sec_type") or "STK").upper()
            if not sym or sym in seen or not sec.startswith("STK"):
                continue
            seen.add(sym)
            await self._resize_oversized_exits(sym)

    async def _resize_oversized_exits(self, symbol: str) -> Optional[Dict[str, Any]]:
        """After a partial exit, shrink working stop/target to held qty.

        Place the replacement via ``execute_proposal`` (oca / stop_order) so
        replace-on-place cancels the old legs only after the new ones are
        accepted. Never market-sells; never flattens the remainder.
        """
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return None
        try:
            positions = await self.connector.get_positions()
            open_orders = await self.connector.get_open_orders()
        except Exception:
            logger.debug(
                "reconciler resize book read failed for %s", symbol, exc_info=True
            )
            return None
        if positions is None or open_orders is None:
            return None
        from abcxauto.trade_plan import exit_resize_ticket

        ticket = exit_resize_ticket(positions, open_orders, symbol=symbol)
        if not ticket:
            return None
        try:
            from abcxauto.executor import execute_proposal
            from abcxauto.proposals import validate_proposal

            proposal = validate_proposal(
                str(ticket["strategy"]),
                dict(ticket["params"]),
                str(ticket["rationale"]),
                quote_last=ticket["params"].get("price_hint"),
            )
            result = await execute_proposal(
                proposal, self.connector, source="protect_resize"
            )
        except Exception:
            logger.exception("protect resize failed for %s", symbol)
            return None
        if isinstance(result, dict) and (
            result.get("success")
            or result.get("stop_order_id")
            or result.get("order_id")
        ):
            logger.warning(
                "resized exits on %s after trim: stop_qty %s → held %s via %s",
                symbol,
                ticket.get("stop_order_qty"),
                ticket.get("held_qty"),
                ticket.get("strategy"),
            )
            return result
        if isinstance(result, dict) and result.get("error"):
            logger.warning(
                "protect resize rejected for %s: %s",
                symbol,
                result.get("error"),
            )
        return result if isinstance(result, dict) else None

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
