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
from datetime import datetime, timezone, tzinfo
from threading import Lock

from ib_insync import IB, Order, Trade, Fill

from abcxauto.aio import bind_thread_loop
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
# EXECUTION TIMESTAMPS
# ============================================================

# A broker clock may run a little ahead of ours; only a gap this large means
# the wall-clock digits were read in the wrong zone.
_FILL_FUTURE_TOLERANCE_S = 300.0


# IANA name so CDT/CST follow DST. A fixed offset would stay wrong after the
# fall-back. Noah's TWS display is Chicago (Configure → Display).
_DEFAULT_TWS_TIMEZONE = "America/Chicago"


def tws_timezone() -> str:
    """Which zone TWS stamps execution times in.

    TWS sends ``execDetails`` time as bare ``YYYYmmdd  HH:MM:SS`` digits. When
    ``IB.TimezoneTWS`` is unset, ib_insync's decoder falls through to
    ``astimezone()`` on that naive value, which reads it as *this machine's*
    local time and shifts every fill by the local UTC offset. Naming the zone
    keeps the digits meaning what TWS meant by them. Default is the IANA
    Chicago zone, not a UTC-5/UTC-6 constant.
    """
    named = (os.environ.get("ABCXAUTO_TWS_TIMEZONE") or _DEFAULT_TWS_TIMEZONE).strip()
    return named or _DEFAULT_TWS_TIMEZONE


def _iso_z(dt: datetime) -> str:
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_ib() -> IB:
    """A fresh ib_insync session that knows which zone TWS timestamps are in."""
    ib = IB()
    ib.TimezoneTWS = tws_timezone()
    return ib


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def fill_ts_iso(
    exec_time: Any,
    *,
    now: Optional[datetime] = None,
    local_tz: Optional[tzinfo] = None,
) -> str:
    """Canonical ``...Z`` UTC stamp for one broker execution.

    Bare digits from TWS are in the TWS clock (``ABCXAUTO_TWS_TIMEZONE``,
    default ``America/Chicago`` on this desk). Naive values are labelled with
    that zone rather than silently assumed UTC.
    An execution cannot have happened after now, so a stamp in the future is
    proof the digits were already read in some other zone; reading that zone's
    wall clock back as UTC undoes exactly that shift. ``local_tz`` defaults to
    this machine's zone, which is the one ib_insync guesses with.
    """
    now_utc = now or datetime.now(timezone.utc)
    dt = _as_datetime(exec_time)
    if dt is None:
        return _iso_z(now_utc)
    if dt.tzinfo is None:
        zone_name = tws_timezone()
        try:
            from zoneinfo import ZoneInfo

            dt = dt.replace(tzinfo=ZoneInfo(zone_name)).astimezone(timezone.utc)
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if (dt - now_utc).total_seconds() > _FILL_FUTURE_TOLERANCE_S:
        reread = dt.astimezone(local_tz).replace(tzinfo=timezone.utc)
        fixed = reread if reread <= now_utc else now_utc
        logger.warning(
            "fill timestamp %s is in the future - reading it as %s; "
            "check IB.TimezoneTWS against the TWS clock",
            _iso_z(dt),
            _iso_z(fixed),
        )
        dt = fixed
    return _iso_z(dt)


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


