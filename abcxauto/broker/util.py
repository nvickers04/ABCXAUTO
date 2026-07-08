"""IBKR endpoint resolution and small async helpers (env-driven, no config layers)."""

from __future__ import annotations

from typing import Tuple

from abcxauto.aio import safe_sleep  # noqa: F401  (re-export for broker modules)
from abcxauto.config import get_config

_LIVE_PORTS = frozenset({7496, 4001})


def get_ibkr_host() -> str:
    return get_config().ibkr_host


def get_ibkr_port() -> int:
    return get_config().ibkr_port


def is_paper_trading() -> bool:
    return get_ibkr_port() not in _LIVE_PORTS


def is_live_trading() -> bool:
    return get_ibkr_port() in _LIVE_PORTS


def resolve_ibkr_endpoint(mode: str | None = None) -> Tuple[str, int, str]:
    """Return (host, port, mode_string). ``mode`` is ignored (kept for API compat)."""
    del mode
    cfg = get_config()
    mode_str = "live" if cfg.ibkr_port in _LIVE_PORTS else "paper"
    return cfg.ibkr_host, cfg.ibkr_port, mode_str


def format_ibkr_endpoint() -> str:
    host, port, mode_str = resolve_ibkr_endpoint()
    return f"{host}:{port} ({mode_str})"
