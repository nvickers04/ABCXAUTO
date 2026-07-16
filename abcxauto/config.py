"""ABCXAUTO configuration — one flat, env-driven config. No profiles, no overlays."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, fields, replace
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Operator-tunable risk knobs (Risk tab + env). Persist via risk_settings.json.
RISK_CONFIG_KEYS = frozenset({
    "risk_posture",
    "risk_gates_enabled",
    "daily_loss_limit_pct",
    "max_position_pct",
    "max_open_positions",
    "max_daily_trades",
    "auto_panic_on_breach",
    "defined_risk_only",
    "cash_only",
    "max_peak_drawdown_pct",
    "max_option_premium_pct",
    "max_risk_per_trade_pct",
    "min_reward_risk",
})
# Knobs Grok may retune via set_risk (clamped to posture envelope).
SET_RISK_KEYS = frozenset({
    "max_risk_per_trade_pct",
    "daily_loss_limit_pct",
    "max_position_pct",
    "max_open_positions",
    "max_daily_trades",
    "min_reward_risk",
})
RISK_POSTURES = frozenset({"defensive", "balanced", "aggressive"})
# Seeds when operator applies a posture; envelopes are Grok's hard box.
_POSTURE_SEEDS: dict[str, dict[str, Any]] = {
    "defensive": {
        "max_risk_per_trade_pct": 0.75,
        "daily_loss_limit_pct": 3.0,
        "max_position_pct": 8.0,
        "max_open_positions": 4,
        "max_daily_trades": 8,
        "min_reward_risk": 1.0,
    },
    "balanced": {
        "max_risk_per_trade_pct": 1.5,
        "daily_loss_limit_pct": 5.0,
        "max_position_pct": 12.0,
        "max_open_positions": 6,
        "max_daily_trades": 12,
        "min_reward_risk": 1.0,
    },
    "aggressive": {
        "max_risk_per_trade_pct": 2.5,
        "daily_loss_limit_pct": 8.0,
        "max_position_pct": 18.0,
        "max_open_positions": 10,
        "max_daily_trades": 20,
        "min_reward_risk": 0.8,
    },
}
# (floor, ceiling) per posture for agent-tunable knobs.
_POSTURE_ENVELOPES: dict[str, dict[str, tuple[float, float]]] = {
    "defensive": {
        "max_risk_per_trade_pct": (0.25, 2.0),
        "daily_loss_limit_pct": (1.0, 6.0),
        "max_position_pct": (2.0, 15.0),
        "max_open_positions": (1, 6),
        "max_daily_trades": (2, 15),
        "min_reward_risk": (0.5, 3.0),
    },
    "balanced": {
        "max_risk_per_trade_pct": (0.25, 4.0),
        "daily_loss_limit_pct": (1.0, 10.0),
        "max_position_pct": (2.0, 25.0),
        "max_open_positions": (1, 10),
        "max_daily_trades": (2, 25),
        "min_reward_risk": (0.5, 3.0),
    },
    "aggressive": {
        "max_risk_per_trade_pct": (0.25, 6.0),
        "daily_loss_limit_pct": (1.0, 15.0),
        "max_position_pct": (2.0, 35.0),
        "max_open_positions": (1, 15),
        "max_daily_trades": (2, 40),
        "min_reward_risk": (0.3, 3.0),
    },
}
_POSTURE_PROMPT_BIAS: dict[str, str] = {
    "defensive": "Prefer quality; size small-to-mid within max_risk_per_trade.",
    "balanced": "Flat RTH: weigh ideas; pick size per trade inside max_risk_per_trade.",
    "aggressive": "Prefer acting on ranked ideas when flat; size up when conviction high.",
}
_runtime_overrides: dict[str, Any] = {}
_file_overrides: dict[str, Any] = {}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_RISK_SETTINGS_PATH = _REPO_ROOT / "risk_settings.json"

DEFAULT_MANDATE = (
    "RELY ON YOUR INTELLIGENCE. You OWN a paper IBKR portfolio — not a playbook, "
    "not a scanner brain. You choose what to trade and how; the shell only gives "
    "tools and order abilities. Protect first; act when edge or risk requires it. "
    "Hold IS VALID when the book is protected. Hold is FORBIDDEN only while "
    "unprotected STK exists (code enforces). Build judgment from the live book, "
    "journal memory, and what you observe. Manage open risk first (stops, targets, "
    "edits, exits). When you take a new stock entry, use bracket or market_bracket "
    "with stop loss and take profit. Never blow up."
)


@dataclass(frozen=True)
class Config:
    # xAI / Grok
    xai_api_key: str = ""
    model: str = "grok-4.5"
    temperature: float = 0.3
    max_tokens: int = 8192

    # MarketData.app
    marketdata_token: str = ""

    # IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497  # 7497 paper TWS, 7496 live TWS, 4002 paper gateway, 4001 live gateway
    ibkr_client_id: int = 42
    trading_mode: str = "paper"  # must match port family (paper↔7497/4002, live↔7496/4001)
    # Exact phrase required before any live-mode connect (empty = refuse live)
    live_confirm: str = ""
    # After broker disconnect, halt new entries if still down after N seconds (0 = disable)
    disconnect_halt_s: float = 120.0

    # Agent
    trading_mandate: str = field(default=DEFAULT_MANDATE, repr=False)
    system_prompt_extra: str = field(default="", repr=False)
    # Deprecated: legacy web dashboard scan loop (abcxauto.web); Pro path ignores these.
    scan_enabled: bool = False
    scan_interval_s: int = 900

    # Autonomous cycle cadence (Pro START AUTONOMOUS loop)
    # Idle sleep between cycles — ~2 min keeps the agent deciding without 8s spam.
    cycle_sleep_s: float = 120.0
    # Min seconds between Grok decision calls when flat/protected (RTH).
    grok_min_interval_s: float = 120.0

    # Background P&L monitor
    monitor_enabled: bool = True
    monitor_poll_s: int = 30
    monitor_review_s: int = 300
    monitor_extended_hours: bool = False

    # Deprecated: legacy web dashboard bind (abcxauto.web); Pro cockpit is the sole UI.
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # Risk gates: halt latch always available; capital/daily limits default OFF
    # until operator applies risk_posture (seeds + envelope for agent set_risk).
    risk_posture: str = ""  # defensive|balanced|aggressive; empty = no posture yet
    risk_gates_enabled: bool = True
    daily_loss_limit_pct: float = 0.0  # 0 disables
    max_position_pct: float = 0.0  # 0 disables
    max_open_positions: int = 0  # 0 disables
    max_daily_trades: int = 0  # 0 disables
    auto_panic_on_breach: bool = False
    defined_risk_only: bool = False
    cash_only: bool = False
    max_peak_drawdown_pct: float = 0.0  # 0 disables; self-clears on recovery (no halt latch)
    max_option_premium_pct: float = 0.0  # 0 disables
    max_risk_per_trade_pct: float = 0.0  # 0 disables
    min_reward_risk: float = 0.0  # 0 disables; enforced in validate_proposal
    # Opt-in: False forces dry-run even on manual Re-test. Default True = paper place→cancel.
    suite_paper_place: bool = True

    @property
    def is_paper(self) -> bool:
        return self.ibkr_port in (7497, 4002)

    @property
    def effective_risk_posture(self) -> str:
        """Posture after live clamp (aggressive → balanced on live)."""
        return resolve_effective_posture(self.risk_posture, self.trading_mode)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def setup_file_logging(
    *,
    path: str | Path | None = None,
    max_bytes: int = 1_000_000,
    backup_count: int = 2,
) -> None:
    """Attach a WARNING+ RotatingFileHandler to the abcxauto logger (once)."""
    log_path = Path(path) if path is not None else Path("logs") / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("abcxauto")
    abs_target = str(log_path.resolve())
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler):
            try:
                if Path(getattr(h, "baseFilename", "")).resolve() == Path(abs_target):
                    return
            except OSError:
                return
    handler = RotatingFileHandler(
        abs_target,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.WARNING)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.WARNING:
        root.setLevel(logging.WARNING)


@lru_cache(maxsize=1)
def _load_env_config() -> Config:
    load_dotenv()
    mandate = _env("ABCXAUTO_TRADING_MANDATE") or DEFAULT_MANDATE
    return Config(
        xai_api_key=_env("XAI_API_KEY") or _env("GROK_API_KEY"),
        model=_env("ABCXAUTO_MODEL", "grok-4.5"),
        temperature=float(_env("ABCXAUTO_TEMPERATURE", "0.3")),
        max_tokens=int(_env("ABCXAUTO_MAX_TOKENS", "8192")),
        marketdata_token=_env("MARKETDATA_TOKEN") or _env("MARKETDATA_API_KEY"),
        ibkr_host=_env("IBKR_HOST", "127.0.0.1"),
        ibkr_port=int(_env("IBKR_PORT", "7497")),
        ibkr_client_id=int(_env("IBKR_CLIENT_ID", "42")),
        trading_mode=_env("TRADING_MODE", "paper"),
        live_confirm=_env("ABCXAUTO_LIVE_CONFIRM"),
        disconnect_halt_s=float(_env("ABCXAUTO_DISCONNECT_HALT_S", "120")),
        trading_mandate=mandate,
        system_prompt_extra=_env("ABCXAUTO_SYSTEM_PROMPT_EXTRA"),
        scan_enabled=_env_bool("ABCXAUTO_SCAN_ENABLED", False),
        scan_interval_s=int(_env("ABCXAUTO_SCAN_INTERVAL_S", "900")),
        cycle_sleep_s=float(_env("ABCXAUTO_CYCLE_SLEEP_S", "120")),
        grok_min_interval_s=float(_env("ABCXAUTO_GROK_MIN_INTERVAL_S", "120")),
        monitor_enabled=_env_bool("ABCXAUTO_MONITOR_ENABLED", True),
        monitor_poll_s=int(_env("ABCXAUTO_MONITOR_POLL_S", "30")),
        monitor_review_s=int(_env("ABCXAUTO_MONITOR_REVIEW_S", "300")),
        monitor_extended_hours=_env_bool("ABCXAUTO_MONITOR_EXTENDED_HOURS", False),
        web_host=_env("ABCXAUTO_WEB_HOST", "127.0.0.1"),
        web_port=int(_env("ABCXAUTO_WEB_PORT", "8000")),
        risk_posture=_normalize_posture(_env("ABCXAUTO_RISK_POSTURE", "")),
        risk_gates_enabled=_env_bool("ABCXAUTO_RISK_GATES_ENABLED", True),
        daily_loss_limit_pct=float(_env("ABCXAUTO_DAILY_LOSS_LIMIT_PCT", "0")),
        max_position_pct=float(_env("ABCXAUTO_MAX_POSITION_PCT", "0")),
        max_open_positions=int(_env("ABCXAUTO_MAX_OPEN_POSITIONS", "0")),
        max_daily_trades=int(_env("ABCXAUTO_MAX_DAILY_TRADES", "0")),
        auto_panic_on_breach=_env_bool("ABCXAUTO_AUTO_PANIC_ON_BREACH", False),
        defined_risk_only=_env_bool("ABCXAUTO_DEFINED_RISK_ONLY", False),
        cash_only=_env_bool("ABCXAUTO_CASH_ONLY", False),
        max_peak_drawdown_pct=float(_env("ABCXAUTO_MAX_PEAK_DRAWDOWN_PCT", "0")),
        max_option_premium_pct=float(_env("ABCXAUTO_MAX_OPTION_PREMIUM_PCT", "0")),
        max_risk_per_trade_pct=float(_env("ABCXAUTO_MAX_RISK_PER_TRADE_PCT", "0")),
        min_reward_risk=float(_env("ABCXAUTO_MIN_REWARD_RISK", "0")),
        suite_paper_place=_env_bool("ABCXAUTO_SUITE_PAPER_PLACE", True),
    )


def _risk_settings_path() -> Path:
    raw = os.environ.get("ABCXAUTO_RISK_SETTINGS_PATH", "").strip()
    if raw:
        return Path(raw)
    return _DEFAULT_RISK_SETTINGS_PATH


def _normalize_posture(raw: Any) -> str:
    p = str(raw or "").strip().lower()
    return p if p in RISK_POSTURES else ""


def resolve_effective_posture(posture: str, trading_mode: str = "paper") -> str:
    """Live maps aggressive → balanced; empty posture stays empty."""
    p = _normalize_posture(posture)
    if not p:
        return ""
    if str(trading_mode or "").strip().lower() == "live" and p == "aggressive":
        return "balanced"
    return p


# Back-compat alias used by tests / call sites.
effective_risk_posture = resolve_effective_posture


def _coerce_risk_value(key: str, value: Any) -> Any:
    """Normalize JSON/env-ish values to Config field types."""
    if key == "risk_posture":
        return _normalize_posture(value)
    if key in (
        "risk_gates_enabled",
        "auto_panic_on_breach",
        "defined_risk_only",
        "cash_only",
    ):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if key in ("max_open_positions", "max_daily_trades"):
        return int(float(value))
    return float(value)


def _read_risk_file(settings_path: Path) -> dict[str, Any]:
    if not settings_path.is_file():
        return {}
    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read risk settings %s", settings_path)
        return {}
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in RISK_CONFIG_KEYS:
            continue
        try:
            cleaned[key] = _coerce_risk_value(key, value)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid risk setting %s=%r", key, value)
    return cleaned


def load_risk_settings(path: Path | None = None) -> dict[str, Any]:
    """Load persisted risk knobs from disk into ``_file_overrides``."""
    global _file_overrides
    _file_overrides = _read_risk_file(path or _risk_settings_path())
    return dict(_file_overrides)


def save_risk_settings(
    values: dict[str, Any],
    *,
    path: Path | None = None,
) -> Path:
    """Merge ``values`` into the on-disk risk settings file."""
    global _file_overrides
    settings_path = path or _risk_settings_path()
    current = _read_risk_file(settings_path)
    for key, value in values.items():
        if key not in RISK_CONFIG_KEYS:
            continue
        current[key] = _coerce_risk_value(key, value)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _file_overrides = dict(current)
    return settings_path


# Load once at import so Pro / agent see last Apply without an extra call.
load_risk_settings()


def get_config() -> Config:
    """Env-backed config plus file-persisted risk knobs and session overrides.

    Precedence: ``.env`` defaults < ``risk_settings.json`` < session overrides.
    """
    base = _load_env_config()
    merged = {**_file_overrides, **_runtime_overrides}
    if not merged:
        return base
    return replace(base, **merged)


# Tests call get_config.cache_clear() to reload env; session Risk overrides stay.
get_config.cache_clear = _load_env_config.cache_clear  # type: ignore[attr-defined]


def update_risk_config(**kwargs: Any) -> Config:
    """Apply risk overrides; persist to ``risk_settings.json`` by default.

    Pass ``persist=False`` for session-only (tests / temporary tweaks).
    SET_RISK_KEYS are clamped to the active posture envelope when posture is set
    (unless ``_skip_clamp=True``, used by ``apply_risk_posture`` seeds).
    """
    persist = bool(kwargs.pop("persist", True))
    skip_clamp = bool(kwargs.pop("_skip_clamp", False))
    unknown = set(kwargs) - RISK_CONFIG_KEYS
    if unknown:
        raise ValueError(f"Unknown risk config keys: {sorted(unknown)}")
    valid = {f.name for f in fields(Config)}
    cleaned: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key not in valid:
            raise ValueError(f"Unknown config field: {key}")
        cleaned[key] = _coerce_risk_value(key, value)
    # If posture is being set in this call, use it for clamping; else current.
    posture_for_clamp = cleaned.get("risk_posture")
    if not skip_clamp and SET_RISK_KEYS.intersection(cleaned):
        tunable = {k: cleaned[k] for k in list(cleaned) if k in SET_RISK_KEYS}
        applied, _notes = clamp_risk_knobs(tunable, posture=posture_for_clamp)
        cleaned.update(applied)
    _runtime_overrides.update(cleaned)
    if persist:
        try:
            save_risk_settings(cleaned)
        except Exception:
            logger.exception("Failed to persist risk settings")
            raise
    return get_config()


def posture_envelope(posture: str | None = None) -> dict[str, tuple[float, float]]:
    """Floor/ceiling map for the effective posture (empty if none)."""
    cfg = get_config()
    p = resolve_effective_posture(
        posture if posture is not None else cfg.risk_posture,
        cfg.trading_mode,
    )
    if not p:
        return {}
    return dict(_POSTURE_ENVELOPES[p])


def posture_prompt_bias(posture: str | None = None) -> str:
    cfg = get_config()
    p = resolve_effective_posture(
        posture if posture is not None else cfg.risk_posture,
        cfg.trading_mode,
    )
    return _POSTURE_PROMPT_BIAS.get(p, "")


def clamp_risk_knobs(
    values: dict[str, Any],
    *,
    posture: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Clamp SET_RISK_KEYS to posture envelope.

    Returns ``(applied, notes)`` where notes map key → {raw, clamped}.
    Without a posture, coerces types only (no envelope).
    """
    cfg = get_config()
    p = resolve_effective_posture(
        posture if posture is not None else cfg.risk_posture,
        cfg.trading_mode,
    )
    env = _POSTURE_ENVELOPES.get(p, {})
    applied: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    for key, value in values.items():
        if key not in SET_RISK_KEYS:
            continue
        try:
            coerced = _coerce_risk_value(key, value)
        except (TypeError, ValueError):
            continue
        if key in env:
            lo, hi = env[key]
            if key in ("max_open_positions", "max_daily_trades"):
                clamped = int(max(lo, min(hi, int(coerced))))
            else:
                clamped = float(max(lo, min(hi, float(coerced))))
            if clamped != coerced:
                notes[key] = {"raw": coerced, "clamped": clamped}
            applied[key] = clamped
        else:
            applied[key] = coerced
    return applied, notes


