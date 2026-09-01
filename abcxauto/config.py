"""ABCXAUTO configuration — one flat, env-driven config. No profiles, no overlays."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Risk capital knobs persisted in risk_settings.json.
RISK_CONFIG_KEYS = frozenset({
    "risk_posture",
    "risk_gates_enabled",
    "sizing_floors",
    "daily_loss_limit_pct",
    "max_position_pct",
    "auto_panic_on_breach",
    "defined_risk_only",
    "cash_only",
    "max_peak_drawdown_pct",
    "max_option_premium_pct",
    "max_risk_per_trade_pct",
    "max_symbol_concentration_pct",
    "max_arena_concentration_pct",
})
# Knobs Grok may retune via self_tune (clamped to the walk-away floor).
SET_RISK_KEYS = frozenset({
    "max_risk_per_trade_pct",
    "daily_loss_limit_pct",
    "max_position_pct",
    "max_peak_drawdown_pct",
    "max_option_premium_pct",
    "max_symbol_concentration_pct",
    "max_arena_concentration_pct",
})
# Book capacity. Disjoint from risk capital keys.
CAPACITY_KEYS = frozenset({
    "max_open_positions",
})
PERSISTED_OPERATOR_KEYS = RISK_CONFIG_KEYS | CAPACITY_KEYS
# Brain / pacing / link knobs the operator sets from Pro Settings.
# scan_fetch_cap is deliberately absent: self_tune is its only writer.
AGENT_CONFIG_KEYS = frozenset({
    "model",
    "temperature",
    "max_tokens",
    "session_look_cap",
    "session_token_cap",
    "monitor_enabled",
    "monitor_poll_s",
    "monitor_review_s",
    "monitor_extended_hours",
    "disconnect_halt_s",
    "ibkr_host",
    "ibkr_client_id",
})
# Two books = two processes, two client ids: a live socket keeps the host and
# id it dialled, so these may only move while the IBKR link is down.
AGENT_DISCONNECTED_ONLY_KEYS = frozenset({"ibkr_host", "ibkr_client_id"})
# Never writable from a settings form: the mode/port pair is set_trading_mode's
# (it validates the confirm phrase) and secrets belong to .env.
AGENT_LOCKED_KEYS = frozenset({
    "trading_mode",
    "ibkr_port",
    "live_confirm",
    "xai_api_key",
    "marketdata_token",
})
# Everything risk_settings.json may hold.
PERSISTED_SETTINGS_KEYS = PERSISTED_OPERATOR_KEYS | AGENT_CONFIG_KEYS
# lo, hi for the numeric agent knobs. Narrower than the field type on purpose:
# a 0 poll or a 0 disconnect halt turns a safety loop off silently.
AGENT_BOUNDS: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "max_tokens": (1024, 131_072),
    "session_look_cap": (1, 400),
    "session_token_cap": (50_000, 10_000_000),
    "monitor_poll_s": (5, 900),
    "monitor_review_s": (30, 21_600),
    "disconnect_halt_s": (1.0, 900.0),
    "ibkr_client_id": (1, 999),
}
_AGENT_BOOL_KEYS = frozenset({"monitor_enabled", "monitor_extended_hours"})
_AGENT_INT_KEYS = frozenset({
    "max_tokens",
    "session_look_cap",
    "session_token_cap",
    "monitor_poll_s",
    "monitor_review_s",
    "ibkr_client_id",
})
_AGENT_TEXT_KEYS = frozenset({"model", "ibkr_host"})
RISK_POSTURES = frozenset({"defensive", "balanced", "aggressive"})
_runtime_overrides: dict[str, Any] = {}
_file_overrides: dict[str, Any] = {}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_RISK_SETTINGS_PATH = _REPO_ROOT / "risk_settings.json"
_DEFAULT_FILE_LOG_PATH = _REPO_ROOT / "logs" / "app.log"


@dataclass(frozen=True)
class Config:
    # xAI / Grok
    xai_api_key: str = ""
    model: str = "grok-4.6"  # ABCXAUTO_MODEL is the env form; see get_config()
    temperature: float = 0.3
    max_tokens: int = 8192
    # Per stay-up session (premarket / RTH). Overnight honors a hit.
    # Paper stay-up does not idle the desk. Not a per-turn cap.
    session_look_cap: int = 160
    session_token_cap: int = 2_500_000

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

    # Max MDA symbols Grok may request per scan.
    scan_fetch_cap: int = 8

    # 0 = full NetLiq (default). Size/loss gates are always % of portfolio.
    trading_budget_usd: float = 0.0

    # Background P&L monitor
    monitor_enabled: bool = True
    monitor_poll_s: int = 30
    monitor_review_s: int = 300
    monitor_extended_hours: bool = False

    # Walk-away floor — ON by default; agent may tighten, never weaken.
    risk_posture: str = "defensive"
    risk_gates_enabled: bool = True
    # Paper default OFF (Grok sizes). Live forced ON in code. Operator chip only.
    sizing_floors: bool = False
    daily_loss_limit_pct: float = 25.0
    max_position_pct: float = 25.0
    auto_panic_on_breach: bool = True
    defined_risk_only: bool = True
    cash_only: bool = True
    max_peak_drawdown_pct: float = 25.0
    max_option_premium_pct: float = 25.0
    max_risk_per_trade_pct: float = 25.0
    # Cap on one underlying across every lot. max_position_pct only sees the
    # order in front of it, so N orders in the same name could stack past it.
    max_symbol_concentration_pct: float = 25.0
    # One sector/theme/cap arena (catalog we already scan), all names in it.
    # Per-name cap cannot see NVDA+SMCI+ARM+AVGO as one bet.
    max_arena_concentration_pct: float = 25.0
    # 0 = off (no count refuse). A positive N is a Grok/operator ceiling.
    max_open_positions: int = 0

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


def default_file_log_path() -> Path:
    """Repo-absolute ``logs/app.log`` (next to the package). Independent of cwd.

    Start-Process of ``logs/_start_pro.py`` can leave process cwd off the repo
    root. A ``Path('logs')/'app.log'`` default then writes elsewhere and leaves
    the operator's ``logs/app.log`` stale while Grok thinks.
    """
    return _DEFAULT_FILE_LOG_PATH.resolve()


def setup_file_logging(
    *,
    path: str | Path | None = None,
    max_bytes: int = 1_000_000,
    backup_count: int = 2,
) -> Path:
    """Attach an INFO+ RotatingFileHandler to the abcxauto logger (once).

    Default path is repo-absolute ``<repo>/logs/app.log``. ``ABCXAUTO_LOG_PATH``
    redirects it. The operator reads ``logs/app.log`` as evidence of what the
    desk did (think / send / fill are INFO), so tests must never land in it.

    DEBUG (ib_insync cancelMktData spam) stays off this file — that noise is
    the child's console / ``desk.out``. The handler lives on ``abcxauto``, not
    the process root, so ``ib_insync`` records never reach it.
    """
    if path is None:
        path = os.environ.get("ABCXAUTO_LOG_PATH") or None
    log_path = Path(path) if path is not None else default_file_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("abcxauto")
    abs_target = str(log_path.resolve())
    resolved = Path(abs_target)
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler):
            try:
                if Path(getattr(h, "baseFilename", "")).resolve() == resolved:
                    h.setLevel(logging.INFO)
                    if root.level == logging.NOTSET or root.level > logging.INFO:
                        root.setLevel(logging.INFO)
                    return resolved
            except OSError:
                return resolved
    handler = RotatingFileHandler(
        abs_target,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return resolved


@lru_cache(maxsize=1)
def _load_env_config() -> Config:
    load_dotenv()
    return Config(
        xai_api_key=_env("XAI_API_KEY") or _env("GROK_API_KEY"),
        model=_env("ABCXAUTO_MODEL", "grok-4.6"),
        temperature=float(_env("ABCXAUTO_TEMPERATURE", "0.3")),
        max_tokens=int(_env("ABCXAUTO_MAX_TOKENS", "8192")),
        session_look_cap=int(_env("ABCXAUTO_SESSION_LOOK_CAP", "160")),
        session_token_cap=int(_env("ABCXAUTO_SESSION_TOKEN_CAP", "2500000")),
        marketdata_token=_env("MARKETDATA_TOKEN") or _env("MARKETDATA_API_KEY"),
        ibkr_host=_env("IBKR_HOST", "127.0.0.1"),
        ibkr_port=int(_env("IBKR_PORT", "7497")),
        ibkr_client_id=int(_env("IBKR_CLIENT_ID", "42")),
        trading_mode=_env("TRADING_MODE", "paper"),
        live_confirm=_env("ABCXAUTO_LIVE_CONFIRM"),
        disconnect_halt_s=float(_env("ABCXAUTO_DISCONNECT_HALT_S", "120")),
        scan_fetch_cap=int(_env("ABCXAUTO_SCAN_FETCH_CAP", "8")),
        trading_budget_usd=float(
            _env("ABCXAUTO_TRADING_BUDGET_USD")
            or _env("ABCXAUTO_TARGET_CAPITAL")
            or "0"
        ),
        monitor_enabled=_env_bool("ABCXAUTO_MONITOR_ENABLED", True),
        monitor_poll_s=int(_env("ABCXAUTO_MONITOR_POLL_S", "30")),
        monitor_review_s=int(_env("ABCXAUTO_MONITOR_REVIEW_S", "300")),
        monitor_extended_hours=_env_bool("ABCXAUTO_MONITOR_EXTENDED_HOURS", False),
        risk_posture=_normalize_posture(_env("ABCXAUTO_RISK_POSTURE", "defensive")),
        risk_gates_enabled=_env_bool("ABCXAUTO_RISK_GATES_ENABLED", True),
        sizing_floors=_env_bool("ABCXAUTO_SIZING_FLOORS", False),
        daily_loss_limit_pct=float(_env("ABCXAUTO_DAILY_LOSS_LIMIT_PCT", "25")),
        max_position_pct=float(_env("ABCXAUTO_MAX_POSITION_PCT", "25")),
        max_open_positions=int(_env("ABCXAUTO_MAX_OPEN_POSITIONS", "0")),
        auto_panic_on_breach=_env_bool("ABCXAUTO_AUTO_PANIC_ON_BREACH", True),
        defined_risk_only=_env_bool("ABCXAUTO_DEFINED_RISK_ONLY", True),
        cash_only=_env_bool("ABCXAUTO_CASH_ONLY", True),
        max_peak_drawdown_pct=float(_env("ABCXAUTO_MAX_PEAK_DRAWDOWN_PCT", "25")),
        max_option_premium_pct=float(_env("ABCXAUTO_MAX_OPTION_PREMIUM_PCT", "25")),
        max_risk_per_trade_pct=float(_env("ABCXAUTO_MAX_RISK_PER_TRADE_PCT", "25")),
        max_symbol_concentration_pct=float(
            _env("ABCXAUTO_MAX_SYMBOL_CONCENTRATION_PCT", "25")
        ),
        max_arena_concentration_pct=float(
            _env("ABCXAUTO_MAX_ARENA_CONCENTRATION_PCT", "25")
        ),
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
        "sizing_floors",
        "auto_panic_on_breach",
        "defined_risk_only",
        "cash_only",
    ):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if key == "max_open_positions":
        return int(max(0, min(10_000, int(float(value)))))
    return float(value)


def broker_link_connected() -> bool:
    """True when this process already holds a live IBKR socket.

    Reads the connector singleton only if the broker module is already imported:
    importing it from here would be a cycle, and no connector means no link.
    """
    import sys

    connector_mod = sys.modules.get("abcxauto.broker.connector")
    conn = getattr(getattr(connector_mod, "IBKRConnector", None), "_instance", None)
    if conn is None:
        return False
    try:
        return bool(conn.connected)
    except Exception:
        return False


def _coerce_agent_value(key: str, value: Any) -> Any:
    """Normalize one agent knob to its Config field type. Raises on garbage."""
    if key in _AGENT_BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if key in _AGENT_TEXT_KEYS:
        text = str(value or "").strip()
        if not text or any(c.isspace() for c in text):
            raise ValueError(f"{key} must be a single non-empty token")
        return text
    if key in _AGENT_INT_KEYS:
        return int(float(value))
    return float(value)


def clamp_agent_knobs(
    values: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Coerce + bound agent knobs. Returns (applied, clamp notes, rejected)."""
    applied: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for key, value in (values or {}).items():
        if key in AGENT_LOCKED_KEYS:
            rejected[key] = "locked — set_trading_mode / .env owns this"
            continue
        if key not in AGENT_CONFIG_KEYS:
            rejected[key] = "not an agent setting"
            continue
        if key in AGENT_DISCONNECTED_ONLY_KEYS and broker_link_connected():
            rejected[key] = "IBKR is connected — disconnect the desk to change this"
            continue
        try:
            coerced = _coerce_agent_value(key, value)
        except (TypeError, ValueError) as exc:
            rejected[key] = str(exc) or "invalid value"
            continue
        bounds = AGENT_BOUNDS.get(key)
        if bounds is not None:
            lo, hi = bounds
            bounded = max(lo, min(hi, coerced))
            if key in _AGENT_INT_KEYS:
                bounded = int(bounded)
            if bounded != coerced:
                notes[key] = {"raw": coerced, "clamped": bounded}
            coerced = bounded
        applied[key] = coerced
    return applied, notes, rejected


