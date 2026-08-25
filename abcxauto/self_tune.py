"""Agent self-modification — live knobs only, no human approval.

Immutable floor (code): daily-loss halt, max position size, max open positions,
defined-risk, cash-only, auto-panic, fail-closed, exits never blocked, live gated.
The agent may *tighten* risk. It cannot weaken the immutable risk floor.
Cadence is wake_bus; process % dials do not exist.
"""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Risk knobs: agent may set within [min, max]. max is the immutable ceiling.
# Percents of NetLiq — same at $1k, $100k, or $1M.
RISK_FLOOR: dict[str, tuple[float, float]] = {
    "daily_loss_limit_pct": (0.5, 25.0),
    "max_position_pct": (5.0, 25.0),
    "max_risk_per_trade_pct": (0.25, 25.0),
    "max_peak_drawdown_pct": (2.0, 25.0),
    "max_option_premium_pct": (1.0, 25.0),
    "max_symbol_concentration_pct": (5.0, 25.0),
}
# Integer capacity: 0 would disable the gate — forbidden. Grok sets N.
MAX_OPEN_POSITIONS_RANGE = (1, 25)

# Booleans the agent cannot turn off.
LOCKED_TRUE: frozenset[str] = frozenset({
    "risk_gates_enabled",
    "auto_panic_on_breach",
    "defined_risk_only",
    "cash_only",
})
# Mode/port are operator-only. Extra keys here are rejects, not knobs.
_LIVE_GATED: frozenset[str] = frozenset({
    "trading_mode",
    "live_confirm",
    "ibkr_port",
})

SCAN_FETCH_CAP_RANGE = (1, 8)

# Unsupervised defaults — walk-away % of the full book.
UNSUPERVISED_DEFAULTS: dict[str, Any] = {
    "trading_budget_usd": 0.0,
    "risk_posture": "defensive",
    "risk_gates_enabled": True,
    "auto_panic_on_breach": True,
    "defined_risk_only": True,
    "cash_only": True,
    "daily_loss_limit_pct": 25.0,
    "max_position_pct": 25.0,
    "max_risk_per_trade_pct": 25.0,
    "max_peak_drawdown_pct": 25.0,
    "max_option_premium_pct": 25.0,
    "max_symbol_concentration_pct": 25.0,
    "max_open_positions": 15,
    "scan_fetch_cap": 8,
}

_SELF_TUNE_ALIASES = frozenset({
    "self_tune", "set_risk",
})


def is_self_tune_strategy(name: str) -> bool:
    return str(name or "").strip().lower() in _SELF_TUNE_ALIASES


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _i(value: Any) -> int | None:
    raw = _f(value)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return None


def clamp_risk_to_floor(key: str, value: Any) -> tuple[Any, dict[str, Any] | None]:
    """Clamp a risk knob so it cannot weaken the immutable floor."""
    if key == "max_open_positions":
        n = _i(value)
        if n is None:
            return None, None
        lo, hi = MAX_OPEN_POSITIONS_RANGE
        clamped = max(lo, min(hi, n))
        note = {"raw": n, "clamped": clamped} if clamped != n else None
        return clamped, note
    if key not in RISK_FLOOR:
        return None, None
    raw = _f(value)
    if raw is None:
        return None, None
    lo, hi = RISK_FLOOR[key]
    clamped = max(lo, min(hi, raw))
    note = {"raw": raw, "clamped": clamped} if clamped != raw else None
    return clamped, note


def _flatten_params(params: dict[str, Any]) -> dict[str, Any]:
    """Accept nested self_tune payload or flat set_risk-style knobs."""
    out: dict[str, Any] = {}
    if not isinstance(params, dict):
        return out
    nested_keys = ("risk", "universe")
    for nk in nested_keys:
        blob = params.get(nk)
        if isinstance(blob, dict):
            for k, v in blob.items():
                out[k] = v
    for k, v in params.items():
        if k in nested_keys:
            continue
        out[k] = v
    return out


