"""xAI AsyncClient wrapper — chat + streaming."""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from typing import Any, Callable

from abcxauto.config import (
    DEFAULT_MODEL,
    RESERVED_CHAT_KEYS,
    coerce_model_params,
    get_config,
)

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


def chat_create_kwargs(
    g: Any,
    *,
    messages: Any,
    tools: Any | None = None,
) -> dict[str, Any]:
    """Core chat.create kwargs plus operator ``model_params``.

    Dedicated knobs (model / temperature / max_tokens / include / tools /
    messages) win. ``effort`` is sent as ``reasoning_effort``. Unknown
    extras are dropped here with an error log — Settings already refused
    them, so this is the last line of defense for a raw map.
    """
    kw: dict[str, Any] = {
        "model": g.model,
        "messages": messages,
        "temperature": g.temperature,
        "max_tokens": int(g.max_tokens or 8192),
        "include": ["verbose_streaming"],
    }
    if tools is not None:
        kw["tools"] = list(tools)
    extras = coerce_model_params(getattr(g, "model_params", None) or {}, strict=False)
    for key, value in extras.items():
        if key in RESERVED_CHAT_KEYS or key in kw:
            continue
        kw[key] = value
    return kw


_LOGGED_CREATE_FP: str | None = None


def _short_create_kw(kwargs: dict[str, Any]) -> str:
    """Operator-readable create line. No messages, tools, or secrets."""
    skip = {"messages", "tools", "api_key", "authorization"}
    parts: list[str] = []
    for key in sorted(kwargs):
        if key in skip:
            continue
        parts.append(f"{key}={kwargs[key]!r}")
    if "reasoning_effort" not in kwargs:
        parts.append("reasoning_effort=(unset; SDK default high on grok-4.6)")
    return " ".join(parts)


def log_chat_create_kwargs(kwargs: dict[str, Any]) -> str:
    """Log exact create kwargs once per distinct payload this process."""
    global _LOGGED_CREATE_FP
    line = _short_create_kw(kwargs)
    if line == _LOGGED_CREATE_FP:
        return line
    _LOGGED_CREATE_FP = line
    logger.info("chat.create %s", line)
    model = str(kwargs.get("model") or "")
    if "-xhigh" in model.lower():
        logger.error(
            "model id %s is not reasoning_effort; "
            "set model_params.reasoning_effort=xhigh",
            model,
        )
    return line


def create_chat(client: Any, **kwargs: Any) -> Any:
    """``chat.create`` that refuses to silently strip extras.

    Tries the full set. On ``TypeError``, drop ``include`` first (older
    clients), then drop unknown extras one key at a time and log each
    drop so a bad alias cannot hide ``reasoning_effort``.
    """
    log_chat_create_kwargs(kwargs)
    create = client.chat.create
    try:
        return create(**kwargs)
    except TypeError:
        if "include" in kwargs:
            no_include = dict(kwargs)
            no_include.pop("include", None)
            logger.warning("chat.create dropped unknown kwarg include")
            try:
                return create(**no_include)
            except TypeError:
                kwargs = no_include
        reserved = {k: v for k, v in kwargs.items() if k in RESERVED_CHAT_KEYS}
        extras = {k: v for k, v in kwargs.items() if k not in RESERVED_CHAT_KEYS}
        prefer = frozenset({"reasoning_effort"})
        drop_order = [k for k in extras if k not in prefer] + [
            k for k in extras if k in prefer
        ]
        kept = dict(extras)
        for drop in drop_order:
            kept.pop(drop, None)
            logger.error("chat.create refused unknown kwarg %s — not sent", drop)
            try:
                return create(**reserved, **kept)
            except TypeError:
                continue
        if extras:
            return create(**reserved)
        raise


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

    def __init__(
        self,
        client: Any | None = None,
        *,
        model: str | None = None,
        session: str = "",
    ) -> None:
        cfg = get_config()
        if client is None:
            if not cfg.xai_api_key:
                raise RuntimeError(
                    "XAI_API_KEY is not set — copy .env.template to .env and fill it in"
                )
            from xai_sdk import AsyncClient

            client = AsyncClient(api_key=cfg.xai_api_key)
        self.client = _wrap_client(client)
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.chat = None
        self._wake_n = 0
        self._wake_appended = False
        self._last_desk_fact = ""
        chosen = str(model or "").strip()
        sess = str(session or "").strip()
        if sess:
            self.apply_session(sess, model=chosen)
        else:
            self.model = chosen or cfg.model or DEFAULT_MODEL
            self.model_params = coerce_model_params(
                getattr(cfg, "model_params", None) or {},
                strict=False,
            )
        logger.info("Grok client ready (model=%s)", self.model)

    def apply_session(self, session: str = "", *, model: str = "") -> None:
        """Bind model + params to this session so RTH thin / fallback apply."""
        from abcxauto.desk_mode import session_model, session_model_params

        cfg = get_config()
        sess = str(session or "").strip()
        chosen = str(model or "").strip()
        if sess:
            if not chosen:
                chosen = session_model(sess, cfg)
            self.model_params = session_model_params(sess, cfg)
        else:
            self.model_params = coerce_model_params(
                getattr(cfg, "model_params", None) or {},
                strict=False,
            )
        self.model = chosen or cfg.model or DEFAULT_MODEL
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
