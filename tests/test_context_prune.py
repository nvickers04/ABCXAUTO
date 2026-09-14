"""Superseded tool results are stubbed in place; the wire stays well-formed."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from xai_sdk import AsyncClient
from xai_sdk.chat import system, tool_result
from xai_sdk.proto import chat_pb2

from abcxauto import brain
from abcxauto.brain import BrainTurn, _dispatch_tool_calls
from abcxauto.context_prune import payload_chars, prune_superseded, supersede_key
from abcxauto.scorecard import estimate_tokens
from tests.test_interrupt_defers_reads import _Call, _world


def _chat():
    """A real SDK chat so the pruned list is the request proto that rides the wire.

    grpc.aio wants a running loop to build the client; earlier tests in the
    suite leave the main thread without one, so make it inside ``asyncio.run``.
    The chat itself is plain proto work afterwards.
    """

    async def make():
        client = AsyncClient(api_key="test-key")
        return client.chat.create(model="grok-4.6", messages=[system("desk")])

    return asyncio.run(make())


def _ask(chat, cid: str, name: str, args: dict, result: str) -> None:
    """One assistant tool_call plus its tool_result, the way a look lays them."""
    tc = chat_pb2.ToolCall(
        id=cid,
        function=chat_pb2.FunctionCall(name=name, arguments=json.dumps(args)),
    )
    chat.append(chat_pb2.Message(role=chat_pb2.MessageRole.ROLE_ASSISTANT, tool_calls=[tc]))
    chat.append(tool_result(result, tool_call_id=cid))
    prune_superseded(chat, name=name, args=args, tool_call_id=cid)


def _fat(tag: str, n: int = 3000) -> str:
    return json.dumps({"tag": tag, "rows": [tag] * (n // (len(tag) + 4))})


def _assert_pairing_valid(chat) -> None:
    """Every tool_call id has exactly one ROLE_TOOL answer; no orphans either way."""
    call_ids: list[str] = []
    result_ids: list[str] = []
    for msg in chat.messages:
        for tc in msg.tool_calls:
            call_ids.append(tc.id)
        if msg.role == chat_pb2.MessageRole.ROLE_TOOL:
            result_ids.append(msg.tool_call_id)
            assert len(msg.content) == 1
            assert msg.content[0].text
    assert sorted(call_ids) == sorted(result_ids)
    assert len(set(result_ids)) == len(result_ids)
    # The request proto is what rides the wire — it must build.
    chat.proto.SerializeToString()


def _result_text(chat, cid: str) -> str:
    for msg in chat.messages:
        if msg.role == chat_pb2.MessageRole.ROLE_TOOL and msg.tool_call_id == cid:
            return msg.content[0].text
    raise AssertionError(cid)


def test_supersede_key_is_book_wide_for_state_reads_and_args_wide_otherwise():
    assert supersede_key("book", {}) == supersede_key("book", {"x": 1}) == "book"
    assert supersede_key("status", None) == "status"
    assert supersede_key("quote", {"symbol": "SPY"}) == supersede_key("quote", {"symbol": "SPY"})
    assert supersede_key("quote", {"symbol": "SPY"}) != supersede_key("quote", {"symbol": "QQQ"})
    assert supersede_key("send", {"strategy": "x"}) is None
    assert supersede_key("self_tune", {}) is None
    assert supersede_key("", {}) is None


def test_repeated_reads_shrink_the_payload_and_keep_the_pairing_valid(capsys):
    chat = _chat()
    _ask(chat, "c1", "book", {}, _fat("book1"))
    _ask(chat, "c2", "status", {}, _fat("status1"))
    _ask(chat, "c3", "quote", {"symbol": "SPY"}, _fat("spy1"))
    _ask(chat, "c4", "fills", {}, _fat("fills1"))
    before = payload_chars(chat)
    # Second sweep of the same reads, plus a different-args quote that must stay.
    _ask(chat, "c5", "quote", {"symbol": "QQQ"}, _fat("qqq1"))
    unpruned_would_be = before + sum(len(_fat(t)) for t in ("book2", "status2", "spy2", "fills2")) + len(
        _fat("qqq1")
    )
    _ask(chat, "c6", "book", {}, _fat("book2"))
    _ask(chat, "c7", "status", {}, _fat("status2"))
    _ask(chat, "c8", "quote", {"symbol": "SPY"}, _fat("spy2"))
    _ask(chat, "c9", "fills", {}, _fat("fills2"))
    after = payload_chars(chat)

    _assert_pairing_valid(chat)
    # Tool-call argument bytes make the two totals differ by a few chars only.
    assert after < unpruned_would_be * 0.6
    for stale in ("c1", "c2", "c3", "c4"):
        stub = json.loads(_result_text(chat, stale))
        assert stub["superseded"] is True
        assert "later" in stub["note"]
    assert json.loads(_result_text(chat, "c5"))["tag"] == "qqq1"
    assert json.loads(_result_text(chat, "c6"))["tag"] == "book2"
    assert json.loads(_result_text(chat, "c8"))["tag"] == "spy2"
    print(
        f"\nprune estimate: unpruned ~{estimate_tokens('x' * unpruned_would_be)} tokens"
        f" -> pruned ~{estimate_tokens('x' * after)} tokens"
    )
    shown = capsys.readouterr().out
    assert "prune estimate" in shown


def test_third_call_stubs_the_second_not_the_stub_again():
    chat = _chat()
    _ask(chat, "c1", "book", {}, _fat("b1"))
    _ask(chat, "c2", "book", {}, _fat("b2"))
    n, saved = (0, 0)
    tc = chat_pb2.ToolCall(id="c3", function=chat_pb2.FunctionCall(name="book", arguments="{}"))
    chat.append(chat_pb2.Message(role=chat_pb2.MessageRole.ROLE_ASSISTANT, tool_calls=[tc]))
    chat.append(tool_result(_fat("b3"), tool_call_id="c3"))
    n, saved = prune_superseded(chat, name="book", args={}, tool_call_id="c3")
    assert n == 1
    assert saved > 2000
    assert json.loads(_result_text(chat, "c1"))["superseded"] is True
    assert json.loads(_result_text(chat, "c2"))["superseded"] is True
    assert json.loads(_result_text(chat, "c3"))["tag"] == "b3"
    _assert_pairing_valid(chat)


def test_send_and_self_tune_results_are_never_stubbed():
    chat = _chat()
    _ask(chat, "s1", "send", {"strategy": "vertical_spread"}, _fat("sent1"))
    _ask(chat, "s2", "send", {"strategy": "vertical_spread"}, _fat("sent2"))
    _ask(chat, "t1", "self_tune", {"size_pct_nl": 3}, _fat("tune1"))
    _ask(chat, "t2", "self_tune", {"size_pct_nl": 3}, _fat("tune2"))
    assert json.loads(_result_text(chat, "s1"))["tag"] == "sent1"
    assert json.loads(_result_text(chat, "t1"))["tag"] == "tune1"
    _assert_pairing_valid(chat)


def test_a_later_error_does_not_erase_the_earlier_fact():
    chat = _chat()
    _ask(chat, "c1", "quote", {"symbol": "SPY"}, _fat("spy1"))
    tc = chat_pb2.ToolCall(
        id="c2", function=chat_pb2.FunctionCall(name="quote", arguments='{"symbol": "SPY"}')
    )
    chat.append(chat_pb2.Message(role=chat_pb2.MessageRole.ROLE_ASSISTANT, tool_calls=[tc]))
    chat.append(tool_result(json.dumps({"error": "quote failed"}), tool_call_id="c2"))
    n, _ = prune_superseded(
        chat, name="quote", args={"symbol": "SPY"}, tool_call_id="c2", is_fact=False
    )
    assert n == 0
    assert json.loads(_result_text(chat, "c1"))["tag"] == "spy1"
    # The good read that follows does supersede the first one.
    _ask(chat, "c3", "quote", {"symbol": "SPY"}, _fat("spy3"))
    assert json.loads(_result_text(chat, "c1"))["superseded"] is True
    _assert_pairing_valid(chat)


def test_knob_off_leaves_every_copy_in_place(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PRUNE_TOOL_RESULTS", "0")
    chat = _chat()
    _ask(chat, "c1", "book", {}, _fat("b1"))
    _ask(chat, "c2", "book", {}, _fat("b2"))
    assert json.loads(_result_text(chat, "c1"))["tag"] == "b1"


def test_test_double_chat_without_messages_is_a_noop():
    class _Chat:
        def append(self, *_a, **_k):
            pass

    assert prune_superseded(_Chat(), name="book", args={}, tool_call_id="c1") == (0, 0)


def test_dispatch_prunes_and_logs_the_estimate(monkeypatch, caplog):
    chat = _chat()
    turn = BrainTurn()
    fat = {"book": _fat("book"), "quote": _fat("spy")}

    async def fake_invoke(name, args, timeout, **_k):
        return fat[name]

    monkeypatch.setattr(brain, "_invoke_named_tool", fake_invoke)

    async def run():
        for i in range(3):
            calls = [_Call(f"b{i}", "book", {}), _Call(f"q{i}", "quote", {"symbol": "SPY"})]
            for c in calls:
                chat.append(
                    chat_pb2.Message(
                        role=chat_pb2.MessageRole.ROLE_ASSISTANT,
                        tool_calls=[
                            chat_pb2.ToolCall(
                                id=c.id,
                                function=chat_pb2.FunctionCall(
                                    name=c.function.name, arguments=c.function.arguments
                                ),
                            )
                        ],
                    )
                )
            await _dispatch_tool_calls(
                calls, chat=chat, connector=None, world=_world(), snap={}, turn=turn
            )
            # Same-think repeat cache answers the 2nd/3rd asks; clear so the
            # tool "runs" again and lands a fresh full result each sweep.
            turn.tool_cache.clear()

    asyncio.run(run())
    assert turn.pruned_results == 4
    assert turn.pruned_chars > 4 * 2000
    _assert_pairing_valid(chat)
    with caplog.at_level(logging.INFO, logger="abcxauto.brain"):
        brain._log_context_prune(chat, turn)
    line = [r for r in caplog.records if "context prune" in r.getMessage()]
    assert line
    msg = line[0].getMessage()
    assert "4 superseded" in msg
    assert "unpruned" in msg
