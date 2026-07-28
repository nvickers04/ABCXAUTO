"""Cycle attention helpers: safety Facts, capacity, primary focus stream.

Not a decision tree. One cycle = one Act. ``primary_stream`` only labels the
Act prompt focus (open book vs new entry). Risk gates still own capital survival.
"""

from __future__ import annotations

from typing import Any

from abcxauto.config import (
    ROTATION_THIN_CASH_PCT,
    get_config,
    rotation_redeploy_lean,
)


def safety_facts_broken(world: Any) -> bool:
    """True when protect/manage on open book must dominate new entries."""
    if bool(getattr(world, "needs_protection", False)):
        return True
    if list(getattr(world, "unprotected", None) or []):
        return True
    stop = getattr(world, "stop_qty_fact", None) or {}
    if isinstance(stop, dict) and stop.get("mismatch"):
        return True
    return False


def capacity_allows_new_risk(world: Any, cfg: Any = None) -> bool:
    cap = getattr(world, "capacity", None) or {}
    if isinstance(cap, dict) and "allows_new_risk" in cap:
        return bool(cap.get("allows_new_risk"))
    c = cfg if cfg is not None else get_config()
    try:
        max_n = int(getattr(c, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        max_n = 0
    if max_n <= 0:
        return True
    from abcxauto.trade_plan import open_position_count

    return open_position_count(getattr(world, "positions", None)) < max_n


def cash_is_thin(world: Any) -> bool:
    """Fact: cash share of NL below Controls rotation threshold."""
    pr = getattr(world, "portfolio_risk", None) or {}
    liq = pr.get("capital_liquidity") if isinstance(pr, dict) else None
    if isinstance(liq, dict):
        if "cash_thin" in liq:
            return bool(liq.get("cash_thin"))
        try:
            return float(liq.get("cash_pct_nl") or 0) < float(ROTATION_THIN_CASH_PCT)
        except (TypeError, ValueError):
            return False
    return False


def book_has_open_risk(world: Any) -> bool:
    if getattr(world, "trade_plan", None):
        return True
    plans = getattr(world, "trade_plans", None) or []
    if plans:
        return True
    try:
        from abcxauto.trade_plan import book_has_risk

        return book_has_risk(getattr(world, "positions", None))
    except Exception:
        return bool(getattr(world, "positions", None))


def primary_stream(
    judgment: dict,
    world: Any,
    *,
    cfg: Any = None,
    needs_prot: bool = False,
) -> str:
    """Single Act focus for this cycle — not a multi-branch allocator.

    open_risk  = work the book (protect / manage / exit / overlay)
    new_risk   = consider entry under capacity

    Stance wins. secondary_intent does not open a second stream (tree retired).
    """
    c = cfg if cfg is not None else get_config()
    stance = str((judgment or {}).get("stance") or "").lower()
    intent = (judgment or {}).get("intent") if isinstance((judgment or {}).get("intent"), dict) else {}
    kind = str(intent.get("kind") or stance).lower()

    if needs_prot or safety_facts_broken(world):
        return "open_risk"

    if stance == "hunt" or kind == "hunt":
        if capacity_allows_new_risk(world, c):
            return "new_risk"
        return "open_risk"

    if stance in ("protect", "manage") or kind in ("protect", "manage"):
        return "open_risk"

    # idle / default: still one Act; focus open book if any, else new_risk capacity
    if book_has_open_risk(world):
        return "open_risk"
    if capacity_allows_new_risk(world, c):
        return "new_risk"
    return "open_risk"


def select_streams(
    judgment: dict,
    world: Any,
    *,
    cfg: Any = None,
    needs_prot: bool = False,
) -> list[str]:
    """Compat: always one stream. Multi-stream merge trees are retired."""
    return [primary_stream(judgment, world, cfg=cfg, needs_prot=needs_prot)]


def merge_send_queue(
    candidates: list[dict[str, Any]],
    *,
    world: Any,
    judgment: dict | None = None,
) -> dict[str, Any] | None:
    """Pick one Act if multiple candidates exist (tests / legacy). Prefer safety."""
    if not candidates:
        return None
    safety = safety_facts_broken(world)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        stream = str(c.get("_stream") or "")
        strat = str(c.get("strategy") or c.get("action") or "").lower()
        if strat in ("", "blocked"):
            continue
        if safety and stream != "open_risk" and strat not in (
            "oca",
            "modify_stop",
            "modify_target",
            "protective_put",
            "market_order",
            "close_option",
        ):
            continue
        pri = 50
        if stream == "open_risk":
            pri = 10 if safety else 20
        elif stream == "new_risk":
            pri = 40
        stance = str((judgment or {}).get("stance") or "").lower()
        if stance == "hunt" and stream == "new_risk" and not safety:
            pri = 15
        if stance in ("manage", "protect") and stream == "open_risk":
            pri = 12
        if strat == "hold" and not safety:
            pri = 80
        ranked.append((pri, c))
    if not ranked:
        return dict(candidates[0])
    ranked.sort(key=lambda t: t[0])
    return dict(ranked[0][1])


def stream_act_prompt_suffix(
    stream: str, *, world: Any = None, cfg: Any = None
) -> str:
    c = cfg if cfg is not None else get_config()
    if stream == "open_risk":
        base = (
            "FOCUS=open_book: manage/protect/exit/overlay on existing risk. "
            "Prefer safety Facts when broken. "
            "Do not open a new unrelated STK name unless Judgment clearly hunts "
            "and capacity allows (then use new_risk focus next cycle)."
        )
        try:
            if rotation_redeploy_lean(c) and world is not None and cash_is_thin(world):
                return (
                    base
                    + " CONTROLS capital_rotation high + cash thin (Fact): "
                    "trim/exit/rotate to free cash is operator-authorized — "
                    "prefer a real exit/trim over idle hold when a better setup "
                    "needs room; shell does not auto-sell."
                )
        except Exception:
            pass
        return base
    if stream == "new_risk":
        thin_note = ""
        try:
            if rotation_redeploy_lean(c) and world is not None and cash_is_thin(world):
                thin_note = (
                    " Cash thin (Fact): size to remaining cash or hold; "
                    "do not force oversized entry."
                )
        except Exception:
            pass
        return (
            "FOCUS=new_entry: propose one structure under capacity + exposure Fact. "
            "Respect CONTROLS max_open_positions and Risk $. "
            "Hunt structure from IBKR live last — not MDA tape last."
            + thin_note
        )
    if stream == "escapade":
        return stream_act_prompt_suffix("new_risk", world=world, cfg=cfg)
    return f"FOCUS={stream}"
