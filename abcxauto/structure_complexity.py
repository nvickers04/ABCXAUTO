"""Controls: entry surface (restrict) + option complexity (add) → Act allowlist."""

from __future__ import annotations

from typing import Any

# Always allowed (protect / manage / exits / hold) — never gated by entry surface.
_ALWAYS = frozenset({
    "hold",
    "set_risk",
    "self_tune",
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

# New stock risk — gated by entry surface (stock | mixed).
_STOCK_ENTRIES = frozenset({
    "bracket",
    "market_bracket",
})

# Defined-risk option / overlay entries — gated by entry surface + complexity.
_OPTION_DEFINED = frozenset({
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

# Exotic / unlimited-risk-capable shapes — complexity full; Risk defined_risk_only
# may still reject naked variants.
_OPTION_FULL = frozenset({
    "straddle",
    "strangle",
    "ratio_spread",
    "jade_lizard",
})


def entry_surface_pct(cfg: Any = None) -> int:
    from abcxauto.config import _control_pct, get_config

    c = cfg if cfg is not None else get_config()
    return _control_pct(c, "control_entry_surface_pct")


def entry_surface_band(pct: int | None = None, cfg: Any = None) -> str:
    """stock | mixed | options — what *new entries* may use."""
    p = int(pct if pct is not None else entry_surface_pct(cfg))
    if p < 40:
        return "stock"
    if p < 70:
        return "mixed"
    return "options"


def complexity_pct(cfg: Any = None) -> int:
    from abcxauto.config import _control_pct, get_config

    c = cfg if cfg is not None else get_config()
    if hasattr(c, "control_complexity_pct"):
        try:
            raw = getattr(c, "control_complexity_pct", None)
            if raw is not None:
                return _control_pct(c, "control_complexity_pct")
        except Exception:
            pass
    return _control_pct(c, "control_options_pct")


def complexity_band(pct: int | None = None, cfg: Any = None) -> str:
    """defined | full — option toolbox depth (ignored when entry surface is stock)."""
    p = int(pct if pct is not None else complexity_pct(cfg))
    if p < 70:
        return "defined"
    return "full"


def option_strategies(pct: int | None = None, cfg: Any = None) -> frozenset[str]:
    band = complexity_band(pct, cfg)
    if band == "full":
        return _OPTION_DEFINED | _OPTION_FULL
    return frozenset(_OPTION_DEFINED)


def allowed_strategies(
    *,
    entry_pct: int | None = None,
    complexity: int | None = None,
    cfg: Any = None,
) -> frozenset[str]:
    surface = entry_surface_band(entry_pct, cfg)
    allowed = set(_ALWAYS)
    if surface in ("stock", "mixed"):
        allowed |= _STOCK_ENTRIES
    if surface in ("mixed", "options"):
        allowed |= option_strategies(complexity, cfg)
    return frozenset(allowed)


def strategy_allowed(strategy: str, cfg: Any = None) -> bool:
    strat = str(strategy or "").strip().lower()
    if not strat or strat == "blocked":
        return True
    return strat in allowed_strategies(cfg=cfg)


def reject_reason(strategy: str, cfg: Any = None) -> str | None:
    """Human reason when strategy_allowed is False."""
    strat = str(strategy or "").strip().lower()
    if not strat or strategy_allowed(strat, cfg=cfg):
        return None
    surface = entry_surface_band(cfg=cfg)
    if strat in _STOCK_ENTRIES and surface == "options":
        return (
            f"entry_surface={surface} blocks stock entry {strat!r} "
            "(turn Entry surface toward stock/mixed, or use an option structure)"
        )
    if strat in (_OPTION_DEFINED | _OPTION_FULL) and surface == "stock":
        return (
            f"entry_surface={surface} blocks option entry {strat!r} "
            "(turn Entry surface toward mixed/options)"
        )
    if strat in _OPTION_FULL and complexity_band(cfg=cfg) == "defined":
        return (
            f"option_complexity=defined blocks {strat!r} "
            "(raise Option complexity toward full multi-leg)"
        )
    return f"Controls allowlist blocks strategy {strat!r}"


def entry_surface_fact(cfg: Any = None) -> str:
    p = entry_surface_pct(cfg)
    band = entry_surface_band(p, cfg)
    labels = {
        "stock": "new entries: stock brackets only",
        "mixed": "new entries: stock + options",
        "options": "new entries: options only (no stock brackets)",
    }
    return f"entry_surface={p} band={band} ({labels[band]})"


def complexity_fact(cfg: Any = None) -> str:
    """Option toolbox Fact — separate from entry surface."""
    p = complexity_pct(cfg)
    band = complexity_band(p, cfg)
    surface = entry_surface_band(cfg=cfg)
    labels = {
        "defined": "defined-risk options/overlays",
        "full": "full multi-leg toolbox (Risk defined_risk_only still applies)",
    }
    note = labels[band]
    if surface == "stock":
        note += "; unused while entry_surface=stock"
    return f"option_complexity={p} band={band} ({note})"