def combo_legs_from_contract(contract: Any) -> List[Dict[str, Any]]:
    """Plain dicts for BAG comboLegs so the riskless-combo cap can classify."""
    raw = getattr(contract, "comboLegs", None) or getattr(contract, "combo_legs", None) or []
    out: List[Dict[str, Any]] = []
    for leg in raw:
        if isinstance(leg, dict):
            con = leg.get("conId") if leg.get("conId") is not None else leg.get("con_id")
            out.append({
                "conId": con,
                "con_id": con,
                "ratio": leg.get("ratio", 1),
                "action": leg.get("action") or "",
                "exchange": leg.get("exchange") or "",
            })
            continue
        con = getattr(leg, "conId", None)
        out.append({
            "conId": con,
            "con_id": con,
            "ratio": getattr(leg, "ratio", 1),
            "action": getattr(leg, "action", "") or "",
            "exchange": getattr(leg, "exchange", "") or "",
        })
    return out


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
            return {
                'error': 'Not connected',
                'ibkr_data_stale': bool(getattr(self, '_ibkr_data_stale', False)),
            }

        try:
            async with self.async_lock:
                account_values = self.ib.accountValues()
                result = {
                    'account_id': self.account_id,
                    'ibkr_data_stale': bool(getattr(self, '_ibkr_data_stale', False)),
                }
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
            return {
                'error': str(e),
                'ibkr_data_stale': bool(getattr(self, '_ibkr_data_stale', False)),
            }

    async def cancel_order(self, order_id: int) -> Dict[str, Any]:
        """Cancel an open order."""
        from abcxauto.protect import cancel_oid_is_blocked

        if not await self._ensure_connected():
            return {'error': 'Not connected'}

        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            return {'error': f'Invalid order_id {order_id}'}
        if cancel_oid_is_blocked(oid):
            return {'success': True, 'order_id': oid, 'already_gone': True}

        try:
            for trade in self.ib.openTrades():
                if trade.order.orderId == oid:
                    if hasattr(self, "_cancel_order_with_tracking"):
                        self._cancel_order_with_tracking(trade.order, source="cancel_order")
                    else:
                        self.ib.cancelOrder(trade.order)
                    return {'success': True, 'order_id': oid}
            # Do not note_cancel_gone here — executor/protect classify flat-book
            # 10147/order_gone as quiet clear-stale vs loud ERROR.
            return {'error': f'Order {oid} not found', 'order_gone': True}
        except Exception as e:
            logger.error(f"Failed to cancel order {oid}: {e}")
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
            from abcxauto.protect import cancel_oid_is_blocked

            for t in self.ib.openTrades():
                status = t.orderStatus.status
                if status not in ACTIVE_STATUSES:
                    continue
                try:
                    oid = int(t.order.orderId)
                except (TypeError, ValueError):
                    oid = None
                if oid is not None and cancel_oid_is_blocked(oid):
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
                    # Bracket/OCA lineage: the only proof that a resting LMT is
                    # protection rather than an entry, so the orphan sweep can
                    # tell a stale take-profit from a working idea.
                    'oca_group': getattr(t.order, 'ocaGroup', '') or None,
                    'parent_id': int(getattr(t.order, 'parentId', 0) or 0) or None,
                }
                if sec_type == 'OPT':
                    order_data.update({
                        'strike': t.contract.strike,
                        'expiration': t.contract.lastTradeDateOrContractMonth,
                        'right': t.contract.right,
                        'multiplier': int(t.contract.multiplier or 100),
                        'local_symbol': t.contract.localSymbol,
                    })
                if sec_type == 'BAG':
                    legs = combo_legs_from_contract(t.contract)
                    order_data['combo_legs'] = legs
                    order_data['comboLegs'] = legs
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

                ts = fill_ts_iso(getattr(execution, "time", None))

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

                sec = getattr(contract, "secType", None) or "STK"
                row = {
                    "ts": ts,
                    "exec_id": getattr(execution, "execId", None),
                    "order_id": getattr(execution, "orderId", None),
                    "symbol": getattr(contract, "symbol", None),
                    "sec_type": sec,
                    "conId": getattr(contract, "conId", None),
                    "con_id": getattr(contract, "conId", None),
                    "side": getattr(execution, "side", None),
                    "quantity": getattr(execution, "shares", None),
                    "price": getattr(execution, "price", None),
                    "commission": commission,
                    "realized_pnl": realized_pnl,
                }
                # BAG legs land as OPT fills — keep strike/right/exp for desk attach.
                if str(sec).upper() in ("OPT", "FOP"):
                    row["strike"] = getattr(contract, "strike", None)
                    row["expiration"] = getattr(
                        contract, "lastTradeDateOrContractMonth", None
                    )
                    row["right"] = getattr(contract, "right", None)
                    local = getattr(contract, "localSymbol", None)
                    if local:
                        row["local_symbol"] = local
                        row["localSymbol"] = local
                out.append(row)
            try:
                from abcxauto.send_marks import attach_fill_quotes

                return await attach_fill_quotes(out, self)
            except Exception:
                logger.debug("fill quote attach failed", exc_info=True)
                from abcxauto.send_marks import QUOTE_REASON_NO_QUOTE

                for row in out:
                    if isinstance(row, dict) and not row.get("quote_reason"):
                        row["quote_reason"] = QUOTE_REASON_NO_QUOTE
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
                contract = fill.contract
                sec = getattr(contract, "secType", None) or "STK"
                ts = fill_ts_iso(getattr(fill.execution, "time", None))
                row = {
                    'symbol': contract.symbol,
                    'side': fill.execution.side,
                    'shares': fill.execution.shares,
                    'quantity': fill.execution.shares,
                    'price': fill.execution.price,
                    'avg_price': fill.execution.avgPrice,
                    'time': ts,
                    'ts': ts,
                    'order_id': fill.execution.orderId,
                    'exec_id': fill.execution.execId,
                    'commission': fill.commissionReport.commission if fill.commissionReport else 0,
                    'sec_type': sec,
                    'secType': sec,
                }
                if str(sec).upper() in ("OPT", "FOP"):
                    row["strike"] = getattr(contract, "strike", None)
                    row["expiration"] = getattr(
                        contract, "lastTradeDateOrContractMonth", None
                    )
                    row["right"] = getattr(contract, "right", None)
                executions.append(row)
            return executions
        except Exception as e:
            logger.error(f"Failed to get executions: {e}")
            return []

    _QUOTE_CACHE_S = 2.5

    def _invalidate_live_caches(self) -> None:
        """Drop quote cache so a reconnect cannot reuse a pre-disconnect last."""
        bag = getattr(self, "_quote_cache", None)
        if isinstance(bag, dict):
            bag.clear()

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
        """IBKR live quotes for one scan sweep (parallel, short cache)."""
        from abcxauto.broker.quotes import quote_batch_cap

        seen: List[str] = []
        batch_cap = quote_batch_cap()
        for raw in symbols or []:
            sym = str(raw or "").strip().upper()
            if sym and sym not in seen:
                seen.append(sym)
            if len(seen) >= batch_cap:
                break
        rows = await asyncio.gather(*[self.get_live_quote(s, fresh=fresh) for s in seen])
        return {
            "source": "ibkr",
            "freshness": "live",
            "quotes": [r for r in rows if isinstance(r, dict)],
        }

    async def get_live_quote(self, symbol: str, *, fresh: bool = False) -> Dict[str, Any]:
        """IBKR stream snapshot for STK. Live last/bid/ask for send geometry."""
        from abcxauto.broker.quotes import quote_from_ticker

        sym = str(symbol or "").strip().upper()
        if not sym:
            return {"error": "symbol required", "source": "ibkr"}
        if bool(getattr(self, "_ibkr_data_stale", False)):
            return {
                "error": "ibkr_data_stale",
                "source": "ibkr",
                "symbol": sym,
                "ibkr_data_stale": True,
            }
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
        # True only if THIS call issued a throwaway snapshot reqMktData.
        # Persistent quote-owned streams stay in _tickers until disconnect.
        # Book mirrors are read-only: never cache them in _tickers (lot exit
        # cancelMktData would leave a dead ticker reused forever).
        subscribed = False
        try:
            streams = getattr(self, "_tickers", None)
            if not isinstance(streams, dict):
                streams = {}
                self._tickers = streams
            owned = getattr(self, "_quote_mkt_syms", None)
            if not isinstance(owned, set):
                owned = set()
                self._quote_mkt_syms = owned
            ticker = streams.get(sym)
            if ticker is not None and sym not in owned:
                streams.pop(sym, None)
                ticker = None
            con_id = int(getattr(contract, "conId", 0) or 0)
            if ticker is None and con_id and con_id in getattr(self, "_book_subs", {}):
                # Book already streams this conId — reuse, do not re-req or cache.
                get_t = getattr(self.ib, "ticker", None)
                if callable(get_t):
                    ticker = get_t(contract)
            if ticker is None:
                # Streaming once (not reqTickersAsync snapshot churn). Keeps the
                # line so TWS UI + this client do not fight on every quote.
                ticker = self.ib.reqMktData(contract, "", False, False)
                streams[sym] = ticker
                owned.add(sym)
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
            # Cancel only a throwaway snapshot this call opened. Never cancel
            # after a reused stream or book sub (was: cancel after
            # reqTickersAsync → flood "No reqId found").
            con_id = int(getattr(contract, "conId", 0) or 0)
            if subscribed and not (con_id and con_id in getattr(self, "_book_subs", {})):
                try:
                    self.ib.cancelMktData(contract)
                except Exception:
                    pass


