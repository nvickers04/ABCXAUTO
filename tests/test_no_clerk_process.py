"""Grok is the only RTH process. There is no clerk runner / assigner / speaker."""

from __future__ import annotations

from types import SimpleNamespace

from abcxauto.brain import BrainTurn, _finish_look_chat, _look_text_is_junk
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.think_stream import emit, reset_speaker


SYSTEM_PROMPT_LOCK = (
    "You own an Interactive Brokers {mode} book. Strategy is yours.\n"
    "Live only follows a promoted playbook. Risk is code.\n"
    "send tickets that match ORDER EXAMPLES.\n"
    "Size vs max_risk_per_trade_pct of NetLiq.\n"
)


def _stub_chat():
    class Chat:
        def append(self, *_a, **_k):
            pass

    class _ChatNS:
        @staticmethod
        def create(**_k):
            return Chat()

    return SimpleNamespace(
        client=SimpleNamespace(chat=_ChatNS()),
        model="grok-4.6",
        temperature=0.3,
        max_tokens=256,
    )


def test_system_prompt_is_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_no_clerk_stream_speaker():
    from abcxauto.think_stream import bind_engine

    reset_speaker()
    st = SimpleNamespace(think_live="")
    bind_engine(SimpleNamespace(state=st))
    try:
        emit("tool", "[scan]\n")
        emit("tool", "hits=3 screens=2 deepest=-6.5% SNDK src=ibkr\n")
        emit("stage", "grok")
        emit("say", "\n[think]\n")
        emit("think", "weigh tape ")
        emit("say", "\n[say]\n")
        emit("say", "watching the gap\n")
        emit("stage", "CLERK")
    finally:
        bind_engine(None)
        reset_speaker()
    live = st.think_live
    assert "--- CLERK ---" not in live
    assert "[clerk]" not in live
    assert "--- GROK ---" in live
    assert "[scan]" in live
    assert "hits=3 screens=2" in live
    assert "watching the gap" in live


def test_real_say_keeps_chat_empty_question_idles():
    from abcxauto.brain import _ensure_chat

    g = _stub_chat()
    chat = _ensure_chat(g, kind="boot")
    _finish_look_chat(g, BrainTurn(text="watching the book"), session="regular")
    assert getattr(g, "chat", None) is chat

    _finish_look_chat(
        g,
        BrainTurn(failed=True, text="watching IWM"),
        session="regular",
    )
    assert getattr(g, "chat", None) is chat

    _finish_look_chat(
        g,
        BrainTurn(text="I'll inspect the book first.\n?"),
        session="regular",
    )
    assert getattr(g, "chat", None) is chat

    _finish_look_chat(g, BrainTurn(text="\u2603"), session="regular")
    assert getattr(g, "chat", None) is chat

    _finish_look_chat(g, BrainTurn(text=""), session="regular")
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(g, BrainTurn(text="?"), session="regular")
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(g, BrainTurn(stream_error="RESOURCE_EXHAUSTED"), session="regular")
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(
        g,
        BrainTurn(stream_error="RESOURCE_EXHAUSTED", text="watching IWM"),
        session="regular",
    )
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(
        g,
        BrainTurn(
            text="",
            sends=[{"result": {"status": "submitted", "success": True}}],
        ),
        session="regular",
    )
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(g, BrainTurn(parked=True, text="gate off"), session="regular")
    assert getattr(g, "chat", None) is None


def test_spoken_look_is_not_a_failed_persist():
    from abcxauto.think_stream import last_turn_look_failed

    assert last_turn_look_failed(
        {"rationale": "Playbook is explore/testing. Scanning gap-down tape.", "_failed": True}
    ) is False
    assert last_turn_look_failed({"rationale": "?", "_failed": True}) is True
    assert last_turn_look_failed({"rationale": "", "_failed": True}) is True
    assert last_turn_look_failed(
        {"rationale": "watching", "_failed": True, "_stream_error": "stalled"}
    ) is False
    assert last_turn_look_failed(
        {"rationale": "", "sends": 1, "_failed": True}
    ) is False
    assert last_turn_look_failed(
        {"rationale": "", "_failed": True, "_stream_error": "stalled"}
    ) is True


def test_look_text_junk_is_only_empty_or_lone_question():
    assert _look_text_is_junk("") is True
    assert _look_text_is_junk("?") is True
    assert _look_text_is_junk("   ") is True
    assert _look_text_is_junk("watching IWM") is False
    assert _look_text_is_junk("I'll inspect the book first.\n?") is False
    assert _look_text_is_junk("\u2603") is False
    assert BrainTurn(text="watching IWM").look_failed() is False
    assert BrainTurn(failed=True, text="watching IWM").look_failed() is False
    assert BrainTurn(
        text="",
        sends=[{"result": {"status": "filled", "filled": True}}],
    ).look_failed() is False
    assert BrainTurn(text="?").look_failed() is True
    assert BrainTurn(text="").look_failed() is True


def test_rth_failed_look_has_no_sit():
    from abcxauto.park_clock import clerk_look_s
    from abcxauto.pro_engine import ProEngine

    eng = ProEngine()
    wait = eng._rearm_after_think(
        {"_failed": True, "_stream_error": "stream stalled", "rationale": "?"},
        session="regular",
    )
    assert wait == 0.0
    assert eng._resume_think is False
    assert eng._cold_next is False
    wait = eng._rearm_after_think({"_failed": True, "rationale": "?"}, session="regular")
    assert wait == 0.0
    wait = eng._rearm_after_think(
        {
            "_failed": True,
            "rationale": "Standing down. Watching IWM. No ticket.",
        },
        session="regular",
    )
    assert wait == 0.0
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert clerk_look_s(flat=True, session="regular") == 0.0
    assert clerk_look_s(flat=False, session="premarket", minutes_to_open=45) == 0.0


def test_no_clerk_cycle_or_run_cycle():
    import importlib

    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("abcxauto.cycle")
    import abcxauto.agent_loop as agent_loop

    assert not hasattr(agent_loop, "run_cycle")


def test_health_strip_does_not_invent_a_next_look_sit(monkeypatch):
    from abcxauto.pro_desktop import ProTerminal

    class _Cfg:
        xai_api_key = "test-key"
        model = "grok-4.6"
        trading_mode = "paper"
        ibkr_port = 7497

        @property
        def is_paper(self) -> bool:
            return True

    class _Page:
        title = ""
        bgcolor = ""
        padding = 0
        theme_mode = None

        def __init__(self):
            self.window = type(
                "W",
                (),
                {"width": 1280, "height": 860, "min_width": 960, "min_height": 720},
            )()
            self.snack_bar = None
            self.overlay = []
            self.controls = []

        def add(self, *_):
            pass

        def update(self):
            pass

        def run_task(self, _):
            pass

    monkeypatch.setattr("abcxauto.pro_desktop.get_config", lambda: _Cfg())
    pro = ProTerminal(_Page())
    s = pro.engine.state
    s.running = True
    s.autonomous = True
    s.status = "On"
    s.backoff_wait_s = 0.0
    pro.engine._fail_streak = 1
    pro._sync_health_strip()
    shown = pro.lbl_hs_next.value or ""
    assert "next look" not in shown
    assert "21s" not in shown


def test_card_still_required_on_new_risk():
    from abcxauto.risk_gates import new_risk_card_error

    assert new_risk_card_error("")
    assert new_risk_card_error("   ")
    assert not new_risk_card_error("large-cap 3pct gap hold")
    assert not new_risk_card_error("moon shot")
