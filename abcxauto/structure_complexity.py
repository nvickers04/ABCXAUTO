"""Structure complexity dial → Act strategy allowlist (hard gate)."""

from __future__ import annotations

from typing import Any

# Always allowed (protect / manage / exits / hold).
_BASE = frozenset({
    "hold",
    "set_risk",
    "bracket",
    "market_bracket",
    "market_order",
    "limit_order",
    "stop_order",
    "stop_limit",
    "oca",
    "modify_stop",
    "modify_target",
    "cancel_order",
    "trailing_stop",
    "trailing_stop_limit",
    "market_on_close",
    "limit_on_close",
    "market_on_open",
    "limit_on_open",
    "close_option",
    "roll_option",
})

_MID = frozenset({
    "vertical_spread",
    "iron_condor",
    "iron_butterfly",
    "butterfly",
    "calendar_spread",
    "diagonal_spread",
    "buy_option",
    "cash_secured_put",
    "covered_call",
    "collar",
    "protective_put",
})

_FULL = frozenset({
    "straddle",
    "strangle",
    "ratio_spread",
    "jade_lizard",
})


def complexity_pct(cfg: Any = None) -> int:
    from abcxauto.config import _control_pct, get_config

    c = cfg if cfg is not None else get_config()
    # Prefer new key; fall back to legacy instruments dial.
    if hasattr(c, "control_complexity_pct"):
        try:
            raw = getattr(c, "control_complexity_pct", None)
            if raw is not None:
                return _control_pct(c, "control_complexity_pct")
        except Exception:
            pass
    return _control_pct(c, "control_options_pct")


def complexity_band(pct: int | None = None, cfg: Any = None) -> str:
    p = int(pct if pct is not None else complexity_pct(cfg))
    if p < 40:
        return "stock"
    if p < 70:
        return "defined"
    return "full"


def allowed_strategies(pct: int | None = None, cfg: Any = None) -> frozenset[str]:
    band = complexity_band(pct, cfg)
    if band == "stock":
        return _BASE
    if band == "defined":
        return _BASE | _MID
    return _BASE | _MID | _FULL


def strategy_allowed(strategy: str, cfg: Any = None) -> bool:
    strat = str(strategy or "").strip().lower()
    if not strat or strat == "blocked":
        return True
    return strat in allowed_strategies(cfg=cfg)


def complexity_fact(cfg: Any = None) -> str:
    p = complexity_pct(cfg)
    band = complexity_band(p, cfg)
    labels = {
        "stock": "stock brackets/exits only",
        "defined": "stock + defined-risk options/overlays",
        "full": "full multi-leg toolbox (Risk defined_risk_only still applies)",
    }
    return f"structure_complexity={p} band={band} ({labels[band]})"
