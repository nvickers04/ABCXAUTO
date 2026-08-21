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
