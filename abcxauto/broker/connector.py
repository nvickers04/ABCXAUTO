"""
IBKR Core Connector - Connection Management and Base Class

This module provides the core IBKRConnector class with:
- Singleton pattern for connection management
- Thread-safe connection/disconnection
- Event handler registration
- Account/position query methods (merged from queries mixin)
- Base infrastructure for order operations

The IBKRConnector class imports the orders mixin from orders.py.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timezone
from threading import Lock

from ib_insync import IB, Order, Trade, Fill

from abcxauto.broker.connection import (
    DisconnectCause,
    TradingModePortError,
    assert_connect_allowed,
    classify_error_code,
    reconnect_backoff_seconds,
    resolve_ibkr_endpoint,
    safe_sleep as _safe_sleep,
)
from abcxauto.config import get_config

logger = logging.getLogger(__name__)


def port_is_closed(exc: BaseException) -> bool:
    """True when TWS/Gateway is not listening (not a stale client-id fight)."""
    if isinstance(exc, ConnectionRefusedError):
        return True
    errno = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if errno in (10061, 1225, 111, 61):
        return True
    text = str(exc).lower()
    return (
        "10061" in text
        or "1225" in text
        or "connection refused" in text
        or "refused the network" in text
        or "connect call failed" in text
    )


# ============================================================
# ORDER STATE TRACKING
# ============================================================

class OrderStatus(Enum):
    """Order status enumeration."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    INACTIVE = "inactive"


@dataclass
class OrderState:
    """
    Tracks state of an individual order.

    Used for monitoring bracket groups and OCA orders.
    """
    order_id: int
    symbol: str
    action: str  # 'BUY' or 'SELL'
    quantity: int
    order_type: str  # 'LMT', 'STP', 'TRAIL', etc.
    status: OrderStatus = OrderStatus.PENDING
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    trail_amount: Optional[float] = None
    trail_percent: Optional[float] = None
    filled_qty: int = 0
    avg_fill_price: Optional[float] = None
    oca_group: Optional[str] = None
    bracket_group: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'action': self.action,
            'quantity': self.quantity,
            'order_type': self.order_type,
            'status': self.status.value,
            'limit_price': self.limit_price,
            'stop_price': self.stop_price,
            'trail_amount': self.trail_amount,
            'trail_percent': self.trail_percent,
            'filled_qty': self.filled_qty,
            'avg_fill_price': self.avg_fill_price,
            'oca_group': self.oca_group,
            'bracket_group': self.bracket_group,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat()
        }


@dataclass
class BracketGroup:
    """
    Tracks a complete bracket order group (entry + stop + target).
    """
    group_id: str
    symbol: str
    direction: str  # 'LONG' or 'SHORT'
    entry_order_id: Optional[int] = None
    stop_order_id: Optional[int] = None
    target_order_id: Optional[int] = None
    oca_group: Optional[str] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    quantity: int = 0
    status: str = "pending"  # pending, active, closed_profit, closed_loss, cancelled
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'group_id': self.group_id,
            'symbol': self.symbol,
            'direction': self.direction,
            'entry_order_id': self.entry_order_id,
            'stop_order_id': self.stop_order_id,
            'target_order_id': self.target_order_id,
            'oca_group': self.oca_group,
            'entry_price': self.entry_price,
            'stop_price': self.stop_price,
            'target_price': self.target_price,
            'quantity': self.quantity,
            'status': self.status,
            'created_at': self.created_at.isoformat(),
            'closed_at': self.closed_at.isoformat() if self.closed_at else None,
            'realized_pnl': self.realized_pnl
        }