def _coerce_persisted_value(key: str, value: Any) -> Any:
    if key in AGENT_CONFIG_KEYS:
        return _coerce_agent_value(key, value)
    return _coerce_risk_value(key, value)


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
        if key not in PERSISTED_SETTINGS_KEYS:
            continue
        try:
            cleaned[key] = _coerce_persisted_value(key, value)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid risk setting %s=%r", key, value)
    return cleaned


def load_risk_settings(path: Path | None = None) -> dict[str, Any]:
    """Load persisted risk, capacity and agent knobs from disk into ``_file_overrides``."""
    global _file_overrides
    _file_overrides = _read_risk_file(path or _risk_settings_path())
    return dict(_file_overrides)


def save_risk_settings(
    values: dict[str, Any],
    *,
    path: Path | None = None,
) -> Path:
    """Merge ``values`` into the on-disk settings file (risk, capacity or agent keys)."""
    global _file_overrides
    settings_path = path or _risk_settings_path()
    current = _read_risk_file(settings_path)
    for key, value in values.items():
        if key not in PERSISTED_SETTINGS_KEYS:
            continue
        current[key] = _coerce_persisted_value(key, value)
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
    """Env-backed config plus file-persisted risk knobs, agent_state, session overrides.

    Precedence: ``.env`` defaults < ``risk_settings.json`` < ``agent_state.json``
    < session overrides. So the ``model`` the operator applies from Pro Settings
    beats ``ABCXAUTO_MODEL``, and ``scan_fetch_cap`` from ``self_tune`` beats both.
    """
    base = _load_env_config()
    agent_extra: dict[str, Any] = {}
    try:
        from abcxauto.self_tune import load_agent_state

        raw = load_agent_state()
        allowed = {
            "scan_fetch_cap",
        }
        agent_extra = {k: v for k, v in raw.items() if k in allowed}
    except Exception:
        agent_extra = {}
    merged = {**_file_overrides, **agent_extra, **_runtime_overrides}
    valid = {f.name for f in fields(Config)}
    cleaned = {k: v for k, v in merged.items() if k in valid}
    cfg = replace(base, **cleaned) if cleaned else base
    try:
        from abcxauto.self_tune import floor_clamp_config_fields

        fixes = floor_clamp_config_fields(cfg)
        if fixes:
            cfg = replace(cfg, **{k: v for k, v in fixes.items() if k in valid})
    except Exception:
        pass
    return cfg


