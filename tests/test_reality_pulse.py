"""Reality Pulse — situational awareness heart."""

from abcxauto.reality_pulse import build_narrative, build_reality_pulse, pulse_clock_view
from abcxauto.rocket import AWARENESS_HEART, RULES


MIXED = [
    {
        "conId": 270639,
        "symbol": "SPY",
        "sec_type": "STK",
        "quantity": 1,
        "avg_cost": 741.39,
        "market_price": 742.46,
        "unrealized_pnl": -1.07,
    },
    {
        "conId": 999001,
        "symbol": "SPY",
        "sec_type": "OPT",
        "quantity": 1,
        "expiration": "20260726",
        "strike": 74.0,
        "right": "C",
        "unrealized_pnl": 12.0,
    },
]


def test_reality_pulse_has_required_heart_fields():
    pulse = build_reality_pulse(
        account={"netliquidation": 100000, "unrealizedpnl": 5},
        positions=MIXED,
        open_orders=[],
        market_hours={
            "session": "regular",
            "is_trading_day": True,
            "minutes_to_close": 83,
        },
        spy_quote={"last": 500, "timestamp": "2026-07-09T18:00:00+00:00"},
        ibkr_connected=True,
    )
    assert "time" in pulse and pulse["time"]["day_of_week"]
    assert pulse["session"]["status"] == "regular"
    assert pulse["session"]["countdown_to"] == "close"
    assert pulse["tradable_now"]["equity_rth"] is True
    assert pulse["tradable_now"]["options"] is True
    assert len(pulse["position_ledger"]) == 2
    assert pulse["position_ledger"][0]["conId"] == 270639
    assert pulse["position_ledger"][1]["secType"].startswith("OPT")
    assert "account" in pulse
    assert "awareness_checklist" in pulse and len(pulse["awareness_checklist"]) >= 4
    assert "Current reality:" in pulse["narrative"]
    assert "conId=270639" in pulse["narrative"]
    assert "conId=999001" in pulse["narrative"]


def test_pulse_clock_view_compact():
    pulse = build_reality_pulse(
        market_hours={"session": "premarket", "minutes_to_open": 45, "is_trading_day": True}
    )
    view = pulse_clock_view(pulse)
    assert view["session_status"] == "premarket"
    assert "countdown" in view
    assert view["narrative"]


def test_narrative_lists_mixed_instruments_separately():
    pulse = build_reality_pulse(positions=MIXED, market_hours={"session": "regular"})
    n = build_narrative(pulse)
    assert "SPY STK" in n and "OPT" in n
    assert "270639" in n and "999001" in n


def test_awareness_heart_in_system_rules():
    assert "REALITY PULSE" in AWARENESS_HEART or "Reality" in AWARENESS_HEART or "awareness" in AWARENESS_HEART.lower()
    assert "awareness_checklist" in AWARENESS_HEART or "checklist" in AWARENESS_HEART.lower()
    assert AWARENESS_HEART in RULES or "SITUATIONAL AWARENESS" in RULES
