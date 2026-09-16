"""Policy slot on the looking worker: ib_insync getLoop() must see the running loop.

ProEngine's worker is ``Thread(target=lambda: asyncio.run(_async_loop))``.
``Client.sendMsg`` uses ``get_event_loop_policy().get_event_loop()``, not
``get_running_loop()``. ``bind_thread_loop`` fills that slot on the thread
that is already looking. Panic / snapshot marshal with ``run_on_loop``.
"""

from __future__ import annotations

import asyncio
import threading
import time

from abcxauto.aio import bind_thread_loop, run_on_loop
from abcxauto.pro_engine import ProEngine


def test_bind_thread_loop_fills_policy_slot_on_lambda_asyncio_run():
    box: dict = {}

    async def _look() -> None:
        running = asyncio.get_running_loop()
        bind_thread_loop(running)
        policy = asyncio.get_event_loop_policy().get_event_loop()
        box["running"] = running
        box["policy"] = policy

    t = threading.Thread(target=lambda: asyncio.run(_look()), daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()
    assert box["policy"] is box["running"]
    assert box["policy"] is not None


def test_run_on_loop_schedules_on_the_running_loop():
    box: dict = {}
    ready = threading.Event()

    async def _stay() -> None:
        box["loop"] = asyncio.get_running_loop()
        ev = asyncio.Event()
        box["hold"] = ev
        ready.set()
        await ev.wait()

    t = threading.Thread(target=lambda: asyncio.run(_stay()), daemon=True)
    t.start()
    assert ready.wait(5)

    async def _mark() -> None:
        box["ran"] = asyncio.get_running_loop()

    fut = run_on_loop(_mark(), box["loop"])
    assert fut is not None
    fut.result(timeout=5)
    assert box["ran"] is box["loop"]
    unused = _mark()
    assert run_on_loop(unused, None) is None
    unused.close()
    box["loop"].call_soon_threadsafe(box["hold"].set)
    t.join(timeout=5)


def test_panic_marshals_flatten_onto_ib_loop(monkeypatch):
    """Panic must flatten on the worker loop, not asyncio.run a second loop."""
    ran: dict = {}
    box: dict = {}
    ready = threading.Event()

    class Conn:
        connected = True

        async def connect(self):
            return True

        async def get_positions(self):
            return [{"symbol": "SPY", "quantity": 1}]

        async def flatten_all(self):
            ran["loop"] = asyncio.get_running_loop()
            ran["thread"] = threading.current_thread()
            return {"status": "ok", "position_results": []}

    async def _stay() -> None:
        loop = asyncio.get_running_loop()
        bind_thread_loop(loop)
        box["loop"] = loop
        box["thread"] = threading.current_thread()
        ev = asyncio.Event()
        box["hold"] = ev
        ready.set()
        await ev.wait()

    t = threading.Thread(target=lambda: asyncio.run(_stay()), daemon=True)
    t.start()
    assert ready.wait(5)

    def boom_run(*_a, **_k):
        raise AssertionError("panic must not asyncio.run")

    monkeypatch.setattr(asyncio, "run", boom_run)

    eng = ProEngine()
    eng.conn = Conn()
    eng._worker_loop = box["loop"]
    eng.worker = t
    eng.panic()

    deadline = time.time() + 5
    while time.time() < deadline and "loop" not in ran:
        time.sleep(0.01)
        eng.drain_apply()
    assert ran["loop"] is box["loop"]
    assert ran["thread"] is box["thread"]
    fut = getattr(eng, "_panic_future", None)
    if fut is not None:
        fut.result(timeout=2)
    box["loop"].call_soon_threadsafe(box["hold"].set)
    t.join(timeout=5)
