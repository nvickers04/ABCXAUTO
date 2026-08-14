"""Clerk: simple entry idea → protected structure.

Grok picks symbol and side. Code fills missing stop / target / size from the
live IBKR quote and the risk floor. Prices Grok already set are never rewritten.
"""

from __future__ import annotations

from typing import Any

from abcxauto.structure_grade import posture_stop_bands

_NAKED_OPEN = frozenset({"market_order", "limit_order", "stop_order"})
_PROTECT_STRATS = frozenset({"market_bracket", "bracket", "oca"})
_DEFAULT_STOP_PCT = 0.01


def _params(act: dict) -> dict[str, Any]:
    raw = act.get("params")
    if not isinstance(raw, dict):
        act["params"] = {}
        return act["params"]
    return raw


def _missing(params: dict[str, Any], key: str) -> bool:
    v = params.get(key)
    if v is None or v == "":
        return True
    if key == "quantity":
        try:
            return int(float(v)) <= 0
        except (TypeError, ValueError):
            return True
    return False


def _px(value: float) -> float:
    return round(float(value) + 1e-9, 2)


def _direction(params: dict[str, Any]) -> str:
    d = str(params.get("direction") or "").upper()
    if d in ("LONG", "SHORT"):
        return d
    action = str(params.get("action") or "").upper()
    if action == "BUY":
        return "LONG"
    if action == "SELL":
        return "SHORT"
    return ""


def _is_exit(act: dict, positions: list) -> bool:
    params = act.get("params") if isinstance(act.get("params"), dict) else {}
    if params.get("closing_position") is True:
        return True
    if act.get("target_conId") or params.get("conId") or params.get("con_id"):
        return True
    return False


def _stk_qty(positions: list, symbol: str) -> int | None:
    want = str(symbol or "").upper()
    if not want:
        return None
    for p in positions or []:
        if str(p.get("symbol") or "").upper() != want:
            continue
        sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
        if not sec.startswith("STK"):
            continue
        try:
            qty = abs(int(float(p.get("quantity") or p.get("position") or 0)))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            return qty
    return None


def promote_naked_entry(act: dict, positions: list | None = None) -> bool:
    """Opening stock tickets become a bracket. Exits stay exits."""
    if not isinstance(act, dict):
        return False
    strat = str(act.get("strategy") or act.get("action") or "").strip().lower()
    if strat not in _NAKED_OPEN:
        return False
    if _is_exit(act, positions or []):
        return False
    params = _params(act)
    if not params.get("symbol") and act.get("symbol"):
        params["symbol"] = act["symbol"]
    if not str(params.get("symbol") or "").strip():
        return False
    direction = _direction(params)
    if not direction:
        return False
    params["direction"] = direction
    if strat == "limit_order" and params.get("limit_price") not in (None, ""):
        act["strategy"] = act["action"] = "bracket"
        if _missing(params, "entry_price"):
            params["entry_price"] = params["limit_price"]
    else:
        act["strategy"] = act["action"] = "market_bracket"
    return True


def fill_missing_protection(
    act: dict,
    *,
    quote_last: float | None,
    equity: float | None = None,
    posture: str = "balanced",
    cfg: Any = None,
    positions: list | None = None,
) -> list[str]:
    """Fill omitted stop / target / qty. Never overwrite Grok's numbers."""
    if not isinstance(act, dict):
        return []
    strat = str(act.get("strategy") or act.get("action") or "").strip().lower()
    if strat not in _PROTECT_STRATS:
        return []
    params = _params(act)
    direction = _direction(params)
    if direction not in ("LONG", "SHORT"):
        return []
    params["direction"] = direction
    try:
        quote = float(quote_last) if quote_last is not None else 0.0
    except (TypeError, ValueError):
        quote = 0.0
    if quote <= 0:
        return []

    filled: list[str] = []
    lo, hi = posture_stop_bands(posture)
    stop_pct = min(hi, max(lo, _DEFAULT_STOP_PCT))
    stop_dist = quote * stop_pct

    if _missing(params, "stop_price"):
        if direction == "LONG":
            params["stop_price"] = _px(quote - stop_dist)
        else:
            params["stop_price"] = _px(quote + stop_dist)
        filled.append("stop_price")
    if _missing(params, "target_price"):
        if direction == "LONG":
            params["target_price"] = _px(quote + stop_dist)
        else:
            params["target_price"] = _px(quote - stop_dist)
        filled.append("target_price")
    if strat == "bracket" and _missing(params, "entry_price"):
        params["entry_price"] = _px(quote)
        filled.append("entry_price")
    if _missing(params, "price_hint"):
        params["price_hint"] = _px(quote)
        filled.append("price_hint")

    if _missing(params, "quantity"):
        qty = None
        if strat == "oca":
            qty = _stk_qty(positions or [], str(params.get("symbol") or ""))
        if qty is None:
            qty = _size_from_risk(
                quote=quote,
                stop=params.get("stop_price"),
                equity=equity,
                cfg=cfg,
            )
        if qty and qty > 0:
            params["quantity"] = int(qty)
            filled.append("quantity")

    if filled:
        act["_protection_filled"] = filled
    return filled


def _size_from_risk(
    *,
    quote: float,
    stop: Any,
    equity: float | None,
    cfg: Any,
) -> int:
    try:
        eq = float(equity or 0)
        stop_px = float(stop)
    except (TypeError, ValueError):
        return 0
    if eq <= 0 or quote <= 0 or stop_px <= 0:
        return 0
    dist = abs(quote - stop_px)
    if dist <= 0:
        return 0
    try:
        risk_pct = float(getattr(cfg, "max_risk_per_trade_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        risk_pct = 1.0
    try:
        pos_pct = float(getattr(cfg, "max_position_pct", 20.0) or 20.0)
    except (TypeError, ValueError):
        pos_pct = 20.0
    risk_qty = int((eq * (risk_pct / 100.0)) / dist)
    cap_qty = int((eq * (pos_pct / 100.0)) / quote)
    qty = min(risk_qty, cap_qty)
    return qty if qty >= 1 else 0
