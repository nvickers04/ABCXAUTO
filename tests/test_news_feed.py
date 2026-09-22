"""Shared news feed formatting for agent + UI."""

import asyncio
import time

import pytest

from abcxauto.news_feed import (
    NEWS_SYMBOL_S,
    NEWS_TRIES,
    _CACHE,
    _universe,
    coalesce_news,
    fetch_agent_news,
    fetch_symbols_news,
    format_news_for_prompt,
    news_hard_miss,
    news_need_symbols,
    news_timed_out,
    public_news_item,
    public_news_items,
    remember_headlines,
    reset_news_cache,
)


@pytest.fixture(autouse=True)
def _clean_news_cache():
    reset_news_cache()
    yield
    reset_news_cache()


def test_public_news_item_keeps_publisher_published_drops_junk():
    raw = {
        "symbol": "AAPL",
        "headline": "Apple raises guidance",
        "publisher": "Yahoo",
        "published": "2026-09-17T12:00:00Z",
        "source": "mda",
        "freshness": "delayed_15m",
        "use": "color_not_trigger",
        "asof_iso": "2026-09-17T12:00:00Z",
        "asof": 1758103200,
        "url": "https://finance.yahoo.com/news/apple-raises-guidance",
        "junk": "blob",
        "raw": {"drop": True},
        "error": None,
    }
    out = public_news_item(raw)
    assert out is not None
    assert out["publisher"] == "Yahoo"
    assert out["published"] == "2026-09-17T12:00:00Z"
    assert out["headline"] == "Apple raises guidance"
    assert out["symbol"] == "AAPL"
    assert out["source"] == "mda"
    assert out["url"] == "https://finance.yahoo.com/news/apple-raises-guidance"
    assert "junk" not in out
    assert "raw" not in out
    assert "asof" not in out
    assert "error" not in out
    assert set(out) <= {
        "symbol",
        "headline",
        "publisher",
        "published",
        "as_of",
        "asof_iso",
        "url",
        "source",
        "freshness",
        "use",
    }
    assert public_news_item({
        "symbol": "NKE",
        "headline": "(unavailable - timed out)",
        "error": "timed out",
        "publisher": "Yahoo",
    }) is None
    rows = public_news_items([raw, {"headline": ""}, None])
    assert [row.get("publisher") for row in rows] == ["Yahoo"]


def test_public_news_item_yahoo_link_becomes_url_and_host():
    out = public_news_item(
        {
            "symbol": "NVDA",
            "headline": "NVDA prints",
            "publisher": "https://finance.yahoo.com/markets/stocks/articles/nvda-1.html",
            "source": "mda",
        }
    )
    assert out is not None
    assert out["publisher"] == "finance.yahoo.com"
    assert out["url"].startswith("https://finance.yahoo.com/")
    assert out["source"] == "mda"


def test_format_news_for_prompt_empty():
    text = format_news_for_prompt([])
    assert "NEWS" in text
    assert "no headlines" in text
    assert "MDA" not in text
    assert "color only" in text
    assert "not a trigger" in text


def test_format_news_for_prompt_items():
    text = format_news_for_prompt([
        {"symbol": "SPY", "headline": "Markets rally into the close"},
        {"symbol": "AAPL", "headline": "Apple supplier update"},
    ])
    assert "[SPY] Markets rally" in text
    assert "[AAPL] Apple supplier" in text
    assert "color only" in text
    assert "not a trigger" in text


def test_format_news_for_prompt_timeout_is_unavailable_not_empty():
    text = format_news_for_prompt([
        {"symbol": "NKE", "headline": "(unavailable - timed out)", "error": "timed out"},
    ])
    assert "unavailable" in text
    assert "timed out" in text
    assert "no headlines" not in text


def test_universe_is_book_not_sandbox_or_index():
    syms = _universe([{"symbol": "CRM"}, {"symbol": "crm"}])
    assert syms == ["CRM"]
    assert "SPY" not in syms
    assert "SNXX" not in syms
    assert "NVDA" not in syms
    assert len(syms) <= 14
    flat = _universe([])
    assert flat == []
    assert "SPY" not in flat


