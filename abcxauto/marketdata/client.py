"""
Market Data App Client - Primary market data source.

Uses direct HTTP calls to the MarketData.app API.
The official SDK has pydantic-settings conflicts with multi-app .env files,
so we use httpx directly instead.

Provides:
- Stock quotes (realtime / delayed / hybrid)
- Option contract quotes
- Market open/closed status

API Docs: https://www.marketdata.app/docs/api
"""

import logging
import asyncio
import os
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone

import httpx

from abcxauto.aio import safe_sleep as _safe_sleep
from abcxauto.prints import asof_fields, bar_time_fields, note_mda_miss, stamp

logger = logging.getLogger(__name__)

# API Base URL
API_BASE = "https://api.marketdata.app/v1"
# 429 is excluded: it means credits exhausted or per-second throttle — retry
# just burns more credits. Circuit breaker handles recovery via reset time.
_OPTIONS_RETRY_STATUSES = {500, 502, 503, 504}
_OPTIONS_MAX_CONCURRENCY = 3
_OPTIONS_RETRY_ATTEMPTS = 3
_MDA_MAX_CONCURRENT = 45  # MDA hard limit is 50; keep 5 headroom


def _normalize_mda_api_key(raw: Optional[str]) -> Optional[str]:
    """Strip whitespace; drop accidental ``Bearer `` prefix; trim wrapping quotes.

    A literal ``Bearer abc`` in ``.env`` would otherwise become
    ``Authorization: Bearer Bearer abc`` → HTTP 403 from MarketData.app.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("bearer "):
        s = s[7:].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s or None


def _format_mda_denied_body(response: httpx.Response) -> str:
    """Best-effort parse of MDA JSON error (e.g. multi-IP policy) for logs."""
    try:
        data = response.json()
    except Exception:
        return (response.text or "")[:500]
    if not isinstance(data, dict):
        return str(data)[:500]
    parts: List[str] = []
    if data.get("errmsg"):
        parts.append(str(data["errmsg"]))
    if data.get("authorizedIP") is not None or data.get("blockedIP") is not None:
        parts.append(
            f"authorizedIP={data.get('authorizedIP')!r} blockedIP={data.get('blockedIP')!r}"
        )
    if data.get("troubleshootingGuide"):
        parts.append(str(data["troubleshootingGuide"]))
    return " | ".join(parts) if parts else str(data)[:500]


class MarketDataClient:
    """
    Async client for the Market Data App API.

    Features:
    - Real-time / hybrid / delayed stock quotes
    - Option contract quotes with Greeks
    - Market status for session hours
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize client.

        Args:
            api_key: API key (uses MARKETDATA_TOKEN env var if not provided)
        """
        self.api_key = _normalize_mda_api_key(
            api_key
            or os.environ.get("MARKETDATA_TOKEN")
            or os.environ.get("MARKETDATA_API_KEY")
        )
        # Per-loop HTTP clients and semaphores keyed by id(loop).
        # A single shared client bound to one loop is unsafe when worker
        # threads (asyncio.to_thread) run nested asyncio.run / run_until_complete
        # with their own loops — they would otherwise race to close and
        # recreate the main-loop client, corrupting in-flight requests.
        self._http_clients: Dict[int, httpx.AsyncClient] = {}
        self._options_semaphores: Dict[int, asyncio.Semaphore] = {}
        self._global_semaphores: Dict[int, asyncio.Semaphore] = {}
        self._request_count = 0
        self._daily_count = 0
        self._daily_reset: Optional[datetime] = None  # Resets at midnight UTC
        self._last_request_time: Optional[datetime] = None
        # Credit tracking from MDA response headers
        self._credits_remaining: Optional[int] = None
        self._credits_limit: Optional[int] = None
        self._credits_reset: Optional[int] = None  # UTC epoch seconds
        self._low_credit_warned: bool = False
        self._mda_warned_35: bool = False
        self._breaker_warned: bool = False

        if not self.api_key:
            logger.warning("No Market Data App API key configured")

    def _is_credits_exhausted(self) -> bool:
        """Circuit breaker: short-circuit requests when MDA credits are depleted.

        Returns True when credits_remaining is known to be <= 0 AND the reset
        time is still in the future. Prevents burning retry attempts (and
        further 429-triggered credit deductions) after the quota runs out.
        """
        if self._credits_remaining is None or self._credits_remaining > 0:
            return False
        # Credits <= 0. If we have a reset time and it has passed, allow through
        # (next response will refresh the counters).
        if self._credits_reset is not None:
            import time
            if time.time() >= self._credits_reset:
                return False
        if not self._breaker_warned:
            logger.warning(
                f"MDA circuit breaker OPEN: credits_remaining={self._credits_remaining}, "
                f"reset_epoch={self._credits_reset}. Suppressing all MDA calls until reset."
            )
            self._breaker_warned = True
        return True

    def _get_http_client(self) -> Optional[httpx.AsyncClient]:
        """Get or create HTTP client for the current event loop.

        Uses a per-loop cache keyed by id(loop) so that worker threads
        running their own event loops (via asyncio.to_thread → _run_async)
        do not clobber the main loop's client.

        Returns None if credits are exhausted (circuit breaker) or no API
        key is configured.
        """
        if not self.api_key:
            return None
        if self._is_credits_exhausted():
            return None
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — cannot create an AsyncClient safely.
            return None
        loop_id = id(current_loop)
        client = self._http_clients.get(loop_id)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                base_url=API_BASE,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30.0,
                limits=httpx.Limits(
                    max_connections=_MDA_MAX_CONCURRENT,
                    max_keepalive_connections=25,
                    keepalive_expiry=30,
                ),
            )
            self._http_clients[loop_id] = client
        return client

    def _get_global_semaphore(self) -> asyncio.Semaphore:
        """Global semaphore enforcing MDA's 50 concurrent request limit (per loop)."""
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = 0
        sem = self._global_semaphores.get(loop_id)
        if sem is None:
            sem = asyncio.Semaphore(_MDA_MAX_CONCURRENT)
            self._global_semaphores[loop_id] = sem
        return sem

    def _get_options_semaphore(self) -> asyncio.Semaphore:
        """Per-event-loop semaphore for high-volume option endpoints."""
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = 0
        sem = self._options_semaphores.get(loop_id)
        if sem is None:
            sem = asyncio.Semaphore(_OPTIONS_MAX_CONCURRENCY)
            self._options_semaphores[loop_id] = sem
        return sem

    async def _get_with_retries(
        self,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        label: str,
        throttle_options: bool = False,
    ) -> Optional[httpx.Response]:
        """Issue a GET with bounded retry/backoff for transient option-data failures."""
        client = self._get_http_client()
        if not client:
            return None

        async def _do_request() -> Optional[httpx.Response]:
            for attempt in range(_OPTIONS_RETRY_ATTEMPTS):
                try:
                    if self._track_request():
                        return None
                    async with self._get_global_semaphore():
                        response = await client.get(path, params=params)
                    self._parse_rate_headers(response)
                    if response.status_code in (401, 403):
                        self._log_http_denied(response, label)
                    if response.status_code not in _OPTIONS_RETRY_STATUSES or attempt == _OPTIONS_RETRY_ATTEMPTS - 1:
                        return response
                    logger.debug(
                        f"Retrying {label} after HTTP {response.status_code} "
                        f"({attempt + 1}/{_OPTIONS_RETRY_ATTEMPTS})"
                    )
                except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, httpx.ReadTimeout,
                        httpx.RemoteProtocolError, httpx.PoolTimeout, httpx.NetworkError) as exc:
                    if attempt == _OPTIONS_RETRY_ATTEMPTS - 1:
                        raise
                    logger.debug(
                        f"Retrying {label} after transport error "
                        f"({attempt + 1}/{_OPTIONS_RETRY_ATTEMPTS}): {exc}"
                    )
                await _safe_sleep(0.5 * (attempt + 1))
            return None

        if not throttle_options:
            return await _do_request()

        async with self._get_options_semaphore():
            return await _do_request()

    @property
    def is_configured(self) -> bool:
        """Check if API key is configured."""
        return bool(self.api_key)

    async def close(self):
        """Close the HTTP client bound to the current event loop.

        Safe to call from any loop; only touches the cached client for
        the running loop. Use `close_all()` to close every cached client
        (must be invoked on each loop that owns one).
        """
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            return
        client = self._http_clients.pop(loop_id, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass

    async def close_all(self):
        """Close every cached HTTP client; caller must run on each loop."""
        clients = list(self._http_clients.values())
        self._http_clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                pass

    def _parse_rate_headers(self, response: httpx.Response) -> None:
        """Extract MDA rate-limit headers and warn when credits run low."""
        try:
            remaining = response.headers.get('X-Api-Ratelimit-Remaining')
            limit = response.headers.get('X-Api-Ratelimit-Limit')
            reset = response.headers.get('X-Api-Ratelimit-Reset')
            if remaining is not None:
                self._credits_remaining = int(remaining)
            if limit is not None:
                self._credits_limit = int(limit)
            if reset is not None:
                self._credits_reset = int(reset)
            # Fallback: if we got a 429 without headers, force breaker trip
            if response.status_code == 429 and (
                self._credits_remaining is None or self._credits_remaining > 0
            ):
                self._credits_remaining = 0
            # Reset breaker-warned flag once credits recover (after reset)
            if self._credits_remaining is not None and self._credits_remaining > 0:
                self._breaker_warned = False
            # Warn when credits drop below configured fractions (once per band).
            if self._credits_remaining is not None and self._credits_limit:
                pct = self._credits_remaining / self._credits_limit
                if pct < 0.35 and not getattr(self, "_mda_warned_35", False):
                    logger.warning(
                        "MDA credits moderate: %s / %s remaining (%.0f%%) — research host will "
                        "pace harder and may skip sub-daily bundles",
                        f"{self._credits_remaining:,}",
                        f"{self._credits_limit:,}",
                        100.0 * pct,
                    )
                    self._mda_warned_35 = True
                elif pct >= 0.35:
                    self._mda_warned_35 = False
                if pct < 0.10 and not self._low_credit_warned:
                    logger.warning(
                        f"MDA credits low: {self._credits_remaining:,} / {self._credits_limit:,} remaining ({pct:.1%})"
                    )
                    self._low_credit_warned = True
                elif pct >= 0.10:
                    self._low_credit_warned = False
        except (ValueError, TypeError):
            pass

    def _log_http_denied(self, response: httpx.Response, label: str) -> None:
        """Log 401/403 with MarketData.app body when present (multi-IP, bad token, etc.)."""
        detail = _format_mda_denied_body(response)
        if detail:
            logger.warning(
                "MarketData.app HTTP %s for %s — %s",
                response.status_code,
                label,
                detail,
            )
        else:
            logger.warning(
                "MarketData.app HTTP %s for %s (empty body)",
                response.status_code,
                label,
            )

    def _track_request(self) -> bool:
        """
        Track request count for observability.
        Resets daily counter at 9:30 AM ET (MDA reset time).
        """
        # ``datetime.utcnow()`` is deprecated in Python 3.12+; use a tz-aware
        # UTC instant instead.  ``.date()`` comparisons below still work.
        now = datetime.now(timezone.utc)

        # Reset daily counter at midnight UTC
        if self._daily_reset is None or now.date() > self._daily_reset.date():
            self._daily_count = 0
            self._daily_reset = now
        
        self._request_count += 1
        self._daily_count += 1
        self._last_request_time = now
        
        return False

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

        out = dict(realtime)
        out["last_is"] = "realtime_mid"
        # Delayed bid/ask/volume sit beside the realtime mid — never as one last.
        if not isinstance(delayed, Exception) and delayed:
            out["bid"] = delayed.get("bid")
            out["ask"] = delayed.get("ask")
            out["volume"] = delayed.get("volume")
            out["bid_freshness"] = "delayed_15m"
            out["ask_freshness"] = "delayed_15m"
            out["volume_freshness"] = "delayed_15m"
        return stamp(
            out,
            source="mda",
            freshness="hybrid_realtime_mid_delayed_bidask",
            use="mda_context_not_send_geometry",
            asof=out.get("updated"),
        )

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
            updated = first(data.get('updated'))
            return stamp(
                {
                    'symbol': symbol,
                    'mid': mid,
                    'last': mid,
                    'last_is': 'realtime_mid',
                    'bid': None,
                    'ask': None,
                    'change': first(data.get('change')),
                    'change_pct': first(data.get('changepct')),
                    'updated': updated,
                },
                source='mda',
                freshness='realtime_mid',
                use='mda_context_not_send_geometry',
                asof=updated,
            )
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

            updated = first(data.get('updated'))
            return stamp(
                {
                    'symbol': symbol,
                    'bid': first(data.get('bid')),
                    'ask': first(data.get('ask')),
                    'last': first(data.get('last')),
                    'mid': first(data.get('mid')),
                    'volume': first(data.get('volume')),
                    'change': first(data.get('change')),
                    'change_pct': first(data.get('changepct')),
                    'updated': updated,
                },
                source='mda',
                freshness='delayed_15m',
                use='mda_context_not_send_geometry',
                asof=updated,
            )
        except asyncio.TimeoutError:
            logger.warning(f"Quote request timed out for {symbol}")
            return None
        except Exception as e:
            logger.warning(f"Quote request failed for {symbol}: {e}")
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

            result: Dict[str, Any] = stamp(
                {
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
                    'is_historical': bool(date or from_date or to_date),
                },
                source='mda',
                freshness='delayed_15m',
                use='greeks_only_not_send_geometry',
            )
            if date:
                result['as_of_date'] = date
            return result
        except Exception as e:
            logger.warning(f"Option quote request failed for {option_symbol}: {e}")
            return None

    async def get_stock_candles(
        self,
        symbol: str,
        *,
        resolution: str = "D",
        days_back: int = 400,
        countback: int | None = None,
    ) -> list[dict]:
        """Daily (or other) OHLCV candles via MDA.

        Correct path: ``/stocks/candles/{resolution}/{symbol}/``
        (resolution is a path segment, not a query param).

        Prefer ``countback`` when set; otherwise ``from``/``to`` from ``days_back``.
        Returns list of dicts with t/o/h/l/c/v, oldest-first. Empty on failure.
        """
        if not self.is_configured or self._is_credits_exhausted():
            return []
        sym = (symbol or "").strip().upper()
        if not sym:
            return []
        res = (resolution or "D").strip() or "D"
        to_dt = datetime.now(timezone.utc)
        params: Dict[str, Any] = {"to": to_dt.strftime("%Y-%m-%d")}
        if countback is not None and int(countback) > 0:
            params["countback"] = int(countback)
        else:
            from_dt = to_dt - timedelta(days=max(2, int(days_back)))
            params["from"] = from_dt.strftime("%Y-%m-%d")
        try:
            resp = await self._get_with_retries(
                f"/stocks/candles/{res}/{sym}/",
                params=params,
                label=f"candles {sym}",
            )
            if resp is None or resp.status_code >= 400:
                if resp is not None:
                    if resp.status_code == 404:
                        note_mda_miss(sym)
                    else:
                        self._log_http_denied(resp, f"candles {sym}")
                return []
            self._parse_rate_headers(resp)
            data = resp.json()
            if not isinstance(data, dict) or str(data.get("s") or "").lower() != "ok":
                return []
            ts = data.get("t") or []
            o = data.get("o") or []
            h = data.get("h") or []
            low = data.get("l") or []
            c = data.get("c") or []
            v = data.get("v") or []
            rows: list[dict] = []
            for i, tval in enumerate(ts):
                row = bar_time_fields(tval)
                row.update({
                    "o": o[i] if i < len(o) else None,
                    "h": h[i] if i < len(h) else None,
                    "l": low[i] if i < len(low) else None,
                    "c": c[i] if i < len(c) else None,
                    "v": v[i] if i < len(v) else None,
                })
                if row.get("t") in (None, "") and tval is not None:
                    row["t"] = tval
                rows.append(row)
            return rows
        except Exception:
            logger.exception("get_stock_candles failed for %s", symbol)
            return []

    async def get_stock_news(
        self, symbol: str, countback: int = 8
    ) -> list[dict]:
        """Fetch recent headlines for ``symbol`` via MDA /stocks/news/{symbol}/.

        Returns a list of {symbol, headline, source, published} dicts.
        Empty list when unconfigured, credits exhausted, or no data.
        """
        if not self.is_configured or self._is_credits_exhausted():
            return []
        sym = (symbol or "").strip().upper()
        if not sym:
            return []
        try:
            resp = await self._get_with_retries(
                f"/stocks/news/{sym}/",
                params={"countback": int(countback)},
                label=f"news {sym}",
            )
            if resp is None:
                return []
            self._parse_rate_headers(resp)
            if resp.status_code == 404:
                note_mda_miss(sym)
                return []
            if resp.status_code >= 400:
                self._log_http_denied(resp, f"news {sym}")
                return []
            data = resp.json()
            if not isinstance(data, dict) or str(data.get("s") or "").lower() != "ok":
                if isinstance(data, dict) and str(data.get("s") or "").lower() in ("no_data", "error"):
                    note_mda_miss(sym)
                return []
            headlines = data.get("headline") or []
            sources = data.get("source") or []
            pubs = data.get("publicationDate") or []
            symbols = data.get("symbol") or []
            out: list[dict] = []
            for i, hl in enumerate(headlines):
                if not hl:
                    continue
                pub = pubs[i] if i < len(pubs) else None
                item = {
                    "symbol": (symbols[i] if i < len(symbols) else sym) or sym,
                    "headline": str(hl).strip(),
                    "publisher": str(sources[i]) if i < len(sources) and sources[i] else "",
                    "published": pub,
                }
                item.update(asof_fields(pub))
                out.append(stamp(
                    item,
                    source="mda",
                    freshness="delayed_15m",
                    use="context_not_live_last",
                    asof=pub,
                ))
            return out
        except Exception:
            logger.exception("get_stock_news failed for %s", symbol)
            return []

    async def get_market_status(
        self,
        date: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Get market open/closed status via MDA /markets/status/.

        Args:
            date: Check a specific date
            from_date: Start of date range
            to_date: End of date range
        """
        params: Dict[str, Any] = {}
        if date:
            params["date"] = date
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        resp = await self._get_with_retries(
            "/markets/status/",
            params=params or None,
            label="market_status",
        )
        if not resp or resp.status_code not in (200, 203):
            return None

        data = resp.json()
        if data.get("s") != "ok":
            return None

        n = len(data.get("date", []))
        statuses = []
        for i in range(n):
            statuses.append({
                "date": data["date"][i] if i < len(data.get("date", [])) else None,
                "status": data["status"][i] if i < len(data.get("status", [])) else None,
            })

        return {
            "statuses": statuses,
            "count": n,
            "source": "mda",
            "freshness": "calendar",
            "use": "session_not_last",
        }


# Singleton instance
_client: Optional[MarketDataClient] = None


def get_marketdata_client() -> MarketDataClient:
    """Get or create the singleton client (token from env / Config)."""
    global _client
    token = ""
    try:
        from abcxauto.config import get_config
        token = (get_config().marketdata_token or "").strip()
    except Exception:
        token = (
            os.environ.get("MARKETDATA_TOKEN")
            or os.environ.get("MARKETDATA_API_KEY")
            or ""
        ).strip()
    if _client is None:
        _client = MarketDataClient(api_key=token or None)
    elif token and not _client.api_key:
        # Singleton created before dotenv/config — pick up the key.
        _client.api_key = _normalize_mda_api_key(token)
    return _client


async def get_quote(symbol: str) -> Optional[Dict[str, Any]]:
    """Convenience function to get a quote."""
    client = get_marketdata_client()
    return await client.get_quote(symbol)
