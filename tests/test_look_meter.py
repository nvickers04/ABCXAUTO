"""Look-level cost meter and result-size clips. Operator-facing only."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from abcxauto import brain
from abcxauto.brain import CLIP_CHARS, _clip
from abcxauto.look_meter import look_meter_for_desk
from abcxauto.park_clock import clear_interrupt
from abcxauto.scorecard import estimate_cost_usd


def _g(chat) -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: chat)),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=8192,
        chat=chat,
        _wake_n=1,
        _chat_had_work=True,
        model_params={"effort": "low"},
    )


def _world():
    from tests.test_brain_tools import _world as _w

    return _w()


def _usage(prompt: int, completion: int = 10, reasoning: int = 4, cached: int = 0):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        cached_prompt_text_tokens=cached,
    )


class _TC:
    def __init__(self, name: str, tid: str) -> None:
        self.id = tid
        self.function = SimpleNamespace(name=name, arguments="{}")


@pytest.mark.asyncio
async def test_look_writes_one_meter_row(monkeypatch):
    clear_interrupt()
    monkeypatch.setattr(brain, "EMPTY_GROK_DEAD_S", 0.0)
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0")

    async def fake_run(name, args, **_k):
        if name == "scan":
            return json.dumps({"ok": True, "hits": [{"symbol": "SPY"}]})
        return json.dumps({"ok": True, "open_lots": []})

    monkeypatch.setattr(brain, "_run_tool", fake_run)

    class Chat:
        n = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.n += 1
            if self.n == 1:
                u = _usage(10_000)
                yield SimpleNamespace(tool_calls=[_TC("book", "1")], usage=u), SimpleNamespace(
                    content="", reasoning_content="book first", usage=u
                )
            elif self.n == 2:
                u = _usage(18_000)
                yield SimpleNamespace(tool_calls=[_TC("scan", "2")], usage=u), SimpleNamespace(
                    content="", reasoning_content="scan next", usage=u
                )
            else:
                u = _usage(25_000, completion=20, reasoning=8)
                yield SimpleNamespace(
                    tool_calls=[],
                    usage=u,
                    choices=[SimpleNamespace(finish_reason="stop")],
                ), SimpleNamespace(
                    content="No ticket.",
                    reasoning_content="done",
                    usage=u,
                )

    turn = await brain.grok_turn(
        _g(Chat()),
        connector=None,
        world=_world(),
        snap={"cycle": 7},
        wake="hi",
        recover=True,
    )
    assert "No ticket" in (turn.text or "")
    rows = look_meter_for_desk(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["look_id"]
    assert row["cycle"] == 7
    assert row["session"] == "regular"
    assert row["model"] == "grok-4.6"
    assert row["calls"] == 3
    assert row["input_tokens"] == 10_000 + 18_000 + 25_000
    assert row["cached_tokens"] == 0
    assert row["output_tokens"] > 0
    assert row["reasoning_tokens"] > 0
    assert row["cost_usd"] > 0
    assert row["duration_s"] >= 0
    assert row["tool_counts"].get("book") == 1
    assert row["tool_counts"].get("scan") == 1
    assert row["tool_result_chars"] > 0
    assert row["call_inputs"] == [10_000, 18_000, 25_000]


@pytest.mark.asyncio
async def test_rebill_curve_records_each_call(monkeypatch):
    clear_interrupt()
    monkeypatch.setattr(brain, "EMPTY_GROK_DEAD_S", 0.0)
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0")
    monkeypatch.setattr(
        brain,
        "_run_tool",
        lambda *a, **k: _async_json({"ok": True}),
    )

    class Chat:
        n = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.n += 1
            prompts = {1: 4_000, 2: 9_000, 3: 15_000}
            if self.n < 3:
                u = _usage(prompts[self.n])
                yield SimpleNamespace(
                    tool_calls=[_TC("quote", str(self.n))], usage=u
                ), SimpleNamespace(content="", reasoning_content="q", usage=u)
            else:
                u = _usage(prompts[3], completion=12)
                yield SimpleNamespace(tool_calls=[], usage=u), SimpleNamespace(
                    content="flat.", reasoning_content="q", usage=u
                )

    await brain.grok_turn(
        _g(Chat()),
        connector=None,
        world=_world(),
        snap={},
        wake="hi",
        recover=True,
    )
    row = look_meter_for_desk(limit=1)[0]
    assert row["calls"] == 3
    assert row["call_inputs"] == [4_000, 9_000, 15_000]
    assert row["call_inputs"][2] > row["call_inputs"][1] > row["call_inputs"][0]


async def _async_json(payload):
    return json.dumps(payload)


@pytest.mark.asyncio
async def test_journal_usage_write_raise_does_not_break_look(monkeypatch):
    clear_interrupt()
    monkeypatch.setattr(brain, "EMPTY_GROK_DEAD_S", 0.0)
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0")

    class Boom:
        enabled = True

        def record_model_usage(self, **_k):
            raise RuntimeError("journal down")

        def model_usage_totals(self):
            raise RuntimeError("journal down")

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: Boom())

    class Chat:
        def append(self, *_a, **_k):
            pass

        async def stream(self):
            u = _usage(1_200, completion=8)
            yield SimpleNamespace(tool_calls=[], usage=u), SimpleNamespace(
                content="Watching.", reasoning_content="ok", usage=u
            )

    turn = await brain.grok_turn(
        _g(Chat()),
        connector=None,
        world=_world(),
        snap={},
        wake="hi",
        recover=True,
    )
    assert "Watching" in (turn.text or "")
    assert turn.failed is False


@pytest.mark.asyncio
async def test_look_meter_write_raise_does_not_break_look(monkeypatch):
    clear_interrupt()
    monkeypatch.setattr(brain, "EMPTY_GROK_DEAD_S", 0.0)
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0")

    def boom(*_a, **_k):
        raise RuntimeError("meter down")

    monkeypatch.setattr("abcxauto.look_meter._write_row", boom)

    class Chat:
        def append(self, *_a, **_k):
            pass

        async def stream(self):
            u = _usage(800, completion=6)
            yield SimpleNamespace(tool_calls=[], usage=u), SimpleNamespace(
                content="Hold.", reasoning_content="ok", usage=u
            )

    turn = await brain.grok_turn(
        _g(Chat()),
        connector=None,
        world=_world(),
        snap={},
        wake="hi",
        recover=True,
    )
    assert "Hold" in (turn.text or "")
    assert turn.failed is False


def test_fat_scan_drops_tail_rows_with_count():
    hits = [
        {"symbol": f"X{i}", "last": 10.0 + i, "pad": "n" * 400}
        for i in range(40)
    ]
    raw = _clip({"ok": True, "hits": hits})
    data = json.loads(raw)
    assert data.get("_clipped") == "hits"
    assert int(data.get("_dropped") or 0) >= 1
    kept = data.get("hits")
    assert isinstance(kept, list)
    assert 1 <= len(kept) < 40
    assert kept[0]["symbol"] == "X0"
    assert kept[-1]["symbol"] != hits[-1]["symbol"]
    assert json.dumps(data, default=str)
    assert len(raw) <= CLIP_CHARS or data.get("_clipped")


def test_estimate_cost_usd_unchanged_from_look_meter_import():
    assert abs(estimate_cost_usd(1_000, 1_000) - 0.008) < 1e-9
    assert abs(estimate_cost_usd(1_000_000, 1_000_000) - 16.0) < 1e-9
