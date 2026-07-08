"""Protection report: positions matched to stop/target orders."""

from abcxauto.monitor import build_protection_report


def _pos(symbol, qty, sec_type="STK", **extra):
    return {"symbol": symbol, "quantity": qty, "sec_type": sec_type, **extra}


def _order(symbol, action, order_type, order_id=1, **extra):
    return {
        "order_id": order_id, "symbol": symbol, "sec_type": "STK",
        "action": action, "quantity": 10, "order_type": order_type, **extra,
    }


def test_protected_long_position():
    report = build_protection_report(
        [_pos("AAPL", 10, unrealized_pnl=50.0)],
        [
            _order("AAPL", "SELL", "STP", order_id=1, aux_price=140.0),
            _order("AAPL", "SELL", "LMT", order_id=2, lmt_price=170.0),
        ],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert entry["stop_orders"][0]["order_id"] == 1
    assert entry["target_orders"][0]["order_id"] == 2
    assert report["unprotected_symbols"] == []


def test_unprotected_position_flagged():
    report = build_protection_report([_pos("NVDA", 5)], [])
    entry = report["positions"][0]
    assert entry["protected"] is False
    assert "stop_loss" in entry["missing"]
    assert "take_profit" in entry["missing"]
    assert report["unprotected_symbols"] == ["NVDA"]


def test_stop_without_target_still_protected_but_missing_target():
    report = build_protection_report(
        [_pos("NVDA", 5)],
        [_order("NVDA", "SELL", "TRAIL", order_id=3)],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert entry["missing"] == ["take_profit"]
    assert report["unprotected_symbols"] == []


def test_short_position_uses_buy_side_orders():
    report = build_protection_report(
        [_pos("TSLA", -10)],
        [
            _order("TSLA", "BUY", "STP", order_id=4, aux_price=260.0),
            _order("TSLA", "SELL", "STP", order_id=5, aux_price=200.0),  # wrong side
        ],
    )
    entry = report["positions"][0]
    assert entry["protected"] is True
    assert [o["order_id"] for o in entry["stop_orders"]] == [4]


def test_option_positions_not_audited():
    report = build_protection_report([_pos("SPY", 1, sec_type="OPT")], [])
    entry = report["positions"][0]
    assert "protected" not in entry
    assert report["unprotected_symbols"] == []
