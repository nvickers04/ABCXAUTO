"""Shared news feed formatting for agent + UI."""

import asyncio

import pytest

from abcxauto.news_feed import (
    _CACHE,
    _universe,
    fetch_agent_news,
    format_news_for_prompt,
    reset_news_cache,
)


@pytest.fixture(autouse=True)
def _clean_news_cache():
    reset_news_cache()
    yield
    reset_news_cache()


def test_format_news_for_prompt_empty():
    text = format_news_for_prompt([])
    assert "NEWS" in text
    assert "no headlines" in text
    assert "MDA" not in text


def test_format_news_for_prompt_items():
    text = format_news_for_prompt([
        {"symbol": "SPY", "headline": "Markets rally into the close"},
        {"symbol": "AAPL", "headline": "Apple supplier update"},
    ])
    assert "[SPY] Markets rally" in text
    assert "[AAPL] Apple supplier" in text


def test_format_news_for_prompt_timeout_is_unavailable_not_empty():
    text = format_news_for_prompt([
        {"symbol": "NKE", "headline": "(unavailable - timed out)", "error": "timed out"},
    ])
    assert "unavailable" in text
    assert "timed out" in text
    assert "no headlines" not in text


def test_universe_is_book_not_sandbox_or_index(tmp_path, monkeypatch):
    from abcxauto.universe import reset_universe_cache, save_allowlist

    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe.json"))
    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": ["NVDA"],
            "exclude_symbols": [],
            "legal_symbols": ["SNXX", "AAOX", "NVDA"],
        }
    )
    reset_universe_cache()
    syms = _universe([{"symbol": "CRM"}, {"symbol": "crm"}])
    assert syms == ["CRM"]
    assert "SPY" not in syms
    assert "SNXX" not in syms
    assert "AAOX" not in syms
    assert "NVDA" not in syms
    assert len(syms) <= 14
    flat = _universe([])
    assert flat == []
    assert "SPY" not in flat
    assert "SNXX" not in flat


def test_universe_does_not_spy_seed(tmp_path, monkeypatch):
    from abcxauto.universe import reset_universe_cache, save_allowlist

    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe.json"))
    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": [],
            "exclude_symbols": [],
            "legal_symbols": ["NKE", "AG", "BE"],
        }
    )
    reset_universe_cache()
    assert _universe([]) == []
    assert _universe([{"symbol": "MRVL"}]) == ["MRVL"]


def test_universe_legal_miss_does_not_pad_spy(monkeypatch):
    def boom():
        raise RuntimeError("sandbox down")

    monkeypatch.setattr("abcxauto.universe.legal_symbols", boom)
    assert _universe([{"symbol": "DECK"}]) == ["DECK"]
    assert _universe([]) == []


class _MDA:
    def __init__(self, impl):
        self.is_configured = True
        self.calls: list[str] = []
        self._impl = impl

    async def get_stock_news(self, symbol, countback=4):
        self.calls.append(str(symbol).upper())
        return await self._impl(symbol, countback)


@pytest.mark.asyncio
async def test_timeout_is_not_empty_success(monkeypatch):
    async def hang(_symbol, _countback):
        await asyncio.sleep(30)
        return [{"symbol": "NKE", "headline": "should not land"}]

    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["NKE"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_agent_news([{"symbol": "NKE"}])
    assert items
    assert items[0].get("error") == "timed out"
    assert "unavailable" in str(items[0].get("headline"))
    assert client.calls == ["NKE", "NKE"]
    text = format_news_for_prompt(items)
    assert "no headlines" not in text
    assert "timed out" in text
    assert not _CACHE["items"]


@pytest.mark.asyncio
async def test_timeout_retries_then_lands(monkeypatch):
    hits = {"n": 0}

    async def once_then_ok(symbol, _countback):
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
    assert [it.get("headline") for it in items] == ["AG printed"]
    assert not any(it.get("error") for it in items)
    assert client.calls == ["AG", "AG"]
    assert _CACHE["items"][0]["headline"] == "AG printed"


@pytest.mark.asyncio
async def test_timeout_does_not_cache_so_next_look_refetches(monkeypatch):
    n = {"hits": 0}

    async def hang(_symbol, _countback):
        n["hits"] += 1
        await asyncio.sleep(30)
        return []

    client = _MDA(hang)
    monkeypatch.setattr("abcxauto.news_feed.NEWS_SYMBOL_S", 0.05)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["BE"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    first = await fetch_agent_news([{"symbol": "BE"}])
    second = await fetch_agent_news([{"symbol": "BE"}])
    assert first[0].get("error") == "timed out"
    assert second[0].get("error") == "timed out"
    assert n["hits"] == 4


@pytest.mark.asyncio
async def test_completed_empty_fetch_is_still_empty(monkeypatch):
    async def none(_symbol, _countback):
        return []

    client = _MDA(none)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["PANW"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_agent_news([{"symbol": "PANW"}])
    assert items == []
    assert format_news_for_prompt(items).count("no headlines") == 1
    assert client.calls == ["PANW"]
