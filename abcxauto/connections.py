"""IBKR / MDA / xAI link status for the agent shell and Pro cockpit.

Adapter only — all IBKR and MDA logic lives in ``abcxauto.broker`` and
``abcxauto.marketdata``. Do not add broker behavior here.
"""

from __future__ import annotations

from typing import Any, Dict

from abcxauto.broker.connector import get_ibkr_connector
from abcxauto.config import get_config
from abcxauto.marketdata.client import get_marketdata_client


def connection_status(connector: Any = None) -> Dict[str, Any]:
    """Lightweight status: IBKR link, MDA/xAI config, trading mode."""
    conn = connector if connector is not None else get_ibkr_connector()
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

    def _as_int(raw: Any, fallback: int) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fallback

    host = getattr(conn, "host", None)
    if not isinstance(host, str) or not host:
        host = str(getattr(cfg, "ibkr_host", "") or "")
    return {
        "ibkr_connected": bool(getattr(conn, "connected", False)),
        "ibkr_host": host,
        "ibkr_port": _as_int(getattr(conn, "port", None), int(getattr(cfg, "ibkr_port", 0) or 0)),
        "ibkr_client_id": _as_int(
            getattr(conn, "client_id", None),
            int(getattr(cfg, "ibkr_client_id", 0) or 0),
        ),
        "mda_configured": mda_ok,
        "xai_configured": bool(getattr(cfg, "xai_api_key", "") or ""),
        "trading_mode": str(getattr(cfg, "trading_mode", "paper") or "paper"),
    }
