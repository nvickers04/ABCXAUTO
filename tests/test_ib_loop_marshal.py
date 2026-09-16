"""Policy slot on the looking worker: ib_insync getLoop() must see the running loop.

ProEngine's worker is ``Thread(target=lambda: asyncio.run(_async_loop))``.
``Client.sendMsg`` uses ``get_event_loop_policy().get_event_loop()``, not
``get_running_loop()``. ``bind_thread_loop`` fills that slot on the thread
that is already looking.
"""

from __future__ import annotations

import asyncio
import threading

from abcxauto.aio import bind_thread_loop, run_on_loop


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


def test_run_on_loop_uses_owning_loop_not_asyncio_run():
    box: dict = {}
    started = threading.Event()

    async def _worker() -> None:
        box["loop"] = asyncio.get_running_loop()
        bind_thread_loop(box["loop"])
        box["release"] = asyncio.Event()
        started.set()
        await box["release"].wait()

    t = threading.Thread(target=lambda: asyncio.run(_worker()), daemon=True)
    t.start()
    assert started.wait(5)

    async def _probe() -> None:
        box["ran"] = asyncio.get_running_loop()

    run_on_loop(_probe(), box["loop"], timeout=2)
    assert box["ran"] is box["loop"]
    box["loop"].call_soon_threadsafe(box["release"].set)
    t.join(timeout=5)
    assert not t.is_alive()


def test_panic_flatten_runs_on_worker_loop(monkeypatch):
    """Panic must flatten on the IB worker loop, not a one-off asyncio.run."""
    from abcxauto.pro_engine import ProEngine

    box: dict = {}
    started = threading.Event()
    flattened = threading.Event()

    class Conn:
        connected = True

        async def connect(self):
            return True

        async def get_positions(self):
            box["pos_loop"] = asyncio.get_running_loop()
            return [{"symbol": "SPY"}]

        async def flatten_all(self):
            box["flat_loop"] = asyncio.get_running_loop()
            box["flat_thread"] = threading.current_thread().ident
            flattened.set()
            return {"status": "ok", "flattened": 1}

    async def _stay() -> None:
        loop = asyncio.get_running_loop()
        bind_thread_loop(loop)
        box["worker_loop"] = loop
        box["worker_thread"] = threading.current_thread().ident
        started.set()
        for _ in range(400):
            if flattened.is_set():
                await asyncio.sleep(0.02)
                break
            await asyncio.sleep(0.01)

    t = threading.Thread(target=lambda: asyncio.run(_stay()), daemon=True)
    t.start()
    assert started.wait(5)

    runs: list[str] = []
    real_run = asyncio.run

    def spy_run(coro, *a, **k):
        name = getattr(getattr(coro, "cr_code", None), "co_name", "") or "?"
        runs.append(str(name))
        return real_run(coro, *a, **k)

    monkeypatch.setattr("abcxauto.aio.asyncio.run", spy_run)
    monkeypatch.setattr(
        "abcxauto.pro_engine.get_ibkr_connector",
        lambda: (_ for _ in ()).throw(
            AssertionError("panic must use the bound worker connector")
        ),
    )

    eng = ProEngine()
    eng._worker_loop = box["worker_loop"]
    eng.conn = Conn()
    eng.panic()
    assert flattened.wait(5)
    t.join(timeout=5)
    assert box["flat_loop"] is box["worker_loop"]
    assert box["pos_loop"] is box["worker_loop"]
    assert box["flat_thread"] == box["worker_thread"]
    assert "_do_panic" not in runs
    panic_events = []
    while True:
        try:
            panic_events.append(eng.ui.get_nowait())
        except Exception:
            break
    assert any(kind == "panic" for kind, _ in panic_events)