def test_universe_does_not_spy_seed():
    assert _universe([]) == []
    assert _universe([{"symbol": "MRVL"}]) == ["MRVL"]
    assert "SPY" not in _universe([{"symbol": "MRVL"}])


def test_universe_does_not_pad_spy_on_empty_book():
    assert _universe([{"symbol": "DECK"}]) == ["DECK"]
    assert _universe([]) == []
    assert "QQQ" not in _universe([])


def test_news_need_symbols_is_a_choice_not_a_spy_timeout():
    out = news_need_symbols()
    assert out == {
        "ok": False,
        "need": "symbols[]",
        "use": "color_not_trigger",
        "freshness": "delayed_15m",
        "source": "mda",
        "items": [],
        "note": "pass symbols[]",
        "fetched": False,
    }
    assert "SPY" not in str(out)
    assert "(unavailable" not in str(out)
    assert "timed out" not in str(out)


class _MDA:
    def __init__(self, impl):
        self.is_configured = True
        self.calls: list[str] = []
        self.timeouts: list[float] = []
        self._impl = impl

    async def get_stock_news(self, symbol, countback=4, timeout=None):
        self.calls.append(str(symbol).upper())
        if timeout is not None:
            self.timeouts.append(float(timeout))
        return await self._impl(symbol, countback, timeout)


def test_news_wait_is_fail_fast_not_a_12s_look():
    """2026-08-26: 12s sequential was the whole look. Cap stays shared batch < 12s."""
    assert NEWS_SYMBOL_S * max(1, int(NEWS_TRIES)) <= 8.0
    assert NEWS_SYMBOL_S < 12.0
    assert NEWS_TRIES == 1


def test_news_hard_miss_only_when_no_headlines():
    assert news_hard_miss([]) is None
    assert news_hard_miss([{"symbol": "NKE", "headline": "print"}]) is None
    assert (
        news_hard_miss(
            [
                {"symbol": "NKE", "headline": "print"},
                {"symbol": "AG", "headline": "(unavailable - timed out)", "error": "timed out"},
            ]
        )
        is None
    )
    assert news_hard_miss(
        [{"symbol": "HEI", "headline": "(unavailable - timed out)", "error": "timed out"}]
    ) == "timed out"


@pytest.mark.asyncio
async def test_timeout_is_not_empty_success(monkeypatch):
    async def hang(_symbol, _countback, _timeout=None):
        await asyncio.sleep(30)
        return [{"symbol": "NKE", "headline": "should not land"}]

    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["NKE"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_agent_news([{"symbol": "NKE"}])
    assert items == []
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in items
    )
    assert news_hard_miss(items) == "timed out"
    assert list(getattr(items, "timed_out", []) or news_timed_out()) == ["NKE"]
    assert client.calls == ["NKE"]
    assert not _CACHE["items"]


@pytest.mark.asyncio
async def test_timeout_does_not_retry_into_the_stall(monkeypatch):
    hits = {"n": 0}

    async def once_then_ok(symbol, _countback, _timeout=None):
        hits["n"] += 1
        if hits["n"] < 2:
            await asyncio.sleep(30)
            return []
        return [{"symbol": symbol, "headline": f"{symbol} printed"}]

    client = _MDA(once_then_ok)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["AG"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_agent_news([{"symbol": "AG"}])
    assert items == []
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in items
    )
    assert news_hard_miss(items) == "timed out"
    assert client.calls == ["AG"]
    assert hits["n"] == 1
    assert not _CACHE["items"]


