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

    Mode bit clamps lottery % even when paper gates are off. A qty already
    inside the band is left alone (#110). Over-ceiling qty is reduced.
    Does not bake 1% or 25% as the working size.
    """
    if not isinstance(params, dict):
        return None
    from abcxauto.mode_size import (
        clamp_size_pct_nl,
        implied_size_pct_nl,
        working_size_ceiling,
    )

    card = params.get("card")
    ceiling = working_size_ceiling(card=card, type=strategy)
    pct_in = params.get(SEND_SIZE_PCT_NL)
    clamped_pct, clamp_note = clamp_size_pct_nl(
        pct_in, card=card, type=strategy
    )
    if clamp_note and clamped_pct is not None:
        params[SEND_SIZE_PCT_NL] = float(clamped_pct)
    pct = params.get(SEND_SIZE_PCT_NL)
    mult = _option_multiplier(strategy, params)

    raw_qty = params.get("quantity")
    try:
        qty_n = int(float(raw_qty))
        has_qty = qty_n >= 1
    except (TypeError, ValueError):
        qty_n = 0
        has_qty = False

    if has_qty:
        implied = implied_size_pct_nl(
            qty_n, net_liq, price, multiplier=mult
        )
        if implied is not None and implied > ceiling + 1e-6:
            new_qty = qty_from_size_pct_nl(
                ceiling, net_liq, price, multiplier=mult
            )
            if new_qty is None:
                return None
            params["quantity"] = new_qty
            params[SEND_SIZE_PCT_NL] = float(ceiling)
            notional = notional_from_size_pct_nl(ceiling, net_liq)
            note = {
                "quantity": new_qty,
                "notional": notional,
                "size_pct_nl": float(ceiling),
                "net_liq": float(net_liq) if _pos_float(net_liq) else net_liq,
                "clamped": True,
                "raw_size_pct_nl": implied,
            }
            return note
        return None

    if pct in (None, ""):
        return None
    use_pct = clamped_pct if clamped_pct is not None else pct
    notional = notional_from_size_pct_nl(use_pct, net_liq)
    qty = qty_from_size_pct_nl(
        use_pct,
        net_liq,
        price,
        multiplier=mult,
    )
    if qty is None or notional is None:
        return None
    params["quantity"] = qty
    out = {
        "quantity": qty,
        "notional": notional,
        "size_pct_nl": float(use_pct),
        "net_liq": float(net_liq),
    }
    if clamp_note:
        out["clamped"] = True
        out["raw_size_pct_nl"] = clamp_note.get("raw")
    return out

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
