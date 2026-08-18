from types import SimpleNamespace

from abcxauto.broker.connection import classify_error_code
from abcxauto.broker.connector import IBKRConnector


def test_apply_req_pnl_overwrites_daily_tag():
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._pnl = SimpleNamespace(dailyPnL=-41.25)
    out = conn._apply_req_pnl({"dailypnl": 0.0, "unrealizedpnl": -800.0})
    assert out["dailypnl"] == -41.25
    assert out["unrealizedpnl"] == -800.0
    conn._pnl = SimpleNamespace(dailyPnL=float("nan"))
    out = conn._apply_req_pnl({"dailypnl": -1.0})
    assert out["dailypnl"] == -1.0
    assert classify_error_code(1100) == "tws_lost"
    conn = IBKRConnector.__new__(IBKRConnector)
    conn._ibkr_data_stale = False
    conn._connected = True
    conn._reconnect_requested = False
    conn._disconnect_cause = "unknown"
    scheduled = []
    conn._schedule_reconnect = lambda reason: scheduled.append(reason)
    conn._on_error(-1, 1100, "Connectivity between IBKR and TWS has been lost.", "")
    assert conn._ibkr_data_stale is True
    assert conn._connected is True
    assert scheduled == []
