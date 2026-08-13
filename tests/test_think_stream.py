"""Live Grok thinking stream."""

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
        emit("stage", "judge")
        emit("say", '{"stance":"idle"}')
    finally:
        bind_engine(None)
    assert "GROK JUDGE" in st.think_live
    assert '{"stance":"idle"}' in st.think_live


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

    class _ChatNS:
        @staticmethod
        def create(**_k):
            return Chat()

    g = SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.5",
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
        model="grok-4.5",
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
