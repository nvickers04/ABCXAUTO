"""Explore/exploit is a MODE BIT that sizes, not a personality label.

Grok still chooses ``size_pct_nl``. This module is the floor/ceiling that
choice is clamped to — on send (``apply_size_pct_nl``) and via ``self_tune``.
It runs even when paper risk gates are off. 25% is the live walk-away
ceiling for the risk knobs, not the working size.
"""

from __future__ import annotations

import math
from typing import Any

# Single-digit % of NL. Not 1. Not 25. Not a working size — a ceiling
# Grok may tighten. Do not copy this number into SYSTEM_PROMPT.
MODE_SIZE_CEILING_EXPLORE = 8.0
MODE_SIZE_FLOOR = 0.25
SIZE_PCT_NL_KEY = "size_pct_nl"


def _pos_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def playbook_mode() -> str:
    """explore | exploit. Persist is gone; explore is the size band."""
    return "explore"


def graduated_names(book: dict[str, Any] | None = None) -> list[str]:
    _ = book
    return []


def _norm_card(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if " [" in raw:
        raw = raw.split(" [", 1)[0].strip()
    return raw


def card_is_graduated(
    card: Any,
    *,
    type: str = "",
    book: dict[str, Any] | None = None,
) -> bool:
    _ = (card, type, book)
    return False


def card_is_learning(
    card: Any,
    *,
    type: str = "",
    book: dict[str, Any] | None = None,
) -> bool:
    """A named card that has not graduated. Empty name is not a card."""
    if not str(card or "").strip():
        return False
    return not card_is_graduated(card, type=type, book=book)


def live_marks_match_paper() -> bool:
    """Paper 7497 never qualifies. No live snapshot without persist."""
    return False


def exploit_may_widen() -> bool:
    """Exploit widen needed graduated cards. Persist is gone."""
    return False


def mode_size_ceiling(
    *,
    card: Any = None,
    type: str = "",
    mode: str | None = None,
) -> float:
    """Hard % NL ceiling for this mode/card. Grok may tighten, not raise.

    Explore and unproven exploit stay single-digit. A learning card never
    inherits a wider band. Exploit plus graduated cards opens the
    walk-away band — it does not set 25% as the working size.
    """
    bit = str(mode or playbook_mode() or "explore").strip().lower()
    if bit not in ("explore", "exploit"):
        bit = "explore"
    explore_hi = MODE_SIZE_CEILING_EXPLORE
    if card_is_learning(card, type=type):
        return explore_hi
    if bit != "exploit":
        return explore_hi
    if not graduated_names():
        return explore_hi
    try:
        from abcxauto.self_tune import RISK_FLOOR

        return float(RISK_FLOOR["max_risk_per_trade_pct"][1])
    except Exception:
        return explore_hi


def tuned_size_pct_nl() -> float | None:
    """Grok-tightened working ceiling, if any. Never a baked 1/25 default."""
    try:
        from abcxauto.self_tune import load_agent_state

        raw = load_agent_state().get(SIZE_PCT_NL_KEY)
    except Exception:
        return None
    return _pos_float(raw)


def working_size_ceiling(
    *,
    card: Any = None,
    type: str = "",
    mode: str | None = None,
) -> float:
    """Ceiling that actually sizes a send. Not 25% unless Grok widened it."""
    hi = mode_size_ceiling(card=card, type=type, mode=mode)
    default = MODE_SIZE_CEILING_EXPLORE
    tuned = tuned_size_pct_nl()
    cap = default if tuned is None else tuned
    return max(MODE_SIZE_FLOOR, min(hi, cap))


def clamp_size_pct_nl(
    value: Any,
    *,
    card: Any = None,
    type: str = "",
    mode: str | None = None,
) -> tuple[float | None, dict[str, Any] | None]:
    """Clamp a % of NL into the mode band. None if unusable."""
    raw = _pos_float(value)
    if raw is None:
        return None, None
    lo = MODE_SIZE_FLOOR
    hi = working_size_ceiling(card=card, type=type, mode=mode)
    clamped = max(lo, min(hi, raw))
    note = {"raw": raw, "clamped": clamped} if clamped != raw else None
    return clamped, note


def implied_size_pct_nl(
    qty: Any,
    net_liq: Any,
    price: Any,
    *,
    multiplier: float = 1.0,
) -> float | None:
    """% of current NL implied by a live qty × price. None if unusable."""
    q = _pos_float(qty)
    nl = _pos_float(net_liq)
    px = _pos_float(price)
    try:
        mult = float(multiplier)
    except (TypeError, ValueError):
        return None
    if q is None or nl is None or px is None:
        return None
    if not math.isfinite(mult) or mult <= 0 or nl <= 0:
        return None
    return 100.0 * q * px * mult / nl


def mode_size_band(
    *,
    card: Any = None,
    type: str = "",
) -> dict[str, Any]:
    """Facts for book/status — not a lecture and not a SYSTEM_PROMPT number."""
    mode = playbook_mode()
    hi = working_size_ceiling(card=card, type=type, mode=mode)
    widen = exploit_may_widen()
    return {
        "mode": mode,
        "min": MODE_SIZE_FLOOR,
        "max": hi,
        "now": tuned_size_pct_nl() if tuned_size_pct_nl() is not None else hi,
        "unit": "pct_nl",
        "widen": widen,
        "cards": "many" if mode == "explore" else "graduated",
        "life": "short" if mode == "explore" else "held",
        "with": "max_open_positions",
        "change": "self_tune",
    }


def exploit_learning_card_error(
    card: Any,
    *,
    type: str = "",
    book: dict[str, Any] | None = None,
) -> str:
    """Graduation persist is gone. Empty string means this ticket may go."""
    _ = (card, type, book)
    return ""


def mode_size_ticket_error(
    params: dict[str, Any] | None,
    *,
    net_liq: Any = None,
    price: Any = None,
    strategy: str = "",
) -> str:
    """Reject if the ticket is still over the mode ceiling after clamp."""
    p = params if isinstance(params, dict) else {}
    card = p.get("card")
    ceiling = working_size_ceiling(card=card, type=strategy)
    pct = _pos_float(p.get(SIZE_PCT_NL_KEY))
    if pct is not None and pct > ceiling + 1e-6:
        return f"mode_size {pct} > {ceiling}"
    try:
        from abcxauto.send import _option_multiplier
    except Exception:
        def _option_multiplier(_s, _p):
            return 1.0

    implied = implied_size_pct_nl(
        p.get("quantity"),
        net_liq,
        price,
        multiplier=_option_multiplier(strategy, p),
    )
    if implied is not None and implied > ceiling + 1e-6:
        return f"mode_size {implied} > {ceiling}"
    return ""
