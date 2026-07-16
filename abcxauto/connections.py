"""Thin connection / market-data façade for the agentic shell.

Adapters only — all IBKR and MDA logic lives in ``abcxauto.broker`` and
``abcxauto.marketdata``. Do not add broker behavior here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.marketdata.client import get_marketdata_client
from abcxauto.marketdata.market_hours import get_session_info as _get_session_info


def get_connector():
    """Return the singleton IBKR connector."""
    return get_ibkr_connector()


async def connect(connector: Any = None) -> bool:
    """Connect to IBKR via the existing connector ``connect()``."""
    conn = connector if connector is not None else get_connector()
    return bool(await conn.connect())


async def snapshot_account(connector: Any) -> Dict[str, Any]:
    """Account summary dict from the connector."""
    return await connector.get_account_summary()


async def snapshot_positions(connector: Any) -> List[Dict[str, Any]]:
    """Open positions from the connector."""
    return await connector.get_positions()


async def snapshot_open_orders(connector: Any) -> List[Dict[str, Any]]:
    """Open orders from the connector."""
    return await connector.get_open_orders()


async def get_quote(symbol: str) -> Dict[str, Any]:
    """MDA quote for ``symbol``, or ``{}`` if unavailable."""
    quote = await get_marketdata_client().get_quote(symbol)
    return quote if isinstance(quote, dict) else {}


def session_info() -> Dict[str, Any]:
    """Market-hours session info (RTH / extended / etc.)."""
    return _get_session_info()


def connection_status(connector: Any = None) -> Dict[str, Any]:
    """Lightweight status: IBKR link, MDA/xAI config, trading mode."""
    conn = connector if connector is not None else get_connector()
    cfg = get_config()
    mda = get_marketdata_client()
    mda_fn = getattr(mda, "is_configured", None)
    if callable(mda_fn):
        try:
            mda_ok = bool(mda_fn())
        except Exception:
            mda_ok = False
    else:
        mda_ok = bool(mda_fn)
    return {
        "ibkr_connected": bool(getattr(conn, "connected", False)),
        "mda_configured": mda_ok,
        "xai_configured": bool(getattr(cfg, "xai_api_key", "") or ""),
        "trading_mode": str(getattr(cfg, "trading_mode", "paper") or "paper"),
    }