# Tests call get_config.cache_clear() to reload env; session Risk overrides stay.
get_config.cache_clear = _load_env_config.cache_clear  # type: ignore[attr-defined]


def update_risk_config(**kwargs: Any) -> Config:
    """Apply risk-capital overrides only (never capacity keys).

    Pass ``persist=False`` for session-only. SET_RISK_KEYS clamped to the
    walk-away floor unless ``_skip_clamp=True``.
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
    if not skip_clamp and SET_RISK_KEYS.intersection(cleaned):
        tunable = {k: cleaned[k] for k in list(cleaned) if k in SET_RISK_KEYS}
        applied, _notes = clamp_risk_knobs(tunable)
        cleaned.update(applied)
    _runtime_overrides.update(cleaned)
    if persist:
        try:
            save_risk_settings(cleaned)
        except Exception:
            logger.exception("Failed to persist risk settings")
            raise
    return get_config()


def update_capacity_config(**kwargs: Any) -> Config:
    """Apply book-capacity overrides only (never risk-capital keys)."""
    persist = bool(kwargs.pop("persist", True))
    unknown = set(kwargs) - CAPACITY_KEYS
    if unknown:
        raise ValueError(f"Unknown capacity config keys: {sorted(unknown)}")
    valid = {f.name for f in fields(Config)}
    cleaned: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key not in valid:
            raise ValueError(f"Unknown config field: {key}")
        cleaned[key] = _coerce_risk_value(key, value)
    _runtime_overrides.update(cleaned)
    if persist:
        try:
            save_risk_settings(cleaned)
        except Exception:
            logger.exception("Failed to persist capacity settings")
            raise
    return get_config()


def update_agent_config(**kwargs: Any) -> Config:
    """Apply brain / pacing / link overrides (never risk, mode, port or secrets).

    Pass ``persist=False`` for session-only. Values are clamped to
    ``AGENT_BOUNDS``; invalid values raise. Clears the env cache so a knob the
    operator changes is live on the next ``get_config()`` without a restart.
    """
    persist = bool(kwargs.pop("persist", True))
    locked = set(kwargs) & AGENT_LOCKED_KEYS
    if locked:
        raise ValueError(
            f"update_agent_config refuses {sorted(locked)}: "
            "trading_mode/ibkr_port/live_confirm go through set_trading_mode, "
            "API keys stay in .env"
        )
    unknown = set(kwargs) - AGENT_CONFIG_KEYS
    if unknown:
        raise ValueError(f"Unknown agent config keys: {sorted(unknown)}")
    cleaned, _notes, rejected = clamp_agent_knobs(kwargs)
    if rejected:
        raise ValueError(f"Invalid agent config: {rejected}")
    _runtime_overrides.update(cleaned)
    if persist:
        try:
            save_risk_settings(cleaned)
        except Exception:
            logger.exception("Failed to persist agent settings")
            raise
    _load_env_config.cache_clear()
    return get_config()


def set_agent_knobs(
    values: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Operator path from Pro Settings. Clamps instead of raising.

    Returns ``{"applied", "clamped", "rejected"}`` so the caller can say what it
    refused rather than silently accepting it.
    """
    applied, notes, rejected = clamp_agent_knobs(values or {})
    if applied:
        _runtime_overrides.update(applied)
        if persist:
            try:
                save_risk_settings(applied)
            except Exception:
                logger.exception("Failed to persist agent settings")
                raise
        _load_env_config.cache_clear()
    return {"applied": applied, "clamped": notes, "rejected": rejected}


