"""xAI AsyncClient wrapper — chat + streaming."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from typing import Any, Callable

from abcxauto.config import get_config

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You own an Interactive Brokers {mode} book. Strategy is yours.
Live only follows a promoted playbook. Risk is code.
send tickets that match ORDER EXAMPLES.
Size vs max_risk_per_trade_pct of NetLiq.
"""

# Short burst only. Not a park, not set_wake, not the look-backoff clock.
CAPACITY_RETRIES = 3
CAPACITY_BACKOFF_MIN_S = 20.0
CAPACITY_BACKOFF_MAX_S = 45.0
CAPACITY_TRIES = 1 + CAPACITY_RETRIES

_CAPACITY_MARKERS = (
    "resource_exhausted",
    "at capacity",
)


def build_system_prompt() -> str:
    cfg = get_config()
    return SYSTEM_PROMPT.format(mode=cfg.trading_mode)


def is_capacity_error(err: Any) -> bool:
    """True when xAI refused the call for capacity / RESOURCE_EXHAUSTED."""
    blob = str(err or "").lower()
    if not blob:
        return False
    return any(m in blob for m in _CAPACITY_MARKERS)


def _capacity_backoff_s() -> float:
    return float(random.uniform(CAPACITY_BACKOFF_MIN_S, CAPACITY_BACKOFF_MAX_S))


def _raise_or_retry(exc: BaseException, *, attempt: int, what: str) -> None:
    if not is_capacity_error(exc) or attempt >= CAPACITY_TRIES:
        raise exc
    logger.warning(
        "xAI capacity on %s (%s/%s): %s",
        what,
        attempt,
        CAPACITY_TRIES,
        exc,
    )


def _sync_capacity_retry(fn: Callable[[], Any], *, what: str) -> Any:
    last: BaseException | None = None
    for attempt in range(1, CAPACITY_TRIES + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            _raise_or_retry(exc, attempt=attempt, what=what)
            time.sleep(_capacity_backoff_s())
    assert last is not None
    raise last


async def _stream_with_capacity_retry(factory: Callable[[], Any]) -> Any:
    last: BaseException | None = None
    for attempt in range(1, CAPACITY_TRIES + 1):
        yielded = False
        try:
            stream = factory()
            if inspect.iscoroutine(stream):
                stream = await stream
            if inspect.isasyncgen(stream) or hasattr(stream, "__aiter__"):
                async for item in stream:
                    yielded = True
                    yield item
                return
            return
        except Exception as exc:
            last = exc
            if yielded:
                raise
            _raise_or_retry(exc, attempt=attempt, what="stream")
            await asyncio.sleep(_capacity_backoff_s())
    assert last is not None
    raise last


def _wrap_client(client: Any) -> Any:
    if getattr(client, "_abcx_capacity_retry", False):
        return client
    return _RetryClient(client)


class _RetryClient:
    """Pass-through client; chat.create / chat.stream retry on capacity."""

    _abcx_capacity_retry = True

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    @property
    def chat(self) -> Any:
        return _RetryChatAPI(self._raw.chat)


class _RetryChatAPI:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        chat = _sync_capacity_retry(
            lambda: self._raw.create(*args, **kwargs),
            what="create",
        )
        return _RetrySession(chat)


class _RetrySession:
    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        return _stream_with_capacity_retry(lambda: self._raw.stream(*args, **kwargs))


class GrokClient:
    """Thin wrapper owning the AsyncClient and chat construction."""

    def __init__(self, client: Any | None = None) -> None:
        cfg = get_config()
        if client is None:
            if not cfg.xai_api_key:
                raise RuntimeError(
                    "XAI_API_KEY is not set — copy .env.template to .env and fill it in"
                )
            from xai_sdk import AsyncClient

            client = AsyncClient(api_key=cfg.xai_api_key)
        self.client = _wrap_client(client)
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.chat = None
        self._wake_n = 0
        self._wake_appended = False
        self._last_desk_fact = ""
        logger.info(f"Grok client ready (model={self.model})")