def apply_self_tune(
    params: dict[str, Any] | None,
    *,
    persist: bool = True,
    rationale: str = "",
) -> dict[str, Any]:
    """Apply agent self-modification. Never weakens the immutable floor.

    No operator approval. Returns a result dict suitable for executor/journal.
    """
    from abcxauto.config import (
        get_config,
        update_capacity_config,
        update_risk_config,
    )

    raw = dict(params or {})
    flat = _flatten_params(raw)
    if not flat:
        return {
            "status": "blocked",
            "note": "self_tune: no valid knobs in params",
            "strategy": "self_tune",
        }

    applied: dict[str, Any] = {}
    clamped: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    cfg = get_config()
    before: dict[str, Any] = {}

    risk_payload: dict[str, Any] = {}
    controls_payload: dict[str, Any] = {}
    scan_cap: int | None = None
    universe_payload: dict[str, Any] = {}

    for key, value in flat.items():
        if key in LOCKED_TRUE:
            if value in (False, 0, "0", "false", "False", "off", "no"):
                rejected[key] = "immutable floor — cannot disable"
            continue
        if key == "risk_posture":
            rejected[key] = "operator-only — agent cannot retune risk_posture"
            continue
        if key in _LIVE_GATED:
            rejected[key] = "live remains gated — agent cannot switch mode"
            continue
        if key == "sizing_floors":
            rejected[key] = "operator-only — agent cannot flip sizing floors"
            continue
        if key == "trading_budget_usd":
            rejected[key] = "size and risk are % of NetLiq — no dollar sleeve"
            continue
        if key in RISK_FLOOR or key == "max_open_positions":
            before[key] = getattr(cfg, key, None)
            new_v, note = clamp_risk_to_floor(key, value)
            if new_v is None:
                rejected[key] = "invalid value"
                continue
            hi = (
                MAX_OPEN_POSITIONS_RANGE[1]
                if key == "max_open_positions"
                else RISK_FLOOR[key][1]
            )
            try:
                as_f = float(new_v)
            except (TypeError, ValueError):
                rejected[key] = "invalid value"
                continue
            if not math.isfinite(as_f):
                rejected[key] = "invalid value"
                continue
            if as_f > hi:
                note = {"raw": value, "clamped": hi}
                new_v = int(hi) if key == "max_open_positions" else float(hi)
            if note:
                clamped[key] = note
            if key == "max_open_positions":
                controls_payload[key] = new_v
            else:
                risk_payload[key] = new_v
            continue
        if key == "scan_fetch_cap":
            before[key] = getattr(cfg, "scan_fetch_cap", None)
            n = _i(value)
            if n is None:
                rejected[key] = "invalid value"
                continue
            lo, hi = SCAN_FETCH_CAP_RANGE
            scan_cap = max(lo, min(hi, n))
            if scan_cap != n:
                clamped[key] = {"raw": n, "clamped": scan_cap}
            continue
        if key in ("enabled_arenas", "custom_symbols", "exclude_symbols"):
            universe_payload[key] = value
            continue
        rejected[key] = "unknown or not agent-tunable"

    if isinstance(raw.get("universe"), dict):
        universe_payload.update({
            k: v for k, v in raw["universe"].items()
            if k in ("enabled_arenas", "custom_symbols", "exclude_symbols")
        })

    persist_kw = {"persist": persist}

    if risk_payload:
        # Skip posture envelope clamp — floor already applied.
        update_risk_config(**risk_payload, **persist_kw, _skip_clamp=True)
        applied.update(risk_payload)
    if controls_payload:
        update_capacity_config(**controls_payload, **persist_kw)
        applied.update(controls_payload)
    if scan_cap is not None:
        from abcxauto.config import _runtime_overrides

        extra = {"scan_fetch_cap": scan_cap}
        _runtime_overrides.update(extra)
        if persist:
            try:
                _persist_agent_state(extra)
            except Exception:
                logger.exception("self_tune persist extra failed")
        applied.update(extra)

    if universe_payload:
        try:
            from abcxauto.universe import load_allowlist, save_allowlist

            cur = load_allowlist()
            save_allowlist({**cur, **universe_payload})
            applied["universe"] = universe_payload
        except Exception as exc:
            logger.exception("self_tune universe failed")
            rejected["universe"] = str(exc)

    if not applied:
        return {
            "status": "blocked",
            "note": "self_tune: nothing applied",
            "strategy": "self_tune",
            "rejected": rejected,
            "before": before,
        }

    try:
        from abcxauto.memory import get_journal

        get_journal().record_self_tune(
            applied=applied,
            clamped=clamped,
            rejected=rejected,
            rationale=rationale,
        )
    except Exception:
        logger.debug("journal self_tune record failed", exc_info=True)

    after = {k: getattr(get_config(), k, None) for k in list(applied) if k != "universe"}
    return {
        "status": "ok",
        "strategy": "self_tune",
        "applied": applied,
        "clamped": clamped,
        "rejected": rejected,
        "before": before,
        "after": after,
        "note": "self_tune applied (floor cannot be weakened)",
    }


