"""Thin order-dispatch façade for the agentic shell.

Agent loops should import dispatch from here only. Validation still runs
inside ``abcxauto.executor.safe_execute`` via ``proposals.validate_proposal`` —
this module does not re-validate.

``size_pct_nl`` is Grok's size: percent of current NetLiquidation. When
quantity is missing, the send clerk derives shares/contracts from that %
and a live price. Hoist still does not invent qty (no NL there).

Paper + a live-family IBKR port (TWS 7496 / Gateway 4001) fails closed here
and never reaches ``safe_execute``. That mismatch must not place and must not
open a live socket. ``trading_mode==live`` is the already-blocked live path —
this module does not enable it.
"""

from __future__ import annotations

import math
from typing import Any, Dict

from abcxauto.config import get_config
from abcxauto.executor import safe_execute
from abcxauto.tool_args import SEND_SIZE_PCT_NL

__all__ = [
    "send_action",
    "safe_execute",
    "SEND_SIZE_PCT_NL",
    "notional_from_size_pct_nl",
    "qty_from_size_pct_nl",
    "apply_size_pct_nl",
]


def _pos_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def notional_from_size_pct_nl(size_pct_nl: Any, net_liq: Any) -> float | None:
    """Dollars = Grok's % of current NetLiquidation. None if unusable."""
    pct = _pos_float(size_pct_nl)
    nl = _pos_float(net_liq)
    if pct is None or nl is None:
        return None
    return nl * (pct / 100.0)


def qty_from_size_pct_nl(
    size_pct_nl: Any,
    net_liq: Any,
    price: Any,
    *,
    multiplier: float = 1.0,
) -> int | None:
    """Whole units from % of current NL and a live price. None if < 1."""
    notional = notional_from_size_pct_nl(size_pct_nl, net_liq)
    px = _pos_float(price)
    try:
        mult = float(multiplier)
    except (TypeError, ValueError):
        return None
    if notional is None or px is None or not math.isfinite(mult) or mult <= 0:
        return None
    qty = int(notional / (px * mult))
    return qty if qty >= 1 else None


def _option_multiplier(strategy: str, params: dict[str, Any]) -> float:
    from abcxauto.strategy_params import OPTION_STRATEGIES

    st = str(strategy or "").strip().lower()
    if st in OPTION_STRATEGIES:
        return 100.0
    if params.get("expiration") not in (None, "") and params.get("strike") not in (None, ""):
        return 100.0
    return 1.0


def apply_size_pct_nl(
    params: dict[str, Any],
    *,
    net_liq: Any,
    price: Any,
    strategy: str = "",
) -> dict[str, Any] | None:
    """Fill ``quantity`` from ``size_pct_nl`` × current NL. None if unused.

    Does not overwrite a valid quantity. Does not bake 1% or 25%.
    """
    if not isinstance(params, dict):
        return None
    raw_qty = params.get("quantity")
    try:
        if int(float(raw_qty)) >= 1:
            return None
    except (TypeError, ValueError):
        pass
    pct = params.get(SEND_SIZE_PCT_NL)
    if pct in (None, ""):
        return None
    notional = notional_from_size_pct_nl(pct, net_liq)
    qty = qty_from_size_pct_nl(
        pct,
        net_liq,
        price,
        multiplier=_option_multiplier(strategy, params),
    )
    if qty is None or notional is None:
        return None
    params["quantity"] = qty
    return {
        "quantity": qty,
        "notional": notional,
        "size_pct_nl": float(pct),
        "net_liq": float(net_liq),
    }

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
