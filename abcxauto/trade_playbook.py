"""TRADE PLAYBOOK — preconditions + shell rejects only (no strategy narrative).

ORDER EXAMPLES teach how to send; this module states book facts and what the
shell will block. Taste belongs in Operator Card or Grok judgment.
"""

from __future__ import annotations

from typing import Any

# reason codes for overlay share guards (structure lessons)
OVERLAY_SHARES_INSUFFICIENT = "overlay_shares_insufficient"
OVERLAY_NO_LONG_STOCK = "overlay_no_long_stock"

_PLAYBOOK: list[dict[str, Any]] = [
    {
        "types": ("market_bracket", "bracket"),
        "stances": frozenset({"hunt"}),
        "precondition": (
            "stance=hunt; capacity slots remain (max_open_positions); "
            "not unprotected; flat confirmed when exiting flat"
        ),
        "shell_reject": "capacity full, unprotected, flat unconfirmed, structure cooldown, bad geometry",
        "needs_long_lot": False,
    },
    {
        "types": (
            "vertical_spread",
            "iron_condor",
            "iron_butterfly",
            "butterfly",
            "calendar_spread",
            "diagonal_spread",
            "buy_option",
            "cash_secured_put",
            "straddle",
            "strangle",
            "ratio_spread",
            "jade_lizard",
        ),
        "stances": frozenset({"hunt", "manage"}),
        "precondition": (
            "hunt: new-risk under capacity + CONTROLS; "
            "manage: may add option structure on open book; "
            "params must match Act order schema"
        ),
        "shell_reject": (
            "stance allowlist; capacity / unprotected / flat-unconfirmed; "
            "defined_risk_only rejects unlimited-risk shapes "
            "(ratio_spread, jade_lizard, short straddle/strangle)"
        ),
        "needs_long_lot": False,
    },
    {
        "types": ("oca",),
        "stances": frozenset({"protect"}),
        "precondition": "unprotected STK in book",
        "shell_reject": "stance allowlist; invalid stop/target geometry",
        "needs_long_lot": False,
    },
    {
        "types": ("trailing_stop", "modify_stop"),
        "stances": frozenset({"manage"}),
        "precondition": "open plan or working stop order exists",
        "shell_reject": "stop wrong-side of live/fill (geometry / scrape codes)",
        "needs_long_lot": False,
    },
    {
        "types": ("market_order", "limit_order"),
        "stances": frozenset({"manage", "protect"}),
        "precondition": (
            "exit by target_conId; quantity may be partial trim "
            "(omit quantity = full close); closing_position required"
        ),
        "shell_reject": (
            "inventory/conId gate; qty > held; after partial trim "
            "stop_order_qty may mismatch held (Fact in WORLD)"
        ),
        "needs_long_lot": False,
    },
    {
        "types": ("roll_option", "close_option"),
        "stances": frozenset({"manage", "protect"}),
        "precondition": (
            "open OPT leg in book; close_option prefers conId "
            "(or expiration+strike+right); quantity may be partial"
        ),
        "shell_reject": "stance allowlist; close_option must match live option legs",
        "needs_long_lot": False,
    },
    {
        "types": ("covered_call",),
        "stances": frozenset({"manage"}),
        "precondition": "long STK ≥100 shares on the named symbol",
        "shell_reject": "overlay_shares_insufficient / overlay_no_long_stock",
        "needs_long_lot": True,
    },
    {
        "types": ("protective_put",),
        "stances": frozenset({"manage", "protect"}),
        "precondition": (
            "long STK ≥100 shares; also used to add put wing when short call "
            "already on (collar conversion path — Grok chooses)"
        ),
        "shell_reject": "overlay_shares_insufficient / overlay_no_long_stock",
        "needs_long_lot": True,
    },
    {
        "types": ("collar",),
        "stances": frozenset({"manage"}),
        "precondition": "long STK ≥100 shares on the named symbol",
        "shell_reject": "overlay_shares_insufficient / overlay_no_long_stock",
        "needs_long_lot": True,
    },
    {
        "types": ("hold",),
        "stances": frozenset({"manage", "idle"}),
        "precondition": "book protected or flat; no gate forcing protect",
        "shell_reject": "hold forbidden while unprotected STK (code)",
        "needs_long_lot": False,
    },
]


def long_share_lots(positions: list[dict] | None) -> dict[str, float]:
    """Symbol → long STK/ETF share quantity (shorts ignored)."""
    lots: dict[str, float] = {}
    for p in positions or []:
        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
        if sec not in ("STK", "ETF", ""):
            continue
        try:
            qty = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        lots[sym] = lots.get(sym, 0.0) + qty
    return lots


