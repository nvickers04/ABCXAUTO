"""Small async helpers shared across layers (no package-internal imports)."""

import asyncio


async def safe_sleep(seconds: float) -> None:
    """Yield ``seconds`` so IBKR callbacks can flush.

    Do not catch IndexError. A time.sleep fallback returns success without
    running the loop — a rejected order or a dead connect then looks finished.
    """
    await asyncio.sleep(seconds)
