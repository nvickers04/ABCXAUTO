"""Session look/token caps: stop a flat grind without a sit-wake loop."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from abcxauto.config import get_config, update_agent_config
from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.pro_engine import ProEngine
from abcxauto.session_caps import (
    DEFAULT_LOOK_CAP,
    DEFAULT_TOKEN_CAP,
    is_capped,
    note_look,
    reset_session_caps,
    session_key,
    usage,
)
from abcxauto.self_tune import apply_self_tune
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK
from tests.test_pro_engine import _stay_up_snap, _wire_stay_up_engine


def _et(y, m, d, h=10, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/New_York"))


def test_defaults_are_the_rth_ceiling():
    cfg = get_config()
    assert cfg.session_look_cap == DEFAULT_LOOK_CAP == 160
    assert cfg.session_token_cap == DEFAULT_TOKEN_CAP == 2_500_000


def test_premarket_and_regular_are_separate_budgets():
    now = _et(2026, 8, 28, 8, 0)
    assert session_key("premarket", now=now) == "2026-08-28:premarket"
    assert session_key("regular", now=now.replace(hour=10)) == "2026-08-28:regular"
    note_look("premarket", tokens=10, now=now)
    assert usage("premarket", now=now)["looks"] == 1
    assert usage("regular", now=now.replace(hour=10))["looks"] == 0


def test_look_cap_stops_further_counts_from_gating():
    update_agent_config(session_look_cap=2, persist=False)
    assert is_capped("regular") is False
    note_look("regular")
    assert is_capped("regular") is False
    note_look("regular")
    assert is_capped("regular") is True
    snap = usage("regular")
    assert snap["looks"] == 2
    assert snap["why"] == "looks"


def test_token_cap_hits_without_many_looks():
    update_agent_config(session_look_cap=160, session_token_cap=50_000, persist=False)
    note_look("regular", tokens=50_000)
    assert is_capped("regular") is True
    assert usage("regular")["why"] == "tokens"


def test_usage_persists_across_reset_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_SESSION_CAPS_PATH", str(tmp_path / "caps.json"))
    reset_session_caps()
    update_agent_config(session_look_cap=8, persist=False)
    note_look("regular", tokens=100)
    reset_session_caps()
    snap = usage("regular")
    assert snap["looks"] == 1
    assert snap["tokens"] == 100


def test_grok_may_tighten_not_weaken_session_caps():
    update_agent_config(session_look_cap=80, session_token_cap=400_000, persist=True)
    out = apply_self_tune({"session_look_cap": 160, "session_token_cap": 2_500_000})
    rejected = out.get("rejected") or {}
    assert "session_look_cap" in rejected
    assert "session_token_cap" in rejected
    assert get_config().session_look_cap == 80
    assert get_config().session_token_cap == 400_000
    out = apply_self_tune({"session_look_cap": 40, "session_token_cap": 100_000})
    assert out["status"] == "ok"
    assert out["applied"]["session_look_cap"] == 40
    assert out["applied"]["session_token_cap"] == 100_000
    assert get_config().session_look_cap == 40
    assert get_config().session_token_cap == 100_000


def test_caps_are_not_in_system_prompt_or_wake():
    from abcxauto.world_state import format_wake

    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert "session_look_cap" not in SYSTEM_PROMPT
    assert "session_token_cap" not in SYSTEM_PROMPT
    assert "look tally" not in SYSTEM_PROMPT
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"names": 0, "lots": 0, "max_risk_per_trade_pct": 25.0},
    )
    assert "session_look_cap" not in text
    assert "session_token_cap" not in text
    assert "looks=" not in text
    assert "160" not in text
    assert "2500000" not in text


def test_finish_look_chat_keep_from_107_still_holds():
    """Cap idle must not change the #107 spoken / send keep rules."""
    from abcxauto.brain import BrainTurn, _ensure_chat, _finish_look_chat
    from tests.test_no_clerk_process import _stub_chat

    g = _stub_chat()
    chat = _ensure_chat(g, kind="boot")
    update_agent_config(session_look_cap=2, persist=False)
    note_look("regular")
    note_look("regular")
    assert is_capped("regular") is True
    _finish_look_chat(g, BrainTurn(text="watching the book"), session="regular")
    assert getattr(g, "chat", None) is chat
    _finish_look_chat(
        g,
        BrainTurn(text="", sends=[{"result": {"status": "submitted", "success": True}}]),
        session="regular",
    )
    assert getattr(g, "chat", None) is chat


def test_rearm_spoken_look_keeps_chat_flag_when_capped():
    """#107 chat keep: a spoken look at the cap is idle, not a wipe."""
    update_agent_config(session_look_cap=2, persist=False)
    note_look("regular")
    note_look("regular")
    eng = ProEngine()
    wait = eng._rearm_after_think(
        {
            "_failed": False,
            "rationale": "Standing down. Watching IWM. No ticket.",
            "sends": 0,
        },
        session="regular",
    )
    assert wait == 0.0
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert eng._session_capped is True
    assert eng.state.status == "Idle"


def test_rearm_send_look_keeps_chat_flag_when_capped():
    update_agent_config(session_look_cap=2, persist=False)
    note_look("regular")
    note_look("regular")
    eng = ProEngine()
    wait = eng._rearm_after_think(
        {"_failed": True, "rationale": "", "sends": 1},
        session="regular",
    )
    assert wait == 0.0
    assert eng._resume_think is False
    assert eng._cold_next is False
    assert eng._session_capped is True


