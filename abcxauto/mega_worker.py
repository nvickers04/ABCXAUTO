"""Mega-worker portfolio: capacity Facts, stream selection, send merge.

Objective gates stay in risk/validate. This module allocates attention under
Controls dials (budget / frequency / deliberation) — no free-text work briefs.
"""

from __future__ import annotations

from typing import Any

from abcxauto.config import (
    FREQUENCY_ALLOW_NEW_RISK_PCT,
    ROTATION_THIN_CASH_PCT,
    _control_pct,
    get_config,
    rotation_redeploy_lean,
)


def safety_facts_broken(world: Any) -> bool:
    """True when open-risk Act must win over new-risk / escapade."""
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


def select_streams(
    judgment: dict,
    world: Any,
    *,
    cfg: Any = None,
    needs_prot: bool = False,
) -> list[str]:
    """Which Act streams to run this cycle (budget-capped; skip idle).

    Labels: open_risk | new_risk | escapade.
    Safety Facts → open_risk only. Halt/unprotected handled by caller gates.
    """
    c = cfg if cfg is not None else get_config()
    budget = _control_pct(c, "control_budget_pct")
    freq = _control_pct(c, "control_frequency_pct")
    delib = _control_pct(c, "control_deliberation_pct")
    stance = str((judgment or {}).get("stance") or "").lower()
    intent = (judgment or {}).get("intent") if isinstance((judgment or {}).get("intent"), dict) else {}
    kind = str(intent.get("kind") or stance).lower()
    secondary = (judgment or {}).get("secondary_intent")
    if not isinstance(secondary, dict):
        secondary = {}
    sec_kind = str(secondary.get("kind") or "").lower()

    if needs_prot or safety_facts_broken(world):
        return ["open_risk"]

    # Idle: at most one Act stream (hold). Cheap skip is `_should_skip_act`;
    # when S2 requires Act, still run a single pass — no new-risk/escapade.
    if stance in ("idle",) and kind in ("idle", "hold", ""):
        return ["open_risk"]

    streams: list[str] = []
    has_book = book_has_open_risk(world)
    want_open = has_book and (
        stance in ("protect", "manage")
        or kind in ("protect", "manage")
        or sec_kind in ("protect", "manage")
        or (stance == "hunt" and has_book)  # continuity while hunting
    )
    want_new = (
        capacity_allows_new_risk(world, c)
        and (
            stance == "hunt"
            or kind == "hunt"
            or sec_kind == "hunt"
            or (freq >= FREQUENCY_ALLOW_NEW_RISK_PCT and stance == "manage" and sec_kind == "hunt")
        )
    )
    # Escapade: deep parallel work — settings-only (high budget + freq + S2)
    want_esc = (
        capacity_allows_new_risk(world, c)
        and budget >= 70
        and freq >= 60
        and delib >= 60
        and stance in ("hunt", "manage")
        and not safety_facts_broken(world)
    )

    max_n = 1
    if budget >= 40:
        max_n = 2
    if budget >= 70 and delib >= 60:
        max_n = 3
    if freq < FREQUENCY_ALLOW_NEW_RISK_PCT:
        want_new = want_new and stance == "hunt"
        want_esc = False

    # Rotation lean + thin cash: keep open_risk so Act can trim/exit to free cash.
    if rotation_redeploy_lean(c) and cash_is_thin(world) and has_book:
        want_open = True

    if want_open:
        streams.append("open_risk")
    if want_new and "new_risk" not in streams:
        streams.append("new_risk")
    if want_esc and "escapade" not in streams:
        streams.append("escapade")

    # Single-stream fallback from stance when nothing selected
    if not streams:
        if stance in ("protect", "manage") or has_book:
            streams = ["open_risk"]
        elif stance == "hunt":
            streams = ["new_risk"] if capacity_allows_new_risk(world, c) else ["open_risk"]
        else:
            streams = []

    return streams[:max_n]


def merge_send_queue(
    candidates: list[dict[str, Any]],
    *,
    world: Any,
    judgment: dict | None = None,
) -> dict[str, Any] | None:
    """Pick one Act from stream candidates. Open-risk wins when safety broken."""
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
        # Priority: open_risk safety acts > open_risk > judgment-matching > new_risk > escapade
        pri = 50
        if stream == "open_risk":
            pri = 10 if safety else 20
        elif stream == "new_risk":
            pri = 40
        elif stream == "escapade":
            pri = 45
        stance = str((judgment or {}).get("stance") or "").lower()
        if stance == "hunt" and stream in ("new_risk", "escapade") and not safety:
            pri = 15
        if stance in ("manage", "protect") and stream == "open_risk":
            pri = 12
        if strat == "hold" and not safety:
            pri = 80  # prefer real work over hold when alternatives exist
        ranked.append((pri, c))
    if not ranked:
        # fall back to first candidate even if hold
        return dict(candidates[0])
    ranked.sort(key=lambda t: t[0])
    return dict(ranked[0][1])


def stream_act_prompt_suffix(
    stream: str, *, world: Any = None, cfg: Any = None
) -> str:
    c = cfg if cfg is not None else get_config()
    if stream == "open_risk":
        base = (
            "STREAM=open_risk: continuity on the existing book — "
            "manage/protect/exit/overlay. Prefer safety Facts when broken. "
            "Do not open a new unrelated STK name in this stream."
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
                    " Cash thin (Fact): do not force new risk until open_risk "
                    "frees capital, unless size fits remaining cash."
                )
        except Exception:
            pass
        return (
            "STREAM=new_risk: entry under capacity + exposure Fact. "
            "Respect CONTROLS trade_frequency, capital_rotation, and max_open_positions. "
            "Hunt structure from IBKR live last."
            + thin_note
        )
    if stream == "escapade":
        return (
            "STREAM=escapade: deep parallel workstream authorized by Controls dials "
            "(frequency/rotation/deliberation/budget) — no operator free-text brief. "
            "Propose a candidate structure within capacity; shell merges send queue. "
            "Do not invent edges outside CONTROLS Fact."
        )
    return f"STREAM={stream}"
