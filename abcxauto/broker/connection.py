"""IBKR TWS/Gateway connection lifecycle — error codes, port/mode guards, reconnect hints.

Used by :mod:`abcxauto.broker.connector` for classifying disconnects (especially TWS
midnight restarts), paper/live safety checks, and structured logging.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple

from abcxauto.aio import safe_sleep  # noqa: F401  (re-export for broker modules)
from abcxauto.config import get_config

# IB API connectivity messages (see IB TWS API Reference → Error Codes)
ERROR_CONNECTIVITY_LOST = 1100  # IB ↔ TWS lost; TWS may still be up
ERROR_CONNECTIVITY_RESTORED_DATA_LOST = 1101
ERROR_CONNECTIVITY_RESTORED = 1102
ERROR_TWS_SERVER_BROKEN = 2110  # TWS ↔ IB server broken (Gateway restart common)

TWS_RESTART_CODES: frozenset[int] = frozenset({ERROR_CONNECTIVITY_LOST, ERROR_TWS_SERVER_BROKEN})
TWS_RESTORED_CODES: frozenset[int] = frozenset(
    {ERROR_CONNECTIVITY_RESTORED_DATA_LOST, ERROR_CONNECTIVITY_RESTORED}
)
FARM_OK_CODES: frozenset[int] = frozenset({2104, 2106, 2158})

PAPER_PORTS: frozenset[int] = frozenset({7497, 4002})
LIVE_PORTS: frozenset[int] = frozenset({7496, 4001})
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_LIVE_TRADING_RISK"


class DisconnectCause(str, Enum):
    """Why the API session dropped (best-effort attribution)."""

    UNKNOWN = "unknown"
    TWS_RESTART = "tws_restart"
    USER_DISCONNECT = "user_disconnect"
    HEARTBEAT_FAILED = "heartbeat_failed"
    CONNECT_FAILED = "connect_failed"


class TradingModePortError(ValueError):
    """TRADING_MODE / IBKR_PORT / live-confirm inconsistency — refuse connect."""


def validate_trading_mode_port(
    mode: str,
    port: int,
    live_confirm: str = "",
) -> None:
    """Raise :class:`TradingModePortError` if mode/port/confirm are inconsistent.

    Paper may only use 7497/4002; live only 7496/4001. Live additionally requires
    ``live_confirm == I_UNDERSTAND_LIVE_TRADING_RISK``.
    """
    normalized = (mode or "paper").strip().lower()
    try:
        port_i = int(port)
    except (TypeError, ValueError) as e:
        raise TradingModePortError(f"Invalid IBKR_PORT={port!r}") from e

    if normalized == "paper":
        if port_i not in PAPER_PORTS:
            raise TradingModePortError(
                f"TRADING_MODE=paper requires IBKR_PORT in {sorted(PAPER_PORTS)} "
                f"(got {port_i}). Live ports are 7496/4001 — set TRADING_MODE=live "
                f"and ABCXAUTO_LIVE_CONFIRM only if you intend real-money trading."
            )
        return

    if normalized == "live":
        if port_i not in LIVE_PORTS:
            raise TradingModePortError(
                f"TRADING_MODE=live requires IBKR_PORT in {sorted(LIVE_PORTS)} "
                f"(got {port_i}). Paper ports are 7497/4002."
            )
        if (live_confirm or "").strip() != LIVE_CONFIRM_PHRASE:
            raise TradingModePortError(
                "TRADING_MODE=live refused: set ABCXAUTO_LIVE_CONFIRM to the exact "
                f"phrase {LIVE_CONFIRM_PHRASE!r} before connecting to a live port. "
                "This is real money — do not set it casually."
            )
        return

    raise TradingModePortError(
        f"Unknown TRADING_MODE={mode!r}; expected 'paper' or 'live'"
    )


def classify_error_code(error_code: int) -> Optional[str]:
    """Return a lifecycle hint for ``ibkr_core`` error handling.

    Returns:
        ``tws_lost`` — IBKR↔TWS link down; API socket may still be up (1100, 2110).
        ``tws_restored`` — connectivity back; session may need refresh (1101, 1102).
        ``farm_ok`` — informational farm message (suppress noise).
        ``None`` — no special handling.
    """
    if error_code in TWS_RESTART_CODES:
        return "tws_lost"
    if error_code in TWS_RESTORED_CODES:
        return "tws_restored"
    if error_code in FARM_OK_CODES:
        return "farm_ok"
    return None


def reconnect_backoff_seconds(
    failure_count: int, *, base: float = 2.0, cap: float = 60.0
) -> float:
    """Exponential backoff between reconnect attempts (seconds).

    ``failure_count`` 0 → ``base``, then doubles each step up to ``cap``.
    """
    n = max(0, int(failure_count))
    return min(cap, base * (2 ** min(n, 5)))


# ---------- endpoint helpers ----------


def resolve_ibkr_endpoint(mode: str | None = None) -> Tuple[str, int, str]:
    """Return (host, port, mode_string) from config.

    ``mode`` is ignored (kept for API compat). Mode comes from ``TRADING_MODE``.
    Call :func:`validate_trading_mode_port` before connecting.
    """
    del mode
    cfg = get_config()
    mode_str = (cfg.trading_mode or "paper").strip().lower() or "paper"
    return cfg.ibkr_host, cfg.ibkr_port, mode_str


def assert_connect_allowed() -> None:
    """Raise if TRADING_MODE / port / live-confirm are inconsistent."""
    cfg = get_config()
    validate_trading_mode_port(cfg.trading_mode, cfg.ibkr_port, cfg.live_confirm)


