"""Shared news feed formatting for agent + UI."""

import asyncio
import time

import pytest

from abcxauto.news_feed import (
    NEWS_BATCH_S,
    NEWS_BREAKER_COOLDOWN_S,
    NEWS_BREAKER_FAILURES,
    NEWS_SYMBOL_COLD_S,
    NEWS_SYMBOL_S,
    NEWS_TRIES,
    _CACHE,
    _FEED_BREAKER,
    _HEADLINES,
    _WARM_SYMBOLS,
    _universe,
    coalesce_news,
    fetch_agent_news,
    fetch_symbols_news,
    format_news_for_prompt,
    is_real_headline,
    news_batch_s,
    news_breaker_cooldown_s,
    news_breaker_failures,
    news_hard_miss,
    news_symbol_cold_s,
    news_symbol_s,
    remember_headlines,
    remembered_headlines,
    reset_news_cache,
)


def _fast_news_timeouts(monkeypatch, *, sym_s=0.05, cold_s=0.05, batch_s=1.0):
    monkeypatch.setattr("abcxauto.news_feed.news_symbol_s", lambda: sym_s)
    monkeypatch.setattr("abcxauto.news_feed.news_symbol_cold_s", lambda: cold_s)
    monkeypatch.setattr("abcxauto.news_feed.news_batch_s", lambda: batch_s)


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


def test_news_wait_is_bounded_not_a_12s_look():
    """Parallel batch + per-symbol caps stay below the old 12s look stall."""
    assert NEWS_SYMBOL_S < 12.0
    assert NEWS_SYMBOL_COLD_S <= NEWS_BATCH_S
    assert news_symbol_s() == NEWS_SYMBOL_S
    assert news_symbol_cold_s() == NEWS_SYMBOL_COLD_S
    assert news_batch_s() == NEWS_BATCH_S
    assert news_breaker_failures() == NEWS_BREAKER_FAILURES
    assert news_breaker_cooldown_s() == NEWS_BREAKER_COOLDOWN_S
    assert NEWS_TRIES == 1


def test_news_timeout_env_overrides_and_clamps(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_NEWS_SYMBOL_S", "99")
    monkeypatch.setenv("ABCXAUTO_NEWS_SYMBOL_COLD_S", "0.5")
    monkeypatch.setenv("ABCXAUTO_NEWS_BATCH_S", "1")
    monkeypatch.setenv("ABCXAUTO_NEWS_BREAKER_FAILURES", "1")
    monkeypatch.setenv("ABCXAUTO_NEWS_BREAKER_COOLDOWN_S", "10")
    assert news_symbol_s() == 15.0
    assert news_symbol_cold_s() == 15.0
    assert news_batch_s() == 4.0
    assert news_breaker_failures() == 2
    assert news_breaker_cooldown_s() == 15.0


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
    async def hang(_symbol, _countback):
        await asyncio.sleep(30)
        return [{"symbol": "NKE", "headline": "should not land"}]

    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["NKE"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_agent_news([{"symbol": "NKE"}])
    assert items
    assert items[0].get("error") == "timed out"
    assert "unavailable" in str(items[0].get("headline"))
    assert client.calls == ["NKE"]
    text = format_news_for_prompt(items)
    assert "no headlines" not in text
    assert "timed out" in text
    assert not _CACHE["items"]


@pytest.mark.asyncio
async def test_timeout_does_not_retry_into_the_stall(monkeypatch):
    hits = {"n": 0}

    async def once_then_ok(symbol, _countback):
        hits["n"] += 1
        if hits["n"] < 2:
            await asyncio.sleep(30)
            return []
        return [{"symbol": symbol, "headline": f"{symbol} printed"}]

    client = _MDA(once_then_ok)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["AG"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_agent_news([{"symbol": "AG"}])
    assert items[0].get("error") == "timed out"
    assert client.calls == ["AG"]
    assert hits["n"] == 1
    assert not _CACHE["items"]


@pytest.mark.asyncio
async def test_timeout_opens_breaker_and_stops_network(monkeypatch):
    n = {"hits": 0}

    async def hang(_symbol, _countback):
        n["hits"] += 1
        await asyncio.sleep(30)
        return []

    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._universe", lambda _p: ["BE"])
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    for _ in range(NEWS_BREAKER_FAILURES):
        out = await fetch_agent_news([{"symbol": "BE"}])
        assert out[0].get("error") == "timed out"
    assert n["hits"] == NEWS_BREAKER_FAILURES
    blocked = await fetch_agent_news([{"symbol": "BE"}])
    assert blocked[0].get("status") == "source_unavailable"
    assert blocked[0].get("error") == "source down"
    assert "no headlines" not in format_news_for_prompt(blocked)
    assert n["hits"] == NEWS_BREAKER_FAILURES
    assert blocked[0].get("retry_after_s", 0) > 0


@pytest.mark.asyncio
async def test_slow_source_does_not_eat_a_12s_look(monkeypatch):
    """Default cap, hanging MDA: miss in the fail-fast window, not 12s empty."""

    async def hang(_symbol, _countback):
        await asyncio.sleep(30)
        return [{"symbol": _symbol, "headline": "should not land"}]

    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch, sym_s=0.05, cold_s=0.05, batch_s=0.5)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    t0 = time.monotonic()
    items = await fetch_symbols_news(["HEI", "WDAY", "GDDY", "SJM", "ROST"])
    elapsed = time.monotonic() - t0
    assert elapsed < 12.0
    assert elapsed < news_batch_s() + 0.5
    assert items
    assert {it.get("error") for it in items} == {"timed out"}
    assert [it.get("symbol") for it in items] == ["HEI", "WDAY", "GDDY", "SJM", "ROST"]
    assert news_hard_miss(items) == "timed out"
    assert client.calls == ["HEI", "WDAY", "GDDY", "SJM", "ROST"]


@pytest.mark.asyncio
async def test_good_fetch_still_returns_items(monkeypatch):
    async def ok(symbol, _countback):
        return [{"symbol": symbol, "headline": f"{symbol} printed"}]

    client = _MDA(ok)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["INTU", "FIG"])
    assert [it.get("headline") for it in items] == ["INTU printed", "FIG printed"]
    assert news_hard_miss(items) is None
    assert not any(it.get("error") for it in items)


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


@pytest.mark.asyncio
async def test_timeout_returns_rail_headline_not_no_print(monkeypatch):
    """2026-08-27: HPQ Q3 was on What's happening while news HPQ timed out at 2s."""

    async def hang(_symbol, _countback):
        await asyncio.sleep(30)
        return [{"symbol": "HPQ", "headline": "should not land"}]

    remember_headlines(
        [{"symbol": "HPQ", "headline": "HPQ Q3 earnings miss", "source": "mda"}]
    )
    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["HPQ"])
    assert news_hard_miss(items) is None
    assert items[0]["headline"] == "HPQ Q3 earnings miss"
    assert items[0].get("stale") is True
    assert items[0].get("stale_age_s", 0) >= 0
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
    assert out[0].get("stale") is True
    assert out[0].get("stale_age_s", 0) >= 0
    assert not any(it.get("error") for it in out)