@pytest.mark.asyncio
async def test_timeout_does_not_cache_so_next_look_refetches(monkeypatch):
    n = {"hits": 0}

    async def hang(_symbol, _countback, _timeout=None):
        n["hits"] += 1
        await asyncio.sleep(30)
        return []

    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["BE"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    first = await fetch_agent_news([{"symbol": "BE"}])
    second = await fetch_agent_news([{"symbol": "BE"}])
    assert first == []
    assert second == []
    assert not any(
        "(unavailable" in str(it.get("headline") or "")
        for it in first + second
    )
    assert news_hard_miss(second) == "timed out"
    assert n["hits"] == 2


@pytest.mark.asyncio
async def test_slow_source_does_not_eat_a_12s_look(monkeypatch):
    """Default cap, hanging MDA: miss in the fail-fast window, not 12s empty."""

    async def hang(_symbol, _countback, _timeout=None):
        await asyncio.sleep(30)
        return [{"symbol": _symbol, "headline": "should not land"}]

    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    t0 = time.monotonic()
    items = await fetch_symbols_news(["HEI", "WDAY", "GDDY", "SJM", "ROST"])
    elapsed = time.monotonic() - t0
    assert elapsed < 12.0
    assert elapsed < NEWS_SYMBOL_S + 2.0
    assert items == []
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in items
    )
    assert news_hard_miss(items) == "timed out"
    assert set(getattr(items, "timed_out", []) or news_timed_out()) == {
        "HEI",
        "WDAY",
        "GDDY",
        "SJM",
        "ROST",
    }
    assert client.calls == ["HEI", "WDAY", "GDDY", "SJM", "ROST"]


@pytest.mark.asyncio
async def test_shared_batch_deadline_not_per_symbol(monkeypatch):
    """Serialized MDA must burn one shared budget, not 2s × N after each slot."""
    gate = asyncio.Semaphore(1)
    seen_timeouts: list[float] = []

    async def gated(_symbol, _countback, timeout=None):
        # Burn the whole remaining budget while holding the only slot — under a
        # per-symbol fresh 2s this would cascade; shared deadline must not.
        budget = float(timeout) if timeout is not None else float(NEWS_SYMBOL_S)
        seen_timeouts.append(budget)
        async with gate:
            await asyncio.sleep(max(budget, 0.01) + 0.05)
            return []

    client = _MDA(gated)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.2)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    names = ["AVGO", "QQQ", "NVDA", "CRCL", "INTC"]
    t0 = time.monotonic()
    items = await fetch_symbols_news(names)
    elapsed = time.monotonic() - t0
    assert items == []
    assert news_hard_miss(items) == "timed out"
    assert elapsed < 0.2 + 0.35
    assert elapsed < 0.2 * len(names)
    assert set(getattr(items, "timed_out", []) or news_timed_out()) == set(names)
    assert seen_timeouts  # at least the first slot was entered under the batch budget


@pytest.mark.asyncio
async def test_partial_batch_returns_hits_and_timed_out(monkeypatch):
    """Fast names land; slow names are timed_out — not invented headlines."""

    async def mixed(symbol, _countback, _timeout=None):
        su = str(symbol).upper()
        if su in {"AMD", "SPY"}:
            return [{"symbol": su, "headline": f"{su} printed"}]
        await asyncio.sleep(30)
        return [{"symbol": su, "headline": "should not land"}]

    client = _MDA(mixed)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.1)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["AMD", "AVGO", "SPY", "NVDA"])
    assert [it.get("headline") for it in items] == ["AMD printed", "SPY printed"]
    assert news_hard_miss(items) is None
    assert set(getattr(items, "timed_out", []) or news_timed_out()) == {"AVGO", "NVDA"}
    assert not any(it.get("error") for it in items)
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in items
    )


@pytest.mark.asyncio
async def test_good_fetch_still_returns_items(monkeypatch):
    async def ok(symbol, _countback, _timeout=None):
        return [{"symbol": symbol, "headline": f"{symbol} printed"}]

    client = _MDA(ok)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["INTU", "FIG"])
    assert [it.get("headline") for it in items] == ["INTU printed", "FIG printed"]
    assert news_hard_miss(items) is None
    assert not any(it.get("error") for it in items)
    assert list(getattr(items, "timed_out", []) or news_timed_out()) == []


