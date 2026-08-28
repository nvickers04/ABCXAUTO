"""The single execution path: validated proposal -> IBKR gateway method."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from abcxauto.config import get_config
from abcxauto.memory import get_journal
from abcxauto.proposals import (
    OrderProposal,
    ProposalValidationError,
    params_for_journal,
    validate_proposal,
)
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

# Protective exits: after a successful place, other working STP/TRAIL on the
# same STK symbol / exit side are cancelled so the new order becomes protection.
_STACKABLE_PROTECTION = frozenset({
    "trailing_stop", "trailing_stop_limit", "oca", "stop_order", "stop_limit",
})


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


async def _resolve_close_option_kwargs(
    kwargs: Dict[str, Any],
    proposal: OrderProposal,
    connector: Any,
) -> Dict[str, Any]:
    """Fill expiration/strike/right from ledger when Act sent conId."""
    params = proposal.params
    target_con = getattr(params, "conId", None) or getattr(params, "con_id", None)
    if target_con is None:
        return kwargs
    target_con = str(target_con).strip()
    try:
        positions = await connector.get_positions()
    except Exception:
        return kwargs
    for p in positions or []:
        sec = str(p.get("sec_type") or p.get("secType") or "").upper()
        if not sec.startswith("OPT"):
            continue
        if str(p.get("conId") or p.get("con_id") or "") != target_con:
            continue
        out = dict(kwargs)
        out["symbol"] = str(p.get("symbol") or out.get("symbol") or "").upper()
        exp = p.get("expiration") or p.get("lastTradeDateOrContractMonth")
        if exp:
            out["expiration"] = str(exp)
        if p.get("strike") is not None:
            out["strike"] = float(p["strike"])
        if p.get("right"):
            out["right"] = str(p["right"]).upper()[:1]
        if out.get("quantity") is None:
            try:
                out["quantity"] = abs(int(float(p.get("quantity") or 0)))
            except (TypeError, ValueError):
                pass
        out.pop("conId", None)
        out.pop("con_id", None)
        return out
    return kwargs


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
    """Reject cancel that would strip the last STK stop.

    Fail closed if open orders / positions cannot be read. Non-stop cancels
    and redundant stops pass. Option covering exits are Grok's to manage.
    modify_stop is unrestricted (not routed here). The rule itself lives in
    ``protect.last_stop_block_reason`` so the orphan sweep applies the same one.
    """
    from abcxauto.protect import last_stop_block_reason

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

    reason = last_stop_block_reason(order_id, orders, positions)
    return {"error": reason} if reason else None


def _keep_ids_from_place_result(result: Dict[str, Any]) -> set[int]:
    """Order ids created by the new place -- never cancel these."""
    keep: set[int] = set()
    keys = ("order_id", "orderId", "stop_order_id", "target_order_id")

    def _eat(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        for k in keys:
            raw = obj.get(k)
            if raw is None:
                continue
            try:
                keep.add(int(raw))
            except (TypeError, ValueError):
                continue
        for v in obj.values():
            if isinstance(v, dict):
                _eat(v)

    _eat(result)
    return keep


def _working_stop_oid(order: Dict[str, Any]) -> Optional[int]:
    raw = order.get("order_id") if order.get("order_id") is not None else order.get("orderId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _cancel_order_ids(
    connector: Any, ids: list[int], *, log_label: str
) -> list[int]:
    """Cancel working order ids. Never used to flatten a position."""
    cancel = getattr(connector, "cancel_order", None)
    if cancel is None:
        logger.warning("%s: connector has no cancel_order", log_label)
        return []
    cancelled: list[int] = []
    for oid in ids:
        try:
            cres = await cancel(order_id=oid)
        except TypeError:
            try:
                cres = await cancel(oid)
            except Exception as e:
                logger.warning("%s: cancel %s failed: %s", log_label, oid, e)
                continue
        except Exception as e:
            logger.warning("%s: cancel %s failed: %s", log_label, oid, e)
            continue
        if isinstance(cres, dict) and cres.get("error"):
            logger.warning("%s: cancel %s: %s", log_label, oid, cres.get("error"))
            continue
        cancelled.append(oid)
        logger.info("%s: cancelled order_id=%s", log_label, oid)
    return cancelled


def _superseded_target_ids(
    orders: list | None,
    symbol: str,
    direction: str,
    keep: set[int],
) -> list[int]:
    """Prior OCA/bracket take-profit legs on the exit side, minus the new ones.

    Only a new ``oca`` supersedes a take-profit, because only ``oca`` ships a
    replacement one. A limit is cancellable here solely on OCA/parent evidence
    that it was placed as protection — an unattached LMT stays put.
    """
    from abcxauto.protect import protective_role

    want_exit = "SELL" if str(direction or "LONG").upper() == "LONG" else "BUY"
    out: list[int] = []
    seen: set[int] = set()
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        if str(o.get("symbol") or "").upper() != symbol:
            continue
        sec = str(o.get("sec_type") or o.get("secType") or "STK").upper()
        if sec and not sec.startswith("STK") and sec != "ETF":
            continue
        action = str(o.get("action") or o.get("side") or "").upper()
        if action and action != want_exit:
            continue
        if protective_role(o) != "bracket_leg":
            continue
        oid = _working_stop_oid(o)
        if oid is None or oid in keep or oid in seen:
            continue
        seen.add(oid)
        out.append(oid)
    return out


async def _replace_protective_exits_after_place(
    proposal: OrderProposal,
    connector: Any,
    result: Dict[str, Any],
) -> list[int]:
    """After a successful protective place, cancel the protection it replaces.

    Place first (brief double-cover), then cancel old. Never cancel keep ids
    from the place result. Stops go for every protective strategy; a prior
    take-profit goes only when the new ticket is an ``oca`` (which brings its
    own). If the place failed or keep ids are missing, cancel nothing.
    """
    if proposal.strategy not in _STACKABLE_PROTECTION:
        return []
    if not _dispatch_succeeded(result):
        return []
    symbol = str(getattr(proposal.params, "symbol", "") or "").upper()
    if not symbol:
        return []
    keep = _keep_ids_from_place_result(result)
    if not keep:
        logger.warning(
            "replace-on-place skipped: no order_id in place result for %s %s",
            proposal.strategy, symbol,
        )
        return []
    try:
        positions = await connector.get_positions()
        orders = await connector.get_open_orders()
    except Exception as e:
        logger.warning("replace-on-place skipped: cannot read book (%s)", e)
        return []
    from abcxauto.trade_plan import iter_working_stops, stk_qty_for_symbol

    held_signed = stk_qty_for_symbol(positions, symbol)
    if abs(held_signed) < 1e-9:
        return []
    direction = "LONG" if held_signed > 0 else "SHORT"
    to_cancel: list[int] = []
    seen: set[int] = set()
    for o, _qty in iter_working_stops(orders, symbol, direction):
        oid = _working_stop_oid(o)
        if oid is None or oid in keep or oid in seen:
            continue
        seen.add(oid)
        to_cancel.append(oid)
    if proposal.strategy == "oca":
        for oid in _superseded_target_ids(orders, symbol, direction, keep):
            if oid not in seen:
                seen.add(oid)
                to_cancel.append(oid)
    if not to_cancel:
        return []
    return await _cancel_order_ids(
        connector, to_cancel, log_label="replace-on-place"
    )


def _dispatch_succeeded(result: Dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return False
    if result.get("success") is False:
        return False
    return True


async def _verify_riskless_combo_cap(
    proposal: OrderProposal, connector: Any
) -> Optional[Dict[str, Any]]:
    """Refuse a second iron/fly BAG so TWS never sees the IBKR [202] confirm.

    One working riskless combo is allowed. Closes use the same place path, so
    this is not an exit bypass. Verticals and calendars are not this cap.
    """
    from abcxauto.riskless_combo import (
        REASON_CODE,
        is_riskless_combo_strategy,
        riskless_combo_block_reason,
        riskless_combo_reject,
    )

    if not is_riskless_combo_strategy(proposal.strategy):
        return None
    get = getattr(connector, "get_open_orders", None)
    if not callable(get):
        return riskless_combo_reject(
            f"{REASON_CODE}: cannot read open orders"
        )
    try:
        orders = await get()
    except Exception as e:
        return riskless_combo_reject(
            f"{REASON_CODE}: cannot read open orders ({e})"
        )
    if not isinstance(orders, list):
        return riskless_combo_reject(
            f"{REASON_CODE}: cannot read open orders"
        )
    gate = get_risk_gate()
    reason = riskless_combo_block_reason(
        proposal.strategy,
        orders,
        cancel_202=gate.sync_riskless_combo_202(orders),
    )
    return riskless_combo_reject(reason) if reason else None


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
        params=params_for_journal(proposal),
        validation_ok=True,
    )

    rejection = await _verify_riskless_combo_cap(proposal, connector)
    if rejection:
        logger.warning(f"Proposal #{proposal.id} blocked: {rejection['error']}")
        journal.record_dispatch(journal_id, False, rejection)
        return rejection

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

    if proposal.strategy in _EXIT_ONLY_STRATEGIES or proposal.strategy in {
        "close_option", "oca", "trailing_stop", "trailing_stop_limit",
    }:
        try:
            live = await connector.get_positions()
        except Exception as e:
            rejection = {"error": f"defined_risk_only: cannot read positions ({e})"}
            journal.record_dispatch(journal_id, False, rejection)
            return rejection
        from abcxauto.world_state import single_leg_vertical_block

        vert_note = single_leg_vertical_block(
            proposal.strategy, proposal.params, live
        )
        if vert_note:
            rejection = {"error": vert_note}
            logger.warning(f"Proposal #{proposal.id} blocked: {vert_note}")
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
    if proposal.strategy == "close_option":
        kwargs = await _resolve_close_option_kwargs(kwargs, proposal, connector)
    logger.info(f"Executing proposal #{proposal.id}: {method_name}({kwargs})")
    quote = {}
    try:
        from abcxauto.send_marks import capture_send_quote

        quote = await capture_send_quote(connector, proposal) or {}
    except Exception:
        logger.debug("send_marks quote failed", exc_info=True)
        quote = {}
    result = await method(**kwargs)
    logger.info(f"Proposal #{proposal.id} result: {result}")
    ok = _dispatch_succeeded(result)
    journal_result = dict(result) if isinstance(result, dict) else {"raw": result}
    payload: Dict[str, Any] = journal_result
    marks: Optional[dict] = None
    try:
        from abcxauto.send_marks import build_dispatch_marks, public_marks

        marks = build_dispatch_marks(
            strategy=proposal.strategy,
            params=proposal.params,
            quote=quote,
            result=journal_result,
            ok=ok,
        )
        payload = dict(journal_result)
        payload["send_marks"] = public_marks(marks)
    except Exception:
        logger.debug("send_marks build failed", exc_info=True)
        marks = None
    dispatch_id = journal.record_dispatch(journal_id, ok, payload)
    if marks is not None:
        journal.record_send_marks(
            proposal_id=journal_id,
            dispatch_id=dispatch_id,
            marks=marks,
            result=journal_result,
        )

    if (
        cfg.risk_gates_enabled
        and not is_exit_or_management(proposal)
        and _dispatch_succeeded(result)
    ):
        get_risk_gate().record_entry()

    if proposal.strategy in _STACKABLE_PROTECTION and _dispatch_succeeded(result):
        if not isinstance(result, dict):
            result = {"raw": result}
        else:
            result = dict(result)
        keep = _keep_ids_from_place_result(result)
        if not keep:
            result["replace_skipped"] = "no_order_id"
        else:
            try:
                cancelled = await _replace_protective_exits_after_place(
                    proposal, connector, result
                )
                result["replaced_ids"] = list(cancelled or [])
            except Exception as e:
                result["replace_skipped"] = str(e)[:200]
                logger.warning(
                    "replace-on-place after %s failed: %s", proposal.strategy, e
                )

    return result


async def collapse_stacked_protective_exits(
    connector: Any,
    positions: list | None,
    open_orders: list | None,
) -> list[int]:
    """Keep one covering STP/TRAIL per STK lot; cancel extras via connector.

    Prefers the newest covering order_id. Does not flatten the position and
    never cancels the last remaining covering stop. Returns cancelled ids.
    """
    from abcxauto.trade_plan import stacked_stop_cancel_ids

    ids = stacked_stop_cancel_ids(positions, open_orders)
    if not ids:
        return []
    return await _cancel_order_ids(
        connector, ids, log_label="collapse stacked exits"
    )


async def _empty_ledger_is_trustworthy(connector: Any) -> bool:
    """An empty position list is also what a stalled portfolio feed looks like.

    ``IBKRConnector.get_positions`` returns ``[]`` when disconnected, when the
    read raises, and before ``ib.portfolio()`` has populated. Account values and
    portfolio items arrive on the same account-update subscription, so a live
    socket plus a readable NetLiquidation is what makes "no lots" a fact rather
    than a gap. Without both, believe nothing and cancel nothing.
    """
    if not getattr(connector, "connected", True):
        return False
    summary = getattr(connector, "get_account_summary", None)
    if not callable(summary):
        return False
    try:
        account = await summary()
    except Exception as e:
        logger.warning("orphan-protection sweep skipped: no account read (%s)", e)
        return False
    if not isinstance(account, dict) or account.get("error"):
        return False
    for key in ("netliquidation", "NetLiquidation"):
        try:
            if float(account.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def cancel_orphaned_protection(
    connector: Any,
    *,
    positions: list | None = None,
    open_orders: list | None = None,
    symbols: Any = None,
    actions: Any = None,
) -> list[int]:
    """Cancel working protection whose position is gone. Never touches a live lot.

    A stale SELL on a flat book is an unhedged entry: that is how 2026-08-13
    went short 50 CSCO with no covering stop anywhere. Every id has to survive
    three refusals before a cancel goes out — a second independent book read
    (a lot can lag its own fill), a live-socket check when the whole ledger is
    empty, and the shared last-stop rule.
    """
    from abcxauto.protect import last_stop_block_reason, orphaned_protection_ids

    if positions is None or open_orders is None:
        try:
            positions = await connector.get_positions()
            open_orders = await connector.get_open_orders()
        except Exception as e:
            logger.warning("orphan-protection sweep skipped: cannot read book (%s)", e)
            return []
    if positions is None or open_orders is None:
        return []

    ids = orphaned_protection_ids(
        positions, open_orders, symbols=symbols, actions=actions
    )
    if not ids:
        return []

    try:
        positions = await connector.get_positions()
        open_orders = await connector.get_open_orders()
    except Exception as e:
        logger.warning("orphan-protection sweep skipped: reread failed (%s)", e)
        return []
    if positions is None or open_orders is None:
        return []
    if not positions and not await _empty_ledger_is_trustworthy(connector):
        logger.warning(
            "orphan-protection sweep skipped: empty ledger not confirmed (%s)", ids
        )
        return []
    still = set(
        orphaned_protection_ids(
            positions, open_orders, symbols=symbols, actions=actions
        )
    )

    safe: list[int] = []
    for oid in ids:
        if oid not in still:
            logger.info("orphan-protection: %s covered on reread, left working", oid)
            continue
        blocked = last_stop_block_reason(oid, open_orders, positions)
        if blocked:
            # Unreachable by construction (orphans are flat), so a hit means the
            # two views of the book disagree — keep the stop and say so.
            logger.error("orphan-protection sweep refused %s: %s", oid, blocked)
            continue
        safe.append(oid)
    if not safe:
        return []
    return await _cancel_order_ids(
        connector, safe, log_label="orphan-protection"
    )


async def safe_execute(action: dict, connector: Any) -> Dict[str, Any]:
    """Paper-trade via execute_proposal, or log proposal if broker is offline."""
    strategy = action.get("strategy") or action.get("action", "")
    if strategy in ("hold", "noop"):
        return {
            "status": "held",
            "note": "hold — no broker dispatch",
            "strategy": "hold",
        }
    if strategy in ("set_risk", "self_tune"):
        from abcxauto.self_tune import apply_self_tune

        return apply_self_tune(
            action.get("params") or {},
            rationale=str(action.get("rationale") or ""),
        )
    if strategy in ("none", "", "skipped", "blocked"):
        return {
            "status": "blocked",
            "note": "no actionable strategy",
            "strategy": strategy or "blocked",
        }
    if not getattr(connector, "connected", False):
        return {
            "status": "error",
            "note": "ibkr_disconnected",
            "strategy": strategy,
        }
    params = action.get("params") or {}
    quote_last = action.get("_quote_last")
    if quote_last is None:
        quote_last = params.get("price_hint")
    posture = action.get("_posture")
    try:
        proposal = validate_proposal(
            strategy,
            params,
            str(action.get("rationale") or "auto"),
            action.get("max_loss"),
            action.get("max_gain"),
            quote_last=float(quote_last) if quote_last is not None else None,
            posture=str(posture) if posture else None,
            session=action.get("_session"),
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
