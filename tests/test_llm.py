"""xAI client wrapper: short capacity retry on create/stream."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto import llm
from abcxauto.llm import CAPACITY_TRIES, GrokClient, is_capacity_error


class _FailThenOkChat:
    def __init__(self, fail_times: int, err: str = "StatusCode.RESOURCE_EXHAUSTED") -> None:
        self.fail_times = fail_times
        self.err = err
        self.stream_calls = 0

    async def stream(self):
        self.stream_calls += 1
        if self.stream_calls <= self.fail_times:
            raise RuntimeError(self.err)
        yield SimpleNamespace(), SimpleNamespace(content="ok")


class _AlwaysExhaustedChat:
    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self):
        self.stream_calls += 1
        raise RuntimeError(f"StatusCode.RESOURCE_EXHAUSTED try {self.stream_calls}")
        yield  # async generator


class _FailThenOkCreate:
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.create_calls = 0
        self.chat = _FailThenOkChat(fail_times=0)

    def create(self, **_k):
        self.create_calls += 1
        if self.create_calls <= self.fail_times:
            raise RuntimeError("StatusCode.RESOURCE_EXHAUSTED")
        return self.chat


def _client(chat) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: chat))


@pytest.fixture
def capacity_sleeps(monkeypatch):
    slept: list[float] = []

    async def _asleep(sec):
        slept.append(float(sec))

    def _sleep(sec):
        slept.append(float(sec))

    monkeypatch.setattr(llm.asyncio, "sleep", _asleep)
    monkeypatch.setattr(llm.time, "sleep", _sleep)
    return slept


def test_capacity_error_detects_resource_exhausted_and_capacity():
    assert is_capacity_error("StatusCode.RESOURCE_EXHAUSTED") is True
    assert is_capacity_error("The model is currently at capacity due to high demand") is True
    assert is_capacity_error("empty assistant text") is False
    assert is_capacity_error("") is False


def test_capacity_backoff_is_short_not_a_park():
    assert llm.CAPACITY_RETRIES in (2, 3)
    assert llm.CAPACITY_BACKOFF_MIN_S == 20.0
    assert llm.CAPACITY_BACKOFF_MAX_S == 45.0
    assert llm.CAPACITY_BACKOFF_MAX_S < 60.0


@pytest.mark.asyncio
async def test_stream_retries_twice_then_succeeds(capacity_sleeps):
    chat = _FailThenOkChat(fail_times=2)
    g = GrokClient(client=_client(chat))
    session = g.client.chat.create(model="grok-4.6")
    chunks = [item async for item in session.stream()]
    assert chat.stream_calls == 3
    assert chunks
    assert chunks[-1][1].content == "ok"
    assert len(capacity_sleeps) == 2
    assert all(20.0 <= s <= 45.0 for s in capacity_sleeps)


@pytest.mark.asyncio
async def test_stream_stays_exhausted_surfaces_last_error(capacity_sleeps):
    chat = _AlwaysExhaustedChat()
    g = GrokClient(client=_client(chat))
    session = g.client.chat.create(model="grok-4.6")
    with pytest.raises(RuntimeError, match=r"RESOURCE_EXHAUSTED try ") as caught:
        async for _ in session.stream():
            pass
    assert chat.stream_calls == CAPACITY_TRIES
    assert str(caught.value) == f"StatusCode.RESOURCE_EXHAUSTED try {CAPACITY_TRIES}"
    assert len(capacity_sleeps) == CAPACITY_TRIES - 1
    assert all(20.0 <= s <= 45.0 for s in capacity_sleeps)


def test_create_retries_twice_then_succeeds(capacity_sleeps):
    api = _FailThenOkCreate(fail_times=2)
    g = GrokClient(client=SimpleNamespace(chat=api))
    session = g.client.chat.create(model="grok-4.6")
    assert api.create_calls == 3
    assert callable(session.stream)
    assert len(capacity_sleeps) == 2
    assert all(20.0 <= s <= 45.0 for s in capacity_sleeps)


def test_create_stays_exhausted_surfaces_last_error(capacity_sleeps):
    class _Dead:
        def __init__(self) -> None:
            self.create_calls = 0

        def create(self, **_k):
            self.create_calls += 1
            raise RuntimeError(f"StatusCode.RESOURCE_EXHAUSTED try {self.create_calls}")

    api = _Dead()
    g = GrokClient(client=SimpleNamespace(chat=api))
    with pytest.raises(RuntimeError, match=r"RESOURCE_EXHAUSTED try ") as caught:
        g.client.chat.create(model="grok-4.6")
    assert api.create_calls == CAPACITY_TRIES
    assert str(caught.value) == f"StatusCode.RESOURCE_EXHAUSTED try {CAPACITY_TRIES}"


@pytest.mark.asyncio
async def test_stream_ordinary_error_does_not_retry(capacity_sleeps):
    chat = _FailThenOkChat(fail_times=5, err="connection reset")
    g = GrokClient(client=_client(chat))
    session = g.client.chat.create(model="grok-4.6")
    with pytest.raises(RuntimeError, match="connection reset"):
        async for _ in session.stream():
            pass
    assert chat.stream_calls == 1
    assert capacity_sleeps == []