def test_remembered_headlines_marks_stale_from_bucket_ts(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr("abcxauto.news_feed.time.monotonic", lambda: t["now"])
    remember_headlines([{"symbol": "SPY", "headline": "Prior print"}])
    t["now"] = 1240.0
    rows = remembered_headlines(["SPY"])
    assert rows[0]["stale"] is True
    assert rows[0]["stale_age_s"] == 240
    assert is_real_headline(rows[0])


@pytest.mark.asyncio
async def test_fresh_fetch_is_not_marked_stale(monkeypatch):
    async def ok(symbol, _countback):
        return [{"symbol": symbol, "headline": f"{symbol} live"}]

    client = _MDA(ok)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["INTU"])
    assert items[0]["headline"] == "INTU live"
    assert not items[0].get("stale")
    assert "stale_age_s" not in items[0]


def test_remember_stale_does_not_reset_or_store_markers(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr("abcxauto.news_feed.time.monotonic", lambda: t["now"])
    remember_headlines([{"symbol": "SPY", "headline": "Print"}])
    t["now"] = 1300.0
    stale_row = remembered_headlines(["SPY"])[0]
    assert stale_row["stale_age_s"] == 300
    remember_headlines([stale_row])
    stored = _HEADLINES["SPY"]["items"][0]
    assert "stale" not in stored
    assert "stale_age_s" not in stored
    t["now"] = 1360.0
    again = remembered_headlines(["SPY"])[0]
    assert again["stale_age_s"] == 360


@pytest.mark.asyncio
async def test_breaker_serves_stale_headlines_when_open(monkeypatch):
    remember_headlines([{"symbol": "SPY", "headline": "Prior SPY print"}])

    async def hang(_symbol, _countback):
        await asyncio.sleep(30)
        return []

    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    for _ in range(NEWS_BREAKER_FAILURES):
        await fetch_symbols_news(["SPY"])
    items = await fetch_symbols_news(["SPY"])
    assert client.calls == ["SPY"] * NEWS_BREAKER_FAILURES
    assert news_hard_miss(items) is None
    assert items[0]["headline"] == "Prior SPY print"
    assert items[0].get("stale") is True
    assert items[0].get("stale_age_s", 0) >= 0


@pytest.mark.asyncio
async def test_breaker_success_resets_symbol(monkeypatch):
    calls = {"n": 0}

    async def flap(symbol, _countback):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(30)
            return []
        return [{"symbol": symbol, "headline": f"{symbol} live"}]

    client = _MDA(flap)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    miss = await fetch_symbols_news(["NOK"])
    assert miss[0].get("error") == "timed out"
    ok = await fetch_symbols_news(["NOK"])
    assert ok[0]["headline"] == "NOK live"
    assert news_hard_miss(ok) is None
    assert calls["n"] == 2

    async def hang(_symbol, _countback):
        calls["n"] += 1
        await asyncio.sleep(30)
        return []

    client._impl = hang
    for _ in range(NEWS_BREAKER_FAILURES):
        await fetch_symbols_news(["NOK"])
    assert calls["n"] == 2 + NEWS_BREAKER_FAILURES
    stale_blocked = await fetch_symbols_news(["NOK"])
    assert stale_blocked[0]["headline"] == "NOK live"
    assert stale_blocked[0].get("stale") is True
    assert calls["n"] == 2 + NEWS_BREAKER_FAILURES

    reset_news_cache()
    client.calls.clear()
    calls["n"] = 0
    for _ in range(NEWS_BREAKER_FAILURES):
        await fetch_symbols_news(["XYZ"])
    blocked = await fetch_symbols_news(["XYZ"])
    assert blocked[0].get("status") == "source_unavailable"
    assert blocked[0].get("error") == "source down"
    assert calls["n"] == NEWS_BREAKER_FAILURES


@pytest.mark.asyncio
async def test_feed_breaker_opens_after_all_miss_batches(monkeypatch):
    async def hang(_symbol, _countback):
        await asyncio.sleep(30)
        return []

    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    for _ in range(NEWS_BREAKER_FAILURES):
        await fetch_symbols_news(["AAA", "BBB"])
    assert _FEED_BREAKER["open_until"] > time.monotonic()
    before = len(client.calls)
    items = await fetch_symbols_news(["CCC"])
    assert len(client.calls) == before
    assert items[0].get("status") == "source_unavailable"
    assert news_hard_miss(items) == "source down"


@pytest.mark.asyncio
async def test_slow_success_within_new_budget(monkeypatch):
    """A 3.5s MDA response succeeds at 6s warm / 10s cold; it failed at 2s."""

    async def slow_ok(symbol, _countback):
        await asyncio.sleep(3.5)
        return [{"symbol": symbol, "headline": f"{symbol} late print"}]

    client = _MDA(slow_ok)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    items = await fetch_symbols_news(["SPY"])
    assert items[0]["headline"] == "SPY late print"
    assert news_hard_miss(items) is None
    assert "SPY" in _WARM_SYMBOLS


@pytest.mark.asyncio
async def test_cold_symbol_gets_longer_budget_than_warm(monkeypatch):
    async def paced(symbol, _countback):
        await asyncio.sleep(7.5)
        return [{"symbol": symbol, "headline": f"{symbol} ok"}]

    client = _MDA(paced)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    cold = await fetch_symbols_news(["NOK"])
    assert cold[0]["headline"] == "NOK ok"
    _HEADLINES.clear()
    warm = await fetch_symbols_news(["NOK"])
    assert warm[0].get("error") == "timed out"
    assert client.calls == ["NOK", "NOK"]


@pytest.mark.asyncio
async def test_two_timeouts_do_not_open_breaker(monkeypatch):
    n = {"hits": 0}

    async def hang(_symbol, _countback):
        n["hits"] += 1
        await asyncio.sleep(30)
        return []

    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    for _ in range(2):
        out = await fetch_symbols_news(["SPY"])
        assert out[0].get("error") == "timed out"
    assert n["hits"] == 2
    third = await fetch_symbols_news(["SPY"])
    assert third[0].get("error") == "timed out"
    assert n["hits"] == 3


@pytest.mark.asyncio
async def test_breaker_cooldown_uses_softer_default(monkeypatch):
    assert NEWS_BREAKER_COOLDOWN_S == 90.0
    assert NEWS_BREAKER_FAILURES == 4

    async def hang(_symbol, _countback):
        await asyncio.sleep(30)
        return []

    client = _MDA(hang)
    _fast_news_timeouts(monkeypatch)
    monkeypatch.setattr("abcxauto.news_feed._get_client", lambda: client)
    for _ in range(NEWS_BREAKER_FAILURES):
        await fetch_symbols_news(["NOK"])
    blocked = await fetch_symbols_news(["NOK"])
    assert blocked[0].get("status") == "source_unavailable"
    assert blocked[0].get("retry_after_s", 999) <= int(NEWS_BREAKER_COOLDOWN_S)
