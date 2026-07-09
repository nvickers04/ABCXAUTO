"""The single execution path: validated proposal -> IBKR gateway method."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from abcxauto.proposals import OrderProposal, ProposalValidationError, validate_proposal

logger = logging.getLogger(__name__)

# Bare orders that are only allowed as exits (validation requires
# closing_position=true); re-verified against live positions here so a
# mislabeled proposal can't open an unprotected position.
_EXIT_ONLY_STRATEGIES = frozenset({"limit_order", "market_order", "stop_order", "stop_limit"})


async def _verify_closes_position(proposal: OrderProposal, connector: Any) -> Optional[Dict[str, Any]]:
    """Return an error dict if the exit-only order would not reduce an existing position.

    Prefer exact conId match (protocol single source of truth). Fall back to symbol+STK
    only when conId is absent — never close an OPT leg with a stock market order.
    """
    params = proposal.params
    symbol = getattr(params, "symbol", "").upper()
    action = getattr(params, "action", "")
    quantity = int(getattr(params, "quantity", 0) or 0)
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


async def execute_proposal(proposal: OrderProposal, connector: Any) -> Dict[str, Any]:
    """Dispatch a validated proposal to the matching gateway method.

    ``connector`` is an IBKRConnector (or a fake with the same methods in tests).
    Returns the gateway's result dict.
    """
    if proposal.strategy in _EXIT_ONLY_STRATEGIES:
        rejection = await _verify_closes_position(proposal, connector)
        if rejection:
            logger.warning(f"Proposal #{proposal.id} blocked: {rejection['error']}")
            return rejection

    method_name = proposal.gateway_method
    method = getattr(connector, method_name)
    kwargs = proposal.params.model_dump(exclude_none=True)
    logger.info(f"Executing proposal #{proposal.id}: {method_name}({kwargs})")
    result = await method(**kwargs)
    logger.info(f"Proposal #{proposal.id} result: {result}")
    return result


async def safe_execute(action: dict, connector: Any) -> Dict[str, Any]:
    """Paper-trade via execute_proposal, or log proposal if broker is offline."""
    strategy = action.get("strategy") or action.get("action", "hold")
    if strategy in ("hold", "none"):
        return {"status": "hold"}
    if not getattr(connector, "connected", False):
        return {"status": "logged", "strategy": strategy, "params": action.get("params")}
    try:
        proposal = validate_proposal(
            strategy, action.get("params") or {}, action.get("rationale", "auto"),
            action.get("max_loss"), action.get("max_gain"),
        )
    except ProposalValidationError as e:
        return {"status": "rejected", "error": str(e)}
    return await execute_proposal(proposal, connector)
