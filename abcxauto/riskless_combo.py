"""IBKR one-active riskless/guaranteed-loss BAG cap.

Paper 2026-08-27: a second iron condor / iron butterfly / butterfly send is
cancelled with [202] and TWS pops Order Confirmation. The first working BAG
is allowed; another must not reach ``placeOrder``.
"""

from __future__ import annotations

from typing import Any, Iterable

# Strategies IBKR treats as riskless / guaranteed-loss combination orders.
RISKLESS_COMBO_STRATEGIES = frozenset({
    "iron_condor",
    "iron_butterfly",
    "butterfly",
})

# Same working set ``get_open_orders`` keeps. User-facing names are
# Submitted / PendingSubmit; PreSubmitted is also live at IBKR.
WORKING_STATUSES = frozenset({
    "Submitted",
    "PendingSubmit",
    "PreSubmitted",
})

REASON_CODE = "riskless_combo_cap"

_ALIAS_STRATEGIES = frozenset({
    "call_butterfly",
    "put_butterfly",
    "ironcondor",
    "ironbutterfly",
})


def _norm_strategy(raw: Any) -> str:
    text = str(raw or "").strip().lower().replace("-", " ")
    return "_".join(text.split())


def is_riskless_combo_strategy(name: Any) -> bool:
    """True for iron_condor / iron_butterfly / butterfly (and place-name aliases)."""
    key = _norm_strategy(name)
    if key in RISKLESS_COMBO_STRATEGIES:
        return True
    if key in _ALIAS_STRATEGIES:
        return True
    return False


def is_riskless_combo_202(error_code: Any, error_string: Any) -> bool:
    """True when IBKR [202] is the riskless/guaranteed-loss combo cap."""
    try:
        code = int(error_code)
    except (TypeError, ValueError):
        return False
    if code != 202:
        return False
    msg = str(error_string or "").lower()
    return (
        "riskless" in msg
        or "guaranteed-loss" in msg
        or "guaranteed loss" in msg
    )


def _is_working_status(order: dict[str, Any]) -> bool:
    status = str(order.get("status") or "").strip()
    if not status:
        return True
    compact = status.replace(" ", "")
    return compact in WORKING_STATUSES or status in WORKING_STATUSES


def _sec_type(order: dict[str, Any]) -> str:
    return str(order.get("sec_type") or order.get("secType") or "").upper()


def _strategy_from_order(order: dict[str, Any]) -> str:
    return _norm_strategy(
        order.get("strategy")
        or order.get("strategy_name")
        or order.get("strat")
    )


def _combo_legs(order: dict[str, Any]) -> list[Any]:
    legs = order.get("combo_legs") or order.get("comboLegs") or []
    return list(legs) if isinstance(legs, list) else []


def _leg_ratio(leg: Any) -> int:
    if isinstance(leg, dict):
        raw = leg.get("ratio")
    else:
        raw = getattr(leg, "ratio", 1)
    try:
        return int(raw or 1)
    except (TypeError, ValueError):
        return 1


def bag_looks_riskless(order: dict[str, Any]) -> bool:
    """4-leg iron / 1:2:1 butterfly. Two-leg verticals and calendars are not."""
    if _sec_type(order) != "BAG":
        return False
    legs = _combo_legs(order)
    if len(legs) >= 4:
        return True
    if len(legs) == 3:
        return 2 in (_leg_ratio(leg) for leg in legs)
    return False


def order_is_unknown_bag(order: dict[str, Any]) -> bool:
    """BAG we cannot classify — after [202], treat as the working combo."""
    if _sec_type(order) != "BAG":
        return False
    if is_riskless_combo_strategy(_strategy_from_order(order)):
        return False
    if bag_looks_riskless(order):
        return False
    if _combo_legs(order):
        return False
    return True


def order_is_working_riskless_combo(order: Any) -> bool:
    if not isinstance(order, dict) or not _is_working_status(order):
        return False
    if is_riskless_combo_strategy(_strategy_from_order(order)):
        return True
    return bag_looks_riskless(order)


def working_riskless_combos(orders: Iterable[Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for order in orders or []:
        if order_is_working_riskless_combo(order):
            out.append(order)
    return out


def working_bag_keeps_202_latch(orders: Iterable[Any] | None) -> bool:
    """After a [202] cancel, block while a riskless or unknown BAG is still working."""
    for order in orders or []:
        if not isinstance(order, dict) or not _is_working_status(order):
            continue
        if order_is_working_riskless_combo(order) or order_is_unknown_bag(order):
            return True
    return False


def _working_label(order: dict[str, Any]) -> str:
    name = _strategy_from_order(order)
    if is_riskless_combo_strategy(name):
        if name in {"call_butterfly", "put_butterfly"}:
            return "butterfly"
        return name
    if bag_looks_riskless(order):
        legs = _combo_legs(order)
        if len(legs) >= 4:
            return "iron_condor"
        return "butterfly"
    return "riskless combo"


def riskless_combo_block_reason(
    strategy: Any,
    orders: Iterable[Any] | None,
    *,
    cancel_202: bool = False,
) -> str | None:
    """Tool error if this send would be a second riskless BAG.

    One working iron/fly is allowed. A second is refused. After an IBKR [202]
    riskless-combo cancel, another is refused until that working BAG is gone.
    Vertical / calendar / other defined-risk tickets are not this cap.
    """
    if not is_riskless_combo_strategy(strategy):
        return None
    working = working_riskless_combos(orders)
    if working:
        row = working[0]
        oid = row.get("order_id")
        if oid is None:
            oid = row.get("orderId")
        sym = str(row.get("symbol") or "?").upper()
        label = _working_label(row)
        oid_bit = f" oid={oid}" if oid not in (None, "") else ""
        return (
            f"{REASON_CODE}: one {label} already working ({sym}{oid_bit}). "
            "IBKR [202] rejects a second riskless/guaranteed-loss BAG and TWS "
            "pops confirm. Wait until that BAG fills or is cancelled."
        )
    if cancel_202 and working_bag_keeps_202_latch(orders):
        return (
            f"{REASON_CODE}: IBKR [202] riskless-combo cancel this session. "
            "A second send pops TWS confirm. Wait until the working BAG is gone."
        )
    return None


def riskless_combo_reject(reason: str) -> dict[str, Any]:
    return {
        "error": reason,
        "status": "rejected",
        "reason_code": REASON_CODE,
    }
