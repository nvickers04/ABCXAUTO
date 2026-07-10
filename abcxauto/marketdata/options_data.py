"""Options chains, quotes, and delta search via MarketData.app."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MarketDataOptionsMixin:
    """Option chain and contract quote helpers."""

    async def get_option_chain(
        self,
        symbol: str,
        expiration: Optional[str] = None,
        side: Optional[str] = None,
        strike_range: Optional[tuple] = None,
        dte_range: Optional[tuple] = None,
        date: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        # Server-side filters (Phase 2)
        delta: Optional[float] = None,
        strike_limit: Optional[int] = None,
        range_filter: Optional[str] = None,
        min_bid: Optional[float] = None,
        max_bid_ask_spread_pct: Optional[float] = None,
        min_open_interest: Optional[int] = None,
        min_volume: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get options chain with Greeks and IV.

        Args:
            symbol: Underlying ticker (e.g., 'AAPL')
            expiration: Specific expiration (YYYY-MM-DD) or None for nearest
            side: 'call', 'put', or None for both
            strike_range: (min_strike, max_strike) or None for all
            dte_range: (min_dte, max_dte) to filter expirations
            date: Historical snapshot date (YYYY-MM-DD); returns chain as-of that date
            from_date: Start of historical range (YYYY-MM-DD)
            to_date: End of historical range (YYYY-MM-DD)
            delta: Server-side delta filter (e.g. 0.30)
            strike_limit: Max strikes to return per expiration
            range_filter: 'itm', 'otm', 'all' (API range param)
            min_bid: Minimum bid price filter
            max_bid_ask_spread_pct: Maximum bid-ask spread as percentage
            min_open_interest: Minimum open interest filter
            min_volume: Minimum volume filter

        Returns:
            Dict with options data including Greeks
        """
        if not self.is_configured:
            logger.warning(f"MarketData not configured, cannot get options for {symbol}")
            return None

        try:
            symbol = symbol.upper().strip()
            is_historical = bool(date or from_date or to_date)

            # Build query params
            params: Dict[str, Any] = {}

            if expiration:
                params['expiration'] = expiration
            elif is_historical:
                params['expiration'] = 'all'
            if side:
                params['side'] = side
            if strike_range:
                params['strike'] = f"{strike_range[0]}-{strike_range[1]}"
            if date:
                params['date'] = date
            if from_date:
                params['from'] = from_date
            if to_date:
                params['to'] = to_date

            # Server-side filters
            if delta is not None:
                params['delta'] = str(delta)
            if strike_limit is not None:
                params['strikeLimit'] = str(strike_limit)
            if range_filter:
                params['range'] = range_filter
            if min_bid is not None:
                params['minBid'] = str(min_bid)
            if max_bid_ask_spread_pct is not None:
                params['maxBidAskSpreadPct'] = str(max_bid_ask_spread_pct)
            if min_open_interest is not None:
                params['minOpenInterest'] = str(min_open_interest)
            if min_volume is not None:
                params['minVolume'] = str(min_volume)

            if dte_range and not expiration:
                target_dte = (dte_range[0] + dte_range[1]) // 2
                params['dte'] = str(target_dte)

            response = await self._get_with_retries(
                f"/options/chain/{symbol}/",
                params=params,
                label=f"options chain {symbol}",
                throttle_options=True,
            )
            if response is None:
                return None

            # 404/400 are common when filters are too narrow (or symbol truly has no listed options).
            # Retry once dropping only the strike filter — keep side, expiration, dte.
            used_fallback = False
            if response.status_code in (400, 404) and params:
                fallback_params: Dict[str, Any] = {}
                if expiration:
                    fallback_params['expiration'] = expiration
                if side:
                    fallback_params['side'] = side
                if date:
                    fallback_params['date'] = date
                if from_date:
                    fallback_params['from'] = from_date
                if to_date:
                    fallback_params['to'] = to_date
                # Preserve DTE so API returns the right expiration window
                if 'dte' in params:
                    fallback_params['dte'] = params['dte']

                response = await self._get_with_retries(
                    f"/options/chain/{symbol}/",
                    params=fallback_params,
                    label=f"options chain fallback {symbol}",
                    throttle_options=True,
                )
                if response is None:
                    return None
                used_fallback = True

            if response.status_code == 404:
                logger.debug(f"No options chain available for {symbol}")
                return None

            if response.status_code == 400:
                logger.debug(f"No valid options chain match for {symbol} with current filters")
                return None

            if response.status_code not in (200, 203):
                logger.warning(f"Options chain request failed for {symbol}: {response.status_code}")
                return None

            data = response.json()
            if data.get('s') != 'ok':
                logger.warning(f"Options chain API error for {symbol}: {data.get('errmsg', 'Unknown')}")
                return None

            # Parse into list of option contracts
            contracts = []
            option_symbols = data.get('optionSymbol', [])
            num_contracts = len(option_symbols)

            def safe_get(arr, idx):
                return arr[idx] if arr and idx < len(arr) else None

            for i in range(num_contracts):
                contract = {
                    'option_symbol': safe_get(option_symbols, i),
                    'underlying': symbol,
                    'expiration': safe_get(data.get('expiration'), i),
                    'strike': safe_get(data.get('strike'), i),
                    'side': safe_get(data.get('side'), i),
                    'bid': safe_get(data.get('bid'), i),
                    'ask': safe_get(data.get('ask'), i),
                    'mid': safe_get(data.get('mid'), i),
                    'last': safe_get(data.get('last'), i),
                    'volume': safe_get(data.get('volume'), i) or 0,
                    'open_interest': safe_get(data.get('openInterest'), i) or 0,
                    'delta': safe_get(data.get('delta'), i),
                    'gamma': safe_get(data.get('gamma'), i),
                    'theta': safe_get(data.get('theta'), i),
                    'vega': safe_get(data.get('vega'), i),
                    'iv': safe_get(data.get('iv'), i),
                    'dte': safe_get(data.get('dte'), i),
                }

                # Filter by DTE if specified
                if dte_range and contract['dte'] is not None:
                    if contract['dte'] < dte_range[0] or contract['dte'] > dte_range[1]:
                        continue

                # Filter by strike range client-side — skip if we used the fallback
                # (fallback already dropped the strike filter to broaden results)
                if strike_range and not used_fallback and contract['strike'] is not None:
                    if contract['strike'] < strike_range[0] or contract['strike'] > strike_range[1]:
                        continue

                contracts.append(contract)

            if not contracts:
                return None

            result: Dict[str, Any] = {
                'symbol': symbol,
                'contracts': contracts,
                'source': 'marketdata',
                'is_historical': bool(date or from_date or to_date),
            }
            if date:
                result['as_of_date'] = date
            elif from_date or to_date:
                result['from_date'] = from_date
                result['to_date'] = to_date
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Options chain request timed out for {symbol}")
            return None
        except Exception as e:
            logger.warning(f"Options chain request failed for {symbol}: {e}")
            return None

    async def get_option_expirations(self, symbol: str) -> Optional[List[str]]:
        """
        Get available option expiration dates.

        Args:
            symbol: Underlying ticker

        Returns:
            List of expiration dates (YYYY-MM-DD format)
        """
        if not self.is_configured:
            return None

        try:
            client = self._get_http_client()
            if not client:
                return None

            if self._track_request():
                return None
            response = await client.get(f"/options/expirations/{symbol}/")

            if response.status_code not in (200, 203):
                return None

            data = response.json()
            if data.get('s') == 'ok':
                return data.get('expirations', [])
            return None
        except Exception as e:
            logger.warning(f"Expirations request failed for {symbol}: {e}")
            return None

    async def get_option_strikes(
        self,
        symbol: str,
        expiration: str
    ) -> Optional[List[float]]:
        """
        Get available strikes for an expiration.

        Args:
            symbol: Underlying ticker
            expiration: Expiration date (YYYY-MM-DD)

        Returns:
            List of strike prices
        """
        if not self.is_configured:
            return None

        try:
            client = self._get_http_client()
            if not client:
                return None

            if self._track_request():
                return None
            response = await client.get(f"/options/strikes/{symbol}/", params={'expiration': expiration})

            if response.status_code not in (200, 203):
                return None

            data = response.json()
            if data.get('s') == 'ok':
                return data.get('strikes', [])
            return None
        except Exception as e:
            logger.warning(f"Strikes request failed for {symbol}: {e}")
            return None

    async def get_option_lookup(self, symbol: str) -> Optional[List[str]]:
        """
        Look up OCC-format option symbols for an underlying via MDA /options/lookup/.

        Args:
            symbol: Underlying ticker (e.g. 'AAPL')

        Returns:
            List of OCC option symbols, or None on failure.
        """
        if not self.is_configured:
            return None

        try:
            client = self._get_http_client()
            if not client:
                return None

            if self._track_request():
                return None
            response = await client.get(f"/options/lookup/{symbol}/")

            if response.status_code not in (200, 203):
                return None

            data = response.json()
            if data.get('s') == 'ok':
                return data.get('optionSymbol', [])
            return None
        except Exception as e:
            logger.warning(f"Option lookup failed for {symbol}: {e}")
            return None

    async def get_option_quote(
        self,
        option_symbol: str,
        date: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get quote for a specific option contract.

        Args:
            option_symbol: OCC option symbol (e.g., 'AAPL230120C00150000')
            date: Historical snapshot date (YYYY-MM-DD)
            from_date: Start of historical range (YYYY-MM-DD)
            to_date: End of historical range (YYYY-MM-DD)

        Returns:
            Dict with bid, ask, Greeks, IV, etc.
        """
        if not self.is_configured:
            return None

        try:
            # Build historical query params if provided
            q_params: Dict[str, Any] = {}
            if date:
                q_params['date'] = date
            if from_date:
                q_params['from'] = from_date
            if to_date:
                q_params['to'] = to_date
            response = await self._get_with_retries(
                f"/options/quotes/{option_symbol}/",
                params=q_params,
                label=f"option quote {option_symbol}",
                throttle_options=True,
            )
            if response is None:
                return None

            if response.status_code not in (200, 203):
                return None

            data = response.json()
            if data.get('s') != 'ok':
                return None

            def first(arr):
                return arr[0] if isinstance(arr, list) and arr else arr

            result: Dict[str, Any] = {
                'option_symbol': option_symbol,
                'underlying': first(data.get('underlying')),
                'strike': first(data.get('strike')),
                'side': first(data.get('side')),
                'expiration': first(data.get('expiration')),
                'bid': first(data.get('bid')),
                'ask': first(data.get('ask')),
                'mid': first(data.get('mid')),
                'last': first(data.get('last')),
                'volume': first(data.get('volume')) or 0,
                'open_interest': first(data.get('openInterest')) or 0,
                'delta': first(data.get('delta')),
                'gamma': first(data.get('gamma')),
                'theta': first(data.get('theta')),
                'vega': first(data.get('vega')),
                'iv': first(data.get('iv')),
                'dte': first(data.get('dte')),
                'source': 'marketdata',
                'is_historical': bool(date or from_date or to_date),
            }
            if date:
                result['as_of_date'] = date
            return result
        except Exception as e:
            logger.warning(f"Option quote request failed for {option_symbol}: {e}")
            return None

    async def get_option_quote_series(
        self,
        option_symbol: str,
        from_date: str,
        to_date: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Get a time series of option quotes between two dates.

        Returns one record per trading day in ascending date order.
        Each record has the same fields as get_option_quote() plus 'date'.

        Args:
            option_symbol: OCC option symbol
            from_date: Start date inclusive (YYYY-MM-DD)
            to_date: End date inclusive (YYYY-MM-DD)

        Returns:
            List of quote dicts ordered by date, or None on failure.
        """
        if not self.is_configured:
            return None

        try:
            response = await self._get_with_retries(
                f"/options/quotes/{option_symbol}/",
                params={'from': from_date, 'to': to_date},
                label=f"option quote series {option_symbol}",
                throttle_options=True,
            )
            if response is None:
                return None

            if response.status_code not in (200, 203):
                return None

            data = response.json()
            if data.get('s') != 'ok':
                return None

            updated_list = data.get('updated', [])
            n = len(data.get('bid', []))
            records = []
            for i in range(n):
                def g(key, idx=i):
                    arr = data.get(key, [])
                    return arr[idx] if idx < len(arr) else None

                ts_raw = g('updated')
                record_date: Optional[str] = None
                if ts_raw is not None:
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        record_date = _dt.fromtimestamp(
                            int(ts_raw), tz=_tz.utc
                        ).strftime('%Y-%m-%d')
                    except Exception:
                        pass

                records.append({
                    'option_symbol': option_symbol,
                    'date': record_date,
                    'bid': g('bid'),
                    'ask': g('ask'),
                    'mid': g('mid'),
                    'last': g('last'),
                    'volume': g('volume') or 0,
                    'open_interest': g('openInterest') or 0,
                    'delta': g('delta'),
                    'gamma': g('gamma'),
                    'theta': g('theta'),
                    'vega': g('vega'),
                    'iv': g('iv'),
                    'dte': g('dte'),
                    'source': 'marketdata',
                    'is_historical': True,
                })
            return records or None
        except Exception as e:
            logger.warning(f"Option quote series request failed for {option_symbol}: {e}")
            return None

    async def find_option_by_delta(
        self,
        symbol: str,
        target_delta: float,
        side: str,
        min_dte: int = 21,
        max_dte: int = 45
    ) -> Optional[Dict[str, Any]]:
        """
        Find option contract closest to target delta.

        Args:
            symbol: Underlying ticker
            target_delta: Target delta (e.g., 0.30 for 30-delta)
            side: 'call' or 'put'
            min_dte: Minimum days to expiration
            max_dte: Maximum days to expiration

        Returns:
            Option contract dict or None
        """
        chain = await self.get_option_chain(
            symbol,
            side=side,
            dte_range=(min_dte, max_dte),
            delta=target_delta,
            strike_limit=5,
            min_bid=0.01,
        )

        if not chain or not chain.get('contracts'):
            return None

        # Filter to valid contracts with delta
        candidates = [
            c for c in chain['contracts']
            if c.get('delta') is not None
            and c.get('bid') and c.get('bid') > 0
        ]

        if not candidates:
            return None

        # For puts, delta is negative, so compare absolute values
        if side == 'put':
            target_delta = -abs(target_delta)
            best = min(candidates, key=lambda c: abs(c['delta'] - target_delta))
        else:
            best = min(candidates, key=lambda c: abs(c['delta'] - target_delta))

        return best
