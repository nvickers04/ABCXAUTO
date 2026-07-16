"""IBKR order types — enum parity with ABC execution/order_types.py."""

from enum import Enum
from typing import Union


class IBKROrderType(str, Enum):
    """IBKR order types — use .value for IBKR API calls."""

    LIMIT = "LMT"
    MARKET = "MKT"
    STOP = "STP"
    STOP_LIMIT = "STP LMT"
    TRAIL = "TRAIL"
    TRAIL_LIMIT = "TRAIL LIMIT"
    MARKET_ON_CLOSE = "MOC"
    LIMIT_ON_CLOSE = "LOC"
    MARKET_ON_OPEN = "MOO"
    LIMIT_ON_OPEN = "LOO"
    MIDPRICE = "MIDPRICE"
    RELATIVE = "REL"
    SNAP_MID = "SNAP MID"
    FILL_OR_KILL = "FOK"
    IMMEDIATE_OR_CANCEL = "IOC"
    VWAP = "VWAP"
    TWAP = "TWAP"
    ICEBERG = "ICEBERG"


STOP_ORDER_TYPES = frozenset({
    IBKROrderType.STOP,
    IBKROrderType.STOP_LIMIT,
    IBKROrderType.TRAIL,
    IBKROrderType.TRAIL_LIMIT,
})

_STOP_ORDER_VALUES = frozenset(t.value for t in STOP_ORDER_TYPES)


def is_stop_order(order_type: Union[str, IBKROrderType]) -> bool:
    if isinstance(order_type, IBKROrderType):
        return order_type in STOP_ORDER_TYPES
    return order_type in _STOP_ORDER_VALUES


def is_limit_order(order_type: Union[str, IBKROrderType]) -> bool:
    limit_types = {
        IBKROrderType.LIMIT.value,
        IBKROrderType.STOP_LIMIT.value,
        IBKROrderType.TRAIL_LIMIT.value,
        IBKROrderType.LIMIT_ON_CLOSE.value,
        IBKROrderType.LIMIT_ON_OPEN.value,
    }
    if isinstance(order_type, IBKROrderType):
        return order_type.value in limit_types
    return order_type in limit_types


def is_market_order(order_type: Union[str, IBKROrderType]) -> bool:
    market_types = {
        IBKROrderType.MARKET.value,
        IBKROrderType.STOP.value,
        IBKROrderType.TRAIL.value,
        IBKROrderType.MARKET_ON_CLOSE.value,
        IBKROrderType.MARKET_ON_OPEN.value,
    }
    if isinstance(order_type, IBKROrderType):
        return order_type.value in market_types
    return order_type in market_types


def is_algo_order(order_type: Union[str, IBKROrderType]) -> bool:
    algo_types = {
        IBKROrderType.VWAP.value,
        IBKROrderType.TWAP.value,
        IBKROrderType.ICEBERG.value,
    }
    if isinstance(order_type, IBKROrderType):
        return order_type.value in algo_types
    return order_type in algo_types
