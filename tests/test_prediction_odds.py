"""Polymarket compact rows — crowd implied only, never a gate or last."""

import pytest

from abcxauto.prediction_odds import compact_event, fetch_odds


def _event(**market):
    return {
        "title": "Fed Decision in September?",
        "slug": "fed-september",
        "closed": False,
        "endDate": "2026-09-16T00:00:00Z",
        "volume": 1000,
        "markets": [market],
    }


def test_compact_event_parses_string_prices():
    row = compact_event(_event(
        question="Will the Fed cut 25 bps?",
        outcomes='["Yes", "No"]',
        outcomePrices='["0.72", "0.28"]',
        volume="500",
        endDate="2026-09-16T00:00:00Z",
    ))
    assert row["title"].startswith("Fed")
    assert row["url"] == "https://polymarket.com/event/fed-september"
    assert row["markets"][0]["implied"][0] == {"name": "Yes", "px": 0.72}


def test_compact_event_skips_closed():
    assert compact_event({"title": "x", "closed": True, "markets": []}) is None


def test_compact_event_does_not_invent_50_from_last():
    """Missing outcomePrices is a miss. last / mid / 50 is not a crowd book."""
    row = compact_event(_event(
        question="Will the Fed cut 25 bps?",
        outcomes='["Yes", "No"]',
        lastTradePrice="0.5",
        bestBid="0.49",
        bestAsk="0.51",
        last=665.2,
        mid=665.2,
        bid=664.9,
        ask=665.5,
    ))
    assert row is None


def test_compact_event_rejects_last_like_and_percent_prices():
    """50 / 665 are last or cents, not a [0, 1] implied. Do not emit them as px."""
    row = compact_event(_event(
        question="Will the Fed cut 25 bps?",
        outcomes='["Yes", "No"]',
        outcomePrices='["50", "665.2"]',
    ))
    assert row is None


def test_compact_event_rejects_nan_implied():
    row = compact_event(_event(
        question="Will the Fed cut 25 bps?",
        outcomes='["Yes", "No"]',
        outcomePrices='["NaN", "0.5"]',
    ))
    assert row["markets"][0]["implied"] == [{"name": "No", "px": 0.5}]


def test_compact_event_does_not_fill_missing_side():
    row = compact_event(_event(
        question="Will the Fed cut 25 bps?",
        outcomes='["Yes", "No"]',
        outcomePrices='["0.72"]',
    ))
    assert row["markets"][0]["implied"] == [{"name": "Yes", "px": 0.72}]


def test_compact_event_keeps_real_fifty_fifty_book():
    row = compact_event(_event(
        question="Will the Fed cut 25 bps?",
        outcomes='["Yes", "No"]',
        outcomePrices='["0.5", "0.5"]',
    ))
    assert row["markets"][0]["implied"] == [
        {"name": "Yes", "px": 0.5},
        {"name": "No", "px": 0.5},
    ]


@pytest.mark.asyncio
async def test_fetch_odds_no_query_does_not_invent_spy():
    class Boom:
        async def get(self, *a, **k):
            raise AssertionError("odds must not invent a SPY search")

    out = await fetch_odds(client=Boom())
    assert out["events"] == []
    assert out["searched"] == []
    assert out["use"] == "crowd_odds_not_send_geometry"
    assert "SPY" not in str(out)
    assert "S&P" not in str(out)


@pytest.mark.asyncio
async def test_fetch_odds_positions_still_search():
    seen: list[str] = []

    class Client:
        async def get(self, url, params=None):
            seen.append(str((params or {}).get("q") or ""))

            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"events": []}

            return Resp()

    out = await fetch_odds(
        positions=[{"symbol": "QQQ", "quantity": 1}],
        client=Client(),
    )
    assert seen == ["Nasdaq"]
    assert out["searched"] == ["Nasdaq"]
    assert out["events"] == []