def apply_risk_posture(
    posture: str,
    *,
    persist: bool = True,
) -> Config:
    """Operator sets posture: seed capital knobs + auto_panic; persist by default."""
    p = _normalize_posture(posture)
    if not p:
        raise ValueError(
            f"Unknown risk_posture {posture!r}; expected one of {sorted(RISK_POSTURES)}"
        )
    cfg = get_config()
    effective = resolve_effective_posture(p, cfg.trading_mode)
    if effective != p:
        logger.info("Live clamp: risk_posture %s → %s", p, effective)
    seed = dict(_POSTURE_SEEDS[effective])
    payload: dict[str, Any] = {
        "risk_posture": p,
        "risk_gates_enabled": True,
        "auto_panic_on_breach": True,
        **seed,
    }
    return update_risk_config(**payload, persist=persist, _skip_clamp=True)


def set_risk_knobs(
    values: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Agent ``set_risk`` path: clamp to envelope, persist, return result dict."""
    cfg = get_config()
    if not resolve_effective_posture(cfg.risk_posture, cfg.trading_mode):
        return {
            "status": "blocked",
            "note": "set_risk requires an operator risk_posture first",
            "strategy": "set_risk",
        }
    before = {k: getattr(cfg, k) for k in SET_RISK_KEYS}
    applied, notes = clamp_risk_knobs(values)
    if not applied:
        return {
            "status": "blocked",
            "note": "set_risk: no valid knobs in params",
            "strategy": "set_risk",
            "before": before,
        }
    update_risk_config(**applied, persist=persist, _skip_clamp=True)
    after = {k: getattr(get_config(), k) for k in SET_RISK_KEYS}
    return {
        "status": "ok",
        "strategy": "set_risk",
        "applied": applied,
        "clamped": notes,
        "before": before,
        "after": after,
    }


def risk_envelope_snapshot() -> dict[str, Any]:
    """UI / prompt: posture, effective posture, current knobs, envelope."""
    cfg = get_config()
    eff = resolve_effective_posture(cfg.risk_posture, cfg.trading_mode)
    env = posture_envelope()
    current = {k: getattr(cfg, k) for k in sorted(SET_RISK_KEYS)}
    return {
        "risk_posture": cfg.risk_posture or "",
        "effective_risk_posture": eff,
        "prompt_bias": posture_prompt_bias(),
        "current": current,
        "envelope": {k: {"floor": lo, "ceil": hi} for k, (lo, hi) in env.items()},
        "live_clamped": bool(
            cfg.risk_posture == "aggressive" and eff == "balanced"
        ),
    }


def clear_runtime_overrides() -> None:
    """Drop in-memory session overrides (file-persisted knobs remain)."""
    _runtime_overrides.clear()


def clear_risk_settings(*, path: Path | None = None) -> None:
    """Clear file + in-memory risk overrides (tests)."""
    global _file_overrides
    _file_overrides = {}
    _runtime_overrides.clear()
    settings_path = path or _risk_settings_path()
    try:
        if settings_path.is_file():
            settings_path.unlink()
    except OSError:
        logger.exception("Failed to remove risk settings %s", settings_path)


def risk_settings_path() -> Path:
    """Absolute path of the persisted risk settings file."""
    return _risk_settings_path().resolve()


def risk_config_snapshot(*, reload: bool = False) -> dict[str, Any]:
    """Current effective risk knobs for the Risk tab.

    Pass ``reload=True`` to re-read ``risk_settings.json`` from disk (Pro UI).
    """
    if reload:
        load_risk_settings()
    cfg = get_config()
    return {k: getattr(cfg, k) for k in sorted(RISK_CONFIG_KEYS)}


def set_trading_mode(mode: str, *, live_confirm: str = "") -> Config:
    """Session-only paper/live switch; remaps IBKR port family and validates live confirm.

    Paper ports 7497/4002 <-> live 7496/4001. Does not write .env. Caller should
    reconnect the broker after this returns.
    """
    from abcxauto.broker.connection import (
        LIVE_CONFIRM_PHRASE,
        LIVE_PORTS,
        PAPER_PORTS,
        validate_trading_mode_port,
    )

    normalized = (mode or "").strip().lower()
    if normalized not in ("paper", "live"):
        raise ValueError(f"Unknown trading mode {mode!r}; expected 'paper' or 'live'")

    cfg = get_config()
    port = int(cfg.ibkr_port)
    confirm = ""
    if normalized == "paper":
        if port in LIVE_PORTS:
            port = 4002 if port == 4001 else 7497
        confirm = ""
    else:
        phrase = (live_confirm or "").strip()
        if phrase != LIVE_CONFIRM_PHRASE:
            raise ValueError(
                f"Live mode requires confirm phrase {LIVE_CONFIRM_PHRASE!r}"
            )
        if port in PAPER_PORTS:
            port = 4001 if port == 4002 else 7496
        confirm = LIVE_CONFIRM_PHRASE

    validate_trading_mode_port(normalized, port, confirm)
    _runtime_overrides.update(
        {
            "trading_mode": normalized,
            "ibkr_port": port,
            "live_confirm": confirm,
        }
    )
    return get_config()