@pytest.mark.asyncio
async def test_session_look_cap_stops_further_looks_and_writes_no_sit_clock(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    update_agent_config(session_look_cap=2, persist=False)
    dropped = {"n": 0}
    chats: list[object] = []
    stamps: list[float] = []

    def boom_drop(g):
        dropped["n"] += 1

    async def think(self, n, g, s, *, resume=False):
        stamps.append(time.monotonic())
        chats.append(getattr(g, "chat", None))
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "Standing down. Watching IWM. No ticket.",
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    monkeypatch.setattr("abcxauto.brain.drop_live_chat", boom_drop)
    monkeypatch.setattr(
        "abcxauto.park_clock.ensure_next_look",
        lambda **_k: (_ for _ in ()).throw(AssertionError("cap must not sit-wake")),
    )
    monkeypatch.setattr(
        "abcxauto.park_clock.set_wake",
        lambda **_k: (_ for _ in ()).throw(AssertionError("cap must not set_wake")),
    )
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(stamps) < 3:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    # Hold idle long enough that a third look would have happened.
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(stamps) == 2
    assert dropped["n"] == 0
    assert chats and chats[-1] is not None
    assert eng._resume_think is False
    assert eng._session_capped is True
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert not (tmp_path / "wake.json").exists()


@pytest.mark.asyncio
async def test_token_cap_stops_looks_without_wiping_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    update_agent_config(session_look_cap=160, session_token_cap=50_000, persist=False)
    toks = {"n": 0}
    stamps: list[float] = []

    monkeypatch.setattr(
        "abcxauto.session_caps.billed_tokens_now", lambda: toks["n"]
    )

    async def think(self, n, g, s, *, resume=False):
        stamps.append(time.monotonic())
        toks["n"] += 50_000
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "Standing down. Watching IWM. No ticket.",
            "sends": 0,
        }

    _wire_stay_up_engine(monkeypatch, session="regular", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(stamps) < 2:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    idle_until = time.time() + 0.4
    while time.time() < idle_until:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert len(stamps) == 1
    assert eng._session_capped is True
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None


@pytest.mark.asyncio
async def test_premarket_cap_does_not_sit_the_open(monkeypatch, tmp_path):
    """Hit in premarket stays idle; regular is a fresh budget. No sit clock."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    update_agent_config(session_look_cap=1, persist=False)
    state = {"session": "premarket"}
    stamps: list[str] = []

    class _Conn:
        connected = True

        async def connect(self):
            return True

    async def fake_snap(_c):
        return _stay_up_snap(state["session"])

    async def think(self, n, g, s, *, resume=False):
        stamps.append(state["session"])
        return {
            "cycle": n,
            "pnl": 0,
            "equity": 100000,
            "_failed": False,
            "rationale": "watching the open",
            "sends": 0,
        }

    async def _al(*_a, **_k):
        return {"legal_symbols": [], "source": "test"}

    monkeypatch.setattr(
        "abcxauto.config.Config.is_paper",
        property(lambda self: True),
    )
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", _Conn)
    monkeypatch.setattr("abcxauto.pro_engine.snap", fake_snap)
    monkeypatch.setattr(
        "abcxauto.pro_engine.GrokClient",
        lambda: SimpleNamespace(chat=object()),
    )
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._start_monitor",
        lambda self: setattr(self, "monitor", type("M", (), {"running": True})()),
    )
    monkeypatch.setattr("abcxauto.universe.refresh_legal_set", _al)
    monkeypatch.setattr("abcxauto.pro_engine.ProEngine._host_think", think)
    monkeypatch.setattr("abcxauto.park_clock.min_look_s", lambda: 0.01)

    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 4
    while time.time() < deadline and len(stamps) < 1:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    assert stamps == ["premarket"]
    assert eng._session_capped is True
    state["session"] = "regular"
    deadline = time.time() + 4
    while time.time() < deadline and len(stamps) < 2:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert stamps == ["premarket", "regular"]
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is None
    assert not (tmp_path / "wake.json").exists()


@pytest.mark.asyncio
async def test_closed_overnight_still_parks_when_caps_exist(monkeypatch, tmp_path):
    """Overnight park stays park_clock. Caps do not invent a sit-wake in RTH."""
    monkeypatch.setenv("ABCXAUTO_GROK_WAKE_PATH", str(tmp_path / "wake.json"))
    from abcxauto.park_clock import set_wake

    set_wake(wake_in_s=30, session="closed", flat=True)
    calls = {"n": 0}

    async def think(self, n, g, s, *, resume=False):
        calls["n"] += 1
        return {"cycle": n, "pnl": 0, "equity": 100000, "_failed": False}

    _wire_stay_up_engine(monkeypatch, session="closed", think=think)
    eng = ProEngine()
    assert eng.start() is None
    deadline = time.time() + 1.2
    while time.time() < deadline:
        eng.drain_apply()
        await asyncio.sleep(0.05)
    eng.stop_engine()
    eng.drain_apply()
    assert calls["n"] == 0
    from abcxauto.park_clock import load_alarm

    assert load_alarm().wake_at is not None


def test_health_strip_cap_idle_does_not_say_next_look(monkeypatch):
    from abcxauto.pro_desktop import ProTerminal

    class _Cfg:
        xai_api_key = "test-key"
        model = "grok-4.6"
        trading_mode = "paper"
        ibkr_port = 7497
        session_look_cap = 160
        session_token_cap = 2_500_000
        temperature = 0.3
        max_tokens = 8192

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
    s.status = "Idle"
    pro.engine._session_capped = True
    pro._sync_health_strip()
    shown = pro.lbl_hs_next.value or ""
    assert "session cap" in shown
    assert "next look" not in shown
    assert "set_wake" not in shown
