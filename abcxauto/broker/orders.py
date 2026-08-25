"""
IBKR Orders Mixin - Order Placement and Management

This module provides all order-related functionality as a mixin class:
- Basic order types (limit, market, stop, stop-limit)
- Bracket orders with OCA protection
- Option position close
- Stop/target modification
- Order state tracking

This mixin is imported by IBKRConnector in connector.py.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from ib_insync import Order, Contract, TagValue
from ib_insync.contract import Stock

from abcxauto.broker.connection import safe_sleep as _safe_sleep

logger = logging.getLogger(__name__)


def _oid_matches(order: Any, order_id: int) -> bool:
    try:
        want = int(order_id)
    except (TypeError, ValueError):
        return False
    for attr in ("orderId", "permId"):
        raw = getattr(order, attr, None)
        if raw in (None, 0, 0.0):
            continue
        try:
            if int(raw) == want:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _finite_px(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > 1e300:
        return None
    return v


def modify_did_stick(*, requested: float, live: float | None, tol: float = 0.005) -> bool:
    """True only if IBKR reread matches the requested modify."""
    if live is None:
        return False
    return abs(float(live) - float(requested)) <= tol


class IBKROrdersMixin:
    """
    Mixin class providing order placement and management methods.

    Must be used with IBKRConnector which provides:
    - self.ib: IB connection instance
    - self._ensure_connected(): Connection check method
    - self._wait_for_fill(): Fill waiting method
    - self._update_account_values(): Account value refresh
    - self._order_states, self._bracket_groups: Order tracking dicts
    - self._order_state_lock: Threading lock for order state
    - self.net_liquidation, self.day_trades_remaining: Account values
    """

    # ========== HELPER METHODS ==========

    async def _prepare_contract(self, symbol: str) -> Optional[Stock]:
        """Prepare and qualify a stock contract. Returns None if not connected."""
        if not await self._ensure_connected():
            return None
        contract = Stock(symbol, 'SMART', 'USD')
        await self.ib.qualifyContractsAsync(contract)
        return contract

    async def _check_order_rejection(self, trade, order_name: str, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Check if a placed order was immediately rejected by IBKR.

        Waits 1s for broker validation, then checks status and trade log
        for errors (margin rejection, etc.).

        Returns error dict if rejected, None if order looks OK.
        """
        await _safe_sleep(1.0)
        await _safe_sleep(0)  # yield to event loop — flushes IBKR callbacks

        status = trade.orderStatus.status
        has_error = any(e.errorCode for e in trade.log)
        if status in ('Cancelled', 'ApiCancelled', 'Inactive') or has_error:
            err_msg = 'Order rejected by broker'
            for entry in reversed(trade.log):
                if entry.errorCode:
                    err_msg = entry.message or f'IBKR error {entry.errorCode}'
                    break
            logger.error(f"{order_name} REJECTED for {symbol}: {err_msg}")
            return {'error': err_msg, 'symbol': symbol, 'order_id': trade.order.orderId}
        return None

    async def _place_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        order_type: str,
        tif: str = 'DAY',
        limit_price: float = None,
        aux_price: float = None,
        order_name: str = None,
        extra_result: Dict = None,
        **order_attrs
    ) -> Dict[str, Any]:
        """
        Generic order placement helper.

        Args:
            symbol: Stock symbol
            action: 'BUY' or 'SELL'
            quantity: Number of shares
            order_type: IBKR order type string
            tif: Time in force
            limit_price: Limit price (optional)
            aux_price: Auxiliary price for stops (optional)
            order_name: Name for logging (defaults to order_type)
            extra_result: Additional fields for result dict
            **order_attrs: Additional Order attributes to set
        """
        contract = await self._prepare_contract(symbol)
        if contract is None:
            return {'error': 'Not connected'}

        # === INFO: Check for existing orders (warn but don't block) ===
        try:
            existing_orders = await self.get_open_orders()
            same_symbol_orders = [o for o in existing_orders if o.get('symbol') == symbol]
            # Just log it, don't block - agent may be intentionally replacing protection
            if same_symbol_orders:
                order_info = ", ".join([f"{o['action']} {o['quantity']} ({o['order_type']})" for o in same_symbol_orders])
                logger.info(f"Note: {symbol} already has orders: {order_info}")
        except Exception as e:
            logger.warning(f"Could not check existing orders: {e}")

        try:
            order = Order()
            order.action = action
            order.totalQuantity = quantity
            order.orderType = order_type
            order.tif = tif
            order.transmit = True

            if limit_price is not None:
                order.lmtPrice = limit_price
            if aux_price is not None:
                order.auxPrice = aux_price

            for attr, value in order_attrs.items():
                setattr(order, attr, value)

            trade = self.ib.placeOrder(contract, order)

            # Check for immediate broker rejection (margin, risk, etc.)
            name = order_name or order_type
            rejection = await self._check_order_rejection(trade, name, symbol)
            if rejection:
                return rejection

            price_info = f" @ ${limit_price:.2f}" if limit_price else ""
            price_info = price_info or (f" @ ${aux_price:.2f}" if aux_price else "")
            logger.info(f"{name} order placed: {action} {quantity} {symbol}{price_info}")

            result = {
                'success': True,
                'filled': False,  # Order placed but not filled yet
                'order_id': trade.order.orderId,
                'symbol': symbol,
                'action': action,
                'quantity': quantity,
                'order_type': order_type,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
            if limit_price is not None:
                result['limit_price'] = limit_price
            if aux_price is not None:
                result['stop_price'] = aux_price
            if extra_result:
                result.update(extra_result)

            return result

        except Exception as e:
            logger.error(f"{order_type} order failed for {symbol}: {e}", exc_info=True)
            return {'error': str(e), 'symbol': symbol}

    async def _place_order_with_fill(
        self,
        symbol: str,
        action: str,
        quantity: int,
        order_type: str,
        tif: str,
        limit_price: float = None,
        timeout: float = 2.0,
        order_name: str = None
    ) -> Dict[str, Any]:
        """Place order and wait for fill result (for IOC/FOK)."""
        contract = await self._prepare_contract(symbol)
        if contract is None:
            return {'error': 'Not connected'}

        try:
            order = Order()
            order.action = action
            order.totalQuantity = quantity
            order.orderType = 'LMT'
            order.lmtPrice = limit_price
            order.tif = tif
            order.transmit = True

            trade = self.ib.placeOrder(contract, order)
            fill_result = await self._wait_for_fill(trade, timeout=timeout)

            name = order_name or tif
            logger.info(f"{name} order completed: {action} {symbol}, filled {fill_result['filled_quantity']}/{quantity}")

            return {
                'success': True,
                'order_id': trade.order.orderId,
                'symbol': symbol,
                'action': action,
                'quantity': quantity,
                'order_type': tif,
                'limit_price': limit_price,
                'filled': fill_result['filled'],
                'fill_status': fill_result['status'],
                'filled_quantity': fill_result['filled_quantity'],
                'avg_fill_price': fill_result['avg_fill_price'],
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"{tif} order failed for {symbol}: {e}")
            return {'error': str(e), 'symbol': symbol}

    # ========== BASIC ORDER TYPES ==========

    async def place_limit_order(
        self, symbol: str, action: str, quantity: int, limit_price: float, tif: str = 'DAY'
    ) -> Dict[str, Any]:
        """Place a basic limit order."""
        return await self._place_order(
            symbol, action, quantity, 'LMT', tif=tif, limit_price=limit_price,
            extra_result={'tif': tif}
        )

    # ========== BRACKET ORDERS (LONG + SHORT) ==========

    async def place_bracket_order(
        self,
        symbol: str,
        quantity: int,
        direction: str,
        entry_price: float,
        stop_price: float,
        target_price: float,
        time_bucket: str = 'short_swing'
    ) -> Dict[str, Any]:
        """
        Place entry order + OCA protective orders (stop loss and take profit).

        Uses OCA (One-Cancels-All) instead of parent-linked brackets because:
        1. IOC entry orders complete before children can be linked
        2. OCA orders don't depend on parent order status
        3. More reliable protection that persists after entry fills
        """
        from abcxauto.broker.connector import BracketGroup
        from abcxauto.broker.order_types import IBKROrderType

        contract = await self._prepare_contract(symbol)
        if contract is None:
            return {'error': 'Not connected'}

        # Track fill state for the outer except (naked-position emergency flatten).
        filled_qty = None
        actual_fill_price = None
        exit_action = None

        try:

            # Determine actions based on direction
            if direction == 'LONG':
                entry_action = 'BUY'
                exit_action = 'SELL'
            else:  # SHORT
                entry_action = 'SELL'
                exit_action = 'BUY'

            # entry_price already has tolerance applied by upstream validation.
            # Risk/concentration/PDT sanity lives in proposal validation +
            # human confirmation — the broker layer just executes.
            limit_price = entry_price
            await self._update_account_values()

            # PDT visibility only (informational — the human decides)
            if self.net_liquidation < 25000 and time_bucket == 'intraday':
                logger.warning(
                    f"PDT account (${self.net_liquidation:,.0f} < $25k) - "
                    f"day trades remaining: {self.day_trades_remaining}"
                )

            # STEP 1: Place entry order
            entry_order = Order()
            entry_order.action = entry_action
            entry_order.totalQuantity = quantity
            entry_order.orderType = 'LMT'
            entry_order.lmtPrice = limit_price
            entry_order.tif = 'DAY'  # DAY order gives time to fill
            entry_order.transmit = True

            # Log order details
            logger.info(f"[ORDER] {entry_action} {quantity} {symbol} @ ${limit_price:.2f}")

            logger.info(f"Placing LIMIT {entry_action} {quantity} {symbol} @ ${limit_price:.2f}")

            entry_trade = self.ib.placeOrder(contract, entry_order)
            entry_id = entry_trade.order.orderId

            # Wait for entry fill confirmation
            fill_result = await self._wait_for_fill(entry_trade, timeout=30.0)

            if not fill_result['filled']:
                # Order didn't fill in time - cancel it
                logger.warning(f"Entry NOT FILLED: {symbol} - {fill_result['status']}")
                try:
                    if hasattr(self, "_cancel_order_with_tracking"):
                        self._cancel_order_with_tracking(entry_trade.order, source="bracket_entry_timeout")
                    else:
                        self.ib.cancelOrder(entry_trade.order)
                except Exception as _cancel_err:
                    logger.debug(f"Cancel unfilled entry failed (may already be cancelled): {_cancel_err}")

                # Fetch current bid/ask from IBKR so caller can retry on live tape
                current_bid, current_ask = None, None
                try:
                    fn = getattr(self, "get_live_quote", None)
                    _q = await fn(symbol, fresh=True) if callable(fn) else None
                    if isinstance(_q, dict) and not _q.get("error"):
                        current_bid = _q.get("bid") if _q.get("bid") and float(_q.get("bid") or 0) > 0 else None
                        current_ask = _q.get("ask") if _q.get("ask") and float(_q.get("ask") or 0) > 0 else None
                except Exception as _mkt_err:
                    logger.debug(f"IBKR quote fetch failed for {symbol}: {_mkt_err}")

                result = {
                    'success': False,
                    'filled': False,
                    'reason': f"Entry order {fill_result['status']} - no fill at ${limit_price:.2f}",
                    'symbol': symbol,
                    'direction': direction,
                }
                if current_bid is not None:
                    result['current_bid'] = current_bid
                if current_ask is not None:
                    result['current_ask'] = current_ask
                if current_bid and current_ask:
                    result['spread'] = round(current_ask - current_bid, 2)
                    result['hint'] = (
                        f"Limit was ${limit_price:.2f}, market is now "
                        f"${current_bid:.2f}/${current_ask:.2f}. "
                        f"Consider limit_order closer to ask, or market_order with stop protection."
                    )
                return result

            # STEP 2: Entry filled - now place OCA protective orders
            actual_fill_price = fill_result['avg_fill_price']
            filled_qty = fill_result['filled_quantity']

            # Recalculate stop/target based on ACTUAL fill price
            # Preserve the PERCENTAGE distances from the original analysis
            if entry_price <= 0:
                logger.error(f"Invalid entry_price {entry_price} - using original stop/target")
                adjusted_stop = stop_price
                adjusted_target = target_price
            elif direction == 'LONG':
                stop_pct = (entry_price - stop_price) / entry_price  # e.g., 2.2% risk
                target_pct = (target_price - entry_price) / entry_price  # e.g., 6.6% reward
                adjusted_stop = round(actual_fill_price * (1 - stop_pct), 2)
                adjusted_target = round(actual_fill_price * (1 + target_pct), 2)
            else:  # SHORT
                stop_pct = (stop_price - entry_price) / entry_price
                target_pct = (entry_price - target_price) / entry_price
                adjusted_stop = round(actual_fill_price * (1 + stop_pct), 2)
                adjusted_target = round(actual_fill_price * (1 - target_pct), 2)

            logger.info(f"Entry FILLED: {direction} {filled_qty} {symbol} @ ${actual_fill_price:.2f}")
            logger.info(f"Placing OCA protection: stop=${adjusted_stop:.2f}, target=${adjusted_target:.2f}")

            # Create unique OCA group to link stop and target
            oca_group = f"OCA_{symbol}_{entry_id}_{int(datetime.now().timestamp())}"

            # Stop loss order (GTC, OCA-linked) — retry up to 3x on failure
            stop_trade = None
            stop_id = None
            for _stop_attempt in range(3):
                stop_order = Order()
                stop_order.action = exit_action
                stop_order.totalQuantity = filled_qty
                stop_order.orderType = IBKROrderType.STOP.value
                stop_order.auxPrice = adjusted_stop
                stop_order.tif = 'GTC'
                stop_order.ocaGroup = oca_group
                stop_order.ocaType = 1  # Cancel on fill
                stop_order.transmit = True

                stop_trade = self.ib.placeOrder(contract, stop_order)
                stop_id = stop_trade.order.orderId

                # Wait for broker to accept/reject
                await _safe_sleep(0.5)
                await _safe_sleep(0)  # flush callbacks

                if stop_trade.orderStatus.status not in ('ApiCancelled', 'Cancelled', 'Error'):
                    break  # Stop accepted
                logger.warning(
                    f"Stop order attempt {_stop_attempt + 1}/3 failed for {symbol}: "
                    f"{stop_trade.orderStatus.status}"
                )
                if _stop_attempt < 2:
                    await _safe_sleep(1.0 * (_stop_attempt + 1))

            # After retries, check if stop is still rejected
            if stop_trade.orderStatus.status in ('ApiCancelled', 'Cancelled', 'Error'):
                logger.critical(
                    f"POSITION AT RISK: Entry filled but stop failed after 3 attempts for {symbol}! "
                    f"Placing emergency market exit."
                )
                # Emergency: close the position with a market order
                try:
                    emergency_order = Order()
                    emergency_order.action = exit_action
                    emergency_order.totalQuantity = filled_qty
                    emergency_order.orderType = 'MKT'
                    emergency_order.tif = 'GTC'
                    emergency_order.transmit = True
                    emergency_trade = self.ib.placeOrder(contract, emergency_order)
                    await _safe_sleep(2.0)
                    logger.critical(
                        f"Emergency market exit placed for {symbol}: "
                        f"order_id={emergency_trade.order.orderId}, "
                        f"status={emergency_trade.orderStatus.status}"
                    )
                except Exception as emerg_err:
                    logger.critical(f"EMERGENCY EXIT ALSO FAILED for {symbol}: {emerg_err}")

                return {
                    'success': False,
                    'filled': True,  # Entry DID fill
                    'entry_price': actual_fill_price,
                    'quantity': filled_qty,
                    'error': f"Stop order failed after 3 retries: {stop_trade.orderStatus.status}",
                    'symbol': symbol,
                    'warning': 'Emergency market exit attempted — verify position manually!'
                }

            # Take profit order (GTC, OCA-linked)
            target_order = Order()
            target_order.action = exit_action
            target_order.totalQuantity = filled_qty
            target_order.orderType = 'LMT'
            target_order.lmtPrice = adjusted_target
            target_order.tif = 'GTC'
            target_order.ocaGroup = oca_group
            target_order.ocaType = 1  # Cancel on fill
            target_order.transmit = True

            target_trade = self.ib.placeOrder(contract, target_order)
            target_id = target_trade.order.orderId

            # Brief wait and verify target order was accepted
            await _safe_sleep(0.1)

            if target_trade.orderStatus.status in ('ApiCancelled', 'Cancelled', 'Error'):
                # Target failed - BUT KEEP THE STOP (position is protected)
                logger.error(f"Target order failed for bracket {symbol}: {target_trade.orderStatus.status}")
                logger.warning(f"Stop order {stop_id} remains active - position is protected")
                # Continue with partial success - stop is more important than target

            logger.info(f"OCA protection placed: stop_id={stop_id}, target_id={target_id}, oca_group={oca_group}")

            # Track bracket group for monitoring
            bracket_group_obj = BracketGroup(
                group_id=f"BRK_{symbol}_{entry_id}",
                symbol=symbol,
                direction=direction,
                entry_order_id=entry_id,
                stop_order_id=stop_id,
                target_order_id=target_id,
                oca_group=oca_group,
                entry_price=actual_fill_price,
                stop_price=adjusted_stop,
                target_price=adjusted_target,
                quantity=filled_qty,
                status='active'
            )
            with self._order_state_lock:
                self._bracket_groups[bracket_group_obj.group_id] = bracket_group_obj

            return {
                'success': True,
                'filled': True,
                'bracket_order_id': entry_id,
                'bracket_group_id': bracket_group_obj.group_id,
                'stop_order_id': stop_id,
                'target_order_id': target_id,
                'oca_group': oca_group,
                'symbol': symbol,
                'quantity': filled_qty,
                'direction': direction,
                'entry_price': actual_fill_price,
                'stop_price': adjusted_stop,
                'target_price': adjusted_target,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"Bracket order failed for {symbol}: {e}", exc_info=True)
            # If entry already filled but protection never confirmed, flatten — do not
            # leave a naked position (mirrors the stop-placement failure path above).
            if filled_qty and exit_action and contract is not None:
                logger.critical(
                    f"POSITION AT RISK: Entry filled for {symbol} but bracket failed "
                    f"before protection confirmed ({e}). Placing emergency market exit."
                )
                emergency_info: Dict[str, Any] = {"attempted": True}
                try:
                    emergency_order = Order()
                    emergency_order.action = exit_action
                    emergency_order.totalQuantity = int(filled_qty)
                    emergency_order.orderType = "MKT"
                    emergency_order.tif = "GTC"
                    emergency_order.transmit = True
                    emergency_trade = self.ib.placeOrder(contract, emergency_order)
                    await _safe_sleep(2.0)
                    emergency_info.update({
                        "order_id": emergency_trade.order.orderId,
                        "status": emergency_trade.orderStatus.status,
                    })
                    logger.critical(
                        f"Emergency market exit placed for {symbol}: "
                        f"order_id={emergency_trade.order.orderId}, "
                        f"status={emergency_trade.orderStatus.status}"
                    )
                except Exception as emerg_err:
                    emergency_info["error"] = str(emerg_err)
                    logger.critical(f"EMERGENCY EXIT ALSO FAILED for {symbol}: {emerg_err}")
                return {
                    "success": False,
                    "filled": True,
                    "entry_price": actual_fill_price,
                    "quantity": int(filled_qty),
                    "error": str(e),
                    "symbol": symbol,
                    "emergency_exit": emergency_info,
                    "warning": (
                        "Entry filled but bracket exception before protection — "
                        "emergency market exit attempted. Verify position manually!"
                    ),
                }
            return {"error": str(e), "symbol": symbol}

    # ========== MARKET BRACKET (market entry + OCA protection) ==========

    async def place_market_bracket(
        self,
        symbol: str,
        quantity: int,
        direction: str,
        stop_price: float,
        target_price: float
    ) -> Dict[str, Any]:
        """
        Market entry, then OCA stop loss + take profit sized to the actual fill.

        Guarantees no naked position: if the entry fills but protection cannot
        be placed, the position is closed with an emergency market order.
        """
        entry_action = 'BUY' if direction == 'LONG' else 'SELL'

        entry = await self.place_market_order(symbol, entry_action, quantity, wait_for_fill=True, timeout=30.0)
        if entry.get('error') or not entry.get('success'):
            return entry
        if not entry.get('filled'):
            # Last chance: position may already exist even if wait reported miss.
            reconciled = await self._reconcile_market_fill(
                symbol=symbol,
                action=entry_action,
                quantity=quantity,
                order_id=entry.get('order_id'),
            )
            if reconciled.get('filled'):
                entry.update(reconciled)
                logger.warning(
                    "market_bracket: fill wait missed but position/fill reconciled for %s",
                    symbol,
                )
            else:
                order_id = entry.get('order_id')
                if order_id:
                    try:
                        await self.cancel_order(order_id)
                    except Exception:
                        pass
                return {
                    'success': False,
                    'filled': False,
                    'reason': f"Market entry not filled within timeout ({entry.get('fill_status')})",
                    'symbol': symbol,
                }

        filled_qty = int(entry.get('filled_quantity') or quantity)
        fill_price = entry.get('avg_fill_price')

        protection = await self.place_oca(symbol, filled_qty, direction, stop_price, target_price)
        if protection.get('error') or not protection.get('success'):
            logger.critical(
                f"POSITION AT RISK: market entry filled for {symbol} but OCA protection failed. "
                f"Placing emergency market exit."
            )
            exit_action = 'SELL' if direction == 'LONG' else 'BUY'
            emergency = await self.place_market_order(symbol, exit_action, filled_qty, wait_for_fill=True)
            return {
                'success': False,
                'filled': True,
                'entry_price': fill_price,
                'quantity': filled_qty,
                'error': f"Protection failed: {protection.get('error') or 'OCA rejected'}",
                'emergency_exit': emergency,
                'symbol': symbol,
                'warning': 'Entry filled but protection failed — emergency exit attempted. Verify position manually!'
            }

        return {
            'success': True,
            'filled': True,
            'symbol': symbol,
            'direction': direction,
            'quantity': filled_qty,
            'entry_price': fill_price,
            'stop_price': stop_price,
            'target_price': target_price,
            'entry_order_id': entry.get('order_id'),
            'stop_order_id': protection.get('stop_order_id'),
            'target_order_id': protection.get('target_order_id'),
            'oca_group': protection.get('oca_group'),
            'timestamp': datetime.now(timezone.utc).isoformat()
        }

    # ========== OCA PROTECTIVE ORDERS ==========

    async def place_oca(
        self,
        symbol: str,
        quantity: int,
        direction: str,
        stop_price: float,
        target_price: float
    ) -> Dict[str, Any]:
        """
        Place OCA (One-Cancels-All) protective orders.
        When stop or target fills, the other is cancelled.
        Use this to add protection to an existing position.
        """
        from abcxauto.broker.order_types import IBKROrderType

        contract = await self._prepare_contract(symbol)
        if contract is None:
            return {'error': 'Not connected'}

        try:
            oca_group = f"OCA_{symbol}_{int(datetime.now().timestamp())}"
            exit_action = 'SELL' if direction == 'LONG' else 'BUY'

            # Stop order
            stop_order = Order()
            stop_order.action = exit_action
            stop_order.totalQuantity = quantity
            stop_order.orderType = IBKROrderType.STOP.value
            stop_order.auxPrice = stop_price
            stop_order.tif = 'GTC'
            stop_order.ocaGroup = oca_group
            stop_order.ocaType = 1
            stop_order.transmit = True

            # Target order
            target_order = Order()
            target_order.action = exit_action
            target_order.totalQuantity = quantity
            target_order.orderType = IBKROrderType.LIMIT.value
            target_order.lmtPrice = target_price
            target_order.tif = 'GTC'
            target_order.ocaGroup = oca_group
            target_order.ocaType = 1
            target_order.transmit = True

            stop_trade = self.ib.placeOrder(contract, stop_order)
            stop_id = stop_trade.order.orderId

            # Brief wait to ensure stop order is accepted before placing target
            await _safe_sleep(0.1)

            # Check if stop order was rejected before placing target
            if stop_trade.orderStatus.status in ('ApiCancelled', 'Cancelled', 'Error'):
                logger.error(f"Stop order failed for {symbol}: {stop_trade.orderStatus.status}")
                return {
                    'success': False,
                    'error': f"Stop order failed: {stop_trade.orderStatus.status}",
                    'symbol': symbol
                }

            target_trade = self.ib.placeOrder(contract, target_order)
            target_id = target_trade.order.orderId

            # Brief wait and verify target order was accepted
            await _safe_sleep(0.1)

            if target_trade.orderStatus.status in ('ApiCancelled', 'Cancelled', 'Error'):
                # Target failed — KEEP the working stop so the position is not naked.
                logger.error(f"Target order failed for {symbol}: {target_trade.orderStatus.status}")
                logger.warning(
                    f"PARTIAL OCA: {symbol} has a working STOP (id={stop_id} @ {stop_price}) "
                    f"but NO TARGET. Position is protected on the downside only — "
                    f"propose modify_target / oca to add take-profit."
                )
                return {
                    'success': True,
                    'partial': True,
                    'warning': (
                        f"Target order failed ({target_trade.orderStatus.status}); "
                        f"stop kept working (id={stop_id})"
                    ),
                    'oca_group': oca_group,
                    'stop_order_id': stop_id,
                    'target_order_id': None,
                    'symbol': symbol,
                    'quantity': quantity,
                    'direction': direction,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                }

            logger.info(f"OCA orders placed for {symbol}: stop=${stop_price:.2f} (id={stop_id}), target=${target_price:.2f} (id={target_id})")

            return {
                'success': True,
                'oca_group': oca_group,
                'stop_order_id': stop_trade.order.orderId,
                'target_order_id': target_trade.order.orderId,
                'symbol': symbol,
                'quantity': quantity,
                'direction': direction,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"OCA orders failed for {symbol}: {e}")
            return {'error': str(e), 'symbol': symbol}

    # Alias for backward compatibility
    place_oca_orders = place_oca

    # ========== SIMPLE STOP ORDER ==========

    async def place_stop_order(
        self, symbol: str, action: str, quantity: int, stop_price: float
    ) -> Dict[str, Any]:
        """Place a simple stop order for position protection."""
        from abcxauto.broker.order_types import IBKROrderType
        return await self._place_order(
            symbol, action, quantity, IBKROrderType.STOP.value, tif='GTC', aux_price=stop_price
        )

    # ========== STOP-LIMIT ORDER ==========

    async def place_stop_limit(
        self, symbol: str, action: str, quantity: int,
        stop_price: float, limit_price: float, tif: str = 'GTC'
    ) -> Dict[str, Any]:
        """Place a stop-limit order. Triggers at stop_price, executes at limit_price."""
        from abcxauto.broker.connector import OrderState, OrderStatus

        contract = await self._prepare_contract(symbol)
        if contract is None:
            return {'error': 'Not connected'}

        try:
            order = Order()
            order.action = action
            order.totalQuantity = quantity
            order.orderType = 'STP LMT'
            order.auxPrice = stop_price
            order.lmtPrice = limit_price
            order.tif = tif
            order.transmit = True

            trade = self.ib.placeOrder(contract, order)

            # Check for immediate broker rejection
            rejection = await self._check_order_rejection(trade, 'Stop-limit', symbol)
            if rejection:
                return rejection

            # Track order state
            with self._order_state_lock:
                self._order_states[trade.order.orderId] = OrderState(
                    order_id=trade.order.orderId, symbol=symbol, action=action,
                    quantity=quantity, order_type='STP LMT',
                    stop_price=stop_price, limit_price=limit_price, status=OrderStatus.SUBMITTED
                )

            logger.info(f"Stop-limit order placed: {action} {quantity} {symbol} stop=${stop_price:.2f} limit=${limit_price:.2f}")
            return {
                'success': True, 'order_id': trade.order.orderId, 'symbol': symbol,
                'action': action, 'quantity': quantity, 'stop_price': stop_price,
                'limit_price': limit_price, 'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"Stop-limit order failed for {symbol}: {e}")
            return {'error': str(e), 'symbol': symbol}

    # Alias for backward compatibility
    place_stop_limit_order = place_stop_limit

    # ========== MARKET ORDER ==========

    async def place_market_order(
        self, symbol: str, action: str, quantity: int,
        wait_for_fill: bool = True, timeout: float = 10.0
    ) -> Dict[str, Any]:
        """Place a market order with optional fill waiting.

        Fast paper fills often leave ``openTrades()`` empty immediately — we also
        look up the Trade in ``ib.trades()`` / fills so brackets do not abort
        after a real fill and leave a naked position.
        """
        result = await self._place_order(
            symbol, action, quantity, 'MKT',
            order_name='MARKET',
        )
        if not result.get('success'):
            return result

        if wait_for_fill:
            order_id = result.get('order_id')
            trade = self._find_trade_by_order_id(order_id) if order_id else None
            if trade is not None:
                fill_result = await self._wait_for_fill(trade, timeout=timeout)
                result.update({
                    'filled': fill_result['filled'],
                    'fill_status': fill_result['status'],
                    'avg_fill_price': fill_result['avg_fill_price'],
                    'filled_quantity': fill_result['filled_quantity'],
                })
                if fill_result['filled']:
                    logger.info(
                        f"Market order FILLED: {action} {fill_result['filled_quantity']} "
                        f"{symbol} @ ${fill_result['avg_fill_price']:.2f}"
                    )
                else:
                    # Wait timed out / cancelled — still check fills & positions.
                    reconciled = await self._reconcile_market_fill(
                        symbol=symbol,
                        action=action,
                        quantity=quantity,
                        order_id=order_id,
                    )
                    if reconciled.get('filled'):
                        result.update(reconciled)
                        logger.warning(
                            f"Market order fill-wait missed but reconciled for {symbol}"
                        )
            else:
                # Trade already gone from open book — reconcile via fills / positions.
                reconciled = await self._reconcile_market_fill(
                    symbol=symbol,
                    action=action,
                    quantity=quantity,
                    order_id=order_id,
                )
                result.update(reconciled)
                if reconciled.get('filled'):
                    logger.info(
                        f"Market order FILLED (reconciled): {action} "
                        f"{reconciled.get('filled_quantity')} {symbol} "
                        f"@ ${reconciled.get('avg_fill_price')}"
                    )
                else:
                    logger.warning(
                        f"Market order {order_id} for {symbol}: trade not found in "
                        f"openTrades/trades and reconcile did not confirm fill"
                    )

        return result

    def _find_trade_by_order_id(self, order_id: int):
        """Find a Trade in open or completed trades by order id."""
        if order_id is None:
            return None
        try:
            for t in self.ib.openTrades():
                if t.order.orderId == order_id:
                    return t
        except Exception:
            pass
        try:
            for t in self.ib.trades():
                if t.order.orderId == order_id:
                    return t
        except Exception:
            pass
        return None

    async def _reconcile_market_fill(
        self,
        *,
        symbol: str,
        action: str,
        quantity: int,
        order_id: int | None,
    ) -> Dict[str, Any]:
        """Confirm a market fill when the Trade left openTrades too fast."""
        sym = str(symbol or "").upper()
        # Prefer execution report for this order id.
        try:
            for t in self.ib.trades():
                if order_id is not None and t.order.orderId != order_id:
                    continue
                status = getattr(t.orderStatus, "status", "") or ""
                filled_qty = int(getattr(t.orderStatus, "filled", 0) or 0)
                avg = getattr(t.orderStatus, "avgFillPrice", None)
                if status == "Filled" or filled_qty > 0:
                    return {
                        "filled": True,
                        "fill_status": status or "Filled",
                        "avg_fill_price": float(avg) if avg else None,
                        "filled_quantity": filled_qty or int(quantity),
                        "reconciled": True,
                    }
        except Exception as exc:
            logger.debug("reconcile via trades failed: %s", exc)

        # Fall back: position in the expected direction appeared.
        try:
            positions = await self.get_positions()
        except Exception:
            positions = []
        want_sign = 1 if str(action).upper() == "BUY" else -1
        for p in positions or []:
            if str(p.get("symbol") or "").upper() != sym:
                continue
            try:
                qty = float(p.get("quantity") or p.get("position") or 0)
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue
            if (qty > 0 and want_sign > 0) or (qty < 0 and want_sign < 0):
                avg = p.get("avg_cost") or p.get("avgCost") or p.get("market_price")
                try:
                    avg_f = float(avg) if avg is not None else None
                except (TypeError, ValueError):
                    avg_f = None
                return {
                    "filled": True,
                    "fill_status": "ReconciledFromPosition",
                    "avg_fill_price": avg_f,
                    "filled_quantity": int(abs(qty)) if abs(qty) >= 1 else int(quantity),
                    "reconciled": True,
                    "conId": p.get("conId") or p.get("con_id"),
                }
        return {
            "filled": False,
            "fill_status": "NotFound",
            "avg_fill_price": None,
            "filled_quantity": 0,
            "reconciled": False,
        }

    # ========== MODIFY ORDER ==========

    def _open_trade_for(self, order_id: int):
        for trade in self.ib.openTrades():
            if _oid_matches(trade.order, order_id):
                return trade
        return None

    async def _reread_order_px(self, order_id: int, *, field: str) -> float | None:
        req = getattr(self.ib, "reqAllOpenOrdersAsync", None)
        if callable(req):
            await req()
        await _safe_sleep(0.4)
        trade = self._open_trade_for(order_id)
        if trade is None:
            return None
        raw = getattr(trade.order, field, None)
        return _finite_px(raw)

    async def modify_stop_price(
        self,
        order_id: int,
        new_stop_price: float
    ) -> Dict[str, Any]:
        """
        Modify the stop price of an existing stop order.

        Use for adjusting stops as position moves in your favor.

        Args:
            order_id: ID of order to modify
            new_stop_price: New stop price

        Returns:
            Dict with modification result
        """
        if not await self._ensure_connected():
            return {'error': 'Not connected'}

        try:
            trade = self._open_trade_for(order_id)
            if trade is None:
                return {'error': f'Order {order_id} not found'}
            trade.order.auxPrice = new_stop_price
            self.ib.placeOrder(trade.contract, trade.order)
            live = await self._reread_order_px(order_id, field="auxPrice")
            if not modify_did_stick(requested=float(new_stop_price), live=live):
                return {
                    'error': (
                        f'modify_stop did not stick: requested {new_stop_price} '
                        f'live {live}'
                    ),
                    'order_id': order_id,
                    'requested': new_stop_price,
                    'live_stop': live,
                }
            logger.info(f"Modified stop order {order_id} to ${new_stop_price:.2f}")
            return {
                'success': True,
                'order_id': order_id,
                'new_stop_price': new_stop_price,
                'live_stop': live,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"Failed to modify order {order_id}: {e}")
            return {'error': str(e)}

    async def modify_target_price(
        self,
        order_id: int,
        new_limit_price: float
    ) -> Dict[str, Any]:
        """
        Modify the limit price of an existing take-profit (limit) order.

        Use for adjusting the profit target on a protected position.
        """
        if not await self._ensure_connected():
            return {'error': 'Not connected'}

        try:
            trade = self._open_trade_for(order_id)
            if trade is None:
                return {'error': f'Order {order_id} not found'}
            trade.order.lmtPrice = new_limit_price
            self.ib.placeOrder(trade.contract, trade.order)
            live = await self._reread_order_px(order_id, field="lmtPrice")
            if not modify_did_stick(requested=float(new_limit_price), live=live):
                return {
                    'error': (
                        f'modify_target did not stick: requested {new_limit_price} '
                        f'live {live}'
                    ),
                    'order_id': order_id,
                    'requested': new_limit_price,
                    'live_lmt': live,
                }
            logger.info(f"Modified target order {order_id} to ${new_limit_price:.2f}")
            return {
                'success': True,
                'order_id': order_id,
                'new_limit_price': new_limit_price,
                'live_lmt': live,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"Failed to modify order {order_id}: {e}")
            return {'error': str(e)}

    # ========== ADVANCED / AUCTION / ALGO ORDER TYPES (ABC parity) ==========

    async def place_trailing_stop(
        self, symbol: str, quantity: int, direction: str,
        trail_amount: float = None, trail_percent: float = None
    ) -> Dict[str, Any]:
        """Place trailing stop order with fixed amount or percentage."""
        from abcxauto.broker.order_types import IBKROrderType

        if trail_amount is None and trail_percent is None:
            return {'error': 'Must provide trail_amount or trail_percent'}

        action = 'SELL' if direction == 'LONG' else 'BUY'
        order_attrs: Dict[str, Any] = {}
        if trail_percent:
            pct = trail_percent * 100 if trail_percent < 1 else trail_percent
            pct = max(0.1, min(pct, 99.0))
            order_attrs['trailingPercent'] = pct

        return await self._place_order(
            symbol, action, quantity, IBKROrderType.TRAIL.value, tif='GTC',
            aux_price=trail_amount if not trail_percent else 0,
            order_name='Trailing stop',
            extra_result={
                'direction': direction,
                'trail_amount': trail_amount,
                'trail_percent': trail_percent,
            },
            **order_attrs,
        )

    async def place_trailing_stop_limit(
        self, symbol: str, quantity: int, direction: str,
        trail_amount: float = None, trail_percent: float = None, limit_offset: float = 0.10
    ) -> Dict[str, Any]:
        """Place trailing stop with limit protection to prevent slippage."""
        if trail_amount is None and trail_percent is None:
            return {'error': 'Must provide trail_amount or trail_percent'}

        action = 'SELL' if direction == 'LONG' else 'BUY'
        order_attrs: Dict[str, Any] = {'lmtPriceOffset': limit_offset}
        if trail_percent:
            pct = trail_percent * 100 if trail_percent < 1 else trail_percent
            pct = max(0.1, min(pct, 99.0))
            order_attrs['trailingPercent'] = pct

        contract = await self._prepare_contract(symbol)
        if contract is None:
            return {'error': 'Not connected'}
        try:
            t = self.ib.reqMktData(contract, '', True, False)
            await _safe_sleep(1.0)
            px = t.last or t.close
            if (px is None or float(px) <= 0) and t.bid and t.ask and float(t.bid) > 0 and float(t.ask) > 0:
                px = (float(t.bid) + float(t.ask)) / 2.0
            if px and float(px) > 0:
                px = float(px)
                d = (direction or "LONG").upper()
                if d == "LONG":
                    order_attrs["trailStopPrice"] = round(px * 0.97, 2)
                else:
                    order_attrs["trailStopPrice"] = round(px * 1.03, 2)
        except Exception as e:
            logger.warning("place_trailing_stop_limit: could not seed trailStopPrice: %s", e)
        finally:
            try:
                self.ib.cancelMktData(contract)
            except Exception:
                pass

        if "trailStopPrice" not in order_attrs:
            return {
                "error": "Could not determine trailStopPrice (no quote). "
                         "Try again with live market data for the symbol.",
                "symbol": symbol,
            }

        return await self._place_order(
            symbol, action, quantity, 'TRAIL LIMIT', tif='GTC',
            aux_price=trail_amount if not trail_percent else None,
            order_name='Trailing stop limit',
            extra_result={
                'direction': direction, 'trail_amount': trail_amount,
                'trail_percent': trail_percent, 'limit_offset': limit_offset,
            },
            **order_attrs,
        )

    async def place_market_on_close(self, symbol: str, action: str, quantity: int) -> Dict[str, Any]:
        """Place Market-on-Close (MOC) order."""
        return await self._place_order(symbol, action, quantity, 'MOC', order_name='MOC')

    async def place_limit_on_close(
        self, symbol: str, action: str, quantity: int, limit_price: float
    ) -> Dict[str, Any]:
        """Place Limit-on-Close (LOC) order."""
        return await self._place_order(
            symbol, action, quantity, 'LOC', limit_price=limit_price, order_name='LOC'
        )

    async def place_market_on_open(self, symbol: str, action: str, quantity: int) -> Dict[str, Any]:
        """Place Market-on-Open (MOO) — MKT + tif=OPG."""
        return await self._place_order(symbol, action, quantity, 'MKT', tif='OPG', order_name='MOO')

    async def place_limit_on_open(
        self, symbol: str, action: str, quantity: int, limit_price: float
    ) -> Dict[str, Any]:
        """Place Limit-on-Open (LOO) — LMT + tif=OPG."""
        return await self._place_order(
            symbol, action, quantity, 'LMT', tif='OPG',
            limit_price=limit_price, order_name='LOO',
        )

    async def place_adaptive(
        self, symbol: str, action: str, quantity: int,
        order_type: str = 'MKT', limit_price: float = None, priority: str = 'Normal'
    ) -> Dict[str, Any]:
        """Place IBKR Adaptive algo. Priority: Patient/Normal/Urgent."""
        if order_type == 'LMT' and limit_price is None:
            return {'error': 'Limit price required for LMT adaptive orders'}
        return await self._place_order(
            symbol, action, quantity, order_type, limit_price=limit_price,
            order_name=f'Adaptive {order_type}',
            extra_result={'priority': priority},
            algoStrategy='Adaptive',
            algoParams=[TagValue('adaptivePriority', priority)],
        )

    place_adaptive_order = place_adaptive

    async def place_midprice(
        self, symbol: str, action: str, quantity: int, price_cap: float = None
    ) -> Dict[str, Any]:
        """Place Midprice order pegged to bid/ask midpoint."""
        return await self._place_order(
            symbol, action, quantity, 'MIDPRICE',
            limit_price=price_cap, order_name='Midprice',
            extra_result={'price_cap': price_cap} if price_cap else None,
        )

    place_midprice_order = place_midprice

    async def place_relative(
        self, symbol: str, action: str, quantity: int,
        offset: float = 0.01, limit_price: float = None
    ) -> Dict[str, Any]:
        """Place Relative (REL) order pegged to bid/ask with offset."""
        return await self._place_order(
            symbol, action, quantity, 'REL', aux_price=offset, limit_price=limit_price,
            order_name='Relative', extra_result={'offset': offset},
        )

    place_relative_order = place_relative

    async def place_limit_order_gtd(
        self, symbol: str, action: str, quantity: int,
        limit_price: float, good_till_date: str
    ) -> Dict[str, Any]:
        """Place Good-Till-Date limit. Format: 'YYYYMMDD HH:MM:SS'."""
        return await self._place_order(
            symbol, action, quantity, 'LMT', tif='GTD', limit_price=limit_price,
            extra_result={'tif': 'GTD', 'good_till_date': good_till_date},
            goodTillDate=good_till_date,
        )

    async def place_fill_or_kill(
        self, symbol: str, action: str, quantity: int, limit_price: float
    ) -> Dict[str, Any]:
        """Place Fill-or-Kill (FOK) order."""
        return await self._place_order_with_fill(
            symbol, action, quantity, 'FOK', 'FOK', limit_price, timeout=2.0, order_name='FOK'
        )

    async def place_immediate_or_cancel(
        self, symbol: str, action: str, quantity: int, limit_price: float
    ) -> Dict[str, Any]:
        """Place Immediate-or-Cancel (IOC) order."""
        return await self._place_order_with_fill(
            symbol, action, quantity, 'IOC', 'IOC', limit_price, timeout=2.0, order_name='IOC'
        )

    async def place_vwap(
        self, symbol: str, action: str, quantity: int,
        start_time: str = None, end_time: str = None, max_pct_volume: float = 25.0
    ) -> Dict[str, Any]:
        """Place VWAP algo order."""
        algo_params = [
            TagValue('maxPctVol', str(max_pct_volume / 100)),
            TagValue('noTakeLiq', '0'),
        ]
        if start_time:
            algo_params.append(TagValue('startTime', start_time))
        if end_time:
            algo_params.append(TagValue('endTime', end_time))
        return await self._place_order(
            symbol, action, quantity, 'MKT', order_name='VWAP',
            extra_result={
                'max_pct_volume': max_pct_volume,
                'start_time': start_time,
                'end_time': end_time,
            },
            algoStrategy='Vwap', algoParams=algo_params,
        )

    place_vwap_order = place_vwap

    async def place_twap(
        self, symbol: str, action: str, quantity: int,
        start_time: str = None, end_time: str = None
    ) -> Dict[str, Any]:
        """Place TWAP algo order."""
        algo_params = [TagValue('allowPastEndTime', '1')]
        if start_time:
            algo_params.append(TagValue('startTime', start_time))
        if end_time:
            algo_params.append(TagValue('endTime', end_time))
        return await self._place_order(
            symbol, action, quantity, 'MKT', order_name='TWAP',
            extra_result={'start_time': start_time, 'end_time': end_time},
            algoStrategy='Twap', algoParams=algo_params,
        )

    place_twap_order = place_twap

    async def place_iceberg_order(
        self, symbol: str, action: str, total_quantity: int,
        display_size: int, limit_price: float
    ) -> Dict[str, Any]:
        """Place Iceberg order (displaySize hides rest of qty)."""
        return await self._place_order(
            symbol, action, total_quantity, 'LMT', limit_price=limit_price,
            order_name='Iceberg',
            extra_result={'total_quantity': total_quantity, 'display_size': display_size},
            displaySize=display_size,
        )

    async def place_snap_to_midpoint(self, symbol: str, action: str, quantity: int) -> Dict[str, Any]:
        """Place Snap-to-Midpoint order."""
        return await self._place_order(symbol, action, quantity, 'SNAP MID', order_name='Snap-to-Mid')

    # ========== OPTION POSITION CLOSE ==========

    async def close_option_position(
        self,
        symbol: str,
        expiration: str = None,
        strike: float = None,
        right: str = None,
        contract: Contract = None,
        quantity: int = None,
        limit_price: float = None,
        reason: str = ''
    ) -> Dict[str, Any]:
        """Close an existing option position (moved from options mixin)."""
        if not await self._ensure_connected():
            return {'error': 'Not connected'}

        try:
            if contract is None:
                positions = self.ib.positions()
                for pos in positions:
                    c = pos.contract
                    if c.symbol == symbol and c.secType == 'OPT':
                        if expiration and c.lastTradeDateOrContractMonth != expiration:
                            continue
                        if strike and abs(c.strike - strike) > 0.01:
                            continue
                        if right and c.right != right:
                            continue
                        contract = c
                        if quantity is None:
                            quantity = abs(int(pos.position))
                        break

            if contract is None:
                return {'error': f'No option position found for {symbol} {right} {strike} {expiration}'}

            if not contract.exchange:
                contract.exchange = 'SMART'

            positions = self.ib.positions()
            current_qty = 0
            for pos in positions:
                if pos.contract.conId == contract.conId:
                    current_qty = int(pos.position)
                    break

            action = 'SELL' if current_qty > 0 else 'BUY'
            close_qty = quantity or abs(current_qty)

            if limit_price is None:
                try:
                    fn = getattr(self, "get_live_option_quote", None)
                    if callable(fn):
                        live = await fn(
                            contract.symbol,
                            contract.lastTradeDateOrContractMonth,
                            contract.strike,
                            contract.right,
                        )
                        from abcxauto.prints import live_limit_px

                        limit_price = live_limit_px(live if isinstance(live, dict) else None)
                        if limit_price:
                            logger.info(
                                "IBKR mid for close %s: %s",
                                getattr(contract, "conId", None),
                                limit_price,
                            )
                except Exception as e:
                    logger.debug(f"IBKR close quote failed for {symbol}: {e}")
                if not limit_price:
                    limit_price = None  # MKT fallback only as last resort

            order = Order()
            order.action = action
            order.totalQuantity = close_qty
            order.orderType = 'LMT' if limit_price else 'MKT'
            if limit_price:
                order.lmtPrice = limit_price
            order.tif = 'DAY'
            order.transmit = True

            trade = self.ib.placeOrder(contract, order)

            logger.info(f"Close option: {action} {close_qty} {symbol} "
                       f"{contract.strike}{contract.right} {contract.lastTradeDateOrContractMonth} "
                       f"{'LMT@' + str(limit_price) if limit_price else 'MKT'} [{reason}]")

            rejection = await self._check_order_rejection(
                trade, f'Close {symbol} {contract.strike}{contract.right}', symbol
            )
            if rejection:
                return rejection

            return {
                'success': True,
                'order_id': trade.order.orderId,
                'symbol': symbol,
                'action': action,
                'quantity': close_qty,
                'strike': contract.strike,
                'right': contract.right,
                'expiration': contract.lastTradeDateOrContractMonth,
                'reason': reason,
                'status': trade.orderStatus.status,
                'timestamp': datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            logger.error(f"Close option failed for {symbol}: {e}")
            return {'error': str(e), 'symbol': symbol}
