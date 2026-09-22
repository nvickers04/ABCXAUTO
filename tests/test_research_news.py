"""research_news: prior stamp vs price_asof; no error=clipped."""

from abcxauto.research_news import mark_headlines, news_package

PRICE_ASOF = "2026-09-22T16:00:00Z"


def test_headline_hour_before_is_prior():
    items = [
        {
            "symbol": "AAPL",
            "headline": "Apple raises guidance",
            "publisher": "Yahoo",
            "published": "2026-09-22T15:00:00Z",
        }
    ]
    out = mark_headlines(items, price_asof=PRICE_ASOF)
    assert len(out) == 1
    assert out[0]["prior"] is True
    assert out[0]["headline"] == "Apple raises guidance"
    assert out[0]["symbol"] == "AAPL"
    assert out[0]["publisher"] == "Yahoo"
    assert out[0]["published"] == "2026-09-22T15:00:00Z"


def test_headline_one_minute_before_is_not_prior():
    items = [
        {
            "symbol": "NVDA",
            "headline": "NVDA prints",
            "publisher": "Reuters",
            "published": "2026-09-22T15:59:00Z",
        }
    ]
    out = mark_headlines(items, price_asof=PRICE_ASOF)
    assert len(out) == 1
    assert out[0]["prior"] is False
    assert out[0]["headline"] == "NVDA prints"


def test_error_payload_is_unavailable_not_clipped():
    pkg = news_package(
        {"error": "timed out", "items": []},
        price_asof=PRICE_ASOF,
    )
    assert pkg["news"] == "unavailable"
    assert pkg["headlines"] == []
    assert pkg.get("error") != "clipped"
    assert "error" not in pkg
    assert set(pkg) == {"news_asof", "headlines", "news"}


def test_ok_payload_marks_headlines():
    pkg = news_package(
        {
            "source": "mda",
            "items": [
                {
                    "symbol": "AAPL",
                    "headline": "Apple raises guidance",
                    "publisher": "Yahoo",
                    "published": "2026-09-22T15:00:00Z",
                },
                {
                    "symbol": "NVDA",
                    "headline": "NVDA prints",
                    "publisher": "Reuters",
                    "published": "2026-09-22T15:59:00Z",
                },
            ],
        },
        price_asof=PRICE_ASOF,
    )
    assert pkg["news"] == "ok"
    assert pkg["news_asof"] == PRICE_ASOF
    assert [h["prior"] for h in pkg["headlines"]] == [True, False]
    assert pkg["headlines"][0]["headline"] == "Apple raises guidance"