def world_hints_from_world(world: Any) -> dict[str, Any]:
    """Build filter hints from a WorldState-like object or dict."""
    if world is None:
        return {
            "flat": True,
            "needs_protection": False,
            "long_lots": {},
            "has_trade_plan": False,
        }
    if isinstance(world, dict):
        positions = world.get("positions") or []
        return {
            "flat": bool(world.get("flat", not positions)),
            "needs_protection": bool(world.get("needs_protection")),
            "long_lots": long_share_lots(positions),
            "has_trade_plan": bool(world.get("trade_plan")),
        }
    positions = getattr(world, "positions", None) or []
    return {
        "flat": bool(getattr(world, "flat", not positions)),
        "needs_protection": bool(getattr(world, "needs_protection", False)),
        "long_lots": long_share_lots(positions),
        "has_trade_plan": bool(getattr(world, "trade_plan", None)),
    }


def max_long_shares(long_lots: dict[str, float] | None) -> float:
    if not long_lots:
        return 0.0
    return max(long_lots.values())


def format_trade_playbook(
    stance: str,
    world_hints: dict[str, Any] | None = None,
    *,
    for_judge: bool = False,
) -> str:
    """Stance + book filtered preconditions / shell rejects for prompts."""
    stance = (stance or "").strip().lower()
    hints = dict(world_hints or {})
    lots = hints.get("long_lots") if isinstance(hints.get("long_lots"), dict) else {}
    lot_100 = sorted((s, q) for s, q in lots.items() if float(q) >= 100)

    stances = {stance} if stance and not for_judge else None
    if for_judge:
        if hints.get("needs_protection"):
            stances = {"protect"}
        elif hints.get("has_trade_plan") or not hints.get("flat", True):
            # Multitask: open book + capacity → manage/protect and hunt
            stances = {"manage", "protect", "hunt"}
        elif hints.get("flat", True):
            stances = {"hunt", "idle"}
        else:
            stances = {"manage", "hunt", "idle", "protect"}

    if for_judge:
        header = (
            "TRADE PLAYBOOK (preconditions + shell rejects — frame intent; "
            "Act picks send shape)"
        )
    else:
        header = (
            "TRADE PLAYBOOK (preconditions + shell rejects only; "
            "see ORDER EXAMPLES for how)"
        )
    lines = [header]
    shown = 0
    for row in _PLAYBOOK:
        if stances is not None and not (row["stances"] & stances):
            continue
        if row.get("needs_long_lot"):
            if not lot_100:
                continue
            eligible = ", ".join(f"{s} x{q:g}" for s, q in lot_100[:4])
            types = "/".join(row["types"])
            lines.append(
                f"- {types} on [{eligible}]: "
                f"Precondition: {row['precondition']} | "
                f"Shell reject: {row['shell_reject']}"
            )
            shown += 1
            continue
        types = "/".join(row["types"])
        lines.append(
            f"- {types}: Precondition: {row['precondition']} | "
            f"Shell reject: {row['shell_reject']}"
        )
        shown += 1
    if shown == 0:
        lines.append(
            "- hold: Precondition: protected or flat | "
            "Shell reject: unprotected STK (code)"
        )
    if lots:
        lot_bits = ", ".join(f"{s} x{q:g}" for s, q in sorted(lots.items())[:6])
        lines.append(f"Book long lots: {lot_bits}")
        under = [f"{s} x{q:g}" for s, q in sorted(lots.items()) if float(q) < 100]
        if under:
            lines.append(
                f"Overlays need ≥100 shares (schema); under-lot: {', '.join(under[:4])}"
            )
    return "\n".join(lines)


def check_overlay_shares(
    strategy: str,
    params: dict | None,
    positions: list[dict] | None,
) -> tuple[bool, str, str]:
    """Validate covered_call/collar/protective_put against long stock.

    Returns (ok, reason_code, message).
    """
    strat = (strategy or "").strip().lower()
    if strat not in ("covered_call", "collar", "protective_put"):
        return True, "ok", "n/a"
    params = params or {}
    sym = str(params.get("symbol") or "").upper()
    lots = long_share_lots(positions)
    if not sym:
        return False, OVERLAY_NO_LONG_STOCK, f"{strat} requires symbol with long stock"
    have = float(lots.get(sym) or 0)
    if have <= 0:
        return (
            False,
            OVERLAY_NO_LONG_STOCK,
            f"{strat} on {sym}: no long STK shares in book",
        )
    try:
        need = float(params.get("shares") or 100)
    except (TypeError, ValueError):
        need = 100.0
    if need <= 0:
        need = 100.0
    if have + 1e-9 < need:
        return (
            False,
            OVERLAY_SHARES_INSUFFICIENT,
            f"{strat} on {sym}: need {need:g} shares, book has {have:g}",
        )
    return True, "ok", "shares ok"