def _persist_agent_state(extra: dict[str, Any]) -> None:
    """Persist scan_fetch_cap beside risk_settings (agent-owned)."""
    import json
    from pathlib import Path

    from abcxauto.config import _REPO_ROOT

    path = Path(
        __import__("os").environ.get("ABCXAUTO_AGENT_STATE_PATH")
        or str(_REPO_ROOT / "agent_state.json")
    )
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                current = {}
        except Exception:
            current = {}
    current.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_agent_state() -> dict[str, Any]:
    import json
    from pathlib import Path

    from abcxauto.config import _REPO_ROOT

    path = Path(
        __import__("os").environ.get("ABCXAUTO_AGENT_STATE_PATH")
        or str(_REPO_ROOT / "agent_state.json")
    )
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def floor_clamp_config_fields(cfg: Any) -> dict[str, Any]:
    """Fields that must change so ``cfg`` cannot be weaker than the walk-away floor."""
    fixes: dict[str, Any] = {}
    _old_size_defaults = {
        "max_risk_per_trade_pct": 1.0,
        "max_option_premium_pct": 5.0,
        "max_position_pct": 20.0,
    }
    for key, (lo, hi) in RISK_FLOOR.items():
        cur = _f(getattr(cfg, key, None))
        if cur is None or cur <= 0 or cur > hi:
            fixes[key] = UNSUPERVISED_DEFAULTS[key]
        elif key in _old_size_defaults and abs(cur - _old_size_defaults[key]) < 1e-9:
            fixes[key] = UNSUPERVISED_DEFAULTS[key]
        elif cur < lo:
            fixes[key] = lo
    mop = _i(getattr(cfg, "max_open_positions", 0))
    lo_p, hi_p = MAX_OPEN_POSITIONS_RANGE
    if mop is None or mop < lo_p or mop > hi_p:
        fixes["max_open_positions"] = (
            UNSUPERVISED_DEFAULTS["max_open_positions"]
            if mop is None or mop < lo_p
            else hi_p
        )
    for key in LOCKED_TRUE:
        if not bool(getattr(cfg, key, False)):
            fixes[key] = True
    cap = _i(getattr(cfg, "scan_fetch_cap", 8))
    lo_c, hi_c = SCAN_FETCH_CAP_RANGE
    if cap is None or cap < lo_c or cap > hi_c:
        fixes["scan_fetch_cap"] = UNSUPERVISED_DEFAULTS["scan_fetch_cap"]
    # Stale $1000 sleeve from an older build — always full NetLiq.
    cur_b = _f(getattr(cfg, "trading_budget_usd", 0))
    if cur_b is not None and cur_b != 0:
        fixes["trading_budget_usd"] = 0.0
    # Live always forces sizing floors ON (paper may stay OFF).
    mode = str(getattr(cfg, "trading_mode", "paper") or "paper").strip().lower()
    if mode == "live" and not bool(getattr(cfg, "sizing_floors", False)):
        fixes["sizing_floors"] = True
    return fixes


