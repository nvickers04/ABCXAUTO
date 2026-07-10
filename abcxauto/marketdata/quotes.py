"""Stock quotes, candles, and ATR via MarketData.app."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from abcxauto.marketdata.helpers import (
    normalize_mda_candle_resolution,
    intraday_countback_from_calendar_days,
    _resolution_countback_is_bars,
)

logger = logging.getLogger(__name__)


class MarketDataQuotesMixin:
    """Equity quotes and historical bars."""

    async def get_hybrid_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get hybrid quote: real-time price + delayed bid/ask/volume.
        
        Combines:
        - /stocks/prices/ for real-time mid price (fresh, accurate)
        - /stocks/quotes/ for bid/ask/volume (15-min delayed but still useful)
        
        This gives the LLM agent the best of both worlds for decision making.
        Fetches both endpoints concurrently for speed.
        """
        # Fetch real-time price and delayed quote concurrently
        realtime_task = self.get_realtime_price(symbol)
        delayed_task = self._get_delayed_quote(symbol)
        realtime, delayed = await asyncio.gather(realtime_task, delayed_task, return_exceptions=True)

        # Handle exceptions from gather
        if isinstance(realtime, Exception) or not realtime:
            return None

        # Enrich with delayed bid/ask/volume if available
        if not isinstance(delayed, Exception) and delayed:
            realtime['bid'] = delayed.get('bid')
            realtime['ask'] = delayed.get('ask')
            realtime['volume'] = delayed.get('volume')
            realtime['source'] = 'marketdata_hybrid'

        return realtime

    async def _get_delayed_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get delayed quote (bid/ask/volume) from /quotes endpoint."""
        try:
            client = self._get_http_client()
            if not client:
                return None
            
            async with self._get_global_semaphore():
                response = await client.get(f"/stocks/quotes/{symbol}/")
            self._parse_rate_headers(response)
            if response.status_code not in (200, 203):
                return None
            
            data = response.json()
            if data.get('s') != 'ok':
                return None
            
            def first(arr):
                return arr[0] if isinstance(arr, list) and arr else arr
            
            return {
                'bid': first(data.get('bid')),
                'ask': first(data.get('ask')),
                'volume': first(data.get('volume')),
            }
        except Exception:
            return None

    async def get_realtime_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get real-time midpoint price for a symbol.
        
        Uses /stocks/prices/ endpoint which provides TRUE real-time data.
        Available to all users, no exchange entitlement required.

        Args:
            symbol: Stock ticker (e.g., 'AAPL')

        Returns:
            Dict with mid, change, change_pct, updated
        """
        if not self.is_configured:
            logger.warning(f"MarketData not configured, cannot get price for {symbol}")
            return None

        try:
            response = await self._get_with_retries(
                f"/stocks/prices/{symbol}/",
                params={},
                label=f"price({symbol})",
            )
            if response is None:
                return None

            if response.status_code not in (200, 203):
                # VIX endpoints are not supported by MarketData.app in some plans.
                # Keep this quiet to avoid log spam.
                if symbol.upper() in ("VIX", "^VIX"):
                    logger.debug(f"Price request failed for {symbol}: {response.status_code}")
                else:
                    logger.warning(f"Price request failed for {symbol}: {response.status_code}")
                return None

            data = response.json()
            if data.get('s') != 'ok':
                logger.warning(f"Price API error for {symbol}: {data.get('errmsg', 'Unknown')}")
                return None

            def first(arr):
                return arr[0] if isinstance(arr, list) and arr else arr

            mid = first(data.get('mid'))
            return {
                'symbol': symbol,
                'mid': mid,
                'last': mid,  # Use mid as last for compatibility
                'bid': None,  # Not provided by /prices endpoint
                'ask': None,
                'change': first(data.get('change')),
                'change_pct': first(data.get('changepct')),
                'updated': first(data.get('updated')),
                'source': 'marketdata_realtime'
            }
        except asyncio.TimeoutError:
            logger.warning(f"Price request timed out for {symbol}")
            return None
        except Exception as e:
            logger.warning(f"Price request failed for {symbol}: {e}")
            return None

    async def get_quote(self, symbol: str, realtime: bool = True, hybrid: bool = True) -> Optional[Dict[str, Any]]:
        """
        Get quote for a symbol.
        
        Args:
            symbol: Stock ticker (e.g., 'AAPL')
            realtime: If True, use real-time /prices endpoint. If False, use delayed /quotes.
            hybrid: If True AND realtime=True, merge delayed bid/ask/volume with real-time price.

        Returns:
            Dict with bid, ask, last, volume, change, etc.
        """
        # Default: hybrid mode gives real-time price + delayed bid/ask/volume
        if realtime and hybrid:
            return await self.get_hybrid_quote(symbol)
        elif realtime:
            return await self.get_realtime_price(symbol)
        
        if not self.is_configured:
            logger.warning(f"MarketData not configured, cannot get quote for {symbol}")
            return None

        try:
            client = self._get_http_client()
            if not client:
                return None

            if self._track_request():
                return None
            async with self._get_global_semaphore():
                response = await client.get(f"/stocks/quotes/{symbol}/")
            self._parse_rate_headers(response)

            # Accept 200 and 203 (Non-Authoritative - cached/proxy data is still valid)
            if response.status_code not in (200, 203):
                logger.warning(f"Quote request failed for {symbol}: {response.status_code}")
                return None

            data = response.json()
            if data.get('s') != 'ok':
                logger.warning(f"Quote API error for {symbol}: {data.get('errmsg', 'Unknown')}")
                return None

            # Extract first element from arrays
            def first(arr):
                return arr[0] if isinstance(arr, list) and arr else arr

            return {
                'symbol': symbol,
                'bid': first(data.get('bid')),
                'ask': first(data.get('ask')),
                'last': first(data.get('last')),
                'mid': first(data.get('mid')),
                'volume': first(data.get('volume')),
                'change': first(data.get('change')),
                'change_pct': first(data.get('changepct')),
                'updated': first(data.get('updated')),
                'source': 'marketdata'
            }
        except asyncio.TimeoutError:
            logger.warning(f"Quote request timed out for {symbol}")
            return None
        except Exception as e:
            logger.warning(f"Quote request failed for {symbol}: {e}")
            return None

    async def get_quotes_bulk(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Get quotes for multiple symbols in a single API call.

        Args:
            symbols: List of tickers

        Returns:
            Dict mapping symbol -> quote data
        """
        if not symbols:
            return {}
        if not self.is_configured:
            return {}

        try:
            client = self._get_http_client()
            if not client:
                return {}

            symbols_upper = [s.upper().strip() for s in symbols]
            if self._track_request():
                return {}
            async with self._get_global_semaphore():
                response = await client.get(
                    "/stocks/bulkquotes/",
                    params={'symbols': ','.join(symbols_upper)}
                )
            self._parse_rate_headers(response)

            if response.status_code not in (200, 203):
                logger.warning(f"Bulk quotes request failed: {response.status_code}")
                # Fallback to individual calls
                return await self._get_quotes_bulk_fallback(symbols_upper)

            data = response.json()
            if data.get('s') != 'ok':
                logger.warning(f"Bulk quotes API error: {data.get('errmsg', 'Unknown')}")
                return await self._get_quotes_bulk_fallback(symbols_upper)

            results = {}
            syms = data.get('symbol', [])
            for i, sym in enumerate(syms):
                def at(arr, idx):
                    return arr[idx] if isinstance(arr, list) and idx < len(arr) else None

                results[sym] = {
                    'symbol': sym,
                    'bid': at(data.get('bid', []), i),
                    'ask': at(data.get('ask', []), i),
                    'last': at(data.get('last', []), i),
                    'mid': at(data.get('mid', []), i),
                    'volume': at(data.get('volume', []), i),
                    'change': at(data.get('change', []), i),
                    'change_pct': at(data.get('changepct', []), i),
                    'updated': at(data.get('updated', []), i),
                    'source': 'marketdata'
                }
            return results

        except Exception as e:
            logger.warning(f"Bulk quotes request failed: {e}")
            return await self._get_quotes_bulk_fallback(symbols_upper)

    async def _get_quotes_bulk_fallback(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Fallback: individual quote calls when bulk endpoint fails."""
        results = {}
        if not symbols:
            return results
        tasks = [self.get_quote(symbol) for symbol in symbols]
        quotes = await asyncio.gather(*tasks, return_exceptions=True)
        for symbol, quote in zip(symbols, quotes):
            if isinstance(quote, Exception):
                logger.warning(f"Failed to get quote for {symbol}: {quote}")
            elif quote:
                results[symbol] = quote
        return results

    async def get_candles(
        self,
        symbol: str,
        resolution: str = 'D',
        days_back: int = 30,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get historical OHLCV candles.

        Args:
            symbol: Stock ticker
            resolution: 'D' (daily), 'H' hourly, '5'/'1' minute (aliases: 5min, 1min, …)
            days_back: If ``from_date`` is omitted — for **daily (and weekly/monthly)**
              this is interpreted as MarketData ``countback`` (**number of candles**).
              For **intraday resolutions** (minutes / hour), callers still pass calendar
              *lookback days* intent (same as signals' ``return_lookback_days``); it is
              converted to an approximate bar ``countback`` so short values are not misread as
              "only N bars (~minutes of history)".
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)

        Returns:
            Dict with open, high, low, close, volume, timestamps arrays
        """
        if not self.is_configured:
            logger.warning(f"MarketData not configured, cannot get candles for {symbol}")
            return None

        try:
            norm_res = normalize_mda_candle_resolution(resolution)
            # Build query params (resolution is already in the URL path)
            params = {}
            if from_date:
                params['from'] = from_date
                if to_date:
                    params['to'] = to_date
            else:
                if _resolution_countback_is_bars(norm_res):
                    cb = intraday_countback_from_calendar_days(norm_res, days_back)
                    params['countback'] = cb
                    if os.getenv("MDA_INTRADAY_EXTENDED", "0") == "1":
                        params["extended"] = "true"
                    logger.debug(
                        "MDA candles %s %s: countback=%d (from ~%d calendar-day lookback)",
                        norm_res, symbol, cb, days_back,
                    )
                else:
                    params['countback'] = days_back

            response = await self._get_with_retries(
                f"/stocks/candles/{norm_res}/{symbol}/",
                params=params,
                label=f"candles({symbol})",
            )
            if response is None:
                return None

            if response.status_code not in (200, 203):
                # 404 is expected outside market hours or for symbols with no intraday data
                level = logging.DEBUG if response.status_code == 404 else logging.WARNING
                logger.log(level, f"Candles request failed for {symbol}: {response.status_code}")
                return None

            data = response.json()
            if data.get('s') != 'ok':
                logger.warning(f"Candles API error for {symbol}: {data.get('errmsg', 'Unknown')}")
                return None

            return {
                'symbol': symbol,
                'open': data.get('o', []),
                'high': data.get('h', []),
                'low': data.get('l', []),
                'close': data.get('c', []),
                'volume': data.get('v', []),
                'timestamps': data.get('t', []),
                'source': 'marketdata'
            }
        except asyncio.TimeoutError:
            logger.warning(f"Candles request timed out for {symbol}")
            return None
        except Exception as e:
            logger.warning(f"Candles request failed for {symbol}: {e}")
            return None

    async def get_bulk_daily_candles(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Get daily candles for multiple symbols in a single API call.

        Uses /stocks/bulkcandles/D/?symbols=... endpoint.
        Only supports daily resolution.

        Args:
            symbols: List of tickers

        Returns:
            Dict mapping symbol -> candle data (same format as get_candles)
        """
        if not symbols or not self.is_configured:
            return {}

        try:
            client = self._get_http_client()
            if not client:
                return {}

            symbols_upper = [s.upper().strip() for s in symbols]
            if self._track_request():
                return {}
            async with self._get_global_semaphore():
                response = await client.get(
                    "/stocks/bulkcandles/D/",
                    params={'symbols': ','.join(symbols_upper)}
                )
            self._parse_rate_headers(response)

            if response.status_code not in (200, 203):
                logger.warning(f"Bulk candles request failed: {response.status_code}")
                return {}

            data = response.json()
            if data.get('s') != 'ok':
                logger.warning(f"Bulk candles API error: {data.get('errmsg', 'Unknown')}")
                return {}

            # Parse bulk response — each row has a symbol field
            results: Dict[str, Dict] = {}
            syms = data.get('symbol', [])
            opens = data.get('o', [])
            highs = data.get('h', [])
            lows = data.get('l', [])
            closes = data.get('c', [])
            volumes = data.get('v', [])
            timestamps = data.get('t', [])

            for i, sym in enumerate(syms):
                if sym not in results:
                    results[sym] = {
                        'symbol': sym,
                        'open': [], 'high': [], 'low': [],
                        'close': [], 'volume': [], 'timestamps': [],
                        'source': 'marketdata'
                    }
                r = results[sym]
                if i < len(opens): r['open'].append(opens[i])
                if i < len(highs): r['high'].append(highs[i])
                if i < len(lows): r['low'].append(lows[i])
                if i < len(closes): r['close'].append(closes[i])
                if i < len(volumes): r['volume'].append(volumes[i])
                if i < len(timestamps): r['timestamps'].append(timestamps[i])

            return results

        except Exception as e:
            logger.warning(f"Bulk candles request failed: {e}")
            return {}

    async def calculate_atr(self, symbol: str, period: int = 14) -> Optional[float]:
        """
        Calculate Average True Range from candle data.

        Args:
            symbol: Stock ticker
            period: ATR period (default 14)

        Returns:
            ATR value or None
        """
        candles = await self.get_candles(symbol, 'D', days_back=period + 5)

        if not candles or len(candles.get('close', [])) < period + 1:
            logger.warning(f"Insufficient candle data for ATR: {symbol}")
            return None

        highs = candles['high']
        lows = candles['low']
        closes = candles['close']

        # Calculate True Range
        true_ranges = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            true_ranges.append(tr)

        if len(true_ranges) < period:
            return None

        # ATR is SMA of True Range
        atr = sum(true_ranges[-period:]) / period
        return round(atr, 2)

    # ========== OPTIONS DATA ==========
