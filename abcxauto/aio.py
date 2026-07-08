"""Small async helpers shared across layers (no package-internal imports)."""

import asyncio
import time as _time


async def safe_sleep(seconds: float) -> None:
    """asyncio.sleep wrapper — falls back to time.sleep on Python 3.13 deque bug."""
    try:
        await asyncio.sleep(seconds)
    except IndexError:
        _time.sleep(seconds)