def ensure_immutable_floor(*, persist: bool = True) -> dict[str, Any]:
    """Seed walk-away floor and persist repairs. Call at agent start."""
    from abcxauto.config import (
        get_config,
        update_capacity_config,
        update_risk_config,
        _runtime_overrides,
    )

    cfg = get_config()
    fixes = floor_clamp_config_fields(cfg)
    risk_fix = {
        k: v for k, v in fixes.items()
        if k in (
            "risk_gates_enabled",
            "sizing_floors",
            "auto_panic_on_breach",
            "defined_risk_only",
            "cash_only",
        ) or k in RISK_FLOOR
    }
    controls_fix = {
        k: v for k, v in fixes.items()
        if k == "max_open_positions"
    }
    extra_fix = {
        k: v for k, v in fixes.items()
        if k == "scan_fetch_cap"
    }

    if persist:
        if risk_fix:
            update_risk_config(**risk_fix, persist=True, _skip_clamp=True)
        if controls_fix:
            update_capacity_config(**controls_fix, persist=True)
        if extra_fix:
            _runtime_overrides.update(extra_fix)
            try:
                _persist_agent_state(extra_fix)
            except Exception:
                logger.exception("ensure_floor persist extra failed")
        # Always persist locked floor so a stale risk_settings.json cannot linger.
        update_risk_config(
            risk_gates_enabled=True,
            auto_panic_on_breach=True,
            defined_risk_only=True,
            cash_only=True,
            daily_loss_limit_pct=float(getattr(get_config(), "daily_loss_limit_pct")),
            max_position_pct=float(getattr(get_config(), "max_position_pct")),
            max_risk_per_trade_pct=float(getattr(get_config(), "max_risk_per_trade_pct")),
            max_peak_drawdown_pct=float(getattr(get_config(), "max_peak_drawdown_pct")),
            max_option_premium_pct=float(getattr(get_config(), "max_option_premium_pct")),
            persist=True,
            _skip_clamp=True,
        )
        update_capacity_config(
            max_open_positions=int(getattr(get_config(), "max_open_positions")),
            persist=True,
        )
    elif extra_fix:
        _runtime_overrides.update(extra_fix)

    overlay = load_agent_state()
    if overlay:
        cleaned = {k: v for k, v in overlay.items() if k == "scan_fetch_cap"}
        if cleaned:
            _runtime_overrides.update(cleaned)

    return {
        "risk": risk_fix,
        "capacity": controls_fix,
        "extra": extra_fix,
        "status": "ok",
    }


def levers_snapshot(cfg: Any = None) -> dict[str, Any]:
    """Current knobs + allowed ranges. Facts for status/book — not a lecture."""
    from abcxauto.config import get_config

    c = cfg if cfg is not None else get_config()

    def _pct(key: str) -> dict[str, Any]:
        lo, hi = RISK_FLOOR[key]
        return {
            "now": getattr(c, key, None),
            "min": lo,
            "max": hi,
            "unit": "pct_nl",
        }

    lo_open, hi_open = MAX_OPEN_POSITIONS_RANGE
    return {
        "max_risk_per_trade_pct": _pct("max_risk_per_trade_pct"),
        "max_option_premium_pct": _pct("max_option_premium_pct"),
        "max_position_pct": _pct("max_position_pct"),
        "daily_loss_limit_pct": _pct("daily_loss_limit_pct"),
        "max_peak_drawdown_pct": _pct("max_peak_drawdown_pct"),
        "max_open_positions": {
            "now": getattr(c, "max_open_positions", None),
            "min": lo_open,
            "max": hi_open,
            "unit": "slots",
        },
        "change": "self_tune",
    }
