"""Grok tools: IBKR live vs MDA delayed."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.brain import (
    AGENT_TOOLS,
    BrainTurn,
    _compact_chain,
    _run_tool,
    _stash_live,
)
from abcxauto.broker.util import quote_from_ticker
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
        "set_wake",
    } <= names
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


def test_stash_live_records_ibkr_only():
    world = _world()
    snap: dict = {}
    _stash_live(world, snap, {"symbol": "QQQ", "last": 400.0, "source": "ibkr"})
    _stash_live(world, snap, {"symbol": "IWM", "last": 200.0, "source": "mda"})
    assert snap["ibkr_live_quotes"]["QQQ"] == 400.0
    assert "IWM" not in snap.get("ibkr_live_quotes", {})
    assert world.ibkr_live_symbol == "QQQ"


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
async def test_news_and_candles_are_labeled_delayed(monkeypatch):
    async def fake_news(_pos=None, **_k):
        return [{"symbol": "SPY", "headline": "Tape note"}]

    class MDA:
        is_configured = True

        async def get_stock_candles(self, symbol, resolution="D", countback=60, **_k):
            return [{"t": 1, "c": 500.0}]

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
    assert candles["source"] == "mda"
    assert "delayed" in candles["freshness"]
    assert candles["freshness"] == "delayed_daily"
    assert candles["mda_last_is"] == "daily_bar_close"
    assert "backtest" in candles["use"]
    assert candles["symbol"] == "SPY"
    assert candles["bars"]


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

    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "SPY", "resolution": "15", "countback": 20},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert data["freshness"] == "ibkr_rth"
    assert data["bars"][0]["c"] == 768.5


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
async def test_candles_skips_hist_when_rt_buffer_warm(monkeypatch):
    class Conn:
        def realtime_bar_buffer(self, symbol):
            return [{"t": "2026-08-19T12:00:00", "c": 311.0}]

        async def get_historical_bars(self, symbol, **_k):
            raise AssertionError("hist must be skipped when the 5s buffer is warm")

        async def get_realtime_bars(self, symbol, **_k):
            return {
                "symbol": symbol,
                "bars": [{"t": "2026-08-19T12:00:00", "c": 311.0}],
                "source": "ibkr",
                "freshness": "ibkr_rt_5s",
                "resolution": "5s",
            }

    class MDA:
        async def get_stock_candles(self, *a, **k):
            raise AssertionError("MDA must not run")

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "AAPL", "resolution": "15"},
            connector=Conn(),
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "ibkr"
    assert data["freshness"] == "ibkr_rt_5s"
    assert data["bars"][0]["c"] == 311.0


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
async def test_candles_15_labeled_intrabar(monkeypatch):
    class MDA:
        is_configured = True

        async def get_stock_candles(self, symbol, resolution="D", countback=60, **_k):
            return [{"t": 1, "c": 717.5}]

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbol": "QQQ", "resolution": "15", "countback": 20},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["freshness"] == "delayed_15m"
    assert data["mda_last_is"] == "intrabar_close"
    assert data["resolution"] == "15"


@pytest.mark.asyncio
async def test_candles_batch_returns_series(monkeypatch):
    seen: list[str] = []

    class MDA:
        is_configured = True

        async def get_stock_candles(self, symbol, resolution="D", countback=60, **_k):
            seen.append(symbol)
            return [{"t": 1, "c": 100.0 + len(seen)}]

    monkeypatch.setattr("abcxauto.marketdata.client.get_marketdata_client", lambda: MDA())
    data = json.loads(
        await _run_tool(
            "candles",
            {"symbols": ["SPY", "QQQ", "IWM"], "resolution": "D", "countback": 20},
            connector=None,
            world=_world(),
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["source"] == "mda"
    assert {row["symbol"] for row in data["series"]} == {"SPY", "QQQ", "IWM"}
    assert set(seen) == {"SPY", "QQQ", "IWM"}


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

    raw = await _run_tool(
        "status", {}, connector=Conn(), world=_world(), snap={}, turn=BrainTurn()
    )
    data = json.loads(raw)
    assert "ibkr_connected" in data
    assert "trading_mode" in data
    assert "session" in data
    assert "ibkr_port" in data or "ibkr_client_id" in data
    assert "levers" in data
    assert "max_risk_per_trade_pct" in data["levers"]
    assert "max_open_positions" in data["levers"]


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
    assert "Standing notes" in data["current"]["instructions"]
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
    assert "Standing notes" in full["current"]["instructions"]


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
    """One wake = one think. Continuity is the playbook, not a recycled chat."""
    from abcxauto.brain import _ensure_chat, _open_wake
    from abcxauto.wake_bus import BookEvent, note_wake

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
    """End-of-turn keeps chat unless set_wake parked; park → new think next open."""
    from abcxauto.brain import BrainTurn, _ensure_chat, _reset_chat

    g, created = _stub_chat_client()
    chat = _ensure_chat(g, kind="boot")
    turn = BrainTurn(parked=False)
    # Simulate RTH yield: no park → chat survives.
    if turn.parked:
        _reset_chat(g)
    assert getattr(g, "chat", None) is chat
    turn.parked = True
    if turn.parked:
        _reset_chat(g)
    assert getattr(g, "chat", None) is None
    nxt = _ensure_chat(g, kind="alarm")
    assert nxt is not chat
    assert len(created) == 2


def test_set_wake_tool_description_is_park_not_next_look():
    from abcxauto.brain import AGENT_TOOLS

    desc = ""
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        name = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        if name == "set_wake":
            desc = str(getattr(fn, "description", None) or getattr(t, "description", "") or "")
            break
    assert desc
    assert "park" in desc.lower()
    assert "next look" not in desc.lower()
    assert "don't park" not in desc.lower()
    assert "do not park" not in desc.lower()


def test_agent_tools_omits_set_wake_in_rth():
    from abcxauto.brain import AGENT_TOOLS, agent_tools

    assert "set_wake" in _names_of(AGENT_TOOLS)
    assert "set_wake" not in _names_of(agent_tools(session="regular"))
    assert "send" in _names_of(agent_tools(session="regular"))


def test_agent_tools_keeps_set_wake_overnight():
    from abcxauto.brain import agent_tools

    for sess in ("closed", "postmarket"):
        assert "set_wake" in _names_of(agent_tools(session=sess))
    assert "set_wake" not in _names_of(agent_tools(session="premarket"))


@pytest.mark.asyncio
async def test_paper_rth_set_wake_tool_is_ignored_same_think(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_risk_gate",
        lambda: type("G", (), {"is_halted": False})(),
    )
    turn = BrainTurn()
    raw = await _run_tool(
        "set_wake",
        {"wake_in_s": 3000, "wake_if": ["fill"]},
        connector=None,
        world=_world(session_status="regular", flat=True),
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data["status"] == "ignored"
    assert data["reason"] == "paper_stay_up"
    assert data["wake_at"] is None
    assert data["session"] == "regular"
    assert data.get("wanted_wake_in_s") == 3000
    assert turn.parked is False


@pytest.mark.asyncio
async def test_overnight_set_wake_tool_parks(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    turn = BrainTurn()
    raw = await _run_tool(
        "set_wake",
        {"wake_in_s": 8 * 3600, "wake_if": ["fill"]},
        connector=None,
        world=_world(session_status="closed", flat=True),
        snap={},
        turn=turn,
    )
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["wake_at"]
    assert turn.parked is True


def test_live_poke_interrupt_skips_reset_chat():
    """fill/order_change/unprotected poke the live episode without _reset_chat."""
    import asyncio

    from abcxauto.brain import BrainTurn, _ensure_chat, _inject_live_poke
    from abcxauto.wake_bus import BookEvent, clear_interrupt, note_interrupt

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