# Import mixins after defining base classes to avoid circular imports
from abcxauto.broker.bars import IBKRBarsMixin
from abcxauto.broker.orders import IBKROrdersMixin
from abcxauto.broker.options import IBKROptionsMixin


_STOP_ORDER_TYPES = {"STP", "STP LMT", "TRAIL", "TRAIL LIMIT", "TRAIL STOP"}


def _flatten_qty(pos: dict) -> float:
    try:
        return float(pos.get("quantity") if pos.get("quantity") is not None else pos.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _flatten_lot_key(pos: dict) -> str:
    cid = pos.get("conId") or pos.get("con_id")
    try:
        cid_i = int(cid)
    except (TypeError, ValueError):
        cid_i = 0
    if cid_i > 0:
        return f"con:{cid_i}"
    symbol = str(pos.get("symbol") or "").upper()
    sec = str(pos.get("sec_type") or pos.get("secType") or "STK").upper()
    if sec.startswith("OPT"):
        return "opt:{}:{}:{}:{}".format(
            symbol,
            pos.get("expiration") or pos.get("lastTradeDateOrContractMonth") or "",
            pos.get("strike") or "",
            pos.get("right") or "",
        )
    return f"stk:{symbol}"


def _is_stop_order_row(order: dict) -> bool:
    raw = str((order or {}).get("order_type") or "").upper().replace("_", " ").replace("-", " ")
    compact = " ".join(raw.split())
    return compact in _STOP_ORDER_TYPES or compact.startswith("TRAIL")


def _cancel_cleared(out: Any) -> bool:
    if not isinstance(out, dict):
        return False
    if out.get("success") or out.get("already_gone") or out.get("order_gone"):
        return True
    return False


def _index_stk_stops(orders: List[Dict[str, Any]]) -> Dict[str, dict]:
    saved: Dict[str, dict] = {}
    for order in orders or []:
        if not _is_stop_order_row(order):
            continue
        sec = str(order.get("sec_type") or order.get("secType") or "STK").upper()
        if sec.startswith("OPT"):
            continue
        px = order.get("aux_price")
        if px is None:
            px = order.get("stop_price")
        saved[_flatten_lot_key(order)] = {
            "action": order.get("action"),
            "quantity": order.get("quantity"),
            "stop_price": px,
            "order_id": order.get("order_id"),
        }
    return saved


def _covering_stop_order(lot: dict, orders: List[Dict[str, Any]]) -> Optional[dict]:
    key = _flatten_lot_key(lot)
    symbol = str(lot.get("symbol") or "").upper()
    want = "SELL" if _flatten_qty(lot) > 0 else "BUY"
    for order in orders or []:
        if not _is_stop_order_row(order):
            continue
        if str(order.get("action") or "").upper() != want:
            continue
        if _flatten_lot_key(order) == key:
            return order
        if symbol and str(order.get("symbol") or "").upper() == symbol:
            sec = str(order.get("sec_type") or order.get("secType") or "STK").upper()
            if sec.startswith("STK"):
                return order
    return None


class IBKRConnector(IBKROrdersMixin, IBKROptionsMixin, IBKRQueriesMixin, IBKRBarsMixin):
    """
    IBKR connector with essential trading functionality.
    Thread-safe singleton pattern.

    Inherits from:
    - IBKROrdersMixin: Stock order placement and management
    - IBKROptionsMixin: Multi-leg / single-option strategies
    - IBKRQueriesMixin: Account and position query methods
    - IBKRBarsMixin: RTH history and the live 5s bar stream

    MRO: Orders before Options so close_option_position uses the orders
    implementation (JSON-friendly symbol/expiry/strike close).
    """

    _instance: Optional['IBKRConnector'] = None
    _lock: Lock = Lock()
    _async_lock: Optional[asyncio.Lock] = None  # Rebound when the bound loop dies

    def __new__(cls) -> 'IBKRConnector':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def _api_socket_live(self) -> bool:
        """True when the current IB() still has an API socket (1100 may keep it)."""
        ib = getattr(self, "ib", None)
        if ib is None:
            return False
        try:
            return bool(ib.isConnected())
        except Exception:
            return False

    def _loop_is_usable(self, loop: Any) -> bool:
        """True when *loop* exists, is not closed, and is running."""
        if loop is None:
            return False
        try:
            if loop.is_closed():
                return False
            return bool(loop.is_running())
        except Exception:
            return False

    def _async_job_in_flight(self, task: Any) -> bool:
        if task is None:
            return False
        done = getattr(task, "done", None)
        if not callable(done):
            return True
        try:
            return not bool(done())
        except Exception:
            return False

    def _book_refresh_in_flight(self) -> bool:
        return self._async_job_in_flight(getattr(self, "_book_refresh_task", None))

    def _ib_usable_on_running_loop(self) -> bool:
        """Live API socket that this event loop is allowed to keep (no new IB())."""
        if not self._api_socket_live():
            return False
        ib_loop = getattr(self.ib, "loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if ib_loop is None:
            return True
        try:
            if ib_loop.is_closed():
                return False
        except Exception:
            return False
        return ib_loop is running

    def _adopt_live_socket(self) -> bool:
        """Keep the existing API socket. Error 1100 forbids a replacement IB()."""
        self._connected = True
        task = getattr(self, "_heartbeat_task", None)
        if task is None or task.done():
            try:
                self._start_heartbeat()
            except Exception:
                logger.debug("Heartbeat restart on live socket failed", exc_info=True)
        logger.info("IBKR API socket still up — keep socket; no new IB()")
        return True

    @property
    def async_lock(self) -> asyncio.Lock:
        """Lock for the current event loop.

        ``asyncio.Lock`` binds to the loop that first acquired it. The Pro
        worker is ``asyncio.run`` per generation, so a singleton lock from a
        dead loop raises ``Lock bound to a different event loop`` on reconnect
        and the desk stays down until the 120s HALT. Replace a lock whose loop
        is closed. If another live loop still owns it, fail closed — do not
        mint a second lock and interleave IB() calls.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        lock = self._async_lock
        # CPython 3.12 uncontended Lock.acquire never writes lock._loop.
        # Track the owner loop on the connector so reconnect can rebind.
        bound = getattr(self, "_async_lock_loop", None)
        if bound is None and lock is not None:
            bound = getattr(lock, "_loop", None)

        if lock is None:
            self._async_lock = asyncio.Lock()
            self._async_lock_loop = running
            return self._async_lock

        if bound is None or running is None or bound is running:
            if bound is None and running is not None:
                self._async_lock_loop = running
            return lock

        try:
            bound_closed = bound.is_closed()
        except Exception:
            bound_closed = True
        if bound_closed:
            logger.warning("IBKR async_lock rebound after event loop closed")
            self._async_lock = asyncio.Lock()
            self._async_lock_loop = running
            return self._async_lock

        raise RuntimeError(
            "IBKR async_lock is bound to a different event loop"
        )

    def __init__(self):
        if self._initialized:
            return

        # Resolve endpoint from environment
        self.host, self.port, self.mode = resolve_ibkr_endpoint()
        # Fixed client ID from config (IBKR_CLIENT_ID, default 42) — MUST be
        # consistent across restarts so the agent can cancel prior-session orders.
        self.client_id = int(get_config().ibkr_client_id)

        # Connection state
        self._async_lock = None
        self._async_lock_loop = None
        self.ib = new_ib()
        self._connected = False
        self._connect_block = ""
        self.account_id: Optional[str] = None
        self.account_name: Optional[str] = None
        self.net_liquidation: float = 0.0
        self.cash_value: float = 0.0  # TotalCashValue - actual cash
        self.available_funds: float = 0.0  # AvailableFunds (INCLUDES MARGIN — do NOT use for order sizing, use cash_value instead)
        self.day_trades_remaining: int = 3  # PDT tracking - updated on connect

        # Quote-owned streaming subscriptions: symbol -> ticker (reqMktData we opened).
        self._tickers: Dict[str, Any] = {}
        self._quote_mkt_syms: set[str] = set()
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
        self._quote_cache: Dict[str, Any] = {}
        self._book_refresh_task: Optional[Any] = None
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
        self._riskless_combo_202 = False

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

    @property
    def ibkr_data_stale(self) -> bool:
        """True after error 1100 / disconnect until a successful book refresh."""
        return bool(getattr(self, "_ibkr_data_stale", False))

    def _mark_ibkr_data_stale(self, *, reason: str) -> None:
        self._ibkr_data_stale = True
        self._invalidate_live_caches()
        logger.warning("IBKR data marked stale (%s) — new risk blocked until refresh", reason)

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
            self._mark_ibkr_data_stale(reason=f"error {errorCode}")
            logger.warning(
                f"IBKR↔TWS link lost [{errorCode}]: {errorString} — "
                "keep API socket; no new IB()"
            )
            return
        if lifecycle == "tws_restored":
            # Stay stale until positions/orders/account refresh. 1101/1102
            # only mean the socket is back — ib_insync caches may still be empty.
            self._mark_ibkr_data_stale(reason=f"restore {errorCode}")
            logger.info(
                f"IBKR connectivity restored [{errorCode}]: {errorString}"
                + (" (data lost)" if errorCode == 1101 else "")
                + " — refresh book before clearing stale"
            )
            self._schedule_book_refresh_after_restore()
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

        from abcxauto.protect import ibkr_error_means_cancel_gone, note_cancel_gone

        if ibkr_error_means_cancel_gone(errorCode, errorString):
            # Async 10147 after protect cancel: clear stale local id quietly.
            # Executor also notes with flat-book clear_stale when it sees the
            # cancel result first; either path must not scream start ERROR.
            note_cancel_gone(
                reqId, code=errorCode, detail=errorString, clear_stale=True
            )
            return

        if errorCode in (2104, 2106, 2108, 2158):
            logger.debug(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")
            return

        if errorCode in self._SUPPRESSED_ERROR_CODES:
            logger.debug(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")
        elif errorCode == 202:  # Order cancelled — check attribution
            from abcxauto.riskless_combo import is_riskless_combo_202

            if is_riskless_combo_202(errorCode, errorString):
                self._riskless_combo_202 = True
                try:
                    from abcxauto.risk_gates import get_risk_gate

                    get_risk_gate().note_riskless_combo_202()
                except Exception:
                    logger.debug("riskless_combo_202 latch failed", exc_info=True)
                logger.warning(
                    f"IBKR [202] riskless-combo cancel: order {reqId} — {errorString}"
                )
            else:
                attr = self.get_cancel_attribution(reqId)
                if attr.get('kind') == 'self_cancel':
                    logger.debug(f"IBKR [202] self-cancel confirmed: order {reqId}")
                else:
                    logger.warning(f"IBKR [202] broker-cancel: order {reqId} — {errorString}")
        elif errorCode in (201, 10198):
            logger.warning(f"IBKR [{errorCode}] order rejected reqId={reqId}: {errorString}")
        else:
            logger.info(f"IBKR [{errorCode}] reqId={reqId}: {errorString}")

        # Durable journal line for order-scoped IBKR errors (not farm chatter).
        try:
            rid = int(reqId or 0)
        except (TypeError, ValueError):
            rid = 0
        if rid > 0 and errorCode not in self._SUPPRESSED_ERROR_CODES:
            try:
                from abcxauto.memory.notes import record_broker_order_note

                sym = ""
                try:
                    sym = str(getattr(contract, "symbol", None) or "").upper().strip()
                except Exception:
                    sym = ""
                record_broker_order_note(
                    symbol=sym,
                    order_id=rid,
                    error_code=errorCode,
                    detail=str(errorString or ""),
                )
            except Exception:
                logger.debug("broker order note from error failed", exc_info=True)

    def _schedule_book_refresh_after_restore(self) -> None:
        """Refresh positions/orders/account after 1101/1102; stay stale on failure.

        Runs ``_refresh_book_after_data_loss`` on the owning loop only.
        Never ``create_task`` on a foreign Flet/worker loop.
        """
        loop = self._resolve_loop()
        if loop is None:
            logger.error("IBKR restore: no event loop to refresh book — stay stale")
            return
        prev = getattr(self, "_book_refresh_task", None)
        if self._async_job_in_flight(prev):
            try:
                prev.cancel()
            except Exception:
                pass

        async def _run() -> None:
            try:
                bind_thread_loop(loop)
                ok = await self._refresh_book_after_data_loss()
                if ok:
                    self._ibkr_data_stale = False
                    logger.info("IBKR book refreshed after data restore")
                    self._maybe_resume_disconnect_halt(book_complete=True)
                else:
                    logger.error("IBKR book refresh failed — stay stale")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IBKR book refresh crashed — stay stale")

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._book_refresh_task = loop.create_task(_run())
            return

        def _spawn() -> None:
            bind_thread_loop(loop)
            self._book_refresh_task = loop.create_task(_run())

        try:
            self._book_refresh_task = asyncio.run_coroutine_threadsafe(_run(), loop)
        except Exception:
            try:
                loop.call_soon_threadsafe(_spawn)
            except Exception:
                logger.exception(
                    "IBKR restore: could not schedule book refresh — stay stale"
                )

    def _retry_stale_book_refresh(self) -> None:
        """Heartbeat retry: schedule refresh if stale, socket live, nothing in flight."""
        if not getattr(self, "_ibkr_data_stale", False):
            return
        if not self._api_socket_live():
            return
        bind_thread_loop(self._resolve_loop())
        if self._book_refresh_in_flight():
            return
        self._schedule_book_refresh_after_restore()

    async def _refresh_book_after_data_loss(self) -> bool:
        """Mandatory positions + open orders + account pull after data loss."""
        if not (self._connected or self._api_socket_live()):
            return False
        owner = self._resolve_loop()
        bind_thread_loop(owner)
        if owner is None:
            logger.error("IBKR book refresh: no owning loop — stay stale")
            return False
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            logger.exception("IBKR book refresh after data restore failed")
            return False
        if running is not owner:
            logger.error("IBKR book refresh on foreign loop — stay stale")
            return False
        try:
            async with self.async_lock:
                req_pos = getattr(self.ib, "reqPositionsAsync", None)
                if callable(req_pos):
                    await req_pos()
                req_ord = getattr(self.ib, "reqAllOpenOrdersAsync", None)
                if callable(req_ord):
                    await req_ord()
            await self._update_account_values()
            return True
        except Exception:
            logger.exception("IBKR book refresh after data restore failed")
            return False

    def _on_disconnect(self) -> None:
        """Handle disconnection from TWS/Gateway (API socket closed)."""
        self._mark_ibkr_data_stale(reason="api_disconnect")
        symbols = list(self._tickers.keys())
        if symbols:
            self._pending_resubscribe.update(s.upper() for s in symbols)
        self._tickers.clear()
        owned = getattr(self, "_quote_mkt_syms", None)
        if isinstance(owned, set):
            owned.clear()
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
        """Owning loop = IB socket loop first, then the loop captured at connect.

        Do not fall back to ``asyncio.get_running_loop()`` — that would
        schedule book refresh / reconnect on a foreign Flet or worker loop
        and raise ``IBKR async_lock is bound to a different event loop``.
        If no owning loop is running, return None (heartbeat will retry).
        """
        ib = getattr(self, "ib", None)
        ib_loop = getattr(ib, "loop", None) if ib is not None else None
        if self._loop_is_usable(ib_loop):
            return ib_loop
        captured = getattr(self, "_loop", None)
        if self._loop_is_usable(captured):
            return captured
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
                    await self._after_connect_restore()
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
                            f"client_id={self.client_id})."
                        )
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

    def _maybe_resume_disconnect_halt(self, *, book_complete: bool) -> None:
        """Disconnect-kind only. Fail closed if the broker or book is unknown."""
        try:
            connected = bool(self.connected)
        except Exception:
            connected = False
        try:
            from abcxauto.risk_gates import get_risk_gate

            get_risk_gate().maybe_resume_disconnect(
                broker_connected=connected,
                book_complete=bool(book_complete),
            )
        except Exception:
            logger.exception("disconnect-halt auto-resume check failed")

    async def _after_connect_restore(self) -> None:
        """Refresh the book after TWS/Gateway connect or reconnect.

        A disconnect-kind halt may auto-resume after a complete book.
        Other halt kinds still require operator resume().
        """
        self._disconnect_cause = DisconnectCause.UNKNOWN.value
        self._reconnect_requested = False
        self._heartbeat_failures = 0
        self._reconnect_attempt = 0
        self._disconnect_since = None
        # Leave _disconnect_halt_fired as-is so we do not re-halt on a later blip
        # in the same outage window; a fresh disconnect resets it in _on_disconnect.
        self._last_heartbeat_ok = time.time()
        self._invalidate_live_caches()

        # Streaming subscribe API removed; nothing to restore.
        n = len(self._pending_resubscribe)
        self._pending_resubscribe.clear()
        if n:
            logger.info(f"Cleared {n} pending market-data resubscribe symbol(s)")
        book_ok = False
        try:
            if await self._refresh_book_after_data_loss():
                self._ibkr_data_stale = False
                book_ok = True
            else:
                # Fresh process starts False; failure must block new risk until verified.
                self._ibkr_data_stale = True
                logger.error("post-reconnect book refresh failed - stay stale")
        except Exception:
            logger.exception("post-reconnect book refresh failed - stay stale")
            self._ibkr_data_stale = True
            book_ok = False
        self._maybe_resume_disconnect_halt(book_complete=book_ok)

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
                'time': fill_ts_iso(getattr(execution, "time", None)),
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
            aux = getattr(trade.order, "auxPrice", None)
            try:
                if aux is not None and float(aux) > 1e300:
                    aux = None
            except (TypeError, ValueError):
                aux = None
            event = {
                "order_id": trade.order.orderId,
                "symbol": symbol,
                "status": status,
                "order_type": order_type,
                "action": str(getattr(trade.order, "action", "") or "").upper(),
                "filled": trade.orderStatus.filled,
                "remaining": trade.orderStatus.remaining,
                "avg_fill_price": trade.orderStatus.avgFillPrice,
                "aux_price": aux,
                "stop_price": aux,
                "quantity": getattr(trade.order, "totalQuantity", None),
            }

            if status == 'Filled':
                logger.info(f"[OK] Order FILLED: {order_type} {symbol}")
            elif status in ('Cancelled', 'ApiCancelled'):
                logger.info(f"[X] Order CANCELLED: {order_type} {symbol}")
                try:
                    from abcxauto.memory.notes import record_broker_order_note

                    record_broker_order_note(
                        symbol=str(symbol or ""),
                        order_id=trade.order.orderId,
                        order_type=str(order_type or ""),
                        status=str(status or ""),
                    )
                except Exception:
                    logger.debug("broker order note from cancel failed", exc_info=True)

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

        Retries on the configured ``IBKR_CLIENT_ID`` only — never ``id + attempt``.
        Walking ids orphans the prior session's orders and breaks the one-id-per-process
        contract. Default attempts: ``IBKR_CONNECT_MAX_ATTEMPTS`` (1–50, default 12).

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
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        # 1100 / heartbeat may clear _connected while the API socket is still up.
        # Always keep that socket — even when the caller is on a foreign loop
        # (10197 competing session). Never replace IB() while it is live.
        if self._api_socket_live():
            if self._resolve_loop() is None and running is not None:
                self._loop = running
            bind_thread_loop(self._resolve_loop() or running)
            return self._adopt_live_socket()

        if running is not None:
            self._loop = running
            bind_thread_loop(running)

        async with self.async_lock:
            # Double-check after acquiring lock
            if self.connected:
                return True
            if self._api_socket_live():
                bind_thread_loop(self._resolve_loop() or running)
                return self._adopt_live_socket()

            for attempt in range(max_retries):
                try:
                    current_client_id = int(self.client_id)
                    logger.info(f"Connecting to IBKR ({self.host}:{self.port}, client_id={current_client_id}, attempt {attempt + 1})")

                    # Clean up old IB instance handlers before creating new one
                    self._unregister_handlers()
                    try:
                        if self.ib is not None and self.ib.isConnected():
                            self.ib.disconnect()
                    except Exception:
                        logger.debug("prior IB disconnect before retry failed", exc_info=True)

                    # Create fresh IB instance on each attempt — same client id.
                    self.ib = new_ib()

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
                        self._invalidate_live_caches()
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

            # Cancel quote-owned streaming subscriptions only
            for ticker in self._tickers.values():
                try:
                    self.ib.cancelMktData(ticker.contract)
                except Exception:
                    pass
            self._tickers.clear()
            owned = getattr(self, "_quote_mkt_syms", None)
            if isinstance(owned, set):
                owned.clear()
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

    async def _last_stop_for_leftover_stk(
        self,
        lot: dict,
        *,
        saved_stops: Dict[str, dict],
        live_orders: List[Dict[str, Any]],
    ) -> dict:
        """Leave a last-stop on leftover stock, or say why we could not."""
        covering = _covering_stop_order(lot, live_orders)
        if covering is not None:
            return {
                "protection": "still_working",
                "protection_order_id": covering.get("order_id"),
            }
        key = _flatten_lot_key(lot)
        saved = saved_stops.get(key) or {}
        px = saved.get("stop_price")
        if px is None:
            px = lot.get("market_price") or lot.get("avg_cost")
        try:
            stop_price = float(px)
        except (TypeError, ValueError):
            stop_price = 0.0
        qty = abs(int(_flatten_qty(lot)))
        symbol = str(lot.get("symbol") or "")
        action = "SELL" if _flatten_qty(lot) > 0 else "BUY"
        if stop_price <= 0 or qty <= 0 or not symbol:
            return {
                "protection": "none",
                "reason": "no last-stop price for leftover stock",
            }
        place = getattr(self, "place_stop_order", None)
        if not callable(place):
            return {"protection": "none", "reason": "place_stop_order missing"}
        try:
            placed = await place(symbol, action, qty, stop_price)
        except Exception as e:
            logger.critical(
                "FLATTEN ALL leftover STK %s last-stop raised: %s", symbol, e
            )
            return {"protection": "none", "reason": str(e)}
        if placed.get("success") or placed.get("order_id"):
            return {
                "protection": "last_stop",
                "protection_order_id": placed.get("order_id"),
                "stop_price": stop_price,
            }
        return {
            "protection": "none",
            "reason": placed.get("error") or "last-stop rejected",
            "order_result": placed,
        }

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
                tif="GTC",
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
        """Cancel working orders and close every lot. success only if the book is flat.

        Per-lot outcomes plus leftover lots (and why) go back to the caller.
        A leftover STK lot gets a last-stop or a loud unprotected failure.
        Close attempts are not skipped when a cancel is rejected.
        """
        if not await self._ensure_connected():
            return {
                "success": False,
                "status": "disconnected",
                "error": "Not connected",
                "failed": [],
                "remaining": [],
                "position_results": [],
                "positions_closed": 0,
                "positions_total": 0,
                "orders_cancelled": 0,
                "orders_total": 0,
                "orders_failed": [],
                "errors": ["Not connected"],
            }

        result: Dict[str, Any] = {
            "orders_cancelled": 0,
            "orders_total": 0,
            "orders_failed": [],
            "positions_closed": 0,
            "positions_total": 0,
            "position_results": [],
            "remaining": [],
            "failed": [],
            "errors": [],
        }

        open_orders: List[Dict[str, Any]] = []
        try:
            open_orders = list(await self.get_open_orders() or [])
        except Exception as e:
            err_msg = f"get_open_orders: {e}"
            logger.error(err_msg)
            result["errors"].append(err_msg)
        result["orders_total"] = len(open_orders)
        saved_stops = _index_stk_stops(open_orders)

        for order in open_orders:
            oid = order.get("order_id")
            if not oid:
                continue
            try:
                cancelled = await self.cancel_order(oid)
            except Exception as e:
                cancelled = {"error": str(e)}
            if _cancel_cleared(cancelled):
                result["orders_cancelled"] += 1
                continue
            why = ""
            if isinstance(cancelled, dict):
                why = str(cancelled.get("error") or "")
            why = why or "cancel rejected"
            row = {
                "order_id": oid,
                "symbol": order.get("symbol"),
                "reason": why,
            }
            result["orders_failed"].append(row)
            err_msg = f"cancel order {oid}: {why}"
            logger.error(err_msg)
            result["errors"].append(err_msg)

        await _safe_sleep(1)

        positions: List[Dict[str, Any]] = []
        try:
            positions = [
                p for p in (await self.get_positions() or []) if _flatten_qty(p)
            ]
        except Exception as e:
            err_msg = f"get_positions: {e}"
            logger.error(err_msg)
            result["errors"].append(err_msg)
        result["positions_total"] = len(positions)

        for pos in positions:
            symbol = pos.get("symbol", "?")
            try:
                pr = await self._flatten_one_position(pos)
            except Exception as e:
                pr = {
                    "success": False,
                    "symbol": symbol,
                    "conId": pos.get("conId") or pos.get("con_id"),
                    "error": str(e),
                    "reasoning": f"flatten {symbol} raised: {e}",
                }
            pr["lot_key"] = _flatten_lot_key(pos)
            if pr.get("conId") in (None, "none"):
                pr["conId"] = pos.get("conId") or pos.get("con_id")
            result["position_results"].append(pr)
            if not pr.get("success"):
                err_msg = f"flatten {symbol}: {pr.get('error') or pr}"
                logger.error(err_msg)
                result["errors"].append(err_msg)

        await _safe_sleep(1)

        remaining: Optional[List[Dict[str, Any]]] = None
        try:
            remaining = [
                p for p in (await self.get_positions() or []) if _flatten_qty(p)
            ]
        except Exception as e:
            err_msg = f"reread positions: {e}"
            logger.error(err_msg)
            result["errors"].append(err_msg)

        if remaining is None:
            result["book_unknown"] = True
            remaining = list(positions)

        remain_keys = {_flatten_lot_key(p) for p in remaining}
        before_keys = {_flatten_lot_key(p) for p in positions}
        result["positions_closed"] = len(before_keys - remain_keys)
        result["remaining"] = remaining

        pr_by_key = {
            str(pr.get("lot_key") or _flatten_lot_key(pr)): pr
            for pr in result["position_results"]
        }
        for pr in result["position_results"]:
            key = str(pr.get("lot_key") or "")
            if pr.get("method") == "noop":
                pr["status"] = "noop"
            elif key and key in remain_keys:
                pr["status"] = "failed"
                pr["success"] = False
                if not pr.get("error"):
                    pr["error"] = "lot still open after flatten"
            else:
                pr["status"] = "closed"

        live_orders: List[Dict[str, Any]] = []
        try:
            live_orders = list(await self.get_open_orders() or [])
        except Exception:
            live_orders = []

        failed: List[Dict[str, Any]] = []
        for lot in remaining:
            key = _flatten_lot_key(lot)
            pr = pr_by_key.get(key) or {}
            why = str(pr.get("error") or "lot still open after flatten")
            rec: Dict[str, Any] = {
                "symbol": lot.get("symbol"),
                "conId": lot.get("conId") or lot.get("con_id"),
                "sec_type": str(lot.get("sec_type") or lot.get("secType") or "STK"),
                "quantity": _flatten_qty(lot),
                "reason": why,
            }
            sec = str(rec["sec_type"]).upper()
            if sec.startswith("STK"):
                prot = await self._last_stop_for_leftover_stk(
                    lot, saved_stops=saved_stops, live_orders=live_orders
                )
                rec.update(prot)
                if prot.get("protection") in ("last_stop", "still_working"):
                    logger.critical(
                        "FLATTEN ALL leftover STK %s qty=%s protection=%s",
                        rec.get("symbol"),
                        rec.get("quantity"),
                        prot.get("protection"),
                    )
                else:
                    logger.critical(
                        "FLATTEN ALL leftover STK %s qty=%s UNPROTECTED: %s",
                        rec.get("symbol"),
                        rec.get("quantity"),
                        prot.get("reason") or why,
                    )
            failed.append(rec)
        result["failed"] = failed

        book_flat = not remaining and not result.get("book_unknown")
        result["success"] = bool(book_flat)
        if result.get("book_unknown"):
            result["status"] = "failed"
        elif book_flat:
            result["status"] = "flat"
        elif result["positions_closed"]:
            result["status"] = "partial"
        else:
            result["status"] = "failed"

        logger.critical(
            "FLATTEN ALL: success=%s status=%s cancelled %s/%s orders, "
            "closed %s/%s lots, remaining=%s failed=%s",
            result["success"],
            result["status"],
            result["orders_cancelled"],
            result["orders_total"],
            result["positions_closed"],
            result["positions_total"],
            len(remaining),
            len(failed),
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
        """Start background heartbeat on the owning IB loop."""
        self._stop_heartbeat()
        loop = self._resolve_loop()
        bind_thread_loop(loop)
        if loop is None:
            logger.error("IBKR heartbeat: no owning loop — not started")
            return

        def _spawn() -> None:
            bind_thread_loop(loop)
            existing = getattr(self, "_heartbeat_task", None)
            if self._async_job_in_flight(existing):
                return
            self._heartbeat_task = loop.create_task(self._heartbeat_loop())

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _spawn()
        else:
            try:
                loop.call_soon_threadsafe(_spawn)
            except Exception:
                logger.exception("IBKR heartbeat: could not schedule on owning loop")
                return
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
            bind_thread_loop(self._resolve_loop())
            while True:
                await _safe_sleep(self._heartbeat_interval_s())
                bind_thread_loop(self._resolve_loop())

                if not self.connected:
                    if self._api_socket_live():
                        self._connected = True
                        logger.warning(
                            "Heartbeat: API socket still up (stale=%s) — "
                            "keep socket; no new IB()",
                            self._ibkr_data_stale,
                        )
                        self._retry_stale_book_refresh()
                        continue
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
                    if self._api_socket_live():
                        logger.warning(
                            f"Heartbeat failed ({e}) — API socket still up; "
                            "keep socket; no new IB()"
                        )
                        self._retry_stale_book_refresh()
                        continue
                    self._connected = False
                    self._disconnect_cause = DisconnectCause.HEARTBEAT_FAILED.value
                    logger.warning(
                        f"Heartbeat failed ({e}) — failures={self._heartbeat_failures}"
                    )
                    self._schedule_reconnect(DisconnectCause.HEARTBEAT_FAILED.value)
                    continue

                self._retry_stale_book_refresh()
        except asyncio.CancelledError:
            pass


# ========== FACTORY FUNCTION ==========

def get_ibkr_connector() -> IBKRConnector:
    """Get singleton IBKR connector instance."""
    return IBKRConnector()
