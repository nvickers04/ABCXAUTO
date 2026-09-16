from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto.brain import (
    BrainTurn,
    _dispatch_tool_calls,
    _grok_turn_impl,
    _inject_live_poke,
    _run_tool,
    grok_turn,
)
from abcxauto.look_snapshot import (
    LOOK_TOOLS,
    begin_look,
    check_ticket_numbers,
    record_look_tool,
    snapshot_bags,
)
from abcxauto.park_clock import BookEvent, clear_interrupt, note_interrupt
from abcxauto.world_state import WorldState


NOK_10C = {
    "symbol": "NOK",
    "expiration": "20260925",
    "strike": 10.0,
    "right": "C",
    "source": "ibkr",
    "freshness": "live",
    "last": 0.32,
    "bid": 0.31,
    "ask": 0.35,
    "mid": 0.33,
}
NOK_105C = {
    "symbol": "NOK",
    "expiration": "20260925",
    "strike": 10.5,
    "right": "C",
    "source": "ibkr",
    "freshness": "live",
    "last": 0.16,
    "bid": 0.16,
    "ask": 0.18,
    "mid": 0.17,
}


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


def _look_bag(snap: dict) -> dict:
    bag = snap.get("_look_tool_snapshot")
    assert isinstance(bag, dict)
    return bag


def _record_nok_legs(snap: dict) -> None:
    begin_look(snap)
    record_look_tool(snap, "option_quote", dict(NOK_10C))
    record_look_tool(snap, "option_quote", dict(NOK_105C))


def _assert_nok_legs_in_cache(snap: dict) -> None:
    bag = _look_bag(snap)
    rows = list(bag.get("option_quote") or [])
    strikes = {row.get("strike") for row in rows if isinstance(row, dict)}
    assert 10.0 in strikes and 10.5 in strikes, rows
    bags = snapshot_bags(snap)
    opt_strikes = {
        key[4] for key in bags if isinstance(key, tuple) and key[:2] == ("OPT", "NOK")
    }
    # _canon(strike) is strike * 10000
    assert 100000 in opt_strikes and 105000 in opt_strikes, bags.keys()


class _Fn:
    def __init__(self, name: str, args: dict):
        self.name = name
        self.arguments = json.dumps(args)


class _Call:
    def __init__(self, cid: str, name: str, args: dict):
        self.id = cid
        self.function = _Fn(name, args)


class _Chat:
    def append(self, *_a, **_k):
        pass

    async def stream(self):
        yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
            content="watching NOK", reasoning_content=""
        )


def _client(chat=None):
    live = chat if chat is not None else _Chat()

    class _ChatNS:
        @staticmethod
        def create(**_k):
            return live

    return SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=chat,
        _wake_n=1 if chat is not None else 0,
        _wake_appended=False,
        _last_desk_fact="",
        _chat_had_work=False,
    )


@pytest.fixture(autouse=True)
def _no_pending_interrupt():
    clear_interrupt()
    yield
    clear_interrupt()


@pytest.mark.asyncio
async def test_option_quote_legs_survive_turn_reentry_same_look():
    """Paid option_quote rows must survive a second _grok_turn_impl.

    The 2026-09-15 NOK vertical: both legs were quoted this look, then a
    [stream loop] abort re-entered the turn and begin_look wiped the cache.
    """
    snap: dict = {}
    _record_nok_legs(snap)
    _assert_nok_legs_in_cache(snap)

    g = _client(chat=_Chat())
    await _grok_turn_impl(
        g,
        connector=None,
        world=_world(),
        snap=snap,
        wake="--- GROK ---",
        resume=True,
    )
    _assert_nok_legs_in_cache(snap)
    bag = _look_bag(snap)
    assert len(bag["option_quote"]) == 2


@pytest.mark.asyncio
async def test_live_book_poke_preserves_price_quotes(monkeypatch):
    snap: dict = {"positions": [{"symbol": "NOK", "qty": -1}]}
    _record_nok_legs(snap)
    _assert_nok_legs_in_cache(snap)
    turn = BrainTurn()
    turn.tool_cache["quote:NOK"] = "{}"

    async def _fresh(_connector):
        return {
            "positions": [{"symbol": "NOK", "qty": 0}],
            "net_liquidation": 37100.0,
            "account": {"netliquidation": 37100.0},
            "protection": {},
            "market_hours": {"session": {"status": "regular"}},
        }

    monkeypatch.setattr("abcxauto.agent_loop.snap", _fresh)
    note_interrupt(BookEvent("fill", "NOK vertical filled"))

    class _Conn:
        connected = True

    ok = await _inject_live_poke(
        _Chat(),
        connector=_Conn(),
        world=_world(),
        snap=snap,
        turn=turn,
    )
    assert ok is True
    _assert_nok_legs_in_cache(snap)
    assert turn.tool_cache == {}


@pytest.mark.asyncio
async def test_mutating_send_clears_look_cache(monkeypatch):
    snap: dict = {}
    _record_nok_legs(snap)
    _assert_nok_legs_in_cache(snap)
    turn = BrainTurn()

    async def _fake_run_tool(name, args, **_k):
        return json.dumps({"status": "submitted", "order_id": 9})

    monkeypatch.setattr("abcxauto.brain._run_tool", _fake_run_tool)
    monkeypatch.setattr("abcxauto.brain._append_tool_result", lambda *_a, **_k: None)

    await _dispatch_tool_calls(
        [_Call("c1", "send", {"strategy": "close_position", "params": {"symbol": "NOK"}})],
        chat=_Chat(),
        connector=None,
        world=_world(),
        snap=snap,
        turn=turn,
    )
    bag = _look_bag(snap)
    assert bag["quote"] == []
    assert bag["option_quote"] == []
    assert bag["book"] is None
    assert turn.tool_cache == {}


