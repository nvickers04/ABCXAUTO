"""Polymarket compact rows — crowd implied only, never a gate or last."""

import pytest

from abcxauto.prediction_odds import compact_event, fetch_odds, related_search_set


def _event(**market):
    return {
        "title": "Fed Decision in September?",
        "slug": "fed-september",
        "closed": False,
        "endDate": "2026-09-16T00:00:00Z",
        "volume": 1000,
        "markets": [market],
    }


def _client(seen: list[str]):
    class Client:
        async def get(self, url, params=None):
            seen.append(str((params or {}).get("q") or ""))

            class Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"events": []}

            return Resp()

    return Client()


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
    assert row["kind"] == "rates"
    assert "pct" not in row
    assert "%" not in row["kind"]


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


def test_compact_event_kind_from_title_question():
    book = {
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.4", "0.6"]',
    }
    earn = compact_event({
        "title": "Nvidia Q2 earnings",
        "slug": "nvda-earn",
        "markets": [{**book, "question": "Will Nvidia beat EPS?"}],
    })
    assert earn["kind"] == "earnings"

    q2 = compact_event({
        "title": "NVIDIA (NVDA) Q2 Data Center Revenue",
        "slug": "nvda-rev",
        "markets": [{**book, "question": "Will revenue be above 40B?"}],
    })
    assert q2["kind"] == "earnings"

    company = compact_event({
        "title": "Will Nvidia be the largest company?",
        "slug": "nvda-mcap",
        "markets": [{**book, "question": "Highest market cap?"}],
    })
    assert company["kind"] == "company"

    index = compact_event({
        "title": "S&P 500 year end",
        "slug": "spx-ye",
        "markets": [{**book, "question": "Will the S&P 500 close above 6000?"}],
    })
    assert index["kind"] == "index"

    other = compact_event({
        "title": "World Cup winner",
        "slug": "wc",
        "markets": [{**book, "question": "Will Brazil win the World Cup?"}],
    })
    assert other["kind"] == "other"


def test_related_search_set_nvda_includes_earnings_not_fed():
    fan = related_search_set(["NVDA"], "")
    assert fan[0] == "Nvidia"
    assert "Nvidia earnings" in fan
    assert "NVDA" in fan
    assert "Fed" not in fan
    assert "CPI" not in fan
    assert "SPY" not in fan
    assert all("S&P" not in q for q in fan)


def test_related_search_set_empty_does_not_invent_spy():
    assert related_search_set([], "") == []
    assert related_search_set([], "  ") == []


def test_related_search_set_query_nvda_fans_earnings():
    fan = related_search_set([], "NVDA")
    assert fan[0] == "Nvidia"
    assert "Nvidia earnings" in fan
    assert "NVDA" in fan
    assert "Fed" not in fan
    assert "SPY" not in fan


def test_related_search_set_fed_query_does_not_invent_spy():
    fan = related_search_set([], "Fed September")
    assert fan[0] == "Fed September"
    assert "CPI" in fan
    assert "SPY" not in fan
    assert all("earnings" not in q.lower() for q in fan)


def test_related_search_set_index_can_include_macro():
    fan = related_search_set(["SPY"], "")
    assert "S&P 500" in fan
    assert "S&P 500 earnings" in fan
    assert "Fed" in fan
    assert "CPI" in fan


@pytest.mark.asyncio
async def test_fetch_odds_no_query_does_not_invent_spy():
    class Boom:
        async def get(self, *a, **k):
            raise AssertionError("odds must not invent a SPY search")

    out = await fetch_odds(client=Boom())
    assert out["events"] == []
    assert out["searched"] == []
    assert out["related_queries"] == []
    assert out["note"] == "no_query"
    assert out["use"] == "crowd_odds_not_send_geometry"
    assert "SPY" not in str(out)
    assert "S&P" not in str(out)


@pytest.mark.asyncio
async def test_fetch_odds_nvda_fans_earnings_not_index_tape():
    seen: list[str] = []
    out = await fetch_odds(symbols=["NVDA"], client=_client(seen))
    assert out["searched"][0] == "Nvidia"
    assert "Nvidia earnings" in out["searched"]
    assert "NVDA" in out["searched"]
    assert seen == out["searched"]
    assert "Fed" not in out["searched"]
    assert "CPI" not in out["searched"]
    assert "SPY" not in out["searched"]
    assert "S&P" not in str(out["searched"])
    assert "SPY" not in out["related_queries"]
    assert "Fed" not in out["related_queries"]


@pytest.mark.asyncio
async def test_fetch_odds_positions_still_search():
    seen: list[str] = []
    out = await fetch_odds(
        positions=[{"symbol": "QQQ", "quantity": 1}],
        client=_client(seen),
    )
    assert "Nasdaq" in seen
    assert "Nasdaq" in out["searched"]
    assert any("earnings" in q.lower() for q in out["searched"])
    assert seen == out["searched"]
    assert out["events"] == []
    assert "SPY" not in out["searched"]
    assert "S&P" not in str(out["searched"])


@pytest.mark.asyncio
async def test_fetch_odds_overflow_earnings_go_to_related_queries():
    seen: list[str] = []
    out = await fetch_odds(
        symbols=["NVDA", "AAPL", "MSFT", "TSLA"],
        client=_client(seen),
    )
    assert out["searched"] == ["Nvidia", "Apple", "Microsoft", "Tesla"]
    assert any(q.endswith("earnings") for q in out["related_queries"])
    assert "Nvidia earnings" in out["related_queries"]
    assert seen == out["searched"]
    assert "SPY" not in out["searched"]
    assert "Fed" not in out["searched"]
