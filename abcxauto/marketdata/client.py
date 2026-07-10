"""MarketData.app async client — composed mixins + process singleton."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from abcxauto.marketdata.options_data import MarketDataOptionsMixin
from abcxauto.marketdata.quotes import MarketDataQuotesMixin
from abcxauto.marketdata.research import MarketDataResearchMixin
from abcxauto.marketdata.transport import MarketDataTransportMixin

# Re-export helpers used by callers / tests
from abcxauto.marketdata.helpers import (  # noqa: F401
    API_BASE,
    normalize_mda_candle_resolution,
    intraday_countback_from_calendar_days,
)


class MarketDataClient(
    MarketDataTransportMixin,
    MarketDataQuotesMixin,
    MarketDataOptionsMixin,
    MarketDataResearchMixin,
):
    """Async client for the Market Data App API."""


_client: Optional[MarketDataClient] = None


def get_marketdata_client() -> MarketDataClient:
    global _client
    if _client is None:
        _client = MarketDataClient()
    return _client


async def get_quote(symbol: str) -> Optional[Dict[str, Any]]:
    return await get_marketdata_client().get_quote(symbol)


async def get_atr(symbol: str, period: int = 14) -> Optional[float]:
    return await get_marketdata_client().calculate_atr(symbol, period=period)


async def get_option_chain(symbol: str, **kwargs) -> Optional[Dict[str, Any]]:
    return await get_marketdata_client().get_option_chain(symbol, **kwargs)


async def find_option_by_delta(*args, **kwargs):
    return await get_marketdata_client().find_option_by_delta(*args, **kwargs)
