"""Policy slot on the looking worker: ib_insync getLoop() must see the running loop.

ProEngine's worker is ``Thread(target=lambda: asyncio.run(_async_loop))``.
``Client.sendMsg`` uses ``get_event_loop_policy().get_event_loop()``, not
``get_running_loop()``. ``bind_thread_loop`` fills that slot on the thread
that is already looking.
"""

from __future__ import annotations

import asyncio
import threading

from abcxauto.aio import bind_thread_loop


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
