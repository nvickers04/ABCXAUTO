"""Zero-token empty sample retries once on the same chat."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.world_state import WorldState


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
        risk_posture="normal",
        effective_posture="normal",
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


@pytest.mark.asyncio
async def test_zero_token_empty_retries_once_then_succeeds(monkeypatch):
    """Cold look: input=0 output=0 stop=empty retries once on the same chat."""
    from abcxauto import brain
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    monkeypatch.setattr(brain, "EMPTY_GROK_DEAD_S", 0.0)
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0")

    append_calls: list = []

    def fake_append_call(**kwargs):
        append_calls.append(kwargs)

    monkeypatch.setattr(
        "abcxauto.look_ledger.append_call", fake_append_call, raising=False
    )

    session_saves: list = []

    def fake_save(card, path=None):
        session_saves.append(card)
        return card or {}

    monkeypatch.setattr(
        "abcxauto.session_work.save", fake_save, raising=False
    )

    class Chat:
        n = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.n += 1
            if self.n == 1:
                # Void: no chunks → empty + response None + zero tokens.
                if False:
                    yield None, None
            else:
                yield SimpleNamespace(tool_calls=[]), SimpleNamespace(
                    content="watching the book",
                    reasoning_content="",
                )

    chat = Chat()
    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: chat)),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=chat,
        _wake_n=1,
        _last_desk_fact="",
    )
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    assert chat.n == 2
    assert "watching the book" in (turn.text or "")
    assert turn.failed is False
    assert g.chat is chat
    # Zero-token attempt must not append_call.
    assert all(
        int(row.get("input_tokens") or 0) > 0 or int(row.get("output_tokens") or 0) > 0
        for row in append_calls
    )
    # Empty first attempt must not write session work.
    assert session_saves == []
    assert str(turn.last_strat or "").lower() != "hold"


@pytest.mark.asyncio
async def test_zero_token_empty_twice_no_hold_sentence(monkeypatch):
    """Second cold empty ends the look with no invented hold text."""
    from abcxauto import brain
    from abcxauto.brain import grok_turn
    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    monkeypatch.setattr(brain, "EMPTY_GROK_DEAD_S", 0.0)
    monkeypatch.setenv("ABCXAUTO_EMPTY_GROK_DEAD_S", "0")

    class Chat:
        n = 0

        def append(self, *_a, **_k):
            pass

        async def stream(self):
            self.n += 1
            if False:
                yield None, None

    chat = Chat()
    g = SimpleNamespace(
        client=SimpleNamespace(chat=SimpleNamespace(create=lambda **_k: chat)),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
        chat=chat,
        _wake_n=1,
        _last_desk_fact="",
    )
    turn = await grok_turn(g, connector=None, world=_world(), snap={}, wake="hi")
    assert chat.n == 2
    assert not (turn.text or "").strip()
    assert str(turn.last_strat or "").lower() != "hold"
    assert str((turn.last_act or {}).get("strategy") or "").lower() != "hold"
    assert g.chat is chat


def test_flush_trim_fill_notes_onto_same_chat():
    """Trim fill qty+symbol lands on the live chat; no new chat started."""
    from abcxauto import brain

    class Chat:
        def __init__(self) -> None:
            self.lines: list = []

        def append(self, msg, **_k):
            self.lines.append(msg)

    chat = Chat()
    g = SimpleNamespace(chat=chat)
    snap = {
        brain._TRIM_FILL_NOTES_KEY: [{"symbol": "AVGO", "qty": 67}],
    }
    brain._flush_trim_fill_notes(g, snap)
    assert snap.get(brain._TRIM_FILL_NOTES_KEY) in (None, [])
    blob = " ".join(str(x) for x in chat.lines)
    for item in chat.lines:
        for attr in ("content", "text", "message"):
            raw = getattr(item, attr, None)
            if raw is not None:
                blob += " " + str(raw)
    assert "AVGO" in blob
    assert "67" in blob
    assert g.chat is chat


def test_quote_only_hold_may_sit():
    """A spoken hold may sit — words with no tool_calls end the look."""
    from abcxauto.brain import look_may_sit

    hold = "Holding XOM. No ticket. The cash stays."
    assert look_may_sit(["quote"], text=hold) is True
    assert look_may_sit([], text="No ticket.") is True
    assert look_may_sit(["quote"], text="watching the book") is True
    assert look_may_sit(["quote", "scan"], text=hold) is True
    assert look_may_sit(["news"], text=hold) is True
    assert look_may_sit(["option_chain"], text=hold) is True
    assert look_may_sit(["quote"], sends=1, text=hold) is True
