"""Thin order-dispatch façade for the agentic shell.

Agent loops should import dispatch from here only. Validation still runs
inside ``abcxauto.executor.safe_execute`` via ``proposals.validate_proposal`` —
this module does not re-validate.

Optional ``size_pct_nl`` is a clerk annotation next to quantity (hoisted by
``tool_args``). Qty stays on the wire; never invent shares from %.

Paper + a live-family IBKR port (TWS 7496 / Gateway 4001) fails closed here
and never reaches ``safe_execute``. That mismatch must not place and must not
open a live socket. ``trading_mode==live`` is the already-blocked live path —
this module does not enable it.
"""

from __future__ import annotations

from typing import Any, Dict

from abcxauto.config import get_config
from abcxauto.executor import safe_execute
from abcxauto.tool_args import SEND_SIZE_PCT_NL

__all__ = ["send_action", "safe_execute", "SEND_SIZE_PCT_NL"]

# TWS 7496 / Gateway 4001 — live socket family. Paper is 7497 / 4002.
_LIVE_IBKR_PORTS = frozenset({7496, 4001})


def _paper_live_port(cfg: Any) -> int | None:
    """Live-family port when TRADING_MODE is not already live; else None."""
    mode = str(getattr(cfg, "trading_mode", "paper") or "paper").strip().lower()
    if mode == "live":
        return None
    try:
        port = int(getattr(cfg, "ibkr_port", 0) or 0)
    except (TypeError, ValueError):
        return None
    if port in _LIVE_IBKR_PORTS:
        return port
    return None


async def send_action(action: dict, connector: Any) -> Dict[str, Any]:
    """Dispatch ``action`` through the single executor path.

    Hold / noop (and other non-actionable strategies) short-circuit inside
    ``safe_execute`` with ``{status: held|blocked}`` and never hit the broker.
    Actionable strategies are validated then dispatched via the connector.

    A paper desk pointed at 7496/4001 returns ``blocked`` and does not call
    ``safe_execute``. Live mode is left to the existing live blockers.
    """
    cfg = get_config()
    live_port = _paper_live_port(cfg)
    if live_port is not None:
        strategy = action.get("strategy") or action.get("action", "")
        return {
            "status": "blocked",
            "note": (
                f"IBKR port {live_port} is the live family (7496/4001); "
                "TRADING_MODE is paper — not placing"
            ),
            "reason_code": "live_port_paper",
            "strategy": strategy or "blocked",
        }
    return await safe_execute(action, connector)
