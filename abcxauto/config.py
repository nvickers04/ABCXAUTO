"""ABCXAUTO configuration — one flat, env-driven config. No profiles, no overlays."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

DEFAULT_MANDATE = (
    "You are running autonomously. Research the account and market, manage existing "
    "positions (stops, targets, order edits), and propose new trades only when you "
    "see a clear edge with defined risk. Every new stock entry must use bracket or "
    "market_bracket with stop loss and take profit."
)


@dataclass(frozen=True)
class Config:
    # xAI / Grok
    xai_api_key: str = ""
    model: str = "grok-4.3"
    temperature: float = 0.3
    max_tokens: int = 8192

    # MarketData.app
    marketdata_token: str = ""

    # IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497  # 7497 paper TWS, 7496 live TWS, 4002 paper gateway, 4001 live gateway
    ibkr_client_id: int = 42
    trading_mode: str = "paper"  # informational; port is what matters

    # Agent
    trading_mandate: str = field(default=DEFAULT_MANDATE, repr=False)
    system_prompt_extra: str = field(default="", repr=False)
    scan_enabled: bool = True
    scan_interval_s: int = 900  # periodic opportunity scan during market hours

    # Background P&L monitor
    monitor_enabled: bool = True
    monitor_poll_s: int = 30
    monitor_review_s: int = 300
    monitor_extended_hours: bool = False

    # Web dashboard
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    @property
    def is_paper(self) -> bool:
        return self.ibkr_port in (7497, 4002)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def get_config() -> Config:
    load_dotenv()
    mandate = _env("ABCXAUTO_TRADING_MANDATE") or DEFAULT_MANDATE
    return Config(
        xai_api_key=_env("XAI_API_KEY") or _env("GROK_API_KEY"),
        model=_env("ABCXAUTO_MODEL", "grok-4.3"),
        temperature=float(_env("ABCXAUTO_TEMPERATURE", "0.3")),
        max_tokens=int(_env("ABCXAUTO_MAX_TOKENS", "8192")),
        marketdata_token=_env("MARKETDATA_TOKEN") or _env("MARKETDATA_API_KEY"),
        ibkr_host=_env("IBKR_HOST", "127.0.0.1"),
        ibkr_port=int(_env("IBKR_PORT", "7497")),
        ibkr_client_id=int(_env("IBKR_CLIENT_ID", "42")),
        trading_mode=_env("TRADING_MODE", "paper"),
        trading_mandate=mandate,
        system_prompt_extra=_env("ABCXAUTO_SYSTEM_PROMPT_EXTRA"),
        scan_enabled=_env_bool("ABCXAUTO_SCAN_ENABLED", True),
        scan_interval_s=int(_env("ABCXAUTO_SCAN_INTERVAL_S", "900")),
        monitor_enabled=_env_bool("ABCXAUTO_MONITOR_ENABLED", True),
        monitor_poll_s=int(_env("ABCXAUTO_MONITOR_POLL_S", "30")),
        monitor_review_s=int(_env("ABCXAUTO_MONITOR_REVIEW_S", "300")),
        monitor_extended_hours=_env_bool("ABCXAUTO_MONITOR_EXTENDED_HOURS", False),
        web_host=_env("ABCXAUTO_WEB_HOST", "127.0.0.1"),
        web_port=int(_env("ABCXAUTO_WEB_PORT", "8000")),
    )