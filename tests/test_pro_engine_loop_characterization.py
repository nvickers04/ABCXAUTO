"""Characterization: ProEngine._async_loop phase order and _host_think.

Pins current connect → bind → first think → stop / gen-mismatch exits.
No TWS, no xAI. Sleeps and PULSE_S are patched so each test finishes quickly.

If a later stay-up pulse phase cannot be pinned without a real broker, that
assertion is omitted rather than fabricated.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from abcxauto.pro_engine import ProEngine


def _regular_snap():
    return {
        "account": {"netliquidation": 100000, "unrealizedpnl": 0},
        "positions": [],
        "open_orders": [],
        "market_hours": {"session": {"status": "regular"}},
        "protection": {"unprotected_symbols": []},
        "reality_pulse": {"session": {"status": "regular"}},
        "fills": [],
    }


def _wire_loop(monkeypatch, *, conn, think=None):
    """Stay-up harness from test_pro_engine, plus short sleeps / no skip."""

    async def fake_snap(_c):
        return _regular_snap()

    async def _al(*_a, **_k):
        return {"legal_symbols": [], "source": "test"}

    async def _fast_sleep(_s=0, *_a, **_k):
        return None

    from abcxauto.park_clock import clear_interrupt

    clear_interrupt()
    monkeypatch.setattr("abcxauto.pro_engine.get_ibkr_connector", lambda: conn)
    monkeypatch.setattr("abcxauto.pro_engine.snap", fake_snap)
    monkeypatch.setattr(
        "abcxauto.pro_engine.GrokClient",
        lambda *a, **k: SimpleNamespace(chat=object()),
    )
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._start_monitor",
        lambda self: setattr(self, "monitor", type("M", (), {"running": True})()),
    )
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._stamp_session_start_nl",
        _fast_sleep,
    )
    monkeypatch.setattr("abcxauto.universe.refresh_legal_set", _al)
    monkeypatch.setattr("abcxauto.park_clock.min_look_s", lambda: 0.01)
    monkeypatch.setattr("abcxauto.park_clock.PULSE_S", 0.01)
    monkeypatch.setattr("abcxauto.park_clock.pulse_sleep_s", lambda *_a, **_k: 0.01)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(
        "abcxauto.pro_engine.ProEngine._kill_look_skip_reason",
        lambda *_a, **_k: "",
    )
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.f10_open_look_halted",
        lambda *_a, **_k: False,
    )
    if think is not None:
        monkeypatch.setattr("abcxauto.pro_engine.ProEngine._host_think", think)


def _ready_engine():
    eng = ProEngine()
    eng.state.autonomous = True
    eng.state.running = True
    eng._resume_think = True
    eng._force_first_look = True
    eng.stop.clear()
    return eng


@pytest.mark.asyncio
async def test_async_loop_bind_thread_loop_before_connect(monkeypatch):
    """``bind_thread_loop`` sees the running loop before ``connect()``."""
    order: list = []

    class _Conn:
        connected = False

        async def connect(self):
            order.append("connect")
            self.connected = True
            return True

    def _bind(loop):
        order.append(("bind", loop))

    monkeypatch.setattr("abcxauto.aio.bind_thread_loop", _bind)

    async def think(engine, *_a, **_k):
        order.append("think")
        engine.stop.set()
        return {"cycle": 1, "sends": 0, "positions": [], "open_orders": []}

    _wire_loop(monkeypatch, conn=_Conn(), think=think)
    eng = _ready_engine()
    running = asyncio.get_running_loop()
    await asyncio.wait_for(eng._async_loop(eng._gen), timeout=3)
    assert order[0] == ("bind", running)
    assert "connect" in order
    assert order.index("connect") > 0
    assert order.index(("bind", running)) < order.index("connect")


@pytest.mark.asyncio
async def test_async_loop_retries_connect_then_succeeds_without_raise(monkeypatch):
    """First ``connect()`` failure retries; a later success does not raise."""
    connects: list[bool] = []

    class _Conn:
        connected = False

        async def connect(self):
            if not connects:
                connects.append(False)
                self.connected = False
                return False
            connects.append(True)
            self.connected = True
            return True

    async def think(engine, *_a, **_k):
        engine.stop.set()
        return {"cycle": 1, "sends": 0, "positions": [], "open_orders": []}

    _wire_loop(monkeypatch, conn=_Conn(), think=think)
    eng = _ready_engine()
    await asyncio.wait_for(eng._async_loop(eng._gen), timeout=3)
    assert connects == [False, True]
    assert eng.conn is not None
    assert eng.conn.connected is True


@pytest.mark.asyncio
async def test_async_loop_first_think_happens_after_connect(monkeypatch):
    """First ``_host_think`` is after a successful connect, with n=1."""
    order: list = []

    class _Conn:
        connected = False

        async def connect(self):
            order.append("connect")
            self.connected = True
            return True

    async def think(engine, n, g, s, resume=False, **_k):
        order.append(("think", n, resume, bool(s)))
        engine.stop.set()
        return {
            "cycle": n,
            "sends": 0,
            "positions": s.get("positions") or [],
            "open_orders": s.get("open_orders") or [],
        }

    _wire_loop(monkeypatch, conn=_Conn(), think=think)
    eng = _ready_engine()
    await asyncio.wait_for(eng._async_loop(eng._gen), timeout=3)
    assert order[0] == "connect"
    thinks = [item for item in order if isinstance(item, tuple) and item[0] == "think"]
    assert thinks, "first think did not run — session/skip gate may have changed"
    assert thinks[0][1] == 1


@pytest.mark.asyncio
async def test_async_loop_stop_flag_ends_loop_before_think(monkeypatch):
    """``stop`` set before the connect while-loop: no connect, no think."""
    connects = []
    thinks = []

    class _Conn:
        connected = False

        async def connect(self):
            connects.append(1)
            self.connected = True
            return True

    async def think(engine, *_a, **_k):
        thinks.append(1)
        return {"cycle": 1, "sends": 0}

    _wire_loop(monkeypatch, conn=_Conn(), think=think)
    eng = _ready_engine()
    eng.stop.set()
    await asyncio.wait_for(eng._async_loop(eng._gen), timeout=3)
    assert connects == []
    assert thinks == []


@pytest.mark.asyncio
async def test_async_loop_stop_after_connect_skips_think_loop(monkeypatch):
    """Successful connect + stop → post-connect guard returns; no ``_host_think``."""
    thinks = []

    class _Conn:
        connected = False

        async def connect(self):
            self.connected = True
            return True

    async def think(engine, *_a, **_k):
        thinks.append(1)
        return {"cycle": 1, "sends": 0}

    _wire_loop(monkeypatch, conn=_Conn(), think=think)
    eng = _ready_engine()

    orig_connect = _Conn.connect

    async def _connect_and_stop(self):
        ok = await orig_connect(self)
        eng.stop.set()
        return ok

    monkeypatch.setattr(_Conn, "connect", _connect_and_stop)
    await asyncio.wait_for(eng._async_loop(eng._gen), timeout=3)
    assert thinks == []


@pytest.mark.asyncio
async def test_async_loop_gen_mismatch_returns_immediately(monkeypatch):
    """Old worker: ``gen != self._gen`` at entry does not bind or connect."""
    order: list = []

    class _Conn:
        connected = False

        async def connect(self):
            order.append("connect")
            raise AssertionError("old worker must not connect")

    def _bind(_loop):
        order.append("bind")

    monkeypatch.setattr("abcxauto.aio.bind_thread_loop", _bind)
    _wire_loop(monkeypatch, conn=_Conn(), think=lambda *_a, **_k: None)
    eng = _ready_engine()
    eng._gen = 3
    await asyncio.wait_for(eng._async_loop(2), timeout=2)
    assert order == []


@pytest.mark.asyncio
async def test_async_loop_gen_bump_during_connect_retry_ends_old_worker(monkeypatch):
    """A ``_gen`` bump during the 15s retry sit exits the old worker."""
    thinks = []

    class _Conn:
        connected = False

        async def connect(self):
            self.connected = False
            return False

    async def think(engine, *_a, **_k):
        thinks.append(1)
        return {"cycle": 1, "sends": 0}

    _wire_loop(monkeypatch, conn=_Conn(), think=think)
    eng = _ready_engine()
    gen = eng._gen

    slept = {"n": 0}

    async def _sleep(_s=0, *_a, **_k):
        slept["n"] += 1
        if slept["n"] == 1:
            eng._gen = gen + 1

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    await asyncio.wait_for(eng._async_loop(gen), timeout=3)
    assert thinks == []
    assert slept["n"] >= 1


@pytest.mark.asyncio
async def test_host_think_is_the_loop_think_and_calls_grok_turn(monkeypatch):
    """``_host_think`` is what the loop calls; it forwards to ``grok_turn``."""
    from abcxauto.brain import BrainTurn

    seen = {}

    async def grok_turn(g, *, connector, world, snap, wake="", resume=False, **_k):
        seen["wake"] = wake
        seen["resume"] = resume
        seen["connector"] = connector
        return BrainTurn(text="hold", last_strat="hold")

    monkeypatch.setattr("abcxauto.brain.grok_turn", grok_turn)
    eng = ProEngine()
    eng.conn = SimpleNamespace(connected=True)
    out = await asyncio.wait_for(
        eng._host_think(1, SimpleNamespace(chat=None), _regular_snap()),
        timeout=3,
    )
    assert seen["connector"] is eng.conn
    assert out.get("cycle") == 1
    # No send this look is yield, not a hold ticket (current loop/host contract).
    assert out.get("strat") == ""
    assert out.get("action_obj") == {}
