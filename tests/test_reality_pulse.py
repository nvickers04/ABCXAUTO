"""Reality Pulse — situational awareness heart."""

from abcxauto.reality_pulse import build_narrative, build_reality_pulse, pulse_clock_view
from abcxauto.cycle import AWARENESS_HEART, RULES


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
    assert str(view["clock"]).endswith(" CT")
    assert "EDT" not in str(view["clock"])
    assert "EST" not in str(view["clock"])


def test_desk_clock_is_chicago_ct_not_edt():
    """Glass 2026-08-26 12:46 CT was painted 1:46pm EDT. Desk is America/Chicago."""
    from datetime import datetime, timezone

    from abcxauto.reality_pulse import format_desk_clock

    now = datetime(2026, 8, 26, 17, 46, tzinfo=timezone.utc)
    assert format_desk_clock(now) == "12:46pm CT"
    pulse = build_reality_pulse(
        now=now,
        market_hours={"session": "regular", "is_trading_day": True, "minutes_to_close": 83},
    )
    assert pulse["time"]["local_clock"] == "12:46pm CT"
    assert pulse["time"]["timezone"] == "America/Chicago"
    assert "EDT" not in pulse["time"]["local_clock"]
    assert "1:46" not in pulse["time"]["local_clock"]


def test_mda_unix_updated_yields_age_not_na():
    """MDA quotes use Unix ``updated`` — must not show age n/a."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    updated = int((now - timedelta(seconds=12)).timestamp())
    pulse = build_reality_pulse(
        spy_quote={"last": 500.0, "updated": updated, "source": "marketdata_hybrid"},
        market_hours={"session": "regular", "is_trading_day": True},
    )
    age = pulse["data_freshness"]["mda_spy_quote_age_s"]
    assert age is not None
    assert 0 <= age < 60
    assert pulse["data_freshness"]["sources"]["mda_spy"] == "fresh"
    view = pulse_clock_view(pulse)
    assert view["data_age"] != "n/a"
    assert view["data_age"].endswith("s")
    assert "MDA age n/a" not in pulse["narrative"]
    assert "MDA data" in pulse["narrative"]


def test_ibkr_spy_quote_is_not_labeled_mda():
    pulse = build_reality_pulse(
        spy_quote={"last": 500.0, "source": "ibkr", "freshness": "live"},
        market_hours={"session": "regular", "is_trading_day": True},
        ibkr_connected=True,
    )
    assert pulse["data_freshness"]["sources"]["spy"] == "ibkr_live"
    assert pulse["data_freshness"]["sources"]["mda_spy"] == "unused"
    assert "SPY from IBKR live" in pulse["narrative"]
    assert "MDA data" not in pulse["narrative"]


def test_narrative_lists_mixed_instruments_separately():
    pulse = build_reality_pulse(positions=MIXED, market_hours={"session": "regular"})
    n = build_narrative(pulse)
    assert "SPY STK" in n and "OPT" in n
    assert "270639" in n and "999001" in n


def test_awareness_heart_in_system_rules():
    assert "SHELL" in AWARENESS_HEART
    assert "conId" in AWARENESS_HEART
    assert AWARENESS_HEART in RULES
    assert "Hold forbidden" in RULES or "hold forbidden" in RULES.lower()
