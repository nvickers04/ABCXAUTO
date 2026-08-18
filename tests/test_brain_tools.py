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


def _tool_names() -> set[str]:
    names = set()
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        names.add(str(getattr(fn, "name", None) or getattr(t, "name", "") or ""))
    return names


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
    assert "backtest" in candles["use"]
    assert candles["symbol"] == "SPY"
    assert candles["bars"]


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


def test_episode_chat_reuses_on_fill_until_max():
    from abcxauto.brain import EPISODE_MAX, _ensure_chat, _open_wake
    from abcxauto.wake_bus import BookEvent, note_wake

    g, created = _stub_chat_client()
    boot = _ensure_chat(g, kind="boot")
    fill = _ensure_chat(g, kind="fill")
    assert fill is boot
    assert _ensure_chat(g, kind="order_change") is boot
    assert _ensure_chat(g, kind="book_move") is boot
    assert len(created) == 1
    for _ in range(EPISODE_MAX):
        _ensure_chat(g, kind="fill")
    assert len(created) == 2
    note_wake(BookEvent(kind="fill", detail="XLF filled"))
    try:
        g2, created2 = _stub_chat_client()
        first = _open_wake(g2, "look brief")
        second = _open_wake(g2, "delta brief")
        assert second is first
        assert len(created2) == 1
    finally:
        note_wake(None)


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
    _open_wake(g, "Cycle 1. session=regular")
    assert got
    assert got[0].role == developer("x").role
    assert got[0].role != user("x").role
    text = "".join(c.text for c in got[0].content)
    assert "Cycle 1." in text
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

    monkeypatch.setattr(brain, "STREAM_CHUNK_S", 0.02)
    monkeypatch.setattr(brain, "STREAM_IDLE_LIMIT", 2)

    class Chat:
        async def stream(self):
            while True:
                await asyncio.sleep(10)
                yield None, SimpleNamespace(content="", reasoning_content="")

    text, resp, _reason = await brain.stream_round(Chat())
    assert resp is None
    assert text == ""


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
    _new_chat(g)
    assert captured.get("tool_choice") != "required"


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
    assert turn.last_strat == "hold"


@pytest.mark.asyncio
async def test_read_tools_run_in_parallel(monkeypatch):
    import asyncio
    import time

    from abcxauto import brain
    from abcxauto.brain import grok_turn

    running = 0
    max_running = 0

    async def slow_read(name, args, **_k):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.12)
        running -= 1
        return json.dumps({"ok": name})

    monkeypatch.setattr(brain, "_run_tool", slow_read)

    class TC:
        def __init__(self, name, cid):
            self.id = cid
            self.function = SimpleNamespace(name=name, arguments="{}")

    class Resp:
        tool_calls = [TC("quote", "1"), TC("news", "2"), TC("scan", "3")]

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
    t0 = time.monotonic()
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    elapsed = time.monotonic() - t0
    assert set(turn.tool_trace) == {"quote", "news", "scan"}
    assert max_running >= 2
    assert elapsed < 0.30


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


def test_send_tool_says_one_ticket_per_call():
    fn = _tool_fn("send")
    desc = str(getattr(fn, "description", "") or "")
    assert "again this turn" in desc.lower()
    assert "place one ticket" not in desc.lower()
    assert "self_tune" in desc.lower()
    props = _tool_props("send")
    strat = props.get("strategy") or {}
    assert "enum" in strat
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
