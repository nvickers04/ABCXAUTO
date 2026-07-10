"""HTTP transport, credits, and rate limiting for MarketData.app."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from abcxauto.aio import safe_sleep as _safe_sleep
from abcxauto.marketdata.helpers import (
    API_BASE,
    MDA_MAX_CONCURRENT,
    OPTIONS_MAX_CONCURRENCY,
    OPTIONS_RETRY_ATTEMPTS,
    OPTIONS_RETRY_STATUSES,
    _format_mda_denied_body,
    _normalize_mda_api_key,
)

logger = logging.getLogger(__name__)


class MarketDataTransportMixin:
    """Per-loop httpx clients, credit circuit breaker, and request tracking."""

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
                    max_connections=MDA_MAX_CONCURRENT,
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
            sem = asyncio.Semaphore(MDA_MAX_CONCURRENT)
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
            sem = asyncio.Semaphore(OPTIONS_MAX_CONCURRENCY)
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
            for attempt in range(OPTIONS_RETRY_ATTEMPTS):
                try:
                    if self._track_request():
                        return None
                    async with self._get_global_semaphore():
                        response = await client.get(path, params=params)
                    self._parse_rate_headers(response)
                    if response.status_code in (401, 403):
                        self._log_http_denied(response, label)
                    if response.status_code not in OPTIONS_RETRY_STATUSES or attempt == OPTIONS_RETRY_ATTEMPTS - 1:
                        return response
                    logger.debug(
                        f"Retrying {label} after HTTP {response.status_code} "
                        f"({attempt + 1}/{OPTIONS_RETRY_ATTEMPTS})"
                    )
                except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout, httpx.ReadTimeout,
                        httpx.RemoteProtocolError, httpx.PoolTimeout, httpx.NetworkError) as exc:
                    if attempt == OPTIONS_RETRY_ATTEMPTS - 1:
                        raise
                    logger.debug(
                        f"Retrying {label} after transport error "
                        f"({attempt + 1}/{OPTIONS_RETRY_ATTEMPTS}): {exc}"
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

    def get_usage(self) -> Dict[str, Any]:
        """Get API usage stats."""
        return {
            'total_requests': self._request_count,
            'daily_requests': self._daily_count,
            'last_request': self._last_request_time.isoformat() if self._last_request_time else None,
            'mda_credits_remaining': self._credits_remaining,
            'mda_credits_limit': self._credits_limit,
            'mda_credits_reset_epoch': self._credits_reset,
            'mda_breaker_open': self._is_credits_exhausted(),
        }
