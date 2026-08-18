"""Polymarket compact rows."""

from abcxauto.prediction_odds import compact_event


def test_compact_event_parses_string_prices():
    row = compact_event({
        "title": "Fed Decision in September?",
        "slug": "fed-september",
        "closed": False,
        "endDate": "2026-09-16T00:00:00Z",
        "volume": 1000,
        "markets": [{
            "question": "Will the Fed cut 25 bps?",
            "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.72", "0.28"]',
            "volume": "500",
            "endDate": "2026-09-16T00:00:00Z",
        }],
    })
    assert row["title"].startswith("Fed")
    assert row["url"] == "https://polymarket.com/event/fed-september"
    assert row["markets"][0]["implied"][0] == {"name": "Yes", "px": 0.72}


def test_compact_event_skips_closed():
    assert compact_event({"title": "x", "closed": True, "markets": []}) is None
