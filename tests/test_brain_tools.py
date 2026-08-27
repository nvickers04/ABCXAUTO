"""Grok tools: IBKR live vs MDA delayed."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.brain import (
    AGENT_TOOLS,
    BrainTurn,
    _apply_candle_session,
    _candle_res_from_tape,
    _clip,
    _compact_chain,
    _news_symbols_for_scan,
    _news_symbols_this_look,
    _run_tool,
    _stash_live,
)
from abcxauto.broker.quotes import quote_from_ticker
from abcxauto.world_state import WorldState


def _names_of(tools) -> set[str]:
    names = set()
    for t in tools:
        fn = getattr(t, "function", None)
        names.add(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    return names


def _tool_names() -> set[str]:
    return _names_of(AGENT_TOOLS)


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def test_agent_tools_cover_ibkr_and_mda():
    names = _tool_names()
    assert {
        "book",
        "status",
        "quote",
        "fills",
        "news",
        "odds",
        "scan",
        "candles",
        "option_chain",
        "option_quote",
        "option_facts",
        "send",
        "self_tune",
        "playbook",
        "write_lab_playbook",
    } <= names
    assert "set_wake" not in names
    assert "journal" not in names
    assert "universe" not in names
    assert "strategies" not in names


def test_quote_from_ticker_skips_nan():
    t = SimpleNamespace(
        contract=SimpleNamespace(symbol="SPY"),
        last=float("nan"),
        close=501.25,
        bid=501.2,
        ask=501.3,
        impliedVolatility=None,
        modelGreeks=None,
    )
    q = quote_from_ticker(t)
    assert q["source"] == "ibkr"
    assert q["freshness"] == "live"
    assert q["last"] == pytest.approx(501.25)
    assert q["mid"] == pytest.approx(501.25)
    assert q["asof"]
    assert q["use"] == "ibkr_live_for_decisions"


def test_quote_from_ticker_does_not_use_prior_close():
    t = SimpleNamespace(
        contract=SimpleNamespace(symbol="SPY"),
        last=None,
        close=480.0,
        bid=None,
        ask=None,
        impliedVolatility=None,
        modelGreeks=None,
    )
    q = quote_from_ticker(t)
    assert q["last"] is None
    assert q["mid"] is None
    assert q["close"] == pytest.approx(480.0)
    assert "change_pct" not in q
    assert "open_gap_pct" not in q


def test_quote_from_ticker_stamps_session_gap():
    t = SimpleNamespace(
        contract=SimpleNamespace(symbol="MU"),
        last=888.0,
        open=935.0,
        close=967.0,
        bid=887.5,
        ask=888.5,
        impliedVolatility=None,
        modelGreeks=None,
    )
    q = quote_from_ticker(t)
    assert q["last"] == pytest.approx(888.0)
    assert q["open"] == pytest.approx(935.0)
    assert q["close"] == pytest.approx(967.0)
    assert q["change_pct"] == pytest.approx((888.0 / 967.0 - 1.0) * 100.0, abs=0.002)
    assert q["open_gap_pct"] == pytest.approx((935.0 / 967.0 - 1.0) * 100.0, abs=0.002)


def test_quote_batch_follows_the_scan_sweep():
    from abcxauto.broker.quotes import quote_batch_cap
    from abcxauto.opportunity_scan import SCAN_QUOTE_CAP

    assert quote_batch_cap() >= SCAN_QUOTE_CAP


def test_stash_live_records_ibkr_only():
    world = _world()
    snap: dict = {}
    _stash_live(world, snap, {"symbol": "QQQ", "last": 400.0, "source": "ibkr"})
    _stash_live(world, snap, {"symbol": "IWM", "last": 200.0, "source": "mda"})
    assert snap["ibkr_live_quotes"]["QQQ"] == 400.0
    assert "IWM" not in snap.get("ibkr_live_quotes", {})
    assert world.ibkr_live_symbol == "QQQ"


def test_scan_sweep_does_not_become_the_desk_last():
    world = _world()
    snap: dict = {}
    _stash_live(
        world,
        snap,
        {"symbol": "SPY", "last": 764.0, "source": "ibkr"},
        mark=True,
    )
    _stash_live(
        world,
        snap,
        {"symbol": "QBTX", "last": 8.07, "source": "ibkr"},
        mark=False,
    )
    assert snap["ibkr_live_quotes"]["QBTX"] == 8.07
    assert snap["ibkr_live_last"] == 764.0
    assert world.ibkr_live_symbol == "SPY"


def test_compact_chain_clips_strikes():
    raw = {
        "symbol": "SPY",
        "strikes": list(range(400, 601)),
        "expirations": [{"expiration": "20260821", "dte": 8}],
        "source": "ibkr",
    }
    out = _compact_chain(raw, last=500.0)
    assert out["n_strikes"] == 201
    assert max(out["strikes"]) <= 560
    assert min(out["strikes"]) >= 440


def test_compact_chain_wrong_last_does_not_dump_itm_head():
    raw = {
        "symbol": "QQQ",
        "strikes": list(range(200, 801)),
        "expirations": [{"expiration": "20260821", "dte": 8}],
        "source": "ibkr",
    }
    out = _compact_chain(raw, last=0.75)
    assert min(out["strikes"]) > 300
    assert "median" in (out.get("strike_note") or "")


@pytest.mark.asyncio
async def test_quote_tool_uses_ibkr_not_mda(monkeypatch):
    called = {"mda": 0}

    class BoomMDA:
        async def get_quote(self, *_a, **_k):
            called["mda"] += 1
            return {"last": 1, "source": "marketdata"}

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: BoomMDA())

    class Conn:
        async def get_live_quote(self, symbol):
            return {"symbol": symbol, "last": 501.0, "bid": 500.9, "ask": 501.1, "source": "ibkr", "freshness": "live"}

    world = _world()
    snap: dict = {}
    raw = await _run_tool(
        "quote",
        {"symbol": "SPY"},
        connector=Conn(),
        world=world,
        snap=snap,
        turn=BrainTurn(),
    )
    data = json.loads(raw)
    assert data["source"] == "ibkr"
    assert data["last"] == 501.0
    assert called["mda"] == 0
    assert snap["ibkr_live_quotes"]["SPY"] == 501.0


@pytest.mark.asyncio
async def test_news_fetches_symbols_in_parallel(monkeypatch):
    """Serial fetching was the 20s timeout: 8 symbols x MDA latency overran it."""
    import asyncio as _asyncio

    concurrent = 0
    peak = 0

    class MDA:
        is_configured = True

        async def get_stock_news(self, symbol, countback=4):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            try:
                await _asyncio.sleep(0.05)
                return [{"symbol": symbol, "headline": f"{symbol} head"}]
            finally:
                concurrent -= 1

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    syms = ["SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "USO", "XLE"]
    data = json.loads(
        await _run_tool(
            "news",
            {"symbols": syms},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert [it["symbol"] for it in data["items"]] == syms  # order preserved
    assert peak > 1, "symbols must not be fetched one at a time"


def test_bare_news_uses_the_scan_tape_not_spy():
    world = _world()
    world.scan_fetched = ["ALB", "NKE", "LULU"]
    snap = {
        "scan_hits": {
            "rows": [
                {"symbol": "ALB", "open_gap_pct": -3.8},
                {"symbol": "NKE", "open_gap_pct": -3.3},
            ]
        }
    }
    assert _news_symbols_this_look(world, snap, [])[:3] == ["ALB", "NKE", "LULU"]
    assert "SPY" not in _news_symbols_this_look(world, snap, [])


@pytest.mark.asyncio
async def test_bare_news_tool_asks_scan_names(monkeypatch):
    seen: list[str] = []

    async def _news(syms, **_k):
        seen.extend(syms)
        return [{"symbol": s, "headline": f"{s} head"} for s in syms]

    monkeypatch.setattr("abcxauto.brain._mda_news", _news)
    world = _world()
    world.scan_fetched = ["ALB", "NKE"]
    data = json.loads(
        await _run_tool("news", {}, connector=None, world=world, snap={}, turn=BrainTurn())
    )
    assert seen[:2] == ["ALB", "NKE"]
    assert data["items"][0]["symbol"] == "ALB"


@pytest.mark.asyncio
async def test_one_stalled_news_symbol_does_not_sink_the_tool(monkeypatch):
    """MDA allows 30s per request; the tool budget is 20s. Cap each symbol."""
    import asyncio as _asyncio

    monkeypatch.setattr("abcxauto.brain.NEWS_SYMBOL_S", 0.05)

    class MDA:
        is_configured = True

        async def get_stock_news(self, symbol, countback=4):
            if symbol == "HANG":
                await _asyncio.sleep(30)
            return [{"symbol": symbol, "headline": f"{symbol} head"}]

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    data = json.loads(
        await _run_tool(
            "news",
            {"symbols": ["SPY", "HANG", "QQQ"]},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    # The stalled symbol is simply absent; the others still land.
    assert [it["symbol"] for it in data["items"]] == ["SPY", "QQQ"]


@pytest.mark.asyncio
async def test_news_is_labeled_delayed_but_candles_never_serves_mda(monkeypatch):
    """news is MDA and says so. candles promises IBKR, so a miss must error.

    2026-08-20: the bars mixin was off the connector MRO, candles fell through
    to MDA, and Grok read the prior session as today's intraday structure.
    """
    async def fake_news(_pos=None, **_k):
        return [{"symbol": "SPY", "headline": "Tape note"}]

    class MDA:
        is_configured = True

        async def get_stock_candles(self, symbol, resolution="D", countback=60, **_k):
            raise AssertionError("candles must never reach MDA")

        async def get_stock_news(self, symbol, countback=4):
            return [{"symbol": symbol, "headline": "Head"}]

    monkeypatch.setattr("abcxauto.news_feed.fetch_agent_news", fake_news)
    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    world = _world()
    news = json.loads(
        await _run_tool("news", {}, connector=None, world=world, snap={}, turn=BrainTurn())
    )
    assert news["source"] == "mda"
    assert "delayed" in news["freshness"]
    candles = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SPY", "resolution": "D", "countback": 20},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert candles["source"] == "ibkr"
    assert candles["freshness"] == "ibkr_miss"
    assert "bar feed" in candles["error"]
    assert not candles.get("bars")
    assert "mda_last_is" not in candles


@pytest.mark.asyncio
async def test_scan_symbols_returns_hits_not_mda_tape(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("scan must not fetch MDA daily metrics")

    monkeypatch.setattr("abcxauto.opportunity_scan.fetch_scan_metrics", boom)
    data = json.loads(
        await _run_tool(
            "scan",
            {"symbols": ["QQQ"]},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert data["symbols"] == ["QQQ"]
    assert data["hits"][0]["symbol"] == "QQQ"
    assert "on_book" in data["hits"][0]
    assert "tape" not in data
    assert "mda_last" not in data["hits"][0]
    assert "last" not in data["hits"][0]


@pytest.mark.asyncio
async def test_scan_union_survives_a_later_empty_screen(monkeypatch):
    """An empty mega/large sort must not wipe names from an earlier tape."""
    payloads = [
        {
            "ok": True,
            "source": "ibkr",
            "symbols": ["NVDA", "MU"],
            "hits": [{"symbol": "NVDA", "last": 210.0}, {"symbol": "MU", "last": 900.0}],
            "quoted": 2,
        },
        {
            "ok": True,
            "source": "empty",
            "symbols": [],
            "hits": [],
            "quoted": 0,
        },
    ]

    async def _fake_scan(**_kw):
        return payloads.pop(0)

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    await _run_tool("scan", {"scan_code": "MOST_ACTIVE"}, connector=None, world=world, snap=snap, turn=BrainTurn())
    await _run_tool("scan", {"arena": "mega_cap", "scan_code": "TOP_PERC_LOSE"}, connector=None, world=world, snap=snap, turn=BrainTurn())
    assert world.scan_fetched == ["NVDA", "MU"]
    assert snap["scan_fetched"] == ["NVDA", "MU"]


@pytest.mark.asyncio
async def test_third_scan_this_look_reuses_merged_hits(monkeypatch):
    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        return {
            "ok": True,
            "source": "ibkr",
            "arena": "mega_cap",
            "scan_code": "TOP_PERC_LOSE",
            "symbols": ["SNDK"],
            "hits": [{"symbol": "SNDK", "open_gap_pct": -6.5, "last": 1485.0}],
            "quoted": 1,
        }

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    first = json.loads(
        await _run_tool(
            "scan",
            {"scan_code": "MOST_ACTIVE"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    second = json.loads(
        await _run_tool(
            "scan",
            {"arena": "mega_cap", "scan_code": "TOP_PERC_LOSE"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    third = json.loads(
        await _run_tool(
            "scan",
            {"scan_code": "LOW_OPEN_GAP"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    assert n["calls"] == 2
    assert first.get("reused") is not True
    assert second.get("reused") is not True
    assert third["reused"] is True
    assert "SNDK" in third["symbols"]
    assert third["screens_this_look"] == 2


@pytest.mark.asyncio
async def test_third_scan_reuses_an_empty_tape_instead_of_requiring_arena(monkeypatch):
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    n = {"calls": 0}

    async def _fake_scan(**_kw):
        n["calls"] += 1
        return {"ok": True, "source": "ibkr", "symbols": [], "hits": [], "quoted": 0}

    async def _no_tags(_conn):
        return {}

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    world = _world()
    snap: dict = {}
    turn = BrainTurn()
    first = json.loads(
        await _run_tool("scan", {"scan_code": "MOST_ACTIVE"}, connector=None, world=world, snap=snap, turn=turn)
    )
    second = json.loads(
        await _run_tool(
            "scan",
            {"arena": "mega_cap", "scan_code": "TOP_PERC_LOSE"},
            connector=None,
            world=world,
            snap=snap,
            turn=turn,
        )
    )
    third = json.loads(
        await _run_tool("scan", {"scan_code": "LOW_OPEN_GAP"}, connector=None, world=world, snap=snap, turn=turn)
    )
    assert n["calls"] == 2
    assert first.get("reused") is not True
    assert second.get("reused") is not True
    assert third["reused"] is True
    assert third["hits"] == []
    assert third.get("ok") is True
    assert "requires arena" not in str(third.get("error") or "")
    assert third.get("run", {}).get("gate") == "off"


@pytest.mark.asyncio
async def test_scan_on_a_news_card_fetches_headlines_and_skips_news_step(monkeypatch):
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            }
        }
    )
    assert update is not None
    save_lab(update)

    async def _fake_scan(**_kw):
        return {
            "ok": True,
            "source": "ibkr",
            "symbols": ["SNDK"],
            "hits": [{"symbol": "SNDK", "last": 91.5, "open_gap_pct": -6.5}],
            "quoted": 1,
        }

    async def _no_tags(_conn):
        return {}

    async def _news(syms, **_k):
        return [{"symbol": "SNDK", "headline": "sales miss"}]

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    monkeypatch.setattr("abcxauto.brain._mda_news", _news)
    turn = BrainTurn()
    data = json.loads(
        await _run_tool(
            "scan",
            {"arena": "mega_cap", "scan_code": "TOP_PERC_LOSE"},
            connector=None,
            world=_world(),
            snap={},
            turn=turn,
        )
    )
    assert data["news"][0]["headline"] == "sales miss"
    assert "news" in turn.tool_trace
    assert data["run"]["next"] == "candles"


@pytest.mark.asyncio
async def test_empty_scan_on_a_live_card_runs_the_written_screens(monkeypatch):
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "when_on": ">=6% earnings-miss gap",
                            "shape": "LONG STK market_bracket",
                            "scan": "most_active + top_losers; mega/large only",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    seen: list[tuple[str, str]] = []

    async def _fake_scan(**kw):
        arena = str(kw.get("arena") or "")
        code = str(kw.get("scan_code") or "")
        seen.append((arena, code))
        if code == "MOST_ACTIVE":
            hits = [{"symbol": "AMD", "last": 472.0, "open_gap_pct": 4.2}]
        elif arena == "large_cap":
            hits = [{"symbol": "ALB", "last": 134.0, "open_gap_pct": -3.8}]
        else:
            hits = [{"symbol": "XOM", "last": 80.0, "open_gap_pct": -1.5}]
        return {
            "ok": True,
            "source": "ibkr",
            "arena": arena,
            "scan_code": code,
            "symbols": [hits[0]["symbol"]],
            "hits": hits,
            "quoted": 1,
        }

    async def _no_tags(_conn):
        return {}

    async def _no_news(*_a, **_k):
        return []

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.universe.verified_pe_tags", _no_tags)
    monkeypatch.setattr("abcxauto.brain._mda_news", _no_news)
    world = _world()
    snap: dict = {}
    first = json.loads(
        await _run_tool("scan", {}, connector=None, world=world, snap=snap, turn=BrainTurn())
    )
    assert seen == [
        ("mega_cap", "TOP_OPEN_PERC_LOSE"),
        ("mega_cap", "TOP_PERC_LOSE"),
        ("mega_cap", "MOST_ACTIVE"),
        ("large_cap", "TOP_OPEN_PERC_LOSE"),
        ("large_cap", "TOP_PERC_LOSE"),
        ("large_cap", "MOST_ACTIVE"),
    ]
    assert first.get("screens") == [
        "mega_cap:TOP_OPEN_PERC_LOSE",
        "mega_cap:TOP_PERC_LOSE",
        "mega_cap:MOST_ACTIVE",
        "large_cap:TOP_OPEN_PERC_LOSE",
        "large_cap:TOP_PERC_LOSE",
        "large_cap:MOST_ACTIVE",
    ]
    assert snap["scan_screens"] == first["screens"]
    assert snap["scan_arenas"] == ["mega_cap", "large_cap"]
    assert snap["scan_calls"] == 6
    assert first.get("arena") in (None, "")
    assert first.get("scan_code") in (None, "")
    assert first["rows"][0]["symbol"] == "ALB"
    assert first["deepest_symbol"] == "ALB"
    assert first["deepest_open_gap_pct"] == -3.8
    assert first["card_min_gap_pct"] == 6.0
    assert first["card_gap_met"] is False
    reused = json.loads(
        await _run_tool(
            "scan",
            {"arena": "large_cap", "scan_code": "TOP_PERC_LOSE"},
            connector=None,
            world=world,
            snap=snap,
            turn=BrainTurn(),
        )
    )
    assert reused.get("reused") is True
    assert reused.get("note") == "card screens already fetched this look"
    assert reused.get("screens") == first["screens"]
    assert "arena" not in reused or reused.get("arena") in (None, "")
    assert reused.get("card_gap_met") is False
    assert seen == [
        ("mega_cap", "TOP_OPEN_PERC_LOSE"),
        ("mega_cap", "TOP_PERC_LOSE"),
        ("mega_cap", "MOST_ACTIVE"),
        ("large_cap", "TOP_OPEN_PERC_LOSE"),
        ("large_cap", "TOP_PERC_LOSE"),
        ("large_cap", "MOST_ACTIVE"),
    ]


def test_scan_news_asks_the_gap_row_before_the_active_page():
    merged = {
        "rows": [
            {"symbol": "SNDK", "open_gap_pct": -6.5},
            {"symbol": "AAPL", "open_gap_pct": -0.4},
        ]
    }
    assert _news_symbols_for_scan(
        merged,
        ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "SNDK"],
    )[:2] == ["SNDK", "AAPL"]


def test_bare_candles_use_five_minute_bars_on_a_gap_tape():
    assert _candle_res_from_tape({}) == "D"
    assert _candle_res_from_tape({"scan_hits": {"rows": [{"symbol": "SPY"}]}}) == "D"
    assert (
        _candle_res_from_tape(
            {"scan_hits": {"rows": [{"symbol": "SNDK", "open_gap_pct": -6.5}]}}
        )
        == "5"
    )


def test_bare_candles_use_five_minute_when_card_needs_opening_low():
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "when_on": "mega/large ≥6% earnings-miss gap",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    assert _candle_res_from_tape({}) == "5"
    assert _candle_res_from_tape({"scan_hits": {"rows": [{"symbol": "SNDK"}]}}) == "5"


def test_candles_use_live_open_when_hist_is_still_yesterday():
    from abcxauto.lab_playbook import clamp_update, save_lab
    from abcxauto.structure_grade import session_usable

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "when_on": "mega/large ≥6% earnings-miss gap",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    out = {
        "bars": [
            {"t": "2026-08-24T15:55:00", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0},
        ]
    }
    snap = {
        "session": {"status": "regular"},
        "scan_hits": {
            "rows": [
                {
                    "symbol": "SNDK",
                    "last": 91.2,
                    "open": 90.0,
                    "open_gap_pct": -10.0,
                    "bid": 91.1,
                    "ask": 91.3,
                    "spread": 0.2,
                }
            ]
        },
    }
    _apply_candle_session(out, sym="SNDK", snap=snap, world=_world(), last=91.2)
    rng = out["session"]
    assert session_usable(rng) is True
    assert rng["print"] == "live_open"
    assert rng["open"] == 90.0
    assert rng["low"] == 90.0
    assert rng["today"] is True
    assert snap["session_range"]["SNDK"]["today"] is True


def test_remember_session_keeps_opening_low_over_live_open():
    from abcxauto.brain import _remember_session

    snap: dict = {}
    rich = {
        "today": True,
        "open": 39.4,
        "high": 39.81,
        "low": 39.03,
        "last": 39.78,
        "n": 40,
        "above_open": True,
    }
    _remember_session(snap, "NKE", rich)
    _remember_session(
        snap,
        "NKE",
        {
            "today": True,
            "open": 39.4,
            "high": 39.79,
            "low": 39.4,
            "last": 39.79,
            "n": 1,
            "print": "live_open",
            "above_open": True,
        },
    )
    assert snap["session_range"]["NKE"]["low"] == 39.03
    assert snap["session_range"]["NKE"]["n"] == 40
    assert snap["session_range"]["NKE"]["last"] == 39.79


def test_candles_pin_scan_open_when_hist_starts_midday():
    from abcxauto.brain import _apply_candle_session

    out = {
        "bars": [
            {"t": "2026-08-25T10:15:00", "o": 133.47, "h": 135.81, "l": 132.94, "c": 134.0},
            {"t": "2026-08-25T10:20:00", "o": 134.0, "h": 134.4, "l": 133.8, "c": 134.05},
        ]
    }
    snap = {
        "scan_hits": {
            "rows": [
                {
                    "symbol": "ALB",
                    "last": 133.71,
                    "open": 136.13,
                    "open_gap_pct": -3.802,
                }
            ]
        }
    }
    _apply_candle_session(out, sym="ALB", snap=snap, world=_world(), last=134.05)
    rng = out["session"]
    assert rng["open"] == 136.13
    assert rng["above_open"] is False
    assert rng["low"] == 132.94


def test_scan_paint_rows_uses_live_last():
    from abcxauto.brain import _scan_paint_rows

    rows = _scan_paint_rows(
        {
            "rows": [
                {
                    "symbol": "ALB",
                    "last": 133.71,
                    "open": 136.13,
                    "close": 141.51,
                    "change_pct": -5.512,
                }
            ]
        },
        quotes={"ALB": 134.05},
    )
    assert rows[0]["last"] == 134.05
    assert rows[0]["open"] == 136.13
    assert rows[0]["change_pct"] == pytest.approx((134.05 / 141.51 - 1.0) * 100.0, rel=1e-3)


@pytest.mark.asyncio
async def test_bare_candles_on_gap_hits_request_five_minute_hist():
    seen: dict = {}

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            seen["symbol"] = symbol
            seen["resolution"] = resolution
            return {
                "symbol": symbol,
                "bars": [{"t": "2026-08-25", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}],
                "source": "ibkr",
                "freshness": "ibkr_rth",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered — do not open the 5s stream")

    snap = {
        "scan_hits": {
            "quoted": 1,
            "rows": [{"symbol": "SNDK", "last": 1485.0, "open_gap_pct": -6.5}],
        }
    }
    await _run_tool(
        "candles",
        {},
        connector=Conn(),
        world=_world(),
        snap=snap,
        turn=BrainTurn(),
    )
    assert seen["symbol"] == "SNDK"
    assert seen["resolution"] == "5"


@pytest.mark.asyncio
async def test_bare_candles_on_session_card_request_five_minute_without_gap_tick():
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    seen: dict = {}

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            seen["symbol"] = symbol
            seen["resolution"] = resolution
            return {
                "symbol": symbol,
                "bars": [{"t": "2026-08-25", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}],
                "source": "ibkr",
                "freshness": "ibkr_rth",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered — do not open the 5s stream")

    await _run_tool(
        "candles",
        {},
        connector=Conn(),
        world=_world(),
        snap={"scan_hits": {"quoted": 1, "rows": [{"symbol": "SNDK", "last": 91.5}]}},
        turn=BrainTurn(),
    )
    assert seen["symbol"] == "SNDK"
    assert seen["resolution"] == "5"


@pytest.mark.asyncio
async def test_candles_stamp_session_range_and_run_next_send():
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": (
                                "LONG STK market_bracket. Stop under opening low. "
                                "Qty so dollar risk <=1% NL and notional <=25% NL."
                            ),
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            }
        }
    )
    assert update is not None
    save_lab(update)

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "symbol": symbol,
                "bars": [
                    {"t": "2026-08-25T09:35:00", "o": 90.0, "h": 91.0, "l": 88.0, "c": 89.0},
                    {"t": "2026-08-25T09:40:00", "o": 89.0, "h": 92.0, "l": 88.5, "c": 91.5},
                ],
                "source": "ibkr",
                "freshness": "ibkr_rth",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered")

    turn = BrainTurn()
    turn.tool_trace = ["book", "scan", "news"]
    snap = {
        "scan_hits": {
            "quoted": 1,
            "rows": [{"symbol": "SNDK", "last": 91.5, "open_gap_pct": -6.5}],
        }
    }
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SNDK", "resolution": "5"},
            connector=Conn(),
            world=_world(),
            snap=snap,
            turn=turn,
        )
    )
    assert data["session"]["open"] == 90.0
    assert data["session"]["low"] == 88.0
    assert data["session"]["above_open"] is True
    assert data["session"]["prior_close"] == pytest.approx(96.2567, rel=1e-3)
    assert data["session"]["retrace_30"] > data["session"]["open"]
    assert data["session"]["size"]["qty"] >= 1
    assert data["session"]["size"]["stop"] == 88.0
    assert data["session"]["size"]["risk_per_share"] == pytest.approx(3.5)
    assert data["session"]["size"]["card_qty"] >= 1
    assert data["session"]["size"]["card_risk_pct"] == 1.0
    assert data["session"]["ticket"]["card"] == "flush bounce"
    assert data["session"]["ticket"]["strategy"] == "market_bracket"
    assert data["session"]["ticket"]["stop_price"] == 88.0
    assert snap["session_range"]["SNDK"]["low"] == 88.0
    assert data["run"]["next"] == "send"
    assert data["run"]["send"]["symbol"] == "SNDK"
    assert data["run"]["send"]["card"] == "flush bounce"
    assert data["run"]["send"]["stop_price"] == 88.0


def test_stamp_session_ticket_skips_when_card_gate_is_off():
    from abcxauto.brain import _stamp_session_ticket
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK market_bracket. Stop under opening low.",
                            "when_on": "mega/large ≥6% earnings-miss gap",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ]
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    on_lows = {
        "today": True,
        "low": 88.0,
        "last": 88.0,
        "above_low": False,
        "open_gap_pct": -6.5,
        "ticket": {"card": "stale"},
    }
    _stamp_session_ticket(on_lows)
    assert "ticket" not in on_lows
    thin_gap = {
        "today": True,
        "low": 900.0,
        "last": 910.0,
        "above_low": True,
        "open_gap_pct": -3.3,
        "ticket": {"card": "stale"},
    }
    _stamp_session_ticket(thin_gap)
    assert "ticket" not in thin_gap
    missing_gap = {
        "today": True,
        "low": 160.0,
        "last": 165.0,
        "above_low": True,
        "ticket": {"card": "stale"},
    }
    _stamp_session_ticket(missing_gap)
    assert "ticket" not in missing_gap
    through = {
        "today": True,
        "low": 88.0,
        "last": 96.0,
        "above_low": True,
        "open_gap_pct": -6.5,
        "retrace_30": 93.0,
        "retrace_50": 95.0,
        "ticket": {"card": "stale"},
    }
    _stamp_session_ticket(through)
    assert "ticket" not in through
    half = {
        "today": True,
        "low": 88.0,
        "last": 94.0,
        "above_low": True,
        "open_gap_pct": -6.5,
        "retrace_30": 93.0,
        "retrace_50": 95.0,
    }
    _stamp_session_ticket(half)
    assert half["ticket"]["target_price"] == 95.0


def test_scan_gap_pct_reads_live_quote_when_scan_row_omits_it():
    from abcxauto.brain import _scan_gap_pct

    snap = {
        "scan_hits": {"rows": [{"symbol": "SNDK", "last": 91.5}]},
        "ibkr_live_quotes": {"SNDK": {"last": 91.5, "open_gap_pct": -6.5}},
    }
    assert _scan_gap_pct(snap, "SNDK") == -6.5
    nested = {
        "scan_hits": {
            "rows": [{"symbol": "SNDK", "ibkr": {"open_gap_pct": -7.1}}],
        }
    }
    assert _scan_gap_pct(nested, "SNDK") == -7.1


def test_clip_keeps_run_when_hits_overflow():
    raw = _clip(
        {
            "ok": True,
            "hits": [{"symbol": f"X{i}", "pad": "n" * 800} for i in range(80)],
            "news": [{"headline": "n" * 400} for _ in range(20)],
            "run": {"next": "send", "card": "flush bounce", "send": {"symbol": "SNDK"}},
        }
    )
    data = json.loads(raw)
    assert data["run"]["next"] == "send"
    assert data["run"]["send"]["symbol"] == "SNDK"
    assert "hits" not in data or data.get("_clipped")


def _fat_session_bars(n: int, *, close: float = 90.0) -> list[dict]:
    """IBKR-shaped 5m bars with t/t_unix/t_iso — the shape that overflowed 24k."""
    bars = []
    for i in range(n):
        minute = 9 * 60 + 30 + i * 5
        hh, mm = divmod(minute, 60)
        bars.append(
            {
                "t": f"20260825 {hh:02d}:{mm:02d}:00",
                "t_unix": 1756133700 + i * 300,
                "t_iso": f"2026-08-25T{hh + 4:02d}:{mm:02d}:00Z",
                "o": close + i * 0.01,
                "h": close + 1 + i * 0.01,
                "l": close - 2 + i * 0.01,
                "c": close + 0.5 + i * 0.01,
                "v": 10000 + i,
            }
        )
    return bars


def _assert_think_bars(bars: list) -> None:
    assert bars, "candles must return OHLC/time, not a metadata stub"
    assert len(bars) >= 5
    last = bars[-1]
    assert last.get("c") is not None
    assert last.get("t") not in (None, "")
    assert "t_unix" not in last
    assert "t_iso" not in last


def test_clip_keeps_candle_bars_when_run_overflows():
    """24k clip used to pop bars and leave run/session. Grok sized off that stub."""
    bars = _fat_session_bars(120)
    raw = _clip(
        {
            "run": {
                "next": "send",
                "card": "flush bounce",
                "send": {"symbol": "SNDK", "stop_price": 88.0},
                "when_on": "mega/large " + ("x" * 4000),
                "scan": "most_active " + ("y" * 4000),
            },
            "session": {
                "n": 120,
                "open": 90.0,
                "low": 88.0,
                "last": 91.0,
                "today": True,
            },
            "metrics": {"sma20": 90.0, "pad": "m" * 4000},
            "bars": bars,
            "source": "ibkr",
            "freshness": "ibkr_rth",
            "symbol": "SNDK",
            "resolution": "5",
        },
        max_chars=24_000,
    )
    data = json.loads(raw)
    _assert_think_bars(data.get("bars") or [])
    assert data.get("_clipped") != "bars"
    assert data["bars"][0]["t"].startswith("20260825 09:30")
    assert data["bars"][-1]["c"] == pytest.approx(bars[-1]["c"])


def test_clip_keeps_batch_series_bars():
    series = []
    for sym in ("SPY", "QQQ", "IWM", "DIA"):
        series.append(
            {
                "symbol": sym,
                "source": "ibkr",
                "bars": _fat_session_bars(80, close=100.0),
                "session": {"n": 80, "open": 100.0, "low": 98.0, "today": True},
                "metrics": {"sma20": 100, "pad": "m" * 2000},
            }
        )
    raw = _clip(
        {
            "run": {"next": "send", "when_on": "z" * 8000},
            "source": "ibkr",
            "series": series,
        },
        max_chars=24_000,
    )
    data = json.loads(raw)
    assert data.get("series")
    assert data.get("_clipped") != "series"
    for row in data["series"]:
        _assert_think_bars(row.get("bars") or [])
        assert row.get("symbol") in {"SPY", "QQQ", "IWM", "DIA"}


@pytest.mark.asyncio
async def test_multi_name_candles_send_sketch_uses_this_look_session(monkeypatch):
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    monkeypatch.setattr("abcxauto.think_stream.last_look_for_hunt", lambda *a, **k: {})

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            low = 88.0 if symbol == "SNDK" else 900.0
            return {
                "symbol": symbol,
                "bars": [
                    {
                        "t": "2026-08-25T09:35:00",
                        "o": low + 2,
                        "h": low + 4,
                        "l": low,
                        "c": low + 3,
                    },
                ],
                "source": "ibkr",
                "freshness": "ibkr_rth",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered")

    snap = {
        "scan_hits": {
            "quoted": 2,
            "rows": [
                {"symbol": "SNDK", "last": 91.5, "open_gap_pct": -6.5},
                {"symbol": "MU", "last": 911.0, "open_gap_pct": -3.3},
            ],
        }
    }
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbols": ["MU", "SNDK"], "resolution": "5"},
            connector=Conn(),
            world=_world(),
            snap=snap,
            turn=BrainTurn(tool_trace=["book", "scan", "news"]),
        )
    )
    assert snap["session_range"]["SNDK"]["low"] == 88.0
    assert data["run"]["next"] == "send"
    assert data["run"]["send"]["symbol"] == "SNDK"
    assert data["run"]["send"]["stop_price"] == 88.0


@pytest.mark.asyncio
async def test_candles_uses_ibkr_hist_when_connected():
    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "symbol": symbol,
                "bars": [{"t": "2026-08-18", "o": 1, "h": 2, "l": 0.5, "c": 768.5, "v": 1}],
                "source": "ibkr",
                "freshness": "ibkr_rth",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered — do not open the 5s stream")

    world = _world()
    snap: dict = {}
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SPY", "resolution": "15", "countback": 20},
            connector=Conn(),
            world=world,
            snap=snap,
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert data["freshness"] == "ibkr_rth"
    assert data["bars"][0]["c"] == 768.5
    assert "mda_last_is" not in data
    assert world.candle_source == "ibkr"
    assert snap["candle_source"] == "ibkr"


@pytest.mark.asyncio
async def test_candles_hist_stamps_ibkr_structure_metrics():
    bars = []
    price = 100.0
    for i in range(60):
        price += 0.15
        bars.append({"t": f"2026-01-{i+1:02d}", "c": price, "o": price, "h": price + 0.5, "l": price - 0.5, "v": 1})

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "symbol": symbol,
                "bars": bars,
                "source": "ibkr",
                "freshness": "ibkr_rth",
                "resolution": "D",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered")

    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SPY", "resolution": "D", "countback": 60},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert data["metrics"]["source"] == "ibkr"
    assert "sma20" in data["metrics"]
    assert "mda_last" not in data["metrics"]
    assert "mda_last_is" not in data


@pytest.mark.asyncio
async def test_candles_ibkr_error_when_hist_and_rt_miss(monkeypatch):
    class Conn:
        connected = True

        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {"error": "no IBKR bars", "source": "ibkr", "symbol": symbol}

        async def get_realtime_bars(self, symbol, **_k):
            return {"error": "no IBKR realtime bars", "source": "ibkr", "symbol": symbol}

    class MDA:
        is_configured = True

        async def get_stock_candles(self, symbol, resolution="D", countback=60, **_k):
            raise AssertionError("MDA must not run when IBKR is connected")

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    world = _world()
    world.ibkr_live_quotes = {"SPY": 310.72}
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SPY", "resolution": "D", "countback": 20},
            connector=Conn(),
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert data.get("error")
    assert data.get("bars") in (None, [])
    assert data["last"] == 310.72


@pytest.mark.asyncio
async def test_candles_uses_hist_for_session_even_when_rt_warm(monkeypatch):
    seen: dict = {}

    class Conn:
        def realtime_bar_buffer(self, symbol):
            return [{"t": "2026-08-25T13:00:00", "c": 39.80}]

        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            seen["resolution"] = resolution
            seen["countback"] = countback
            return {
                "symbol": symbol,
                "bars": [
                    {"t": "2026-08-25T09:35:00", "o": 39.4, "h": 39.9, "l": 39.03, "c": 39.5},
                    {"t": "2026-08-25T12:55:00", "o": 39.78, "h": 39.82, "l": 39.76, "c": 39.80},
                ],
                "source": "ibkr",
                "freshness": "ibkr_rth",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered — do not open the 5s stream")

    class MDA:
        async def get_stock_candles(self, *a, **k):
            raise AssertionError("MDA must not run")

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "NKE", "resolution": "5"},
            connector=Conn(),
            world=_world(),
            snap={
                "scan_hits": {
                    "rows": [{"symbol": "NKE", "open": 39.4, "open_gap_pct": -3.3}]
                }
            },
            turn=BrainTurn(),
        )
    )
    assert seen["resolution"] == "5"
    assert data["freshness"] == "ibkr_rth"
    assert data["session"]["open"] == 39.4
    assert data["session"]["low"] == 39.03


@pytest.mark.asyncio
async def test_candles_uses_ibkr_rt_when_hist_fails(monkeypatch):
    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {"error": "no IBKR bars", "source": "ibkr", "symbol": symbol}

        async def get_realtime_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "symbol": symbol,
                "bars": [{"t": "2026-08-18T17:45:05", "o": 310.4, "h": 310.8, "l": 310.3, "c": 310.6, "v": 9}],
                "source": "ibkr",
                "freshness": "ibkr_rt_5s",
                "resolution": "5s",
                "requested_resolution": resolution,
                "use": "live_5s_not_hist",
            }

    class MDA:
        is_configured = True

        async def get_stock_candles(self, symbol, resolution="D", countback=60, **_k):
            raise AssertionError("MDA must not run when the IBKR 5s stream has bars")

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "AAPL", "resolution": "15", "countback": 20},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert data["freshness"] == "ibkr_rt_5s"
    assert data["resolution"] == "5s"
    assert data["requested_resolution"] == "15"
    assert data["bars"][0]["c"] == 310.6


@pytest.mark.asyncio
async def test_candles_error_carries_the_live_last_so_the_turn_can_continue():
    """A miss is still useful: hand back the IBKR last instead of nothing."""
    world = _world()
    world.ibkr_live_quotes = {"QQQ": 708.9}
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "QQQ", "resolution": "15", "countback": 20},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["freshness"] == "ibkr_miss"
    assert data["last"] == 708.9
    assert data["use"] == "no_bars_use_quote"


@pytest.mark.asyncio
async def test_candles_batch_returns_series(monkeypatch):
    seen: list[str] = []

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            seen.append(symbol)
            return {
                "symbol": symbol,
                "bars": [{"t": 1, "c": 100.0 + len(seen)}],
                "source": "ibkr",
            }

    class MDA:
        async def get_stock_candles(self, *_a, **_k):
            raise AssertionError("candles must never reach MDA")

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbols": ["SPY", "QQQ", "IWM"], "resolution": "D", "countback": 20},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert {row["symbol"] for row in data["series"]} == {"SPY", "QQQ", "IWM"}
    assert set(seen) == {"SPY", "QQQ", "IWM"}
    for row in data["series"]:
        assert row.get("bars")
        assert row["bars"][0]["c"] is not None


@pytest.mark.asyncio
async def test_candles_fat_hist_returns_ohlc_not_run_stub():
    bars = _fat_session_bars(120)

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "symbol": symbol,
                "bars": bars,
                "source": "ibkr",
                "freshness": "ibkr_rth",
                "resolution": resolution,
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered")

    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SNDK", "resolution": "5", "countback": 120},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    _assert_think_bars(data.get("bars") or [])
    assert data["source"] == "ibkr"
    assert data.get("_clipped") != "bars"
    assert data["bars"][-1]["c"] == pytest.approx(bars[-1]["c"])


@pytest.mark.asyncio
async def test_candles_batch_fat_hist_returns_series_bars():
    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "symbol": symbol,
                "bars": _fat_session_bars(80, close=100.0),
                "source": "ibkr",
                "freshness": "ibkr_rth",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered")

    data = json.loads(
        await _run_tool(
            "candles",
            {"symbols": ["SPY", "QQQ", "IWM", "DIA"], "resolution": "5", "countback": 80},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data.get("series")
    assert data.get("_clipped") != "series"
    assert {row["symbol"] for row in data["series"]} == {"SPY", "QQQ", "IWM", "DIA"}
    for row in data["series"]:
        _assert_think_bars(row.get("bars") or [])


@pytest.mark.asyncio
async def test_candles_miss_is_error_not_clipped_metadata():
    from abcxauto.lab_playbook import clamp_update, save_lab

    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "news", "quote", "candles", "send"],
                    "cards": [
                        {
                            "name": "flush bounce",
                            "thesis": "gap retrace",
                            "shape": "LONG STK. Stop under opening low.",
                            "retire_if": {"sample": 3, "condition": "no bounce"},
                        }
                    ],
                }
            }
        }
    )
    assert update is not None
    save_lab(update)

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {"error": "no IBKR bars", "source": "ibkr", "symbol": symbol}

        async def get_realtime_bars(self, symbol, **_k):
            return {"error": "no IBKR realtime bars", "source": "ibkr", "symbol": symbol}

    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SNDK", "resolution": "5", "countback": 60},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data.get("error")
    assert data.get("freshness") == "ibkr_miss"
    assert data.get("bars") in (None, [])
    assert data.get("_clipped") not in ("bars", "series", "payload")
    assert data.get("source") == "ibkr"


@pytest.mark.asyncio
async def test_option_chain_batch_returns_chains():
    class Conn:
        async def get_option_chain(self, symbol, min_dte=7, max_dte=45):
            return {
                "symbol": symbol,
                "source": "ibkr",
                "freshness": "live",
                "expirations": ["20260821"],
                "strikes": [100, 101, 102],
            }

    data = json.loads(
        await _run_tool(
            "option_chain",
            {"symbols": ["SPY", "QQQ"]},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert [row["symbol"] for row in data["chains"]] == ["SPY", "QQQ"]
    assert data["chains"][0]["expirations"] == ["20260821"]


@pytest.mark.asyncio
async def test_option_quote_keeps_ibkr_and_mda_apart(monkeypatch):
    class Conn:
        async def get_live_option_quote(self, symbol, expiration, strike, right):
            return {
                "symbol": symbol,
                "expiration": expiration,
                "strike": strike,
                "right": right,
                "bid": 1.1,
                "ask": 1.2,
                "last": 1.15,
                "source": "ibkr",
                "freshness": "live",
            }

    class MDA:
        async def get_option_quote(self, occ, **_k):
            return {
                "delta": 0.4,
                "iv": 0.18,
                "bid": 9.9,
                "ask": 10.1,
                "last": 10.0,
                "source": "marketdata",
            }

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    out = json.loads(
        await _run_tool(
            "option_quote",
            {"symbol": "SPY", "expiration": "20260821", "strike": 500, "right": "C"},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert out["ibkr"]["source"] == "ibkr"
    assert out["ibkr"]["last"] == 1.15
    assert out["mda"]["delta"] == 0.4
    assert "delayed" in out["mda"]["freshness"]
    assert "bid" not in out["mda"]
    assert "ask" not in out["mda"]
    assert "last" not in out["mda"]


@pytest.mark.asyncio
async def test_option_quote_batches_contracts(monkeypatch):
    seen: list[tuple] = []

    class Conn:
        async def get_live_option_quote(self, symbol, expiration, strike, right):
            seen.append((symbol, expiration, strike, right))
            return {
                "symbol": symbol,
                "strike": strike,
                "bid": 1.0,
                "ask": 1.1,
                "source": "ibkr",
                "freshness": "live",
            }

    class MDA:
        async def get_option_quote(self, occ, **_k):
            return {"delta": 0.2, "bid": 9.9, "ask": 10.1}

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    monkeypatch.setattr(
        "abcxauto.universe.legal_symbols",
        lambda **_k: ["SPY", "QQQ"],
    )
    out = json.loads(
        await _run_tool(
            "option_quote",
            {
                "contracts": [
                    {"symbol": "SPY", "expiration": "20260821", "strike": 500, "right": "C"},
                    {"symbol": "QQQ", "expiration": "20260821", "strike": 400, "right": "P"},
                ]
            },
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert len(out["quotes"]) == 2
    assert out["quotes"][0]["ibkr"]["source"] == "ibkr"
    assert out["quotes"][1]["symbol"] == "QQQ"
    assert "bid" not in (out["quotes"][0]["mda"] or {})
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_status_tool():
    class Conn:
        connected = True

    snap = {
        "reality_pulse": {
            "session": {
                "status": "regular",
                "countdown_to": "close",
                "countdown_s": 3600,
                "countdown_human": "1h 0m",
            },
            "tradable_now": {"equity_rth": True},
            "data_freshness": {
                "ibkr_connected": True,
                "ibkr_snapshot_age_s": 2.0,
                "spy_last": 500.0,
                "vix": 15.0,
            },
        }
    }
    raw = await _run_tool(
        "status", {}, connector=Conn(), world=_world(), snap=snap, turn=BrainTurn()
    )
    data = json.loads(raw)
    assert "ibkr_connected" in data
    assert "trading_mode" in data
    assert "session" in data
    assert "ibkr_port" in data or "ibkr_client_id" in data
    assert "levers" in data
    assert "max_risk_per_trade_pct" in data["levers"]
    assert "max_open_positions" in data["levers"]
    assert data["countdown"]["to"] == "close"
    assert data["tradable_now"]["equity_rth"] is True
    assert data["freshness"]["spy_last"] == 500.0
    assert data["freshness"]["vix"] == 15.0


@pytest.mark.asyncio
async def test_quote_batch_stashes_each_symbol():
    class Conn:
        async def get_live_quotes(self, symbols):
            return {
                "source": "ibkr",
                "freshness": "live",
                "quotes": [
                    {"symbol": s, "last": 100.0 + i, "source": "ibkr", "freshness": "live"}
                    for i, s in enumerate(symbols)
                ],
            }

    world = _world()
    snap: dict = {}
    raw = await _run_tool(
        "quote",
        {"symbols": ["SPY", "QQQ"]},
        connector=Conn(),
        world=world,
        snap=snap,
        turn=BrainTurn(),
    )
    data = json.loads(raw)
    assert len(data["quotes"]) == 2
    assert snap["ibkr_live_quotes"]["SPY"] == 100.0
    assert snap["ibkr_live_quotes"]["QQQ"] == 101.0


@pytest.mark.asyncio
async def test_playbook_tool_returns_current_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    from abcxauto.lab_playbook import save_lab

    save_lab(
        {
            "mode": "explore",
            "instructions": "Standing notes for the lab.",
            "do_more": "size",
            "stop_doing": "clones",
            "ready_to_promote": False,
        },
        scorecard={"beating_model": False, "edge_usd": -12.0},
    )
    data = json.loads(
        await _run_tool("playbook", {}, connector=None, world=_world(), snap={}, turn=BrainTurn())
    )
    assert data["scope"] == "lab"
    assert "Standing notes" in data["tree"]
    assert data["ledger"][0]["revision"] == 1
    assert "instructions" not in data["ledger"][0]
    full = json.loads(
        await _run_tool(
            "playbook",
            {"full": True},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert "Standing notes" in full["tree"]


def test_book_is_structured_facts_not_worldstate_lecture(monkeypatch):
    from abcxauto.brain import _book_payload

    monkeypatch.setattr("abcxauto.universe.legal_symbols", lambda **_k: ["SPY", "QQQ"])
    blob = _book_payload(_world(ibkr_live_quotes={"SPY": 501.0}))
    assert isinstance(blob["world"], dict)
    assert blob["world"]["session"] == "regular"
    assert "legal_n" not in blob["world"]
    assert "legal_sample" not in blob["world"]
    assert "working_thesis" not in blob["world"]
    assert "floor" not in blob
    assert "operator_card" not in blob
    assert "scorecard" not in blob
    assert blob["ibkr_live_quotes"]["SPY"] == 501.0
    dumped = json.dumps(blob["world"])
    assert "WORLDSTATE" not in dumped
    assert "trade_plan" in blob["world"]
    assert "book_unreliable" in blob["world"]
    assert "path" in blob
    assert "max_risk_per_trade_pct" in blob["levers"]
    assert blob["levers"]["max_open_positions"]["max"] == 25
    assert blob["levers"]["max_open_positions"]["min"] == 1
    assert "day" in blob
    assert "edge_usd" in blob["day"]
    assert blob["day"]["edge_meaning"] == "nl_vs_start_minus_model"
    assert "open_upnl" in blob["day"]
    assert "ibkr_daily_pnl" in blob["day"]
    assert "cloned" in blob["day"]
    assert list(blob.keys())[0] == "day"
    assert "do_more" not in (blob.get("playbook") or {})
    assert "stop_doing" not in (blob.get("playbook") or {})
    assert "instructions" not in (blob.get("playbook") or {})
    pb = blob.get("day", {}).get("playbook") or {}
    assert "do_more" not in pb
    assert "stop_doing" not in pb


def test_book_lists_full_capacity(monkeypatch):
    from abcxauto.brain import _book_payload

    monkeypatch.setattr("abcxauto.universe.legal_symbols", lambda **_k: ["SPY"])
    positions = [
        {
            "symbol": "SPY",
            "sec_type": "OPT",
            "quantity": 1,
            "conId": i,
            "strike": 500 + i,
            "right": "C",
        }
        for i in range(15)
    ]
    blob = _book_payload(_world(positions=positions))
    assert len(blob["world"]["positions"]) == 15


def test_quote_cache_hit():
    from abcxauto.broker.connector import IBKRQueriesMixin

    bag = SimpleNamespace(_QUOTE_CACHE_S=2.5)
    IBKRQueriesMixin._live_quote_remember(bag, "SPY", {"symbol": "SPY", "last": 501.0, "source": "ibkr"})
    hit = IBKRQueriesMixin._live_quote_cached(bag, "SPY")
    assert hit["last"] == 501.0
    ts, payload = bag._quote_cache["SPY"]
    bag._quote_cache["SPY"] = (ts - 10, payload)
    assert IBKRQueriesMixin._live_quote_cached(bag, "SPY") is None


@pytest.mark.asyncio
async def test_quote_fresh_bypasses_cache():
    class Conn:
        calls = 0

        async def get_live_quote(self, symbol, *, fresh=False):
            self.calls += 1
            return {
                "symbol": symbol,
                "last": 501.0,
                "source": "ibkr",
                "freshness": "live",
                "cached": not fresh,
            }

    from abcxauto.tools import run_readonly_tool

    c = Conn()
    raw = await run_readonly_tool("quote", {"symbol": "SPY", "fresh": True}, c)
    data = json.loads(raw)
    assert data["cached"] is False
    assert c.calls == 1


def _stub_chat_client():
    created: list[object] = []

    class Chat:
        def append(self, *_a, **_k):
            pass

    class _ChatNS:
        @staticmethod
        def create(**_k):
            chat = Chat()
            created.append(chat)
            return chat

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
    )
    return g, created


def test_ensure_chat_rotates_non_episode():
    from abcxauto.brain import _ensure_chat

    g, created = _stub_chat_client()
    first = _ensure_chat(g, kind="boot")
    assert _ensure_chat(g, kind="alarm") is not first
    assert _ensure_chat(g, kind="operator") is not created[1]
    assert len(created) == 3


def test_every_wake_opens_a_fresh_linear_think():
    """A cold _open_wake is a new chat. Stay-up resume is the other path."""
    from abcxauto.brain import _ensure_chat, _open_wake
    from abcxauto.park_clock import BookEvent, note_wake

    g, created = _stub_chat_client()
    boot = _ensure_chat(g, kind="boot")
    for kind in ("fill", "order_change", "book_move", "unprotected"):
        assert _ensure_chat(g, kind=kind) is not boot
    assert len(created) == 5
    note_wake(BookEvent(kind="fill", detail="XLF filled"))
    try:
        g2, created2 = _stub_chat_client()
        first = _open_wake(g2, "look brief")
        second = _open_wake(g2, "delta brief")
        assert second is not first
        assert len(created2) == 2
    finally:
        note_wake(None)


def test_rth_yield_keeps_chat_park_resets():
    """End-of-turn keeps chat on paper stay-up; park / overnight drop it."""
    from abcxauto.brain import BrainTurn, _ensure_chat, _finish_look_chat

    g, created = _stub_chat_client()
    chat = _ensure_chat(g, kind="boot")
    _finish_look_chat(g, BrainTurn(text="watching the book"), session="regular")
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(g, BrainTurn(parked=True, text="gate off"), session="regular")
    assert getattr(g, "chat", None) is None
    nxt = _ensure_chat(g, kind="alarm")
    assert nxt is not chat
    assert len(created) == 2


def test_finish_look_chat_overnight_and_dead_stream_drop():
    from abcxauto.brain import BrainTurn, _ensure_chat, _finish_look_chat

    g, _created = _stub_chat_client()
    chat = _ensure_chat(g, kind="boot")
    _finish_look_chat(g, BrainTurn(text="watching the book"), session="premarket")
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(g, BrainTurn(text="watching the book"), session="closed")
    assert getattr(g, "chat", None) is None
    chat = _ensure_chat(g, kind="boot")
    _finish_look_chat(g, BrainTurn(stream_error="RESOURCE_EXHAUSTED"), session="regular")
    assert getattr(g, "chat", None) is None


def test_finish_look_chat_junk_empty_failed_drop():
    """A stay-up empty/junk/failed look must not keep the live chat."""
    from abcxauto.brain import BrainTurn, _ensure_chat, _finish_look_chat

    g, created = _stub_chat_client()
    chat = _ensure_chat(g, kind="boot")
    _finish_look_chat(g, BrainTurn(text="watching IWM"), session="regular")
    assert getattr(g, "chat", None) is chat

    for dead in (
        BrainTurn(text=""),
        BrainTurn(text="?"),
        BrainTurn(text="  "),
        BrainTurn(failed=True, text="watching IWM"),
        BrainTurn(text="I'll inspect the book first.\n?"),
    ):
        g.chat = chat
        _finish_look_chat(g, dead, session="regular")
        assert getattr(g, "chat", None) is None, dead

    g.chat = chat
    _finish_look_chat(g, BrainTurn(text=""), session="premarket")
    assert getattr(g, "chat", None) is None
    assert len(created) == 1


def test_finish_look_chat_refused_send_keeps_chat():
    """A clerk-blocked ticket is not junk. Stay-up still resumes the same chat."""
    from abcxauto.brain import BrainTurn, _ensure_chat, _finish_look_chat

    g, _created = _stub_chat_client()
    chat = _ensure_chat(g, kind="boot")
    _finish_look_chat(
        g,
        BrainTurn(
            text="blocked; standing down",
            sends=[
                {
                    "act": {"strategy": "market_bracket"},
                    "result": {"status": "blocked"},
                }
            ],
            last_result={"status": "blocked"},
            last_strat="market_bracket",
        ),
        session="regular",
    )
    assert getattr(g, "chat", None) is chat


def test_finish_look_chat_live_regular_drops(monkeypatch):
    from abcxauto.brain import BrainTurn, _ensure_chat, _finish_look_chat

    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: False),
    )
    g, _created = _stub_chat_client()
    _ensure_chat(g, kind="boot")
    _finish_look_chat(g, BrainTurn(text="watching the book"), session="regular")
    assert getattr(g, "chat", None) is None


def test_stay_up_open_wake_reuses_live_chat():
    from abcxauto.brain import _open_wake

    g, created = _stub_chat_client()
    first = _open_wake(g, "session=regular send.")
    second = _open_wake(g, "session=regular send.", resume=True)
    assert second is first
    assert len(created) == 1
    third = _open_wake(g, "wake", resume=True, reset=True)
    assert third is not first
    assert len(created) == 2


def test_stay_up_resume_skips_append_when_poke_pending():
    from abcxauto.brain import _open_wake
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    class Chat:
        def __init__(self):
            self.appended: list[object] = []

        def append(self, msg, **_k):
            self.appended.append(msg)

    g, _created = _stub_chat_client()
    live = Chat()
    g.chat = live
    g._wake_n = 1
    clear_interrupt()
    note_interrupt(BookEvent("fill", "QQQ"))
    try:
        out = _open_wake(g, "session=regular send.", resume=True)
        assert out is live
        assert live.appended == []
    finally:
        clear_interrupt()


def test_agent_tools_omit_set_wake_in_every_session():
    """Cadence is clerk + playbook. A Grok clock was the nap exploit."""
    from abcxauto.brain import AGENT_TOOLS, agent_tools

    assert "set_wake" not in _names_of(AGENT_TOOLS)
    for sess in ("regular", "premarket", "postmarket", "closed"):
        assert "set_wake" not in _names_of(agent_tools(session=sess)), sess
        assert "send" in _names_of(agent_tools(session=sess)), sess


@pytest.mark.asyncio
async def test_set_wake_is_unknown_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    turn = BrainTurn()
    raw = await _run_tool(
        "set_wake",
        {"wake_in_s": 3000},
        connector=None,
        world=_world(session_status="regular", flat=True),
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data.get("error")
    assert "unknown tool" in str(data.get("error") or "")
    assert turn.parked is False


def test_live_poke_interrupt_skips_reset_chat():
    """fill/order_change/unprotected poke the live episode without _reset_chat."""
    import asyncio

    from abcxauto.brain import BrainTurn, _ensure_chat, _inject_live_poke
    from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt

    g, _created = _stub_chat_client()
    _ensure_chat(g, kind="boot")
    clear_interrupt()
    note_interrupt(BookEvent("fill", "QQQ"))
    world = _world(session_status="regular", flat=True, unprotected=[])
    turn = BrainTurn()
    appended: list[object] = []

    class Chat:
        def append(self, msg, **_k):
            appended.append(msg)

    live = Chat()
    g.chat = live

    ok = asyncio.run(
        _inject_live_poke(live, connector=None, world=world, snap={}, turn=turn)
    )
    assert ok is True
    assert turn.interrupted is True
    assert getattr(g, "chat", None) is live
    assert appended
    text = "".join(
        getattr(c, "text", "") for c in (getattr(appended[0], "content", None) or [])
    )
    if not text:
        text = str(appended[0])
    assert "event=fill" in text
    assert "session=" in text
    assert "This is a delta" not in text
    assert "yield resume" not in text
    assert "send." in text
    assert "set_wake" not in text
    assert "ORDER EXAMPLES" not in text
    assert "AWARENESS" not in text
    clear_interrupt()


def test_open_wake_is_developer_not_user():
    from xai_sdk.chat import developer, user
    from abcxauto.brain import _open_wake

    got: list[object] = []

    class Chat:
        def append(self, msg, **_k):
            got.append(msg)

    class _ChatNS:
        @staticmethod
        def create(**_k):
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
    )
    _open_wake(g, "session=regular")
    assert got
    assert got[0].role == developer("x").role
    assert got[0].role != user("x").role
    text = "".join(c.text for c in got[0].content)
    assert "session=regular" in text
    assert "CLERK WAKE" not in text
    assert "no operator" not in text.lower()


def test_append_hist_caps_and_drops_old_snapshots():
    from abcxauto.agent_loop import _HIST_CAP, _append_hist

    h: list[dict] = []
    for i in range(_HIST_CAP + 5):
        _append_hist(h, {"snapshot": {"i": i}, "cycle": i})
    assert len(h) == _HIST_CAP
    assert "snapshot" not in h[0]
    assert "snapshot" in h[-1]


@pytest.mark.asyncio
async def test_stream_round_breaks_when_stalled(monkeypatch):
    import asyncio

    from abcxauto import brain
    from abcxauto.think_stream import subscribe, unsubscribe

    monkeypatch.setattr(brain, "STREAM_CHUNK_S", 0.02)
    monkeypatch.setattr(brain, "STREAM_IDLE_LIMIT", 2)
    painted: list[str] = []

    def cap(kind: str, text: str) -> None:
        painted.append(f"{kind}:{text}")

    class Chat:
        async def stream(self):
            while True:
                await asyncio.sleep(10)
                yield None, SimpleNamespace(content="", reasoning_content="")

    subscribe(cap)
    try:
        text, resp, reason = await brain.stream_round(Chat())
    finally:
        unsubscribe(cap)
    assert resp is None
    assert text == ""
    blob = "".join(painted)
    assert "?" not in blob
    assert "…" not in blob
    assert reason in ("stalled", "ok")


def test_stream_is_looping_ready_spam():
    from abcxauto.brain import stream_is_looping

    assert not stream_is_looping("I'll hold the three keepers.")
    spam = "I'm ready. " * 20
    assert stream_is_looping(spam)
    slots = (
        "3 slots remain open. 3 lots on XLE and QQQ are already at limit, "
        "so no new entries. "
    ) * 6
    assert stream_is_looping(slots)
    assert not stream_is_looping(
        "3 slots remain open. 3 lots on XLE and QQQ are already at limit, "
        "so no new entries."
    )


def test_stream_is_looping_fake_cycle_cadence():
    from abcxauto.brain import stream_is_looping

    lines = [
        f"Cycle {n} complete. Book 7 positions. Ready for Cycle {n + 1}."
        for n in range(5, 20)
    ]
    assert not stream_is_looping("\n".join(lines[:2]))
    assert stream_is_looping("\n".join(lines))


@pytest.mark.asyncio
async def test_stream_round_breaks_on_think_loop():
    from abcxauto import brain

    class Chat:
        async def stream(self):
            chunk = "I'm ready. "
            acc = ""
            for _ in range(40):
                acc += chunk
                yield None, SimpleNamespace(content="", reasoning_content=acc)

    text, _resp, reason = await brain.stream_round(Chat())
    assert reason == "loop"
    assert text == ""


def test_open_wake_resets_dead_chat():
    from abcxauto.brain import _open_wake

    created: list[int] = []

    class Chat:
        def append(self, *_a, **_k):
            pass

    class _ChatNS:
        @staticmethod
        def create(**_k):
            created.append(1)
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=object(),
        _wake_n=3,
    )
    chat = _open_wake(g, "wake", reset=True)
    assert isinstance(chat, Chat)
    assert len(created) == 1
    assert g._wake_n == 1


def test_new_chat_does_not_force_a_tool():
    from abcxauto.brain import _new_chat

    captured: dict = {}

    class _ChatNS:
        @staticmethod
        def create(**k):
            captured.update(k)
            return SimpleNamespace()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=None,
        _wake_n=0,
    )
    _new_chat(g, session="regular")
    assert captured.get("tool_choice") != "required"
    assert "set_wake" not in _names_of(captured.get("tools") or [])
    assert "send" in _names_of(captured.get("tools") or [])


def test_new_chat_premarket_omits_set_wake():
    from abcxauto.brain import _new_chat

    captured: dict = {}

    class _ChatNS:
        @staticmethod
        def create(**k):
            captured.update(k)
            return SimpleNamespace()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=None,
        _wake_n=0,
    )
    _new_chat(g, session="premarket")
    assert "set_wake" not in _names_of(captured.get("tools") or [])


@pytest.mark.asyncio
async def test_tool_timeout_returns_error(monkeypatch):
    import asyncio

    from abcxauto import brain
    from abcxauto.brain import grok_turn

    monkeypatch.setattr(brain, "TOOL_S", 0.05)

    async def hang(*_a, **_k):
        await asyncio.sleep(2)
        return "{}"

    monkeypatch.setattr(brain, "_run_tool", hang)

    class TC:
        id = "1"
        function = SimpleNamespace(name="book", arguments="{}")

    class Resp:
        tool_calls = [TC()]

    class Chat:
        n = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.n += 1
            if self.n == 1:
                yield Resp(), SimpleNamespace(content="", reasoning_content="")

    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: Chat())),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=Chat(),
        _wake_n=1,
    )
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    assert "book" in turn.tool_trace
    assert not turn.sends
    assert turn.last_strat != "hold"


@pytest.mark.asyncio
async def test_read_tools_run_in_parallel(monkeypatch):
    """Overlap, not wall clock — a busy CPU must not decide this.

    Every read stub parks on a gate that only opens once all of them are in
    flight, so a serial dispatch cannot get past the first one and fails on the
    gate timeout instead of passing on a fast machine.
    """
    import asyncio

    from abcxauto import brain
    from abcxauto.brain import grok_turn

    reads = ("quote", "news", "scan")
    writes = ("send", "self_tune", "write_lab_playbook")
    gate_s = 2.0

    reads_in_flight = 0
    peak_reads = 0
    writes_in_flight = 0
    peak_writes = 0
    entered: list[str] = []
    all_reads_in = asyncio.Event()

    async def gated(name, args, **_k):
        nonlocal reads_in_flight, peak_reads, writes_in_flight, peak_writes
        entered.append(name)
        if name in reads:
            reads_in_flight += 1
            peak_reads = max(peak_reads, reads_in_flight)
            if reads_in_flight == len(reads):
                all_reads_in.set()
            try:
                await asyncio.wait_for(all_reads_in.wait(), gate_s)
            finally:
                reads_in_flight -= 1
        else:
            writes_in_flight += 1
            peak_writes = max(peak_writes, writes_in_flight)
            # Yield: gathered writes would show up as overlap here.
            for _ in range(3):
                await asyncio.sleep(0)
            writes_in_flight -= 1
        return json.dumps({"ok": name})

    monkeypatch.setattr(brain, "_run_tool", gated)

    class TC:
        def __init__(self, name, cid):
            self.id = cid
            self.function = SimpleNamespace(name=name, arguments="{}")

    class Resp:
        tool_calls = [
            TC(name, str(i)) for i, name in enumerate((*reads, *writes), start=1)
        ]

    class Chat:
        n = 0
        results = []

        def append(self, *_a, **_k):
            self.results.append(_a)

        async def stream(self):
            self.n += 1
            if self.n == 1:
                yield Resp(), SimpleNamespace(content="", reasoning_content="")

    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: Chat())),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=Chat(),
        _wake_n=1,
    )
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    assert set(turn.tool_trace) == {*reads, *writes}
    # The gate only opens when all three reads are inside it at once.
    assert peak_reads == len(reads)
    # Tickets and knobs stay one at a time, and only after the facts are in.
    assert peak_writes == 1
    assert set(entered[: len(reads)]) == set(reads)
    assert entered[len(reads):] == list(writes)


def _tool_fn(name: str):
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        got = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        if got == name:
            return fn
    raise AssertionError(f"{name} tool missing")


def _tool_props(name: str) -> dict:
    fn = _tool_fn(name)
    params = getattr(fn, "parameters", None) or {}
    if isinstance(params, str):
        params = json.loads(params)
    elif hasattr(params, "model_dump"):
        params = params.model_dump()
    return dict((params or {}).get("properties") or {})


def test_playbook_tools_are_a_notebook_not_a_form():
    playbook = str(getattr(_tool_fn("playbook"), "description", "") or "")
    write = str(getattr(_tool_fn("write_lab_playbook"), "description", "") or "")
    assert "outcome" in playbook.lower()
    assert "WHAT_WORKED" not in write
    assert "wake clock" in write.lower()
    assert "next-look-you" not in write


def test_send_tool_says_one_ticket_per_call():
    fn = _tool_fn("send")
    desc = str(getattr(fn, "description", "") or "")
    assert "again this turn" in desc.lower()
    assert "place one ticket" not in desc.lower()
    assert "self_tune" in desc.lower()
    props = _tool_props("send")
    strat = props.get("strategy") or {}
    assert "enum" in strat
    assert "hold" not in strat["enum"]
    assert "self_tune" not in strat["enum"]
    assert "bracket" in strat["enum"]
    assert "symbol" in props
    assert "quantity" in props
    card = props.get("card") or {}
    assert "required on new risk" in str(card.get("description") or "").lower()
    assert "not law" in str(card.get("description") or "").lower()
    assert "prose is not a send gate" in str(card.get("description") or "").lower()


def test_self_tune_tool_is_flat():
    props = _tool_props("self_tune")
    assert "max_risk_per_trade_pct" in props
    assert "enabled_arenas" in props
    assert "controls" not in props
    assert "params" not in props


@pytest.mark.asyncio
async def test_self_tune_tool_applies_flat_knobs(monkeypatch):
    seen: dict = {}

    def fake_apply(params, persist=True, rationale=""):
        seen["params"] = dict(params)
        seen["rationale"] = rationale
        return {"status": "ok", "strategy": "self_tune", "applied": params}

    monkeypatch.setattr("abcxauto.self_tune.apply_self_tune", fake_apply)
    turn = BrainTurn()
    raw = await _run_tool(
        "self_tune",
        {
            "max_risk_per_trade_pct": 0.75,
            "enabled_arenas": ["index_etfs"],
            "rationale": "cut size",
        },
        connector=None,
        world=_world(),
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert seen["params"]["max_risk_per_trade_pct"] == 0.75
    assert seen["params"]["enabled_arenas"] == ["index_etfs"]
    assert "controls" not in seen["params"]
    assert seen["rationale"] == "cut size"
    assert turn.last_strat == "self_tune"


@pytest.mark.asyncio
async def test_set_risk_alias_is_self_tune_tool(monkeypatch):
    seen: dict = {}

    def fake_apply(params, persist=True, rationale=""):
        seen["params"] = dict(params)
        return {"status": "ok", "strategy": "self_tune", "applied": params}

    monkeypatch.setattr("abcxauto.self_tune.apply_self_tune", fake_apply)
    turn = BrainTurn()
    await _run_tool(
        "set_risk",
        {"max_risk_per_trade_pct": 0.5},
        connector=None,
        world=_world(),
        snap={},
        turn=turn,
    )
    assert turn.last_strat == "self_tune"
    assert seen["params"]["max_risk_per_trade_pct"] == 0.5


@pytest.mark.asyncio
async def test_send_counts_tickets_and_combo_fact(monkeypatch):
    async def fake_exec(act, *_a, **_k):
        return {"status": "ok", "note": "IBKR combo (BAG)", "strategy": act.get("strategy")}

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", fake_exec)
    turn = BrainTurn()
    world = _world()
    raw = await _run_tool(
        "send",
        {"strategy": "vertical_spread", "params": {}},
        connector=None,
        world=world,
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data["sends_this_turn"] == 1
    assert "BAG" in data["combo"]
    raw2 = await _run_tool(
        "send",
        {"strategy": "buy_option", "params": {}},
        connector=None,
        world=world,
        snap={},
        turn=turn,
    )
    data2 = json.loads(raw2)
    assert data2["sends_this_turn"] == 2


@pytest.mark.asyncio
async def test_send_writes_last_turn_from_live_book(monkeypatch, tmp_path):
    """Successful send() stamps last_turn from the live book before cycle persist."""
    from abcxauto import think_stream as ts
    from abcxauto.world_state import lot_labels

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")

    live_pos = [{"symbol": "SPY", "secType": "STK", "quantity": 11}]
    live_ord = [
        {
            "symbol": "SPY",
            "sec_type": "STK",
            "action": "SELL",
            "order_type": "STP",
            "aux_price": 766.7,
        },
        {
            "symbol": "SPY",
            "sec_type": "STK",
            "action": "SELL",
            "order_type": "LMT",
            "lmt_price": 773.8,
        },
    ]

    persist_calls: list[int] = []

    def _boom(out):
        persist_calls.append(1)
        raise AssertionError("cycle persist must not be required")

    monkeypatch.setattr("abcxauto.agent_loop._persist_cycle", _boom)

    async def fake_exec(act, *_a, **_k):
        return {
            "success": True,
            "status": "submitted",
            "symbol": "SPY",
            "strategy": act.get("strategy"),
        }

    class Conn:
        async def get_positions(self):
            return live_pos

        async def get_open_orders(self):
            return live_ord

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", fake_exec)
    world = _world(flat=True, positions=[], net_liquidation=37000.0)
    turn = BrainTurn()
    await _run_tool(
        "send",
        {
            "strategy": "market_bracket",
            "params": {
                "symbol": "SPY",
                "quantity": 11,
                "direction": "LONG",
                "stop_price": 766.7,
                "target_price": 773.8,
            },
            "rationale": "spy long",
        },
        connector=Conn(),
        world=world,
        snap={
            "positions": [],
            "open_orders": [],
            "reality_pulse": {"ibkr_connected": True},
        },
        turn=turn,
    )
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert persist_calls == []
    assert last["stale"] is False
    assert last["flat"] is False
    assert last["sends"] == 1
    assert last["strat"] == "market_bracket"
    assert last["open_lots"] == lot_labels(live_pos)
    assert world.flat is False
    assert world.positions == live_pos


@pytest.mark.asyncio
async def test_book_has_combo_avg_and_sends_count():
    world = _world(
        positions=[{
            "symbol": "IWM",
            "conId": 1,
            "secType": "OPT",
            "quantity": 1,
            "avg_cost": 126.0,
            "market_price": 1.26,
            "expiration": "20260821",
            "strike": 306.0,
            "right": "C",
        }],
        book_reconciled=True,
    )
    turn = BrainTurn()
    raw = await _run_tool("book", {}, connector=None, world=world, snap={}, turn=turn)
    data = json.loads(raw)
    assert data["sends_this_turn"] == 0
    assert "BAG" in (data.get("world") or {}).get("combo", "")
    pos = (data.get("world") or {}).get("positions") or []
    assert pos
    assert abs(float(pos[0]["avg"]) - 1.26) < 1e-9
    assert pos[0]["avg_usd"] == 126.0
    assert pos[0]["expiration"] == "20260821"
    assert pos[0]["strike"] == 306.0
    assert pos[0]["right"] == "C"


def test_write_lab_playbook_has_no_think_essay_dump():
    """Keep [write_lab_playbook] marker only — no notebook text in the think stream."""
    import abcxauto.brain as brain

    assert not hasattr(brain, "_emit_write_lab_playbook_think")
    assert not hasattr(brain, "_LAB_PLAYBOOK_THINK_CAP")


@pytest.mark.asyncio
async def test_invoke_write_lab_playbook_emits_marker_only(monkeypatch):
    from abcxauto import brain
    from abcxauto.brain import BrainTurn, _invoke_named_tool
    from abcxauto.think_stream import subscribe, unsubscribe

    async def fake_run(name, args, **_k):
        return json.dumps({"status": "ok", "instructions": args.get("instructions")})

    monkeypatch.setattr(brain, "_run_tool", fake_run)
    got: list[str] = []

    def cap(kind: str, text: str) -> None:
        if kind == "say":
            got.append(text)

    note = "Paper: prefer debit verticals on index ETFs."
    subscribe(cap)
    try:
        out = await _invoke_named_tool(
            "write_lab_playbook",
            {"instructions": note, "mode": "explore"},
            1.0,
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    finally:
        unsubscribe(cap)
    assert json.loads(out)["status"] == "ok"
    assert got == ["\n[write_lab_playbook]\n"]
    joined = "".join(got)
    assert note not in joined
    assert "... [truncated]" not in joined


@pytest.mark.asyncio
async def test_invoke_write_lab_playbook_long_notebook_stays_off_stream(monkeypatch):
    from abcxauto import brain
    from abcxauto.brain import BrainTurn, _invoke_named_tool
    from abcxauto.think_stream import subscribe, unsubscribe

    async def fake_run(name, args, **_k):
        return json.dumps({"status": "ok"})

    monkeypatch.setattr(brain, "_run_tool", fake_run)
    got: list[str] = []

    def cap(kind: str, text: str) -> None:
        if kind == "say":
            got.append(text)

    note = ("AAPL — wait. " * 400) + ("x" * 2000)
    subscribe(cap)
    try:
        await _invoke_named_tool(
            "write_lab_playbook",
            {"instructions": note},
            1.0,
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
        await _invoke_named_tool(
            "write_lab_playbook",
            {"instructions": "   "},
            1.0,
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
        await _invoke_named_tool(
            "write_lab_playbook",
            {},
            1.0,
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    finally:
        unsubscribe(cap)
    assert got == [
        "\n[write_lab_playbook]\n",
        "\n[write_lab_playbook]\n",
        "\n[write_lab_playbook]\n",
    ]
    joined = "".join(got)
    assert "AAPL" not in joined
    assert "wait" not in joined
    assert "xxxx" not in joined
    assert "... [truncated]" not in joined


@pytest.mark.asyncio
async def test_invoke_other_tool_emits_marker_only(monkeypatch):
    from abcxauto import brain
    from abcxauto.brain import BrainTurn, _invoke_named_tool
    from abcxauto.think_stream import subscribe, unsubscribe

    async def fake_run(name, args, **_k):
        return json.dumps({"ok": name})

    monkeypatch.setattr(brain, "_run_tool", fake_run)
    got: list[str] = []

    def cap(kind: str, text: str) -> None:
        if kind == "say":
            got.append(text)

    subscribe(cap)
    try:
        await _invoke_named_tool(
            "book",
            {"instructions": "should not dump"},
            1.0,
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    finally:
        unsubscribe(cap)
    assert any("[book]" in t for t in got)
    assert not any("should not dump" in t for t in got)


def test_look_failed_question_empty_and_stream_error():
    assert BrainTurn(text="?").look_failed() is True
    assert BrainTurn(text="").look_failed() is True
    assert BrainTurn(text="  ").look_failed() is True
    assert BrainTurn(text="watching IWM").look_failed() is False
    assert BrainTurn(text="?", sends=[{"strat": "buy_option"}]).look_failed() is False
    assert BrainTurn(text="?", parked=True).look_failed() is False
    assert BrainTurn(failed=True, text="ok").look_failed() is True
    assert BrainTurn(last_result={"status": "error"}).look_failed() is True
    # First round said something; second round died as '?' — still a failed look.
    assert BrainTurn(
        text="I'll inspect the book, status, and playbook first.\n?",
        tool_trace=["book", "status", "playbook"],
    ).look_failed() is True
    assert BrainTurn(
        text="I'll inspect the book, status, and playbook first.",
        tool_trace=["book", "status", "playbook"],
    ).look_failed() is False
    # Unknown glyphs still smash to '?' and count as a dead last round.
    assert BrainTurn(text="\u2603").look_failed() is True
    assert BrainTurn(text="watching IWM \u2014 wait").look_failed() is False


@pytest.mark.asyncio
async def test_question_mark_turn_is_failed():
    from abcxauto.brain import grok_turn

    class Chat:
        def append(self, *_a, **_k):
            pass

        async def stream(self):
            yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                content="?", reasoning_content=""
            )

    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: Chat())),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=Chat(),
        _wake_n=1,
    )
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    assert turn.failed is True
    assert turn.look_failed() is True
    assert turn.parked is False
    assert getattr(g, "chat", None) is None


@pytest.mark.asyncio
async def test_trailing_question_after_tools_is_failed(monkeypatch):
    from abcxauto import brain
    from abcxauto.brain import grok_turn

    async def fake_read(name, args, **_k):
        return json.dumps({"ok": name})

    monkeypatch.setattr(brain, "_run_tool", fake_read)

    class TC:
        id = "1"
        function = SimpleNamespace(name="book", arguments="{}")

    class Chat:
        n = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.n += 1
            if self.n == 1:
                yield SimpleNamespace(tool_calls=[TC()]), SimpleNamespace(
                    content="I'll inspect the book first.",
                    reasoning_content="",
                )
            else:
                yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                    content="?", reasoning_content=""
                )

    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: Chat())),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=Chat(),
        _wake_n=1,
    )
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    assert "book" in turn.tool_trace
    assert turn.failed is True
    assert turn.look_failed() is True
    assert turn.parked is False
    assert getattr(g, "chat", None) is None


@pytest.mark.asyncio
async def test_resource_exhausted_turn_is_failed():
    from abcxauto.brain import grok_turn

    class Chat:
        def append(self, *_a, **_k):
            pass

        async def stream(self):
            raise RuntimeError("StatusCode.RESOURCE_EXHAUSTED")
            yield  # makes this an async generator

    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: Chat())),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=Chat(),
        _wake_n=1,
    )
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    assert turn.failed is True
    assert turn.look_failed() is True
    assert turn.parked is False
    assert getattr(g, "chat", None) is None


def _stay_up_chat_client(*, replies: list[str] | None = None):
    bag = list(replies or ["watching the book"])
    created: list[object] = []
    n = {"i": 0}

    class Chat:
        def __init__(self, text: str):
            self.appended: list[object] = []
            self.rounds = 0
            self._text = text

        def append(self, msg, **_k):
            self.appended.append(msg)

        async def stream(self):
            self.rounds += 1
            yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                content=self._text, reasoning_content=""
            )

    class _ChatNS:
        @staticmethod
        def create(**_k):
            i = min(n["i"], len(bag) - 1)
            n["i"] += 1
            chat = Chat(bag[i])
            created.append(chat)
            return chat

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=None,
        _wake_n=0,
    )
    return g, created


@pytest.mark.asyncio
async def test_stay_up_resume_keeps_chat_cold_start_does_not():
    """A finished paper RTH look continues the live chat. A cold look does not."""
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    g, created = _stay_up_chat_client()
    first = await grok_turn(
        g, connector=None, world=_world(), snap={}, wake="session=regular send."
    )
    assert first.look_failed() is False
    assert len(created) == 1
    live = g.chat
    assert live is created[0]

    second = await grok_turn(
        g,
        connector=None,
        world=_world(),
        snap={},
        wake="session=regular send.",
        resume=True,
    )
    assert second.look_failed() is False
    assert g.chat is live
    assert len(created) == 1

    third = await grok_turn(
        g,
        connector=None,
        world=_world(),
        snap={},
        wake="session=regular send.",
        resume=False,
    )
    assert third.look_failed() is False
    assert g.chat is not live
    assert len(created) == 2


@pytest.mark.asyncio
async def test_junk_stay_up_look_drops_chat_next_resume_is_cold():
    """Empty/? stay-up look drops the live chat. Resume after that is a new think."""
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    g, created = _stay_up_chat_client(replies=["?", "watching the book"])
    first = await grok_turn(
        g, connector=None, world=_world(), snap={}, wake="session=regular send."
    )
    assert first.look_failed() is True
    assert first.failed is True
    assert g.chat is None
    assert len(created) == 1
    dead = created[0]

    second = await grok_turn(
        g,
        connector=None,
        world=_world(),
        snap={},
        wake="session=regular send.",
        resume=True,
    )
    assert second.look_failed() is False
    assert g.chat is created[1]
    assert g.chat is not dead
    assert len(created) == 2


@pytest.mark.asyncio
async def test_empty_stay_up_look_drops_chat():
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    g, created = _stay_up_chat_client(replies=[""])
    turn = await grok_turn(
        g, connector=None, world=_world(), snap={}, wake="session=regular send."
    )
    assert turn.look_failed() is True
    assert g.chat is None
    assert len(created) == 1


@pytest.mark.asyncio
async def test_chat_start_error_drops_live_chat():
    """A dead stay-up append must not leave the junk chat for the next resume."""
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    g, created = _stay_up_chat_client()
    first = await grok_turn(
        g, connector=None, world=_world(), snap={}, wake="session=regular send."
    )
    assert first.look_failed() is False
    live = g.chat
    assert live is created[0]

    def boom(*_a, **_k):
        raise RuntimeError("developer append failed")

    live.append = boom
    turn = await grok_turn(
        g,
        connector=None,
        world=_world(),
        snap={},
        wake="session=regular send.",
        resume=True,
    )
    assert turn.failed is True
    assert turn.stream_error
    assert g.chat is None


@pytest.mark.asyncio
async def test_stay_up_resume_keeps_chat_in_premarket():
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    g, created = _stay_up_chat_client()
    first = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="premarket"),
        snap={},
        wake="session=premarket send.",
    )
    assert first.look_failed() is False
    live = g.chat
    assert live is created[0]
    second = await grok_turn(
        g,
        connector=None,
        world=_world(session_status="premarket"),
        snap={},
        wake="session=premarket send.",
        resume=True,
    )
    assert second.look_failed() is False
    assert g.chat is live
    assert len(created) == 1


@pytest.mark.asyncio
async def test_stay_up_resume_drops_refused_send_keeps_chat(monkeypatch):
    """Clerk-blocked send is not the next look's send target. Chat stays."""
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()

    async def blocked(*_a, **_k):
        return {"status": "blocked", "note": "clerk_block: no card"}

    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", blocked)

    created: list[object] = []

    class TC:
        id = "1"
        function = SimpleNamespace(
            name="send",
            arguments=json.dumps(
                {
                    "strategy": "market_bracket",
                    "params": {
                        "symbol": "NVDA",
                        "quantity": 1,
                        "direction": "LONG",
                        "stop_price": 100.0,
                    },
                    "rationale": "flush bounce",
                }
            ),
        )

    class Chat:
        def __init__(self):
            self.rounds = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.rounds += 1
            if self.rounds == 1:
                yield SimpleNamespace(tool_calls=[TC()]), SimpleNamespace(
                    content="sending", reasoning_content=""
                )
            else:
                yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                    content="blocked; standing down", reasoning_content=""
                )

    class _ChatNS:
        @staticmethod
        def create(**_k):
            chat = Chat()
            created.append(chat)
            return chat

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=None,
        _wake_n=0,
    )
    first = await grok_turn(
        g, connector=None, world=_world(), snap={}, wake="session=regular send."
    )
    assert first.last_act.get("strategy") == "market_bracket"
    assert first.last_result.get("status") == "blocked"
    assert first.sends
    live = g.chat
    assert live is created[0]

    second = await grok_turn(
        g,
        connector=None,
        world=_world(),
        snap={},
        wake="session=regular send.",
        resume=True,
    )
    assert g.chat is live
    assert len(created) == 1
    assert second.last_act == {}
    assert second.sends == []
    assert second.last_strat == ""
    assert str(second.last_result.get("status") or "") != "blocked"


@pytest.mark.asyncio
async def test_closed_look_drops_chat_after_think():
    from abcxauto.brain import grok_turn

    g, created = _stay_up_chat_client()
    await grok_turn(
        g,
        connector=None,
        world=_world(session_status="closed"),
        snap={},
        wake="session=closed send.",
    )
    assert g.chat is None
    assert len(created) == 1
