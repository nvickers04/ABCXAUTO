"""Small async helpers shared across layers (no package-internal imports)."""

import asyncio


async def safe_sleep(seconds: float) -> None:
    """Yield ``seconds`` so IBKR callbacks can flush.

    Do not catch IndexError. A time.sleep fallback returns success without
    running the loop — a rejected order or a dead connect then looks finished.
    """
    await asyncio.sleep(seconds)


def bind_thread_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Fill this thread's policy slot so ib_insync ``getLoop()`` sees the running loop.

    ``Client.sendMsg`` calls ``asyncio.get_event_loop_policy().get_event_loop()``,
    which does not fall back to the running loop. A stay-up look already runs on
    ProEngine's ``Thread-N (<lambda>)`` inside ``asyncio.run(_async_loop)``.
    If that policy slot is empty, every IBKR ticket raises
    ``There is no current event loop in thread ...``.
    """
    if loop is None:
        return
    try:
        if loop.is_closed():
            return
    except Exception:
        return
    try:
        asyncio.set_event_loop(loop)
    except Exception:
        pass
