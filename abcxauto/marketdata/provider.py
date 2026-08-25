"""Slim sync facade over the async MarketData.app client.

The broker layer (options combos, order retry hints) needs a synchronous
``get_quote`` even when called from inside a running event loop, so the async
client call is executed on a dedicated worker thread with its own loop.

Grok's chat tools are async and use :func:`abcxauto.marketdata.client.get_marketdata_client`
directly — this facade exists only for the broker layer.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from abcxauto.marketdata.client import get_marketdata_client

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    """Delayed/hybrid MDA quote. Not send geometry."""

    symbol: str
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    volume: int
    change_pct: Optional[float]
    source: str = "mda"
    freshness: str = "delayed_15m"

    @property
    def mid(self) -> Optional[float]:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last


class DataProvider:
    """Sync quote access for broker code (single worker thread, own event loop)."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mda-sync")

    def get_quote(self, symbol: str) -> Optional[Quote]:
        try:
            raw = self._executor.submit(
                asyncio.run, get_marketdata_client().get_quote(symbol)
            ).result(timeout=15)
        except Exception as e:
            logger.debug(f"get_quote({symbol}) failed: {e}")
            return None
        if not raw:
            return None
        return Quote(
            symbol=symbol.upper(),
            last=raw.get("last") or raw.get("mid"),
            bid=raw.get("bid"),
            ask=raw.get("ask"),
            volume=int(raw.get("volume") or 0),
            change_pct=raw.get("change_pct"),
            source=str(raw.get("source") or "mda"),
            freshness=str(raw.get("freshness") or "delayed_15m"),
        )


_provider: Optional[DataProvider] = None


def get_data_provider() -> DataProvider:
    global _provider
    if _provider is None:
        _provider = DataProvider()
    return _provider