@pytest.mark.asyncio
async def test_completed_empty_fetch_is_still_empty(monkeypatch):
    async def none(_symbol, _countback, _timeout=None):
        return []

    client = _MDA(none)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["PANW"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_agent_news([{"symbol": "PANW"}])
    assert items == []
    assert format_news_for_prompt(items).count("no headlines") == 1
    assert client.calls == ["PANW"]


@pytest.mark.asyncio
async def test_timeout_returns_rail_headline_not_no_print(monkeypatch):
    """2026-08-27: HPQ Q3 was on What's happening while news HPQ timed out."""

    async def hang(_symbol, _countback, _timeout=None):
        await asyncio.sleep(30)
        return [{"symbol": "HPQ", "headline": "should not land"}]

    remember_headlines(
        [{"symbol": "HPQ", "headline": "HPQ Q3 earnings miss", "source": "mda"}]
    )
    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["HPQ"])
    assert news_hard_miss(items) is None
    assert items[0]["headline"] == "HPQ Q3 earnings miss"
    assert not items[0].get("error")
    assert "unavailable" not in str(items[0].get("headline"))
    text = format_news_for_prompt(items)
    assert "HPQ Q3 earnings miss" in text
    assert "no headlines" not in text
    assert "timed out" not in text


def test_coalesce_news_replaces_timeout_with_rail_print():
    remember_headlines([{"symbol": "HPQ", "headline": "HPQ Q3 earnings miss"}])
    out = coalesce_news(
        [{"symbol": "HPQ", "headline": "(unavailable - timed out)", "error": "timed out"}],
        ["HPQ"],
    )
    assert out[0]["headline"] == "HPQ Q3 earnings miss"
    assert not any(it.get("error") for it in out)


def test_coalesce_news_timeout_only_is_not_a_headline():
    out = coalesce_news(
        [
            {
                "symbol": "NVDA",
                "headline": "(unavailable - timed out)",
                "error": "timed out",
            }
        ],
        ["NVDA"],
    )
    assert out == []
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in out
    )


def test_coalesce_news_drops_cross_ticker_and_empty_symbol():
    out = coalesce_news(
        [
            {"symbol": "EXXON", "headline": "Exxon posts profit"},
            {"symbol": "", "headline": "oil patch note"},
            {"symbol": "NVDA", "headline": "NVDA chip demand"},
        ],
        ["NVDA"],
    )
    assert [it.get("symbol") for it in out] == ["NVDA"]
    assert [it.get("headline") for it in out] == ["NVDA chip demand"]


def test_coalesce_news_fills_nvda_timeout_from_remembered():
    remember_headlines([{"symbol": "NVDA", "headline": "NVDA printed"}])
    out = coalesce_news(
        [
            {
                "symbol": "NVDA",
                "headline": "(unavailable - timed out)",
                "error": "timed out",
            }
        ],
        ["NVDA"],
    )
    assert [it.get("headline") for it in out] == ["NVDA printed"]
    assert not any(it.get("error") for it in out)
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in out
    )


@pytest.mark.asyncio
async def test_timeout_only_fetch_has_no_unavailable_items(monkeypatch):
    async def hang(_symbol, _countback, _timeout=None):
        await asyncio.sleep(30)
        return [{"symbol": "NVDA", "headline": "should not land"}]

    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["NVDA"])
    assert items == []
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in items
    )
    assert news_hard_miss(items) == "timed out"


@pytest.mark.asyncio
async def test_asked_nvda_drops_exxon_and_empty_symbol(monkeypatch):
    async def junk(_symbol, _countback, _timeout=None):
        return [
            {"symbol": "EXXON", "headline": "Exxon posts profit"},
            {"symbol": "", "headline": "oil patch note"},
            {"symbol": "NVDA", "headline": "NVDA chip demand"},
        ]

    client = _MDA(junk)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["NVDA"])
    assert [it.get("symbol") for it in items] == ["NVDA"]
    assert [it.get("headline") for it in items] == ["NVDA chip demand"]
    assert not any(
        str(it.get("symbol") or "").upper() == "EXXON" for it in items
    )
    assert not any(not str(it.get("symbol") or "").strip() for it in items)


@pytest.mark.asyncio
async def test_remembered_nvda_fills_timeout_fetch(monkeypatch):
    async def hang(_symbol, _countback, _timeout=None):
        await asyncio.sleep(30)
        return [{"symbol": "NVDA", "headline": "should not land"}]

    remember_headlines([{"symbol": "NVDA", "headline": "NVDA printed"}])
    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["NVDA"])
    assert [it.get("headline") for it in items] == ["NVDA printed"]
    assert not any(it.get("error") for it in items)
    assert not any(
        "(unavailable" in str(it.get("headline") or "") for it in items
    )