class IBKRQueriesMixin:
    """Slim account/position/order query methods (merged from queries.py)."""

    async def refresh_positions(self) -> None:
        """Force refresh position data from TWS."""
        if not self._connected:
            return
        try:
            async with self.async_lock:
                await self.ib.reqPositionsAsync()
                logger.debug("Position data refreshed from TWS")
        except Exception as e:
            logger.warning(f"Position refresh failed: {e}")

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions with P/L data (refreshes from TWS first)."""
        if not await self._ensure_connected():
            return []

        try:
            await self.refresh_positions()
            await _safe_sleep(0.3)

            portfolio_items = self.ib.portfolio()
            if not portfolio_items:
                await _safe_sleep(0.5)
                portfolio_items = self.ib.portfolio()

            positions = []
            for item in portfolio_items:
                if item.position == 0:
                    continue

                contract = item.contract
                sec_type = contract.secType or 'STK'
                pos_data = {
                    'symbol': contract.symbol,
                    'quantity': item.position,
                    'avg_cost': item.averageCost,
                    'market_value': item.marketValue,
                    'unrealized_pnl': item.unrealizedPNL,
                    'realized_pnl': item.realizedPNL,
                    'market_price': item.marketPrice,
                    'sec_type': sec_type,
                    'con_id': contract.conId,
                    'conId': contract.conId,
                }
                if sec_type == 'OPT':
                    pos_data.update({
                        'strike': contract.strike,
                        'expiration': contract.lastTradeDateOrContractMonth,
                        'right': contract.right,
                        'multiplier': int(contract.multiplier or 100),
                        'local_symbol': contract.localSymbol,
                    })
                positions.append(pos_data)
            return positions
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    async def get_account_summary(self) -> Dict[str, Any]:
        """Get account summary."""
        if not await self._ensure_connected():
            return {'error': 'Not connected'}

        try:
            async with self.async_lock:
                account_values = self.ib.accountValues()
                result = {'account_id': self.account_id}
                target_tags = {
                    'NetLiquidation',
                    'TotalCashValue',
                    'AvailableFunds',
                    'DailyPnL',
                    'UnrealizedPnL',
                    'RealizedPnL',
                }
                for av in account_values:
                    if av.tag in target_tags and av.currency == 'USD':
                        result[av.tag.lower()] = float(av.value)
                self._apply_req_pnl(result)
                return result
        except Exception as e:
            logger.error(f"Failed to get account summary: {e}")
            return {'error': str(e)}

    async def cancel_order(self, order_id: int) -> Dict[str, Any]:
        """Cancel an open order."""
        if not await self._ensure_connected():
            return {'error': 'Not connected'}

        try:
            for trade in self.ib.openTrades():
                if trade.order.orderId == order_id:
                    if hasattr(self, "_cancel_order_with_tracking"):
                        self._cancel_order_with_tracking(trade.order, source="cancel_order")
                    else:
                        self.ib.cancelOrder(trade.order)
                    return {'success': True, 'order_id': order_id}
            return {'error': f'Order {order_id} not found'}
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return {'error': str(e)}

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get all open orders including from other client sessions."""
        if not await self._ensure_connected():
            return []

        ACTIVE_STATUSES = {'PreSubmitted', 'Submitted', 'PendingSubmit'}
        try:
            await self.ib.reqAllOpenOrdersAsync()
            await _safe_sleep(0.3)

            orders = []
            for t in self.ib.openTrades():
                status = t.orderStatus.status
                if status not in ACTIVE_STATUSES:
                    continue

                lmt_price = t.order.lmtPrice
                if lmt_price > 1e300:
                    lmt_price = None

                trail_pct = getattr(t.order, 'trailingPercent', None)
                aux = t.order.auxPrice
                if t.order.orderType == 'TRAIL' and trail_pct:
                    aux = None

                sec_type = t.contract.secType or 'STK'
                order_data = {
                    'order_id': t.order.orderId,
                    'symbol': t.contract.symbol,
                    'sec_type': sec_type,
                    'action': t.order.action,
                    'quantity': t.order.totalQuantity,
                    'order_type': t.order.orderType,
                    'aux_price': aux,
                    'lmt_price': lmt_price,
                    'status': status,
                    'con_id': t.contract.conId,
                    'conId': t.contract.conId,
                }
                if sec_type == 'OPT':
                    order_data.update({
                        'strike': t.contract.strike,
                        'expiration': t.contract.lastTradeDateOrContractMonth,
                        'right': t.contract.right,
                        'multiplier': int(t.contract.multiplier or 100),
                        'local_symbol': t.contract.localSymbol,
                    })
                if trail_pct:
                    order_data['trail_percent'] = trail_pct
                orders.append(order_data)
            return orders
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    async def get_fills(self) -> List[Dict[str, Any]]:
        """Get session fills as plain dicts for the trade journal."""
        if not await self._ensure_connected():
            return []

        try:
            fills = self.ib.fills()
            out: List[Dict[str, Any]] = []
            for fill in fills:
                execution = fill.execution
                contract = fill.contract
                commission_report = getattr(fill, "commissionReport", None)

                exec_time = getattr(execution, "time", None)
                if exec_time is None:
                    ts = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%S.%f"
                    )[:-3] + "Z"
                elif getattr(exec_time, "tzinfo", None) is None:
                    ts = exec_time.replace(tzinfo=timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    )
                else:
                    ts = exec_time.astimezone(timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    )

                commission = None
                realized_pnl = None
                if commission_report is not None:
                    raw_comm = getattr(commission_report, "commission", None)
                    if raw_comm is not None:
                        try:
                            commission = float(raw_comm)
                        except (TypeError, ValueError):
                            commission = None
                    raw_pnl = getattr(commission_report, "realizedPNL", None)
                    if raw_pnl is not None:
                        try:
                            realized_pnl = float(raw_pnl)
                        except (TypeError, ValueError):
                            realized_pnl = None

                out.append({
                    "ts": ts,
                    "exec_id": getattr(execution, "execId", None),
                    "order_id": getattr(execution, "orderId", None),
                    "symbol": getattr(contract, "symbol", None),
                    "sec_type": getattr(contract, "secType", None) or "STK",
                    "conId": getattr(contract, "conId", None),
                    "con_id": getattr(contract, "conId", None),
                    "side": getattr(execution, "side", None),
                    "quantity": getattr(execution, "shares", None),
                    "price": getattr(execution, "price", None),
                    "commission": commission,
                    "realized_pnl": realized_pnl,
                })
            return out
        except Exception as e:
            logger.error(f"Failed to get fills: {e}")
            return []

    async def get_recent_executions(self) -> List[Dict[str, Any]]:
        """Get recent execution fills from IBKR."""
        if not await self._ensure_connected():
            return []

        try:
            fills = self.ib.fills()
            executions = []
            for fill in fills:
                executions.append({
                    'symbol': fill.contract.symbol,
                    'side': fill.execution.side,
                    'shares': fill.execution.shares,
                    'price': fill.execution.price,
                    'avg_price': fill.execution.avgPrice,
                    'time': fill.execution.time.isoformat() if fill.execution.time else None,
                    'order_id': fill.execution.orderId,
                    'exec_id': fill.execution.execId,
                    'commission': fill.commissionReport.commission if fill.commissionReport else 0,
                })
            return executions
        except Exception as e:
            logger.error(f"Failed to get executions: {e}")
            return []

    _QUOTE_CACHE_S = 2.5

    def _live_quote_cached(self, symbol: str) -> Optional[Dict[str, Any]]:
        bag = getattr(self, "_quote_cache", None)
        if not isinstance(bag, dict):
            return None
        hit = bag.get(symbol)
        if not hit:
            return None
        ts, payload = hit
        try:
            if time.monotonic() - float(ts) > self._QUOTE_CACHE_S:
                return None
        except (TypeError, ValueError):
            return None
        return dict(payload) if isinstance(payload, dict) else None

    def _live_quote_remember(self, symbol: str, payload: Dict[str, Any]) -> None:
        if not payload or payload.get("error"):
            return
        if payload.get("last") is None and payload.get("mid") is None:
            return
        bag = getattr(self, "_quote_cache", None)
        if not isinstance(bag, dict):
            bag = {}
            self._quote_cache = bag
        if len(bag) >= 32:
            bag.clear()
        bag[symbol] = (time.monotonic(), dict(payload))

    async def get_live_quotes(self, symbols: List[str], *, fresh: bool = False) -> Dict[str, Any]:
        """IBKR live quotes for up to 8 symbols (parallel, short cache)."""
        seen: List[str] = []
        for raw in symbols or []:
            sym = str(raw or "").strip().upper()
            if sym and sym not in seen:
                seen.append(sym)
            if len(seen) >= 8:
                break
        rows = await asyncio.gather(*[self.get_live_quote(s, fresh=fresh) for s in seen])
        return {
            "source": "ibkr",
            "freshness": "live",
            "quotes": [r for r in rows if isinstance(r, dict)],
        }

    async def get_live_quote(self, symbol: str, *, fresh: bool = False) -> Dict[str, Any]:
        """IBKR stream snapshot for STK. Live last/bid/ask for send geometry."""
        from abcxauto.broker.util import quote_from_ticker

        sym = str(symbol or "").strip().upper()
        if not sym:
            return {"error": "symbol required", "source": "ibkr"}
        if not fresh:
            cached = self._live_quote_cached(sym)
            if cached is not None:
                cached["cached"] = True
                return cached
        if not await self._ensure_connected():
            return {"error": "Not connected", "source": "ibkr", "symbol": sym}
        contract = None
        try:
            if sym in ("VIX", "^VIX"):
                try:
                    from ib_insync.contract import Index

                    idx = Index("VIX", "CBOE")
                    await self.ib.qualifyContractsAsync(idx)
                    if int(getattr(idx, "conId", 0) or 0) > 0:
                        contract = idx
                except Exception:
                    logger.debug("VIX Index qualify failed", exc_info=True)
            if contract is None:
                prepare = getattr(self, "_prepare_contract", None)
                if callable(prepare):
                    contract = await prepare(sym)
        except Exception as exc:
            logger.warning("qualify %s failed: %s", sym, exc)
            return {"error": str(exc), "source": "ibkr", "symbol": sym}
        if contract is None:
            return {"error": "qualify failed", "source": "ibkr", "symbol": sym}
        ticker = None
        try:
            req = getattr(self.ib, "reqTickersAsync", None)
            if callable(req):
                tickers = await req(contract)
                ticker = tickers[0] if tickers else None
            if ticker is None:
                ticker = self.ib.reqMktData(contract, "", True, False)
                await _safe_sleep(0.8)
            out = quote_from_ticker(ticker, symbol=sym)
            if out.get("last") is None and out.get("mid") is None:
                out["error"] = "no IBKR tick yet"
            else:
                self._live_quote_remember(sym, out)
            return out
        except Exception as exc:
            logger.warning("IBKR live quote failed for %s: %s", sym, exc)
            return {"error": str(exc), "source": "ibkr", "symbol": sym}
        finally:
            con_id = int(getattr(contract, "conId", 0) or 0)
            if con_id and con_id in getattr(self, "_book_subs", {}):
                pass
            else:
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass


# Import mixins after defining base classes to avoid circular imports
from abcxauto.broker.orders import IBKROrdersMixin
from abcxauto.broker.options import IBKROptionsMixin


class IBKRConnector(IBKROrdersMixin, IBKROptionsMixin, IBKRQueriesMixin):
    """
    IBKR connector with essential trading functionality.
    Thread-safe singleton pattern.

    Inherits from:
    - IBKROrdersMixin: Stock order placement and management
    - IBKROptionsMixin: Multi-leg / single-option strategies
    - IBKRQueriesMixin: Account and position query methods

    MRO: Orders before Options so close_option_position uses the orders
    implementation (JSON-friendly symbol/expiry/strike close).
    """

    _instance: Optional['IBKRConnector'] = None
    _lock: Lock = Lock()
    _async_lock: Optional[asyncio.Lock] = None  # Created lazily per event loop

    def __new__(cls) -> 'IBKRConnector':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    @property
    def async_lock(self) -> asyncio.Lock:
        """Get async lock, creating if needed for current event loop."""
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        return self._async_lock

    def __init__(self):
        if self._initialized:
            return

        # Resolve endpoint from environment
        self.host, self.port, self.mode = resolve_ibkr_endpoint()
        # Fixed client ID from config (IBKR_CLIENT_ID, default 42) — MUST be
        # consistent across restarts so the agent can cancel prior-session orders.
        self.client_id = int(get_config().ibkr_client_id)

        # Connection state
        self.ib = IB()
        self._connected = False
        self._connect_block = ""
        self.account_id: Optional[str] = None
        self.account_name: Optional[str] = None
        self.net_liquidation: float = 0.0
        self.cash_value: float = 0.0  # TotalCashValue - actual cash
        self.available_funds: float = 0.0  # AvailableFunds (INCLUDES MARGIN — do NOT use for order sizing, use cash_value instead)
        self.day_trades_remaining: int = 3  # PDT tracking - updated on connect

        # Active streaming subscriptions: symbol -> ticker (legacy; no public subscribe API)
        self._tickers: Dict[str, Any] = {}
        self._pnl: Any = None

        # Background heartbeat task
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None

        # Connection lifecycle (TWS restart, reconnect)
        self._disconnect_cause: str = DisconnectCause.UNKNOWN.value
        self._reconnect_requested: bool = False
        self._pending_resubscribe: set[str] = set()
        self._last_heartbeat_ok: Optional[float] = None
        self._heartbeat_failures: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._handlers_on_ib: Optional[IB] = None
        self._disconnect_since: Optional[float] = None
        self._disconnect_halt_fired: bool = False
        self._reconnect_attempt: int = 0
        self._ibkr_data_stale: bool = False
        self._book_subs: Dict[int, Any] = {}
        self._book_sub_live: set[int] = set()

        # Execution tracking - stores ALL fills with actual prices
        # Key: symbol, Value: list of execution records
        self._executions: Dict[str, List[Dict[str, Any]]] = {}
        self._execution_lock = Lock()

        # Order state tracking
        self._order_states: Dict[int, OrderState] = {}  # order_id -> OrderState
        self._bracket_groups: Dict[str, BracketGroup] = {}  # group_id -> BracketGroup
        self._order_state_lock = Lock()
        self._order_status_listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._local_cancel_requests: Dict[int, Dict[str, Any]] = {}
        self._local_cancel_lock = Lock()

        # Store strong references to event handlers (prevents weakref issues)
        self._disconnect_handler = self._on_disconnect
        self._execution_handler = self._on_execution
        self._order_status_handler = self._on_order_status
        self._error_handler = self._on_error

        # Register event handlers
        self._register_handlers()

        self._initialized = True
        logger.info(f"IBKRConnector initialized ({self.mode} mode, {self.host}:{self.port})")

    def _register_handlers(self):
        """Register event handlers on the current IB instance (idempotent)."""
        if self._handlers_on_ib is self.ib:
            return
        if self._handlers_on_ib is not None:
            self._unregister_handlers()
        self.ib.disconnectedEvent += self._disconnect_handler
        self.ib.execDetailsEvent += self._execution_handler
        self.ib.orderStatusEvent += self._order_status_handler
        self.ib.errorEvent += self._error_handler
        self._handlers_on_ib = self.ib

    def is_connected(self) -> bool:
        """Check if connected to IBKR TWS/Gateway."""
        return self._connected and self.ib.isConnected()

    def _record_local_cancel_request(self, order_id: int, source: str = "unknown") -> None:
        """Track a local cancel request so later Error 202 can be attributed."""
        if not order_id:
            return
        now = datetime.now(timezone.utc)
        with self._local_cancel_lock:
            self._local_cancel_requests[int(order_id)] = {
                "timestamp": now,
                "source": source,
            }
            # Keep map small
            stale = [
                oid for oid, meta in self._local_cancel_requests.items()
                if (now - meta.get("timestamp", now)).total_seconds() > 300
            ]
            for oid in stale:
                self._local_cancel_requests.pop(oid, None)

    def _cancel_order_with_tracking(self, order: Order, source: str = "unknown") -> None:
        """Cancel order while recording local attribution metadata."""
        order_id = int(getattr(order, "orderId", 0) or 0)
        if order_id:
            self._record_local_cancel_request(order_id, source=source)
        self.ib.cancelOrder(order)

    def get_cancel_attribution(self, order_id: int, ttl_seconds: int = 30) -> Dict[str, Any]:
        """Classify cancel as self-initiated vs broker-side using a recent local cancel ledger."""
        now = datetime.now(timezone.utc)
        with self._local_cancel_lock:
            meta = self._local_cancel_requests.get(int(order_id))

        if not meta:
            return {
                "kind": "broker_cancel",
                "order_id": int(order_id),
            }

        ts = meta.get("timestamp", now)
        age_s = max((now - ts).total_seconds(), 0.0)
        if age_s <= ttl_seconds:
            return {
                "kind": "self_cancel",
                "order_id": int(order_id),
                "source": meta.get("source", "unknown"),
                "age_seconds": round(age_s, 2),
            }

        return {
            "kind": "broker_cancel",
            "order_id": int(order_id),
            "stale_local_cancel": True,
            "age_seconds": round(age_s, 2),
        }

    def _unregister_handlers(self):
        """Safely remove event handlers from the current IB instance."""
        target = self._handlers_on_ib or self.ib
        try:
            target.disconnectedEvent -= self._disconnect_handler
        except Exception:
            pass
        try:
            target.execDetailsEvent -= self._execution_handler
        except Exception:
            pass
        try:
            target.orderStatusEvent -= self._order_status_handler
        except Exception:
            pass
        try:
            target.errorEvent -= self._error_handler
        except Exception:
            pass
        self._handlers_on_ib = None

    # ── Noisy IBKR error codes to suppress (log at DEBUG instead of WARNING) ──
    _SUPPRESSED_ERROR_CODES = {
        10168,  # Market data subscription not found (no IBKR data entitlement — we use external)
        10147,  # OrderId not found (stale cancel on old/filled orders)
        165,    # HMDS / scanner subscription messages (cancel between arena scans)
        2104,   # Market data farm connection is OK
        2106,   # HMDS data farm connection is OK
        2158,   # Sec-def data farm connection is OK
    }

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: str) -> None:
        """Handle IBKR error/warning events. Suppress noisy codes to DEBUG."""
        lifecycle = classify_error_code(errorCode)
        if lifecycle == "tws_lost":
            self._ibkr_data_stale = True
            logger.warning(
                f"IBKR↔TWS link lost [{errorCode}]: {errorString} — "
                "keep API socket; no new IB()"
            )
            return
        if lifecycle == "tws_restored":
            self._ibkr_data_stale = False
            logger.info(
                f"IBKR connectivity restored [{errorCode}]: {errorString}"
                + (" (data lost)" if errorCode == 1101 else "")
            )
            return
        if lifecycle == "farm_ok":
            logger.debug(f"IBKR data farm OK [{errorCode}]: {errorString}")
            return
        if errorCode == 10141 or "disclaimer must first be accepted" in str(errorString or "").lower():
            self._connect_block = "paper_disclaimer"
            logger.error("IBKR paper API disclaimer not accepted — stop walking client ids")
            return

        msg_l = str(errorString or "").lower()
        if "scanner subscription" in msg_l and "cancel" in msg_l:
            logger.debug(f"IBKR [{errorCode}] scanner settle: {errorString}")
            return

        if errorCode in self._SUPPRESSED_ERROR_CODES:
            logger.debug(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")
        elif errorCode == 202:  # Order cancelled — check attribution
            attr = self.get_cancel_attribution(reqId)
            if attr.get('kind') == 'self_cancel':
                logger.debug(f"IBKR [202] self-cancel confirmed: order {reqId}")
            else:
                logger.warning(f"IBKR [202] broker-cancel: order {reqId} — {errorString}")
        elif errorCode in (201, 10198):
            logger.warning(f"IBKR [{errorCode}] order rejected reqId={reqId}: {errorString}")
        else:
            logger.info(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")

    def _on_disconnect(self) -> None:
        """Handle disconnection from TWS/Gateway (API socket closed)."""
        symbols = list(self._tickers.keys())
        if symbols:
            self._pending_resubscribe.update(s.upper() for s in symbols)
        self._tickers.clear()
        drop_rt = getattr(self, "abandon_realtime_bars", None)
        if callable(drop_rt):
            try:
                drop_rt()
            except Exception:
                pass
        self._clear_book_subs(cancel=False)
        self._connected = False
        cause = self._disconnect_cause
        if cause == DisconnectCause.USER_DISCONNECT.value:
            logger.info(
                f"IBKR disconnected (user-requested, client_id={self.client_id}, "
                f"{self.host}:{self.port})"
            )
            return
        if cause == DisconnectCause.UNKNOWN.value:
            cause = DisconnectCause.TWS_RESTART.value
            self._disconnect_cause = cause
        if self._disconnect_since is None:
            self._disconnect_since = time.monotonic()
            self._disconnect_halt_fired = False
            self._reconnect_attempt = 0
        logger.warning(
            f"IBKR disconnected (cause={cause}, client_id={self.client_id}, "
            f"{self.host}:{self.port}, resubscribe_pending={len(self._pending_resubscribe)})"
        )
        self._schedule_reconnect(cause)

    def _resolve_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Prefer the running loop; fall back to the loop captured at connect."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            pass
        loop = self._loop
        if loop is not None and not loop.is_closed():
            return loop
        return None

    def _schedule_reconnect(self, reason: str) -> None:
        """Kick off reconnect on the connector loop without blocking callers."""
        if self._disconnect_cause == DisconnectCause.USER_DISCONNECT.value:
            return
        self._reconnect_requested = True
        if self._disconnect_cause == DisconnectCause.UNKNOWN.value:
            self._disconnect_cause = reason
        if self._disconnect_since is None:
            self._disconnect_since = time.monotonic()
            self._disconnect_halt_fired = False
            self._reconnect_attempt = 0

        loop = self._resolve_loop()
        if loop is None:
            logger.warning("Cannot schedule IBKR reconnect — no event loop available")
            return

        def _start() -> None:
            if self._reconnect_task and not self._reconnect_task.done():
                return
            self._reconnect_task = loop.create_task(
                self._reconnect_after_disconnect(reason)
            )

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _start()
        else:
            loop.call_soon_threadsafe(_start)

    def _maybe_halt_on_prolonged_disconnect(self) -> None:
        """Latch risk gate if still disconnected past configured threshold."""
        if self._disconnect_halt_fired or self._disconnect_since is None:
            return
        try:
            threshold = float(get_config().disconnect_halt_s)
        except Exception:
            threshold = 120.0
        if threshold <= 0:
            return
        elapsed = time.monotonic() - self._disconnect_since
        if elapsed < threshold:
            return
        self._disconnect_halt_fired = True
        reason = f"broker disconnected >{int(threshold)}s"
        logger.critical(
            f"IBKR still disconnected after {elapsed:.0f}s "
            f"(threshold={threshold:.0f}s) — risk gate HALT: {reason}"
        )
        try:
            from abcxauto.risk_gates import get_risk_gate

            get_risk_gate().halt(reason, kind="disconnect")
        except Exception as e:
            logger.critical(f"Failed to halt risk gate after disconnect: {e}")

    async def _reconnect_after_disconnect(self, reason: str) -> None:
        """Background reconnect with exponential backoff; may halt after threshold."""
        try:
            while True:
                if self._disconnect_cause == DisconnectCause.USER_DISCONNECT.value:
                    return
                if self.connected:
                    self._reconnect_requested = False
                    return

                self._maybe_halt_on_prolonged_disconnect()

                backoff = reconnect_backoff_seconds(self._reconnect_attempt)
                logger.info(
                    f"Reconnect backoff {backoff:.1f}s "
                    f"(attempt={self._reconnect_attempt}, reason={reason})"
                )
                await _safe_sleep(backoff)

                if self._disconnect_cause == DisconnectCause.USER_DISCONNECT.value:
                    return
                if self.connected:
                    self._reconnect_requested = False
                    return

                self._maybe_halt_on_prolonged_disconnect()
                logger.info(
                    f"Reconnect attempt (reason={reason}, client_id={self.client_id}, "
                    f"attempt={self._reconnect_attempt})"
                )
                try:
                    ok = await self.connect()
                except TradingModePortError as e:
                    logger.critical(f"Reconnect blocked by mode/port guard: {e}")
                    self._maybe_halt_on_prolonged_disconnect()
                    return
                except Exception as e:
                    ok = False
                    self._heartbeat_failures += 1
                    logger.error(f"Reconnect error (reason={reason}): {e}")

                if ok:
                    try:
                        from abcxauto.risk_gates import get_risk_gate

                        gate = get_risk_gate()
                        if gate.is_halted:
                            logger.warning(
                                f"IBKR reconnected successfully (reason={reason}, "
                                f"client_id={self.client_id}), but risk-gate halt "
                                f"remains (kind={gate.halt_kind!r}: {gate.halt_reason}). "
                                "Manual resume() required before new entries."
                            )
                        else:
                            logger.info(
                                f"IBKR reconnected successfully (reason={reason}, "
                                f"client_id={self.client_id})."
                            )
                    except Exception:
                        logger.info(
                            f"IBKR reconnected successfully (reason={reason}, "
                            f"client_id={self.client_id}). "
                            "Any risk-gate halt remains until human/monitor resume."
                        )
                    await self._after_connect_restore()
                    return

                self._reconnect_attempt += 1
                self._heartbeat_failures += 1
                logger.error(
                    f"Reconnect failed (reason={reason}, "
                    f"attempt={self._reconnect_attempt})"
                )
        except asyncio.CancelledError:
            raise
        finally:
            self._reconnect_task = None

    async def _after_connect_restore(self) -> None:
        """Resubscribe market data after TWS/Gateway reconnect.

        Does not clear a risk-gate halt — human/monitor must resume.
        """
        self._disconnect_cause = DisconnectCause.UNKNOWN.value
        self._reconnect_requested = False
        self._heartbeat_failures = 0
        self._reconnect_attempt = 0
        self._disconnect_since = None
        # Leave _disconnect_halt_fired as-is so we do not re-halt on a later blip
        # in the same outage window; a fresh disconnect resets it in _on_disconnect.
        self._last_heartbeat_ok = time.time()

        # Streaming subscribe API removed; nothing to restore.
        n = len(self._pending_resubscribe)
        self._pending_resubscribe.clear()
        if n:
            logger.info(f"Cleared {n} pending market-data resubscribe symbol(s)")

    def _on_execution(self, trade: Trade, fill: Fill) -> None:
        """
        Handle execution event - stores actual fill data.

        This is event-driven, so we capture EVERY fill as it happens.
        No more estimated exit prices!
        """
        try:
            symbol = trade.contract.symbol
            execution = fill.execution
            commission = fill.commissionReport.commission if fill.commissionReport else 0

            exec_record = {
                'symbol': symbol,
                'side': execution.side,  # 'BOT' or 'SLD'
                'shares': int(execution.shares),
                'price': float(execution.price),
                'avg_price': float(execution.avgPrice),
                'time': execution.time.isoformat() if execution.time else datetime.now(timezone.utc).isoformat(),
                'order_id': execution.orderId,
                'exec_id': execution.execId,
                'commission': commission,
                'order_type': trade.order.orderType,
                'oca_group': trade.order.ocaGroup or None
            }

            with self._execution_lock:
                if symbol not in self._executions:
                    self._executions[symbol] = []
                self._executions[symbol].append(exec_record)

            logger.info(f"Execution captured: {execution.side} {execution.shares} {symbol} @ ${execution.price:.2f}")

        except Exception as e:
            logger.error(f"Failed to process execution event: {e}", exc_info=True)

    def _on_order_status(self, trade: Trade) -> None:
        """Handle order status changes."""
        try:
            status = trade.orderStatus.status
            symbol = trade.contract.symbol
            order_type = trade.order.orderType
            event = {
                "order_id": trade.order.orderId,
                "symbol": symbol,
                "status": status,
                "order_type": order_type,
                "filled": trade.orderStatus.filled,
                "remaining": trade.orderStatus.remaining,
                "avg_fill_price": trade.orderStatus.avgFillPrice,
            }

            if status == 'Filled':
                logger.info(f"[OK] Order FILLED: {order_type} {symbol}")
            elif status == 'Cancelled':
                logger.info(f"[X] Order CANCELLED: {order_type} {symbol}")

            for listener in list(self._order_status_listeners):
                try:
                    listener(event)
                except Exception as e:
                    logger.debug(f"Order status listener error: {e}")
        except Exception as e:
            logger.debug(f"Order status event error: {e}")

    def register_order_status_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        if callback not in self._order_status_listeners:
            self._order_status_listeners.append(callback)

    def unregister_order_status_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        if callback in self._order_status_listeners:
            self._order_status_listeners.remove(callback)

    @property
    def connected(self) -> bool:
        """Check if actually connected (not just flag)."""
        if not self._connected:
            return False
        # Verify connection is still alive
        return self.ib.isConnected()

    @connected.setter
    def connected(self, value: bool):
        self._connected = value

    def __del__(self):
        try:
            self._stop_heartbeat()
        except Exception:
            pass

    # ========== CONNECTION ==========

    async def connect(self, max_retries: Optional[int] = None) -> bool:
        """Connect to IBKR TWS/Gateway.

        Tries ``IBKR_CLIENT_ID + attempt`` for ``attempt`` in ``0 .. max_retries-1`` so a
        stale or competing session on the base id does not block connect. Default span is
        controlled by ``IBKR_CONNECT_MAX_ATTEMPTS`` (1–50, default 12). After success,
        ``self.client_id`` is set to the working id for this process.

        Refuses to attempt a socket connect when TRADING_MODE / port / live-confirm
        are inconsistent (:class:`TradingModePortError`).
        """
        if max_retries is None:
            max_retries = max(1, min(50, int(os.environ.get("IBKR_CONNECT_MAX_ATTEMPTS", "12"))))

        # Refresh endpoint from config each connect (env may have changed in tests)
        self.host, self.port, self.mode = resolve_ibkr_endpoint()
        try:
            assert_connect_allowed()
        except TradingModePortError as e:
            logger.error(f"IBKR connect refused: {e}")
            raise

        if self.connected:
            return True
        self._connect_block = ""

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        async with self.async_lock:
            # Double-check after acquiring lock
            if self.connected:
                return True

            for attempt in range(max_retries):
                try:
                    # Use the fixed client_id. On retry, try client_id+attempt to handle
                    # stale connection on the same ID (e.g., TWS still thinks old session is active)
                    current_client_id = self.client_id + attempt
                    logger.info(f"Connecting to IBKR ({self.host}:{self.port}, client_id={current_client_id}, attempt {attempt + 1})")

                    # Clean up old IB instance handlers before creating new one
                    self._unregister_handlers()

                    # Create fresh IB instance on each attempt
                    self.ib = IB()

                    # Re-register all event handlers on new IB instance
                    self._register_handlers()

                    await self.ib.connectAsync(
                        host=self.host,
                        port=self.port,
                        clientId=current_client_id,
                        timeout=10
                    )

                    # Brief wait for connection to stabilize
                    await _safe_sleep(0.5)

                    if self.ib.isConnected():
                        self._connected = True
                        self.client_id = current_client_id  # Store the working client ID
                        self._disconnect_since = None
                        if self._disconnect_cause != DisconnectCause.USER_DISCONNECT.value:
                            self._disconnect_cause = DisconnectCause.UNKNOWN.value

                        # Purge stale order tracking from prior session (prevents 10147)
                        with self._order_state_lock:
                            self._order_states.clear()
                            self._bracket_groups.clear()
                        with self._local_cancel_lock:
                            self._local_cancel_requests.clear()
                        logger.debug("Cleared stale order tracking on reconnect")

                        # Always use LIVE data (type 1) per user preference — they have
                        # active subscription to market data app for real-time live data
                        # (no extra cost, better than delayed)
                        data_type = 1
                        self.ib.reqMarketDataType(data_type)
                        logger.info(f"Market data type set to LIVE ({data_type})")

                        # Get account ID
                        accounts = self.ib.managedAccounts()
                        if accounts:
                            self.account_id = accounts[0]
                            self.account_name = None
                            logger.info(f"Connected to IBKR account: {self.account_id}")

                            # Fetch account values
                            await self._update_account_values()
                            self._refresh_account_identity()
                            self._subscribe_account_pnl()

                        self._last_heartbeat_ok = time.time()
                        self._heartbeat_failures = 0
                        logger.info(
                            f"IBKR connected: account={self.account_id} "
                            f"client_id={self.client_id} {self.host}:{self.port} ({self.mode})"
                        )

                        # Start background heartbeat
                        self._start_heartbeat()

                        return True
                    else:
                        logger.warning(f"Connection timeout on attempt {attempt + 1}")

                except TradingModePortError:
                    raise
                except Exception as e:
                    logger.warning(f"Connection attempt {attempt + 1} failed: {e}")
                    if self._connect_block == "paper_disclaimer":
                        logger.error(
                            "IBKR paper disclaimer blocks API — accept it in TWS, then retry"
                        )
                        return False
                    if port_is_closed(e):
                        logger.error(
                            "IBKR port %s refused — TWS/Gateway is not listening "
                            "(not a client-id conflict)",
                            self.port,
                        )
                        return False

                if attempt < max_retries - 1:
                    await _safe_sleep(2)

            logger.error(f"Failed to connect after {max_retries} attempts")
            return False


    def _subscribe_account_pnl(self) -> None:
        """TWS Daily P&L comes from reqPnL, not the accountValues DailyPnL tag."""
        if not self.account_id:
            return
        try:
            self._cancel_account_pnl()
            self._pnl = self.ib.reqPnL(self.account_id)
        except Exception:
            logger.debug("reqPnL subscribe failed", exc_info=True)
            self._pnl = None

    def _cancel_account_pnl(self) -> None:
        pnl = getattr(self, "_pnl", None)
        self._pnl = None
        if pnl is None or not self.account_id:
            return
        try:
            self.ib.cancelPnL(self.account_id)
        except Exception:
            logger.debug("cancelPnL failed", exc_info=True)

    def _apply_req_pnl(self, result: dict[str, Any]) -> dict[str, Any]:
        pnl = getattr(self, "_pnl", None)
        if pnl is None:
            return result
        raw = getattr(pnl, "dailyPnL", None)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return result
        if val != val:
            return result
        result["dailypnl"] = val
        return result

    def _refresh_account_identity(self) -> None:
        """Best-effort account display name from IBKR accountValues tags."""
        try:
            values = self.ib.accountValues(self.account_id) if self.account_id else self.ib.accountValues()
        except Exception:
            values = []
        tags = {}
        for av in values or []:
            tag = str(getattr(av, "tag", "") or "")
            val = str(getattr(av, "value", "") or "").strip()
            if tag and val:
                tags[tag] = val
        # Prefer human labels when present; fall back to account type + id.
        for key in ("AccountTitle", "AccountOrGroup", "AccountCode"):
            if tags.get(key):
                self.account_name = tags[key]
                break
        if not self.account_name:
            atype = tags.get("AccountType") or ""
            if atype and self.account_id:
                self.account_name = f"{atype} {self.account_id}"
            elif self.account_id:
                self.account_name = f"IBKR {self.account_id}"

    async def disconnect(self):
        """Disconnect from IBKR."""
        self._disconnect_cause = DisconnectCause.USER_DISCONNECT.value
        self._reconnect_requested = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reconnect_task = None
        self._disconnect_since = None

        if self.connected:
            # Stop heartbeat
            self._stop_heartbeat()

            # Cancel all streaming subscriptions
            for ticker in self._tickers.values():
                try:
                    self.ib.cancelMktData(ticker.contract)
                except Exception:
                    pass
            self._tickers.clear()
            self._cancel_account_pnl()
            drop_rt = getattr(self, "abandon_realtime_bars", None)
            if callable(drop_rt):
                try:
                    drop_rt()
                except Exception:
                    pass
            self._clear_book_subs(cancel=True)

            self.ib.disconnect()
            self.connected = False
            logger.info("Disconnected from IBKR")

    async def _update_account_values(self):
        """Fetch and update account values (available funds, net liquidation, PDT status)."""
        try:
            # Use accountValues (synchronous, cached) instead of accountSummary
            account_values = self.ib.accountValues(self.account_id)

            for av in account_values:
                if av.tag == 'NetLiquidation':
                    self.net_liquidation = float(av.value)
                elif av.tag == 'TotalCashValue':
                    self.cash_value = float(av.value)
                elif av.tag == 'AvailableFunds':
                    self.available_funds = float(av.value)
                elif av.tag == 'DayTradesRemaining':
                    self.day_trades_remaining = int(float(av.value))

            if self.available_funds > 0 or self.net_liquidation > 0:
                pdt_display = "Unlimited" if self.day_trades_remaining == -1 else self.day_trades_remaining
                logger.info(f"Account values - Available: ${self.available_funds:,.2f}, Cash: ${self.cash_value:,.2f}, Net Liq: ${self.net_liquidation:,.2f}, Day Trades: {pdt_display}")
            else:
                # If still zero, request subscription
                self.ib.reqAccountUpdates(subscribe=True, account=self.account_id)
                await _safe_sleep(0.5)  # Give time for update
                account_values = self.ib.accountValues(self.account_id)
                for av in account_values:
                    if av.tag == 'NetLiquidation':
                        self.net_liquidation = float(av.value)
                    elif av.tag == 'TotalCashValue':
                        self.cash_value = float(av.value)
                    elif av.tag == 'AvailableFunds':
                        self.available_funds = float(av.value)
                logger.info(f"Account values (subscribed) - Available: ${self.available_funds:,.2f}, Cash: ${self.cash_value:,.2f}, Net Liq: ${self.net_liquidation:,.2f}")
        except Exception as e:
            logger.warning(f"Failed to fetch account values: {e}")

    async def _ensure_connected(self) -> bool:
        """Ensure we're connected, attempt reconnect if not."""
        if self.connected:
            return True

        logger.info(
            f"Reconnecting (cause={self._disconnect_cause}, client_id={self.client_id})"
        )
        self._connected = False
        result = await self.connect()
        if result:
            self.ib.reqMarketDataType(1)
            logger.info("Market data type set to live")
            await self._update_account_values()
            await self._after_connect_restore()
        return result

    async def _wait_for_fill(self, trade, timeout: float = 5.0) -> Dict[str, Any]:
        """
        Wait for a trade to fill or be cancelled.

        Args:
            trade: The ib_insync Trade object to monitor
            timeout: Max seconds to wait for fill

        Returns:
            Dict with 'filled', 'status', 'avg_fill_price', 'filled_quantity'
        """
        start_time = asyncio.get_event_loop().time()
        poll_interval = 0.1  # 100ms between checks

        while True:
            # Check if we've exceeded timeout
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                logger.warning(f"Fill wait timeout after {elapsed:.1f}s, status: {trade.orderStatus.status}")
                return {
                    'filled': False,
                    'status': 'Timeout',
                    'avg_fill_price': None,
                    'filled_quantity': 0
                }

            # Check current status
            status = trade.orderStatus.status

            if status == 'Filled':
                avg_price = trade.orderStatus.avgFillPrice
                filled_qty = trade.orderStatus.filled
                logger.info(f"Order FILLED: {filled_qty} @ ${avg_price:.2f}")
                return {
                    'filled': True,
                    'status': 'Filled',
                    'avg_fill_price': avg_price,
                    'filled_quantity': int(filled_qty)
                }

            if status in ('Cancelled', 'ApiCancelled'):
                logger.warning(f"Order {status}, not filled")
                return {
                    'filled': False,
                    'status': status,
                    'avg_fill_price': None,
                    'filled_quantity': 0
                }

            # Inactive means order is valid but not immediately fillable
            # This can happen with limit orders - keep waiting for DAY orders
            if status == 'Inactive':
                # For paper trading, Inactive often means price moved -
                # give it more time before giving up
                if elapsed < timeout * 0.9:
                    await _safe_sleep(poll_interval)
                    continue
                else:
                    logger.warning(f"Order Inactive after {elapsed:.1f}s - limit price may be stale")
                    return {
                        'filled': False,
                        'status': status,
                        'avg_fill_price': None,
                        'filled_quantity': 0
                    }

            # Still pending (PreSubmitted, Submitted, etc.)
            await _safe_sleep(poll_interval)

    # ========== EMERGENCY OPERATIONS ==========

    async def _flatten_one_position(self, pos: dict) -> dict:
        """Close one position leg independently. STK→MKT, OPT→close_option_position."""
        symbol = pos.get("symbol", "")
        qty = pos.get("quantity", pos.get("qty", 0))
        sec = str(pos.get("sec_type") or pos.get("secType") or "STK").upper()
        cid = pos.get("conId") or pos.get("con_id") or "none"
        if qty == 0 or not symbol:
            return {
                "success": True,
                "method": "noop",
                "symbol": symbol,
                "conId": cid,
                "reasoning": f"Closing target = conId={cid} — zero qty or no symbol",
            }
        action = "SELL" if qty > 0 else "BUY"
        close_qty = abs(int(qty))
        if sec.startswith("OPT"):
            expiration = pos.get("expiration") or pos.get("lastTradeDateOrContractMonth")
            strike = pos.get("strike")
            right = pos.get("right")
            try:
                order_result = await self.close_option_position(
                    symbol,
                    expiration=expiration,
                    strike=float(strike) if strike is not None else None,
                    right=str(right) if right is not None else None,
                    quantity=close_qty,
                    reason="panic_flatten",
                )
                ok = bool(order_result.get("success") or order_result.get("order_id"))
                return {
                    "success": ok,
                    "method": "close_option_position",
                    "symbol": symbol,
                    "sec_type": "OPT",
                    "conId": cid,
                    "quantity": close_qty,
                    "order_result": order_result,
                    "reasoning": (
                        f"Closing target = conId={cid} — independent close via "
                        "close_option_position for OPT leg"
                    ),
                }
            except Exception as e:
                return {
                    "success": False,
                    "method": "close_option_position",
                    "symbol": symbol,
                    "conId": cid,
                    "error": str(e),
                    "reasoning": f"Closing target = conId={cid} — OPT close failed: {e}",
                }
        try:
            order_result = await self._place_order(
                symbol=symbol,
                action=action,
                quantity=close_qty,
                order_type="MKT",
                tif="IOC",
                order_name="EMERGENCY_FLATTEN_STK",
            )
            return {
                "success": bool(order_result.get("success")),
                "method": "stock_mkt",
                "symbol": symbol,
                "sec_type": "STK",
                "conId": cid,
                "quantity": close_qty,
                "order_result": order_result,
                "reasoning": (
                    f"Closing target = conId={cid} — independent close via stock MKT for STK leg"
                ),
            }
        except Exception as e:
            return {
                "success": False,
                "method": "stock_mkt",
                "symbol": symbol,
                "conId": cid,
                "error": str(e),
                "reasoning": f"Closing target = conId={cid} — STK flatten failed: {e}",
            }

    async def flatten_all(self) -> dict:
        """Cancel all open orders and close all positions per-leg (STK vs OPT).

        Returns a summary dict with cancelled/closed counts and position_results.
        This is the broker-level nuclear option — caller decides when to invoke.
        """
        if not await self._ensure_connected():
            return {"success": False, "error": "Not connected"}

        result = {
            "orders_cancelled": 0,
            "orders_total": 0,
            "positions_closed": 0,
            "positions_total": 0,
            "position_results": [],
            "errors": [],
        }

        # Step 1: Cancel ALL open orders
        try:
            open_orders = await self.get_open_orders()
            result["orders_total"] = len(open_orders)
            for order in open_orders:
                try:
                    oid = order.get("order_id")
                    if oid:
                        await self.cancel_order(oid)
                        result["orders_cancelled"] += 1
                except Exception as e:
                    err_msg = f'cancel order {order.get("order_id")}: {e}'
                    logger.error(err_msg)
                    result["errors"].append(err_msg)
        except Exception as e:
            err_msg = f"get_open_orders: {e}"
            logger.error(err_msg)
            result["errors"].append(err_msg)

        await _safe_sleep(1)  # Let cancellations process

        # Step 2: Close each position leg independently (STK vs OPT routing)
        try:
            positions = await self.get_positions()
            result["positions_total"] = len(positions)
            for pos in positions:
                try:
                    pr = await self._flatten_one_position(pos)
                    result["position_results"].append(pr)
                    if pr.get("success") and pr.get("method") != "noop":
                        result["positions_closed"] += 1
                    elif not pr.get("success"):
                        err_msg = f'flatten {pos.get("symbol", "?")}: {pr}'
                        logger.error(err_msg)
                        result["errors"].append(err_msg)
                except Exception as e:
                    err_msg = f'flatten {pos.get("symbol", "?")}: {e}'
                    logger.error(err_msg)
                    result["errors"].append(err_msg)
        except Exception as e:
            err_msg = f"get_positions: {e}"
            logger.error(err_msg)
            result["errors"].append(err_msg)

        result["success"] = True
        logger.critical(
            f"FLATTEN ALL: cancelled {result['orders_cancelled']}/{result['orders_total']} orders, "
            f"closed {result['positions_closed']}/{result['positions_total']} (per-leg)"
        )
        return result

    # ========== HEARTBEAT ==========

    def _clear_book_subs(self, *, cancel: bool = False) -> None:
        live = getattr(self, "_book_sub_live", None)
        if not isinstance(live, set):
            live = set()
            self._book_sub_live = live
        if cancel:
            for cid, contract in list(self._book_subs.items()):
                if cid not in live:
                    continue
                try:
                    if contract is not None:
                        self.ib.cancelMktData(contract)
                except Exception:
                    pass
        self._book_subs.clear()
        live.clear()

    async def ensure_book_ticks(self, positions: list | None) -> None:
        """Keep streaming ticks for open lots. Cancel only when the lot is gone."""
        if not self.connected or self.ib is None:
            return
        live = getattr(self, "_book_sub_live", None)
        if not isinstance(live, set):
            live = set()
            self._book_sub_live = live
        want: Dict[int, Any] = {}
        for p in positions or []:
            if not isinstance(p, dict):
                continue
            try:
                cid = int(p.get("conId") or p.get("con_id") or 0)
            except (TypeError, ValueError):
                cid = 0
            if cid <= 0:
                continue
            want[cid] = p
        gone = [cid for cid in list(self._book_subs) if cid not in want]
        for cid in gone:
            contract = self._book_subs.pop(cid, None)
            if cid in live:
                try:
                    if contract is not None:
                        self.ib.cancelMktData(contract)
                except Exception:
                    pass
                live.discard(cid)
        for cid, p in want.items():
            c = self._book_subs.get(cid)
            if c is None:
                try:
                    from ib_insync import Contract

                    c = Contract(conId=cid, exchange="SMART", currency="USD")
                    await self.ib.qualifyContractsAsync(c)
                    self.ib.reqMktData(c, "", False, False)
                    self._book_subs[cid] = c
                    live.add(cid)
                except Exception:
                    logger.debug("book tick subscribe failed conId=%s", cid, exc_info=True)
                    continue
            sec = str(p.get("secType") or p.get("sec_type") or "").upper()
            sym = str(p.get("symbol") or "").strip().upper()
            start_rt = getattr(self, "start_realtime_bars", None)
            if callable(start_rt) and sym and sec not in ("OPT", "FOP", "BAG"):
                try:
                    prepare = getattr(self, "_prepare_contract", None)
                    stk = await prepare(sym) if callable(prepare) else None
                    start_rt(sym, stk if stk is not None else c)
                except Exception:
                    logger.debug("book rt bars failed %s", sym, exc_info=True)

    def _heartbeat_interval_s(self) -> float:
        """Fast poll when unhealthy; slow when connected."""
        fast = float(os.environ.get("IBKR_HEARTBEAT_FAST_INTERVAL_S", "15"))
        slow = float(os.environ.get("IBKR_HEARTBEAT_INTERVAL_S", "60"))
        if not self.connected or self._reconnect_requested:
            return max(5.0, fast)
        return max(10.0, slow)

    def _start_heartbeat(self):
        """Start background heartbeat to prevent idle disconnect."""
        self._stop_heartbeat()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Heartbeat started (interval {self._heartbeat_interval_s():.0f}s)")

    def _stop_heartbeat(self):
        """Cancel background heartbeat."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
            logger.debug("Heartbeat stopped")

    async def _heartbeat_loop(self):
        """Ping TWS on a cadence; reconnect on loss (incl. Gateway restart)."""
        try:
            while True:
                await _safe_sleep(self._heartbeat_interval_s())

                if not self.connected:
                    self._disconnect_cause = self._disconnect_cause or DisconnectCause.HEARTBEAT_FAILED.value
                    logger.warning(
                        f"Heartbeat: disconnected (cause={self._disconnect_cause}, "
                        f"failures={self._heartbeat_failures})"
                    )
                    self._schedule_reconnect(self._disconnect_cause)
                    continue

                try:
                    await self.ib.reqCurrentTimeAsync()
                    self.ib.reqMarketDataType(1)
                    self._last_heartbeat_ok = time.time()
                    self._heartbeat_failures = 0
                    self._reconnect_requested = False
                    logger.debug("Heartbeat OK")
                except Exception as e:
                    self._heartbeat_failures += 1
                    self._connected = False
                    self._disconnect_cause = DisconnectCause.HEARTBEAT_FAILED.value
                    logger.warning(
                        f"Heartbeat failed ({e}) — failures={self._heartbeat_failures}"
                    )
                    self._schedule_reconnect(DisconnectCause.HEARTBEAT_FAILED.value)
        except asyncio.CancelledError:
            pass


# ========== FACTORY FUNCTION ==========

def get_ibkr_connector() -> IBKRConnector:
    """Get singleton IBKR connector instance."""
    return IBKRConnector()
