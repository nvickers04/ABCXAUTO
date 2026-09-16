"""Reasoning activity is not silence. Paid think must not be thrown away.

Regression for the 2026-09-16 silent-tip burn: stream_round treated a
live reasoning_content stream as hung after SILENT_GROK_TIP_S (48s),
classified it empty, and grok_turn re-billed the same prompt twice.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from abcxauto import brain
from abcxauto.park_clock import clear_interrupt


class _Clock:
    def __init__(self, t: float = 1_000.0) -> None:
        self.t = float(t)

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def _g(chat, *, recover_chat: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: chat)),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=8192,
        chat=chat if recover_chat else None,
        _wake_n=1,
        _chat_had_work=True,
        model_params={"effort": "low"},
    )


def _world():
    from tests.test_brain_tools import _world as _w

    return _w()


@pytest.mark.asyncio
async def test_continuous_reasoning_past_48s_then_say_succeeds(monkeypatch):
    """Think chunks for longer than SILENT_GROK_TIP_S, then [say], must succeed.

    Fake monotonic — do not sleep the real 48s. Pre-fix this aborts empty
    because tip_t0 is never reset by reasoning_content.
    """
    clear_interrupt()
    clock = _Clock()
    monkeypatch.setattr(brain.time, "monotonic", clock.monotonic)
    # Real 48s budget. Chunk wait is unused because yields are instant.
    monkeypatch.setattr(brain, "STREAM_CHUNK_S", 30.0)
    monkeypatch.setattr(brain, "STREAM_IDLE_LIMIT", 6)

    class Chat:
        async def stream(self):
            acc = ""
            for i in range(6):
                clock.advance(10.0)
                acc += f"weighing tape {i} "
                yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                    content="", reasoning_content=acc
                )
            yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                content="No ticket. Watching SPY.",
                reasoning_content=acc,
            )

    text, _resp, reason = await brain.stream_round(Chat(), emit_stage=False)
    assert reason == "ok"
    assert "No ticket" in (text or "")
    assert clock.t - 1_000.0 >= brain.SILENT_GROK_TIP_S


@pytest.mark.asyncio
async def test_think_only_is_one_paid_call_not_three(monkeypatch):
    """Think-only on a live chat must complete once. No identical re-bill."""
    clear_interrupt()
    monkeypatch.setattr(brain, "EMPTY_GROK_DEAD_S", 0.0)
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0")

    class Chat:
        n = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.n += 1
            yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                content="",
                reasoning_content="IV on the 765C looks rich.",
            )

    chat = Chat()
    turn = await brain.grok_turn(
        _g(chat),
        connector=None,
        world=_world(),
        snap={},
        wake="hi",
        recover=True,
    )
    assert chat.n == 1
    assert turn.trailing_empty_grok is False
    assert getattr(turn, "trailing_think_only", False) is True


@pytest.mark.asyncio
async def test_dead_stream_still_aborts_within_bound(monkeypatch):
    """No chunks at all must still abort. Hang detection survives."""
    import asyncio

    clear_interrupt()
    monkeypatch.setattr(brain, "STREAM_CHUNK_S", 0.02)
    monkeypatch.setattr(brain, "STREAM_IDLE_LIMIT", 2)
    monkeypatch.setenv("ABCXAUTO_SILENT_GROK_TIP_S", "0.08")
    monkeypatch.setenv("ABCXAUTO_THINK_ONLY_CEILING_S", "30")

    class Chat:
        async def stream(self):
            while True:
                await asyncio.sleep(10)
                yield None, SimpleNamespace(content="", reasoning_content="")

    t0 = asyncio.get_event_loop().time()
    text, resp, reason = await brain.stream_round(Chat(), emit_stage=False)
    elapsed = asyncio.get_event_loop().time() - t0
    assert resp is None
    assert not (text or "").strip()
    assert reason in ("empty", "stalled")
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_stream_round_tool_calls_unchanged(monkeypatch):
    """A stream that ends with tool_calls still hands them back."""
    clear_interrupt()

    class TC:
        id = "1"
        function = SimpleNamespace(name="book", arguments="{}")

    class Chat:
        async def stream(self):
            yield SimpleNamespace(tool_calls=[TC()]), SimpleNamespace(
                content="", reasoning_content=""
            )

    text, resp, reason = await brain.stream_round(Chat(), emit_stage=False)
    calls = list(getattr(resp, "tool_calls", None) or [])
    assert len(calls) == 1
    assert getattr(calls[0].function, "name", "") == "book"
    assert reason != "think_only"


@pytest.mark.asyncio
async def test_think_only_stay_up_does_not_freeze(monkeypatch, tmp_path):
    """After a reasoning-only round the stay-up loop waits for a poke.

    Must not arm same-chat recover (that re-bills) and must not sit
    forever on a dead tip — worker stays up, one look, then poke-wait.
    """
    import asyncio
    import time

    from abcxauto.pro_engine import ProEngine
    from tests.test_pro_engine import _Cfg, _wire_stay_up_engine

    monkeypatch.setattr("abcxauto.pro_engine.get_config", lambda: _Cfg())
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0.01")
    looks: list[int] = []

    async def think(self, n, g, s, *, resume=False):
        looks.append(n)
        g.chat = g.chat or object()
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "_think_only": True,
            "_empty_grok": False,
            "_skip_identical_retry": True,
            "rationale": "",
            "tool_trace": ["option_quote"],
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(looks) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.5
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    looking = bool(eng.worker and eng.worker.is_alive())
    resume_think = bool(getattr(eng, "_resume_think", False))
    recover = bool(getattr(eng, "_recover_same_chat", False))
    cold = bool(getattr(eng, "_cold_next", False))
    eng.stop_engine()
    eng.drain_apply()
    assert looks == [1]
    assert looking
    assert recover is False
    # Wait for poke (keep chat) or keep looking. Must not freeze a dead tip
    # via same-chat recover, and must not have died.
    assert resume_think is False or cold is True


@pytest.mark.asyncio
async def test_per_call_log_has_token_split_and_stop_not_api_key(caplog, monkeypatch):
    """One diagnosis line per call: tokens + finish_reason + stop. No secrets."""
    clear_interrupt()

    class Chat:
        async def stream(self):
            usage = SimpleNamespace(
                prompt_tokens=28925,
                completion_tokens=100,
                reasoning_tokens=2498,
            )
            yield SimpleNamespace(
                tool_calls=[],
                usage=usage,
                choices=[SimpleNamespace(finish_reason="stop")],
            ), SimpleNamespace(
                content="holding the vert.",
                reasoning_content="weigh tape",
                usage=usage,
                choices=[SimpleNamespace(finish_reason="stop")],
            )

    with caplog.at_level(logging.INFO, logger="abcxauto.brain"):
        _text, _resp, reason = await brain.stream_round(Chat(), emit_stage=False)
    assert reason == "ok"
    blob = caplog.text
    assert "28925" in blob
    assert "stop" in blob.lower()
    assert "reasoning" in blob.lower()
    assert reason in blob
    assert "api_key" not in blob.lower()
    assert "xai_api_key" not in blob.lower()
    assert "SECRET" not in blob
    assert "sk-" not in blob


def test_chat_create_logs_kwargs_once_without_secrets(caplog):
    from abcxauto.llm import create_chat

    seen: list[dict] = []

    class _ChatNS:
        @staticmethod
        def create(**kwargs):
            seen.append(kwargs)
            return SimpleNamespace(name="chat")

    client = SimpleNamespace(chat=_ChatNS())
    with caplog.at_level(logging.INFO, logger="abcxauto.llm"):
        create_chat(
            client,
            model="grok-4.6",
            temperature=0.3,
            max_tokens=8192,
            include=["verbose_streaming"],
            reasoning_effort="low",
            api_key="SECRET_KEY_XYZ",
            xai_api_key="sk-secret-should-never-log",
        )
    blob = caplog.text
    assert "grok-4.6" in blob
    assert "8192" in blob
    assert "verbose_streaming" in blob or "include" in blob
    assert "reasoning_effort" in blob
    assert "low" in blob
    assert "SECRET_KEY_XYZ" not in blob
    assert "sk-secret-should-never-log" not in blob
    assert "api_key" not in blob.lower()


@pytest.mark.asyncio
async def test_think_only_ceiling_ends_unbounded_think(monkeypatch):
    """A think-forever stream still ends at the overall ceiling."""
    clear_interrupt()
    clock = _Clock()
    monkeypatch.setattr(brain.time, "monotonic", clock.monotonic)
    monkeypatch.setenv("ABCXAUTO_THINK_ONLY_CEILING_S", "50")
    monkeypatch.setattr(brain, "STREAM_CHUNK_S", 30.0)
    monkeypatch.setattr(brain, "STREAM_IDLE_LIMIT", 99)

    class Chat:
        async def stream(self):
            acc = ""
            for i in range(20):
                clock.advance(10.0)
                acc += f"still thinking {i} "
                yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                    content="", reasoning_content=acc
                )
            raise AssertionError("stream must end at the think-only ceiling")

    text, _resp, reason = await brain.stream_round(Chat(), emit_stage=False)
    assert reason == "think_only"
    assert not (text or "").strip()
    assert clock.t - 1_000.0 >= 50.0