@pytest.mark.asyncio
async def test_new_look_starts_with_empty_look_cache():
    leftover: dict = {}
    _record_nok_legs(leftover)
    _assert_nok_legs_in_cache(leftover)

    g = _client(chat=None)
    await grok_turn(
        g,
        connector=None,
        world=_world(),
        snap=leftover,
        wake="session=regular send.",
        resume=False,
    )
    bag = leftover.get("_look_tool_snapshot")
    assert isinstance(bag, dict)
    assert bag.get("quote") in (None, [])
    assert bag.get("option_quote") in (None, [])
    assert bag.get("book") in (None,)


def test_mda_and_delayed_tools_cannot_populate_look_cache():
    assert LOOK_TOOLS == ("quote", "option_quote", "book")
    snap: dict = {}
    begin_look(snap)
    record_look_tool(
        snap,
        "scan",
        {"source": "mda", "freshness": "delayed_15m", "last": 91.5, "symbol": "SNDK"},
    )
    record_look_tool(
        snap,
        "news",
        {"source": "mda", "freshness": "delayed_15m", "headline": "x", "last": 12.3},
    )
    record_look_tool(
        snap,
        "option_facts",
        {"source": "mda", "freshness": "delayed_15m", "iv": 0.99, "last": 0.44},
    )
    record_look_tool(
        snap,
        "candles",
        {"source": "mda", "freshness": "delayed_15m", "last": 500.12},
    )
    bag = _look_bag(snap)
    assert bag["quote"] == []
    assert bag["option_quote"] == []
    assert bag["book"] is None

    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "expiration": "20260718",
            "strike": 500.0,
            "right": "C",
            "ibkr": {"last": 1.20, "bid": 1.18, "ask": 1.22},
            "mda": {"iv": 0.99, "last": 9.99, "bid": 9.9, "ask": 10.1},
        },
    )
    ok, code, _msg = check_ticket_numbers(
        "vertical_spread",
        {"symbol": "SPY", "iv": 0.99, "limit_price": 9.99},
        snap,
    )
    assert ok is False
    assert code == "stale_or_invented_number"


@pytest.mark.asyncio
async def test_mda_run_tools_do_not_record_look_prints(monkeypatch):
    snap: dict = {}
    begin_look(snap)

    async def _fake_scan(**_k):
        return {
            "ok": True,
            "source": "mda",
            "freshness": "delayed_15m",
            "hits": [{"symbol": "SNDK", "last": 91.5}],
        }

    async def _fake_news(*_a, **_k):
        return [{"symbol": "SNDK", "headline": "x", "source": "mda", "last": 91.5}]

    monkeypatch.setattr("abcxauto.brain.criteria_scan", _fake_scan)
    monkeypatch.setattr("abcxauto.brain._mda_news", _fake_news)
    monkeypatch.setattr(
        "abcxauto.brain_tools.run_readonly_tool",
        lambda *_a, **_k: json.dumps({"error": "offline"}),
    )

    await _run_tool(
        "scan",
        {"scan_code": "MOST_ACTIVE"},
        connector=None,
        world=_world(),
        snap=snap,
        turn=BrainTurn(),
    )
    await _run_tool(
        "news",
        {"symbols": ["SNDK"]},
        connector=None,
        world=_world(),
        snap=snap,
        turn=BrainTurn(),
    )
    bag = _look_bag(snap)
    assert bag["quote"] == []
    assert bag["option_quote"] == []
    assert bag["book"] is None

@pytest.mark.asyncio
async def test_send_then_reentry_does_not_revive_quotes(monkeypatch):
    snap: dict = {}
    _record_nok_legs(snap)
    turn = BrainTurn()

    async def _fake_run_tool(name, args, **_k):
        return json.dumps({"status": "submitted", "order_id": 9})

    monkeypatch.setattr("abcxauto.brain._run_tool", _fake_run_tool)
    monkeypatch.setattr("abcxauto.brain._append_tool_result", lambda *_a, **_k: None)
    await _dispatch_tool_calls(
        [_Call("c1", "send", {"strategy": "close_position", "params": {"symbol": "NOK"}})],
        chat=_Chat(),
        connector=None,
        world=_world(),
        snap=snap,
        turn=turn,
    )
    g = _client(chat=_Chat())
    await _grok_turn_impl(
        g,
        connector=None,
        world=_world(),
        snap=snap,
        wake="--- GROK ---",
        resume=True,
    )
    bag = _look_bag(snap)
    assert bag["option_quote"] == []
    assert bag["quote"] == []
    assert bag["book"] is None


@pytest.mark.asyncio
async def test_same_look_reentry_restores_cache_onto_a_fresh_snap():
    paid: dict = {}
    _record_nok_legs(paid)
    chat = _Chat()
    g = _client(chat=chat)
    from abcxauto.brain import _stash_look_tool_bag

    _stash_look_tool_bag(chat, paid)
    _stash_look_tool_bag(g, paid)
    fresh: dict = {"positions": [], "net_liquidation": 37000.0}
    await _grok_turn_impl(
        g,
        connector=None,
        world=_world(),
        snap=fresh,
        wake="--- GROK ---",
        resume=True,
    )
    _assert_nok_legs_in_cache(fresh)