def agent_config_snapshot(*, reload: bool = False) -> dict[str, Any]:
    """Current effective agent knobs (brain, pacing, link)."""
    if reload:
        load_risk_settings()
    cfg = get_config()
    return {k: getattr(cfg, k) for k in sorted(AGENT_CONFIG_KEYS)}


def clamp_risk_knobs(
    values: dict[str, Any],
    *,
    posture: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Clamp SET_RISK_KEYS to the walk-away floor. ``posture`` is ignored."""
    from abcxauto.self_tune import clamp_risk_to_floor

    _ = posture
    applied: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    for key, value in values.items():
        if key not in SET_RISK_KEYS:
            continue
        try:
            coerced = _coerce_risk_value(key, value)
        except (TypeError, ValueError):
            continue
        new_v, note = clamp_risk_to_floor(key, coerced)
        if new_v is None:
            continue
        applied[key] = new_v
        if note:
            notes[key] = note
    return applied, notes


def set_risk_knobs(
    values: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Agent self-tune path (set_risk alias). No operator approval. Floor-clamped."""
    from abcxauto.self_tune import apply_self_tune

    return apply_self_tune(values or {}, persist=persist)


def risk_envelope_snapshot() -> dict[str, Any]:
    """Current knobs + walk-away floor. Facts for book/status — not a lecture."""
    from abcxauto.self_tune import RISK_FLOOR

    cfg = get_config()
    eff = resolve_effective_posture(cfg.risk_posture, cfg.trading_mode)
    current = {k: getattr(cfg, k) for k in sorted(SET_RISK_KEYS)}
    envelope = {
        k: {"floor": lo, "ceil": hi}
        for k, (lo, hi) in RISK_FLOOR.items()
        if k in SET_RISK_KEYS
    }
    return {
        "risk_posture": cfg.risk_posture or "",
        "effective_risk_posture": eff,
        "current": current,
        "envelope": envelope,
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
    """Current effective risk + capacity knobs.

    Pass ``reload=True`` to re-read ``risk_settings.json`` from disk.
    """
    if reload:
        load_risk_settings()
    cfg = get_config()
    return {k: getattr(cfg, k) for k in sorted(PERSISTED_OPERATOR_KEYS)}


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
