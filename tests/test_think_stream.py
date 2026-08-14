"""Live Grok thinking stream."""

import json
from types import SimpleNamespace

import pytest

from abcxauto.think_stream import (
    ascii_text,
    bind_engine,
    emit,
    subscribe,
    unsubscribe,
)


def test_ascii_text_is_cp1252_safe():
    out = ascii_text("idle -> hold  thesis: AAPL — wait")
    assert all(ord(c) < 128 for c in out)


def test_emit_reaches_subscriber():
    got: list[tuple[str, str]] = []

    def cap(kind: str, text: str) -> None:
        got.append((kind, text))

    subscribe(cap)
    try:
        emit("stage", "judge")
        emit("say", "hello")
    finally:
        unsubscribe(cap)
    assert ("stage", "judge") in got
    assert ("say", "hello") in got


def test_bind_engine_appends_think_live():
    st = SimpleNamespace(think_live="")
    bind_engine(SimpleNamespace(state=st))
    try:
        emit("stage", "grok")
        emit("say", '{"stance":"idle"}')
    finally:
        bind_engine(None)
    assert "--- GROK ---" in st.think_live
    assert '{"stance":"idle"}' in st.think_live


def test_think_tail_and_last_turn_files(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "_TAIL_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(ts, "_last_tail_write", 0.0)
    st = SimpleNamespace(think_live="")
    ts.bind_engine(SimpleNamespace(state=st))
    try:
        ts.emit("say", "Wake Grok.\n")
    finally:
        ts.bind_engine(None)
    assert "Wake Grok" in (tmp_path / "think_tail.txt").read_text(encoding="utf-8")
    ts.write_last_turn({
        "cycle": 3,
        "strat": "hold",
        "rationale": "premarket",
        "validation": "ok",
        "tool_trace": ["book", "quote"],
        "scan_fetched": ["NVDA"],
        "reality_pulse": {"session": {"status": "premarket"}},
    })
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert last["tool_trace"] == ["book", "quote"]
    assert last["session"]["status"] == "premarket"


def test_run_identity_stale_last_turn(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "THINK_TAIL_PATH", tmp_path / "think_tail.txt")
    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "RUN_PATH", tmp_path / "run.json")
    ts._run = {}
    first = ts.begin_run()
    ts.write_last_turn({"cycle": 7, "strat": "hold", "tool_trace": ["book"]})
    live = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert live["run_id"] == first["run_id"]
    assert live["stale"] is False
    assert ts.last_turn_is_live(live) is True
    second = ts.begin_run()
    assert second["run_id"] != first["run_id"]
    stale = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert stale["stale"] is True
    assert ts.last_turn_is_live(stale) is False
    assert ts.last_turn_is_live(live) is False


def test_bind_engine_keeps_prior_cycle_on_new_wake():
    st = SimpleNamespace(think_live="Cycle 1: snapping book, then Grok...\n")
    bind_engine(SimpleNamespace(state=st))
    try:
        emit("stage", "grok")
        emit("say", "weigh tape")
    finally:
        bind_engine(None)
    assert "Cycle 1" in st.think_live
    assert "--- GROK ---" in st.think_live
    assert "weigh tape" in st.think_live


@pytest.mark.asyncio
async def test_grok_streams_think_and_say(monkeypatch):
    from abcxauto.agent_loop import grok

    class Ch:
        def __init__(self, content="", reasoning_content=""):
            self.content = content
            self.reasoning_content = reasoning_content

    class Chat:
        async def stream(self):
            yield None, Ch(reasoning_content="weigh tape ")
            yield None, Ch(content='{"stance":"idle"}')

    created: dict = {}

    class _ChatNS:
        @staticmethod
        def create(**k):
            created.update(k)
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
    )
    got: list[tuple[str, str]] = []

    def cap(kind: str, text: str) -> None:
        got.append((kind, text))

    subscribe(cap)
    try:
        out = await grok(g, "prompt", stage="judge")
    finally:
        unsubscribe(cap)
    assert out == '{"stance":"idle"}'
    assert created.get("include") == ["verbose_streaming"]
    assert any(k == "think" and t == "weigh tape " for k, t in got)
    assert any(k == "say" and '{"stance":"idle"}' in t for k, t in got)
    assert any(k == "stage" and t == "judge" for k, t in got)


@pytest.mark.asyncio
async def test_grok_streams_cumulative_reasoning_as_delta(monkeypatch):
    from abcxauto.agent_loop import grok

    class Ch:
        def __init__(self, content="", reasoning_content=""):
            self.content = content
            self.reasoning_content = reasoning_content

    class Chat:
        async def stream(self):
            yield None, Ch(reasoning_content="weigh")
            yield None, Ch(reasoning_content="weigh tape")
            yield None, Ch(content='{"stance":"idle"}')

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
    thinks: list[str] = []

    def cap(kind: str, text: str) -> None:
        if kind == "think":
            thinks.append(text)

    subscribe(cap)
    try:
        await grok(g, "prompt", stage="judge")
    finally:
        unsubscribe(cap)
    assert thinks == ["weigh", " tape"]


@pytest.mark.asyncio
async def test_grok_uses_client_max_tokens_not_2048_cap():
    from abcxauto.agent_loop import grok

    class Ch:
        def __init__(self, content="", reasoning_content=""):
            self.content = content
            self.reasoning_content = reasoning_content

    class BoomResp:
        @property
        def reasoning_content(self):
            raise AssertionError("stream must not read Response.reasoning_content")

        @property
        def content(self):
            raise AssertionError("stream must not read Response.content")

    class Chat:
        async def stream(self):
            yield BoomResp(), Ch(reasoning_content="weigh tape ")
            yield BoomResp(), Ch(content='{"stance":"idle"}')

    created: dict = {}

    class _ChatNS:
        @staticmethod
        def create(**k):
            created.update(k)
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=8192,
    )
    out = await grok(g, "prompt", stage="judge")
    assert out == '{"stance":"idle"}'
    assert created.get("max_tokens") == 8192
