"""The single execution path: validated proposal -> IBKR gateway method."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from abcxauto.broker.order_types import is_stop_order
from abcxauto.config import get_config
from abcxauto.memory import get_journal
from abcxauto.proposals import OrderProposal, ProposalValidationError, validate_proposal
from abcxauto.risk_gates import get_risk_gate, is_exit_or_management

logger = logging.getLogger(__name__)

from abcxauto.strategy_params import EXIT_ONLY_EXTRA

# Bare orders that are only allowed as exits (validation requires
# closing_position=true); re-verified against live positions here so a
# mislabeled proposal can't open an unprotected position.
_EXIT_ONLY_STRATEGIES = frozenset({
    "limit_order", "market_order", "stop_order", "stop_limit",
}) | EXIT_ONLY_EXTRA

# Protection-placement strategies: capital gates bypass, but executor requires
# a matching open position (any nonzero qty) before dispatch.
_PROTECTION_STRATEGIES = frozenset({"oca", "trailing_stop", "trailing_stop_limit"})


async def _verify_has_open_position(
    proposal: OrderProposal, connector: Any
) -> Optional[Dict[str, Any]]:
    """Return an error dict if protection order has no matching open position.

    Prefer conId when present; else any nonzero position for the symbol
    (STK or OPT). Flat symbols are rejected — protection must not invent exposure.
    """
    params = proposal.params
    symbol = str(getattr(params, "symbol", "") or "").upper()
    target_con = getattr(params, "conId", None) or getattr(params, "con_id", None)
    if target_con is not None:
        target_con = str(target_con).strip()

    try:
        positions = await connector.get_positions()
    except Exception as e:
        return {"error": f"Could not verify position for protection order: {e}"}

    for p in positions or []:
        try:
            qty = float(p.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            continue
        if target_con:
            p_con = str(p.get("conId") or p.get("con_id") or "")
            if p_con == target_con:
                return None
            continue
        if str(p.get("symbol", "")).upper() == symbol:
            return None

    label = f"conId={target_con}" if target_con else (symbol or "(missing)")
    return {
        "error": f"protection order rejected: no open position in {label}",
    }


async def _verify_closes_position(proposal: OrderProposal, connector: Any) -> Optional[Dict[str, Any]]:
    """Return an error dict if the exit-only order would not reduce an existing position.

    Prefer exact conId match (protocol single source of truth). Fall back to symbol+STK
    only when conId is absent — never close an OPT leg with a stock market order.
    """
    params = proposal.params
    symbol = getattr(params, "symbol", "").upper()
    action = getattr(params, "action", "")
    quantity = int(
        getattr(params, "quantity", 0)
        or getattr(params, "total_quantity", 0)
        or 0
    )
    target_con = getattr(params, "conId", None) or getattr(params, "con_id", None)
    if target_con is not None:
        target_con = str(target_con).strip()

    try:
        positions = await connector.get_positions()
    except Exception as e:
        return {"error": f"Could not verify position for exit order: {e}"}

    held = 0
    has_option_position = False
    matched_sec = None
    for p in positions or []:
        sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
        p_con = str(p.get("conId") or p.get("con_id") or "")
        if target_con:
            if p_con != target_con:
                continue
            matched_sec = sec
            try:
                held = int(float(p.get("quantity", 0) or 0))
            except (TypeError, ValueError):
                held = 0
            break
        if str(p.get("symbol", "")).upper() != symbol:
            continue
        if sec.startswith("STK"):
            try:
                held = int(float(p.get("quantity", 0) or 0))
            except (TypeError, ValueError):
                held = 0
            matched_sec = "STK"
        elif sec.startswith("OPT"):
            has_option_position = True

    if target_con and matched_sec is None:
        return {
            "error": (
                f"Exit-order check failed: target_conId={target_con} not in live ledger. "
                "Never close by symbol alone."
            )
        }
    if target_con and matched_sec and not str(matched_sec).startswith("STK"):
        return {
            "error": (
                f"Exit-order check failed: conId={target_con} is {matched_sec}, not STK. "
                "Use close_option for option legs."
            )
        }

    if action == "SELL" and held >= quantity > 0:
        return None
    if action == "BUY" and held <= -quantity and quantity > 0:
        return None

    hint = (
        f" NOTE: {symbol} has an OPTION position — stock orders do not close options; "
        f"use the 'close_option' strategy instead."
        if has_option_position
        else " New positions require a bracket/market_bracket with stop loss and take profit."
    )
    cid_bit = f" conId={target_con}" if target_con else ""
    return {
        "error": (
            f"Exit-order check failed: {action} {quantity} {symbol}{cid_bit} does not reduce an "
            f"existing STOCK position (current stock position: {held}).{hint}"
        )
    }


def _opt_field_matches(pos: Dict[str, Any], key: str, expected: Any) -> bool:
    """Compare option identity fields with light normalization."""
    raw = pos.get(key)
    if raw is None and key == "expiration":
        raw = pos.get("lastTradeDateOrContractMonth") or pos.get("expiry")
    if raw is None and key == "right":
        raw = pos.get("option_right")
    if raw is None:
        return False
    if key == "strike":
        try:
            return abs(float(raw) - float(expected)) < 0.01
        except (TypeError, ValueError):
            return False
    if key == "right":
        return str(raw).upper()[:1] == str(expected).upper()[:1]
    if key == "expiration":
        return str(raw).replace("-", "")[:8] == str(expected).replace("-", "")[:8]
    return str(raw).upper() == str(expected).upper()


async def _verify_closes_option(proposal: OrderProposal, connector: Any) -> Optional[Dict[str, Any]]:
    """Return an error dict if close_option would not reduce a matching OPT position.

    Match by conId when provided on params; else symbol/right/strike/expiry.
    Partial closes are allowed (close qty <= |held|).
    """
    params = proposal.params
    symbol = getattr(params, "symbol", "").upper()
    expiration = getattr(params, "expiration", None)
    strike = getattr(params, "strike", None)
    right = getattr(params, "right", None)
    close_qty = getattr(params, "quantity", None)
    target_con = getattr(params, "conId", None) or getattr(params, "con_id", None)
    if target_con is not None:
        target_con = str(target_con).strip()

    try:
        positions = await connector.get_positions()
    except Exception as e:
        return {"error": f"Could not verify option position for close_option: {e}"}

    held = 0
    matched = False
    for p in positions or []:
        sec = str(p.get("sec_type") or p.get("secType") or "").upper()
        if not sec.startswith("OPT"):
            continue
        p_con = str(p.get("conId") or p.get("con_id") or "")
        if target_con:
            if p_con != target_con:
                continue
        else:
            if str(p.get("symbol", "")).upper() != symbol:
                continue
            if expiration is not None and not _opt_field_matches(p, "expiration", expiration):
                continue
            if strike is not None and not _opt_field_matches(p, "strike", strike):
                continue
            if right is not None and not _opt_field_matches(p, "right", right):
                continue
        matched = True
        try:
            held = int(float(p.get("quantity", 0) or 0))
        except (TypeError, ValueError):
            held = 0
        break

    if not matched or held == 0:
        detail = (
            f"conId={target_con}"
            if target_con
            else f"{symbol} {right} {strike} {expiration}"
        )
        return {
            "error": (
                f"close_option check failed: no matching option position for {detail}."
            )
        }

    needed = int(close_qty) if close_qty is not None else abs(held)
    if needed <= 0:
        return {"error": "close_option check failed: quantity must be positive"}
    if needed > abs(held):
        return {
            "error": (
                f"close_option check failed: close qty {needed} exceeds held "
                f"|{held}| for {symbol} {right} {strike} {expiration}."
            )
        }
    return None


async def _verify_cancel_not_last_stop(
    proposal: OrderProposal, connector: Any
) -> Optional[Dict[str, Any]]:
    """Reject cancel_order when it would remove the only working stop on an open stock position.

    Fail closed if open orders / positions cannot be read. Non-stop cancels,
    stops on flat symbols, and redundant stops pass. modify_order / modify_stop
    are unrestricted (not routed here).
    """
    order_id = int(proposal.params.order_id)
    try:
        orders = await connector.get_open_orders()
    except Exception as e:
        return {
            "error": (
                f"cancel_order rejected (fail-closed): cannot read open orders ({e}). "
                "Place replacement protection first or use modify_stop."
            )
        }
    try:
        positions = await connector.get_positions()
    except Exception as e:
        return {
            "error": (
                f"cancel_order rejected (fail-closed): cannot read positions ({e}). "
                "Place replacement protection first or use modify_stop."
            )
        }

    if orders is None or positions is None:
        return {
            "error": (
                "cancel_order rejected (fail-closed): open orders or positions unavailable. "
                "Place replacement protection first or use modify_stop."
            )
        }

    target = None
    for o in orders:
        try:
            oid = int(o.get("order_id") if o.get("order_id") is not None else o.get("orderId"))
        except (TypeError, ValueError):
            continue
        if oid == order_id:
            target = o
            break

    if target is None:
        # Order not found among open orders — let the gateway return its own error.
        return None

    if not is_stop_order(str(target.get("order_type") or target.get("orderType") or "")):
        return None

    symbol = str(target.get("symbol") or "").upper()
    if not symbol:
        return None

    # Open stock position for this symbol?
    held = 0
    for p in positions:
        sec = str(p.get("sec_type") or p.get("secType") or "STK").upper()
        if not sec.startswith("STK"):
            continue
        if str(p.get("symbol", "")).upper() != symbol:
            continue
        try:
            held = int(float(p.get("quantity", 0) or 0))
        except (TypeError, ValueError):
            held = 0
        break

    if held == 0:
        return None  # flat — cancelling a stop is fine

    # Count other working stops on the same symbol (stock side)
    other_stops = 0
    for o in orders:
        try:
            oid = int(o.get("order_id") if o.get("order_id") is not None else o.get("orderId"))
        except (TypeError, ValueError):
            continue
        if oid == order_id:
            continue
        if str(o.get("symbol") or "").upper() != symbol:
            continue
        sec = str(o.get("sec_type") or o.get("secType") or "STK").upper()
        if not sec.startswith("STK"):
            continue
        if is_stop_order(str(o.get("order_type") or o.get("orderType") or "")):
            other_stops += 1

    if other_stops == 0:
        return {
            "error": (
                f"cancel_order rejected: order {order_id} is the only working stop "
                f"protecting open {symbol} position (qty={held}). "
                "First place replacement protection (oca / stop_order) "
                "or use modify_stop to move the existing stop."
            )
        }
    return None


def _dispatch_succeeded(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    if result.get("success") is False:
        return False
    return True


async def execute_proposal(
    proposal: OrderProposal, connector: Any, *, source: str = "agent"
) -> Dict[str, Any]:
    """Dispatch a validated proposal to the matching gateway method.

    ``connector`` is an IBKRConnector (or a fake with the same methods in tests).
    Returns the gateway's result dict.
    """
    journal = get_journal()
    journal_id = journal.record_proposal(
        source=source,
        strategy=proposal.strategy,
        symbol=getattr(proposal.params, "symbol", "") or "",
        direction=getattr(proposal.params, "direction", "")
        or getattr(proposal.params, "action", "")
        or "",
        quantity=getattr(proposal.params, "quantity", None),
        params=proposal.params.model_dump(exclude_none=True),
        validation_ok=True,
    )

    cfg = get_config()
    if cfg.risk_gates_enabled and not is_exit_or_management(proposal):
        gate = get_risk_gate()
        ok, reason = await gate.pre_trade_check(proposal, connector)
        journal.record_gate_decision(journal_id, ok, reason)
        if not ok:
            logger.warning(f"Proposal #{proposal.id} blocked by risk gate: {reason}")
            return {"error": reason, "status": "rejected"}

    if proposal.strategy in _EXIT_ONLY_STRATEGIES:
        rejection = await _verify_closes_position(proposal, connector)
        if rejection:
            logger.warning(f"Proposal #{proposal.id} blocked: {rejection['error']}")
            journal.record_dispatch(journal_id, False, rejection)
            return rejection

    if proposal.strategy in _PROTECTION_STRATEGIES:
        rejection = await _verify_has_open_position(proposal, connector)
        if rejection:
            logger.warning(f"Proposal #{proposal.id} blocked: {rejection['error']}")
            journal.record_dispatch(journal_id, False, rejection)
            return rejection

    if proposal.strategy == "close_option":
        rejection = await _verify_closes_option(proposal, connector)
        if rejection:
            logger.warning(f"Proposal #{proposal.id} blocked: {rejection['error']}")
            journal.record_dispatch(journal_id, False, rejection)
            return rejection

    if proposal.strategy == "cancel_order":
        rejection = await _verify_cancel_not_last_stop(proposal, connector)
        if rejection:
            logger.warning(f"Proposal #{proposal.id} blocked: {rejection['error']}")
            journal.record_dispatch(journal_id, False, rejection)
            return rejection

    method_name = proposal.gateway_method
    method = getattr(connector, method_name)
    kwargs = proposal.params.model_dump(exclude_none=True)
    logger.info(f"Executing proposal #{proposal.id}: {method_name}({kwargs})")
    result = await method(**kwargs)
    logger.info(f"Proposal #{proposal.id} result: {result}")
    journal.record_dispatch(journal_id, _dispatch_succeeded(result), result)

    if (
        cfg.risk_gates_enabled
        and not is_exit_or_management(proposal)
        and _dispatch_succeeded(result)
    ):
        get_risk_gate().record_entry()

    return result


async def safe_execute(action: dict, connector: Any) -> Dict[str, Any]:
    """Paper-trade via execute_proposal, or log proposal if broker is offline."""
    strategy = action.get("strategy") or action.get("action", "")
    if strategy in ("hold", "noop"):
        return {
            "status": "held",
            "note": "hold — no broker dispatch",
            "strategy": "hold",
        }
    if strategy == "set_risk":
        from abcxauto.config import set_risk_knobs

        return set_risk_knobs(action.get("params") or {})
    if strategy in ("none", "", "skipped", "blocked"):
        return {
            "status": "blocked",
            "note": "no actionable strategy",
            "strategy": strategy or "blocked",
        }
    if not getattr(connector, "connected", False):
        return {"status": "logged", "strategy": strategy, "params": action.get("params")}
    params = action.get("params") or {}
    quote_last = action.get("_quote_last")
    if quote_last is None:
        quote_last = params.get("price_hint")
    posture = action.get("_posture")
    try:
        proposal = validate_proposal(
            strategy,
            params,
            action.get("rationale", "auto"),
            action.get("max_loss"),
            action.get("max_gain"),
            quote_last=float(quote_last) if quote_last is not None else None,
            posture=str(posture) if posture else None,
        )
    except ProposalValidationError as e:
        err = str(e)
        get_journal().record_proposal(
            source="cycle",
            strategy=strategy,
            symbol=str(params.get("symbol") or ""),
            direction=str(
                params.get("direction") or params.get("side") or params.get("action") or ""
            ),
            quantity=params.get("quantity"),
            params=params,
            validation_ok=False,
            validation_reason=err,
        )
        try:
            from abcxauto.structure_grade import (
                GEOMETRY_REJECTED,
                append_structure_event,
            )

            code = err.split(":", 1)[0].strip() if ":" in err else GEOMETRY_REJECTED
            append_structure_event(
                {
                    "strategy": strategy,
                    "symbol": str(params.get("symbol") or "").upper(),
                    "direction": str(params.get("direction") or ""),
                    "quote": quote_last,
                    "params": {
                        k: params.get(k)
                        for k in (
                            "stop_price",
                            "target_price",
                            "entry_price",
                            "quantity",
                            "price_hint",
                        )
                        if k in params
                    },
                    "outcome": GEOMETRY_REJECTED,
                    "reason_code": code,
                    "message": err[:300],
                }
            )
        except Exception:
            pass
        return {
            "status": "rejected",
            "error": err,
            "learn": err,
            "reason_code": err.split(":", 1)[0].strip() if ":" in err else "rejected",
        }
    return await execute_proposal(proposal, connector, source="cycle")
