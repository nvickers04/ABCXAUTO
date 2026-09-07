"""KEEP-2: abort fuse latches cancel resting non-exit working orders.

Scorecard fuses F10 / DD30 / QTY0_STREAK used to be a label only. Resting
entry working could survive. On latch: cancel those via IBKR ``cancel_order``
(not flatten, not last-stop). Exits stay allowed. Fail-closed if the book
or cancel path is unreadable.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

ACTION_FUSES = frozenset({"F10", "DD30", "QTY0_STREAK"})

_latch: dict[str, Any] = {
    "fuse": "none",
    "cancel_all_invoked": False,
    "working_orders_after_abort": None,
}


def reset_abort_fuse_for_tests() -> None:
    """Drop process-life abort-cancel overlay. Tests only."""
    _latch["fuse"] = "none"
    _latch["cancel_all_invoked"] = False
    _latch["working_orders_after_abort"] = None


def abort_cancel_overlay() -> dict[str, Any]:
    """Scorecard fields stamped after cancel-all on abort."""
    remaining = _latch.get("working_orders_after_abort")
    if remaining is not None:
        try:
            remaining = int(remaining)
        except (TypeError, ValueError):
            remaining = None
    return {
        "cancel_all_invoked": bool(_latch.get("cancel_all_invoked")),
        "working_orders_after_abort": remaining,
    }


def scorecard_abort_fuse() -> str:
    """Named window fuse, or ``none``. Journal unreadable → none (caller gates)."""
    try:
        from abcxauto.memory import get_journal

        win = get_journal().pcs_kill_window()
        fuse = str((win or {}).get("abort_fuse") or "none").strip()
        if fuse in ACTION_FUSES:
            return fuse
    except Exception:
        logger.debug("scorecard abort_fuse unreadable", exc_info=True)
    return "none"


def live_f10_abort_fuse(f10: dict[str, Any] | None = None) -> str:
    """Hard F10 only. Preferred tripwire and model_cost_cap are not this fuse."""
    from abcxauto.thin_rth_kill_look import REASON_F10

    gate = f10 if isinstance(f10, dict) else None
    if gate is None:
        try:
            from abcxauto.thin_rth_kill_look import live_f10_gate

            gate = live_f10_gate()
        except Exception:
            logger.debug("live F10 gate unreadable", exc_info=True)
            return "none"
    if str((gate or {}).get("reason_code") or "") == REASON_F10:
        return "F10"
    return "none"


def resolve_abort_fuse(
    abort_fuse: str | None = None,
    *,
    f10: dict[str, Any] | None = None,
) -> str:
    """Explicit fuse wins. Else scorecard window, else live hard F10."""
    if abort_fuse is not None:
        token = str(abort_fuse or "none").strip()
        return token if token in ACTION_FUSES else "none"
    fuse = scorecard_abort_fuse()
    if fuse in ACTION_FUSES:
        return fuse
    return live_f10_abort_fuse(f10)


def _as_oid(order: Any) -> int | None:
    row = order if isinstance(order, dict) else {}
    raw = row.get("order_id")
    if raw is None:
        raw = row.get("orderId")
    try:
        oid = int(raw)
    except (TypeError, ValueError):
        return None
    return oid if oid > 0 else None


def non_exit_working_ids(
    orders: list[Any] | None,
    positions: list[Any] | None = None,
) -> list[int]:
    """Resting working ids that are not last-stop / exit cover.

    Last-stop (F9) and compact role=exit stay. Entry / add / unclassified
    non-last-stop working are cancelled on abort.
    """
    from abcxauto.protect import last_stop_block_reason
    from abcxauto.world_state import compact_working_orders

    book = [o for o in (orders or []) if isinstance(o, dict)]
    lots = [p for p in (positions or []) if isinstance(p, dict)]
    keep: set[int] = set()
    for o in book:
        oid = _as_oid(o)
        if oid is None:
            continue
        if last_stop_block_reason(oid, book, lots):
            keep.add(oid)

    roles: dict[int, str] = {}
    for row in compact_working_orders(book, positions=lots, limit=10_000):
        if not isinstance(row, dict):
            continue
        oid = _as_oid(row)
        if oid is None:
            continue
        roles[oid] = str(row.get("role") or "")

    out: list[int] = []
    seen: set[int] = set()
    for o in book:
        oid = _as_oid(o)
        if oid is None or oid in seen:
            continue
        seen.add(oid)
        if oid in keep:
            continue
        if roles.get(oid) == "exit":
            continue
        out.append(oid)
    return out


def _idle_result(fuse: str = "none") -> dict[str, Any]:
    return {
        "abort_fuse": fuse if fuse in ACTION_FUSES else "none",
        "cancel_all_invoked": False,
        "working_orders_after_abort": None,
        "cancelled": [],
        "errors": [],
    }


async def _read_book(
    connector: Any,
    *,
    positions: list[Any] | None,
    open_orders: list[Any] | None,
) -> tuple[list[Any] | None, list[Any] | None, str]:
    orders = open_orders
    lots = positions
    if orders is None:
        get_orders = getattr(connector, "get_open_orders", None)
        if get_orders is None:
            return None, lots, "connector has no get_open_orders"
        try:
            raw = await get_orders()
        except Exception as exc:
            return None, lots, f"get_open_orders failed: {exc}"
        orders = list(raw or [])
    if lots is None:
        get_pos = getattr(connector, "get_positions", None)
        if get_pos is None:
            lots = []
        else:
            try:
                raw_p = await get_pos()
                lots = list(raw_p or [])
            except Exception as exc:
                return orders, None, f"get_positions failed: {exc}"
    return orders, lots, ""


async def _cancel_one(connector: Any, oid: int) -> dict[str, Any]:
    cancel = getattr(connector, "cancel_order", None)
    if cancel is None:
        return {"error": "connector has no cancel_order"}
    try:
        cres = await cancel(order_id=oid)
    except TypeError:
        cres = await cancel(oid)
    if isinstance(cres, dict):
        return cres
    return {"success": True, "order_id": oid}


async def apply_abort_fuse_cancel(
    connector: Any,
    *,
    fuse: str,
    positions: list[Any] | None = None,
    open_orders: list[Any] | None = None,
) -> dict[str, Any]:
    """Cancel non-exit working on a named abort fuse. Never flatten. Never last-stop."""
    token = str(fuse or "none").strip()
    if token not in ACTION_FUSES:
        return _idle_result(token)

    _latch["fuse"] = token
    _latch["cancel_all_invoked"] = True

    orders, lots, err = await _read_book(
        connector, positions=positions, open_orders=open_orders
    )
    if err:
        logger.error("abort fuse %s: %s — fail-closed", token, err)
        _latch["working_orders_after_abort"] = None
        return {
            "abort_fuse": token,
            "cancel_all_invoked": True,
            "working_orders_after_abort": None,
            "cancelled": [],
            "errors": [err],
        }

    ids = non_exit_working_ids(orders, lots)
    cancelled: list[int] = []
    errors: list[str] = []
    live = list(orders or [])

    if ids and getattr(connector, "cancel_order", None) is None:
        msg = "connector has no cancel_order"
        logger.error("abort fuse %s: %s — fail-closed", token, msg)
        remaining = len(ids)
        _latch["working_orders_after_abort"] = remaining
        return {
            "abort_fuse": token,
            "cancel_all_invoked": True,
            "working_orders_after_abort": remaining,
            "cancelled": [],
            "errors": [msg],
        }

    for oid in ids:
        try:
            cres = await _cancel_one(connector, oid)
        except Exception as exc:
            errors.append(f"cancel {oid}: {exc}")
            logger.error("abort fuse: cancel %s failed: %s", oid, exc)
            continue
        if isinstance(cres, dict) and cres.get("error"):
            if cres.get("order_gone") or cres.get("already_gone"):
                cancelled.append(oid)
                live = [o for o in live if _as_oid(o) != oid]
                continue
            errors.append(f"cancel {oid}: {cres.get('error')}")
            logger.error(
                "abort fuse: cancel %s failed: %s", oid, cres.get("error")
            )
            continue
        cancelled.append(oid)
        live = [o for o in live if _as_oid(o) != oid]

    remaining_ids = non_exit_working_ids(live, lots)
    try:
        get_orders = getattr(connector, "get_open_orders", None)
        if get_orders is not None:
            fresh = await get_orders()
            if isinstance(fresh, list):
                remaining_ids = non_exit_working_ids(fresh, lots)
    except Exception:
        logger.debug("abort fuse: refresh open orders failed", exc_info=True)

    remaining = len(remaining_ids)
    _latch["working_orders_after_abort"] = remaining
    if remaining:
        logger.error(
            "abort fuse %s: cancel_all invoked, working_orders_after_abort=%s "
            "(best-effort fail-closed)",
            token,
            remaining,
        )
    else:
        logger.warning(
            "abort fuse %s: cancel_all invoked, working_orders_after_abort=0",
            token,
        )
    return {
        "abort_fuse": token,
        "cancel_all_invoked": True,
        "working_orders_after_abort": remaining,
        "cancelled": cancelled,
        "errors": errors,
    }


async def maybe_apply_abort_fuse(
    connector: Any,
    *,
    abort_fuse: str | None = None,
    f10: dict[str, Any] | None = None,
    positions: list[Any] | None = None,
    open_orders: list[Any] | None = None,
) -> dict[str, Any]:
    """No-op unless a named abort fuse is latched. Exits are not this path."""
    try:
        fuse = resolve_abort_fuse(abort_fuse, f10=f10)
        if fuse not in ACTION_FUSES:
            return _idle_result()
        return await apply_abort_fuse_cancel(
            connector,
            fuse=fuse,
            positions=positions,
            open_orders=open_orders,
        )
    except Exception as exc:
        logger.exception("abort fuse cancel failed closed")
        _latch["cancel_all_invoked"] = True
        _latch["working_orders_after_abort"] = None
        return {
            "abort_fuse": str(abort_fuse or _latch.get("fuse") or "none"),
            "cancel_all_invoked": True,
            "working_orders_after_abort": None,
            "cancelled": [],
            "errors": [str(exc)],
        }
