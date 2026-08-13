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

# Risk tab only — capital survival. Never overlaps CONTROL_KEYS.
RISK_CONFIG_KEYS = frozenset({
    "risk_posture",
    "risk_gates_enabled",
    "daily_loss_limit_pct",
    "max_position_pct",
    "auto_panic_on_breach",
    "defined_risk_only",
    "cash_only",
    "max_peak_drawdown_pct",
    "max_option_premium_pct",
    "max_risk_per_trade_pct",
    "trading_budget_usd",
})
# Knobs Grok may retune via set_risk (clamped to capital envelope).
SET_RISK_KEYS = frozenset({
    "max_risk_per_trade_pct",
    "daily_loss_limit_pct",
    "max_position_pct",
    "max_peak_drawdown_pct",
    "max_option_premium_pct",
})
# Controls tab — attention + toolbox + book capacity (disjoint from Risk).
CONTROL_KEYS = frozenset({
    "control_deliberation_pct",
    "control_budget_pct",
    "control_frequency_pct",
    "control_entry_surface_pct",
    "control_complexity_pct",
    "control_rotation_pct",
    "max_open_positions",
})
# Persisted together in risk_settings.json but saved via disjoint APIs.
PERSISTED_OPERATOR_KEYS = RISK_CONFIG_KEYS | CONTROL_KEYS
# S2 lean at/above this: shell must not cheap-skip Act after Judge hold.
DELIBERATION_REQUIRE_ACT_PCT = 60
# Frequency ≥ this: allocator may spend budget on new-risk / escapade streams.
FREQUENCY_ALLOW_NEW_RISK_PCT = 40
# Rotation ≥ this + thin cash: labeled redeploy Heuristic + open_risk stream bias.
ROTATION_REDEPLOY_PCT = 60
# Cash share of NL below this counts as "thin" for rotation process (Fact threshold).
ROTATION_THIN_CASH_PCT = 15.0
_CONTROL_DEFAULTS: dict[str, int] = {
    "control_deliberation_pct": 40,
    "control_budget_pct": 25,
    "control_frequency_pct": 30,
    "control_entry_surface_pct": 50,  # mixed
    "control_complexity_pct": 40,  # defined options
    "control_rotation_pct": 40,
}
RISK_POSTURES = frozenset({"defensive", "balanced", "aggressive"})
# Risk-only capital presets (never touch Controls / capacity / universe).
_POSTURE_SEEDS: dict[str, dict[str, Any]] = {
    "defensive": {
        "max_risk_per_trade_pct": 0.75,
        "daily_loss_limit_pct": 3.0,
        "max_position_pct": 8.0,
        "max_peak_drawdown_pct": 8.0,
    },
    "balanced": {
        "max_risk_per_trade_pct": 1.5,
        "daily_loss_limit_pct": 5.0,
        "max_position_pct": 12.0,
        "max_peak_drawdown_pct": 12.0,
    },
    "aggressive": {
        "max_risk_per_trade_pct": 2.5,
        "daily_loss_limit_pct": 8.0,
        "max_position_pct": 18.0,
        "max_peak_drawdown_pct": 20.0,
    },
}
# (floor, ceiling) for agent-tunable capital knobs only.
_POSTURE_ENVELOPES: dict[str, dict[str, tuple[float, float]]] = {
    "defensive": {
        "max_risk_per_trade_pct": (0.25, 2.0),
        "daily_loss_limit_pct": (1.0, 6.0),
        "max_position_pct": (2.0, 15.0),
        "max_peak_drawdown_pct": (2.0, 15.0),
        "max_option_premium_pct": (0.0, 5.0),
    },
    "balanced": {
        "max_risk_per_trade_pct": (0.25, 4.0),
        "daily_loss_limit_pct": (1.0, 10.0),
        "max_position_pct": (2.0, 25.0),
        "max_peak_drawdown_pct": (2.0, 25.0),
        "max_option_premium_pct": (0.0, 8.0),
    },
    "aggressive": {
        "max_risk_per_trade_pct": (0.25, 6.0),
        "daily_loss_limit_pct": (1.0, 15.0),
        "max_position_pct": (2.0, 35.0),
        "max_peak_drawdown_pct": (2.0, 35.0),
        "max_option_premium_pct": (0.0, 12.0),
    },
}
_POSTURE_PROMPT_BIAS: dict[str, str] = {
    "defensive": (
        "Capital envelope: tight (see Risk gates). Hunt requires setup_grade A (code). "
        "Not a style tip."
    ),
    "balanced": (
        "Capital envelope: mid. setup_grade C hunts blocked (code)."
    ),
    "aggressive": (
        "Capital envelope: wide. Not a directive to trade more or chase a ranked tape."
    ),
}
_runtime_overrides: dict[str, Any] = {}
_file_overrides: dict[str, Any] = {}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_RISK_SETTINGS_PATH = _REPO_ROOT / "risk_settings.json"

DEFAULT_MANDATE = (
    "You OWN the whole paper IBKR book as % of NetLiq (full account; no dollar sleeve). "
    "Product is Grokfolio: construct ~15 long holdings, rebalance on the hourly/daily "
    "RTH clock. Hunt/hold scalping is NOT the product. Same rules at any NetLiq. "
    "No human approval. Hard risk is code and cannot be weakened (daily-loss halt, "
    "max position, defined-risk, unprotected-STK protect-first, exits never blocked, "
    "fail-closed). You MAY self_tune: Controls dials, pacing (only lengthen), universe "
    "focus, prompt_extra, strategy tweaks, and tighter risk. You cannot disable "
    "grokfolio or set a dollar sleeve. "
    "Primary scorecard: book return % on starting NetLiq must beat model API cost. "
    "Read your journal + scorecard every cycle and tune yourself. "
    "Protect first. Hold IS VALID when protected. Hold is FORBIDDEN only while "
    "unprotected STK exists (code enforces). Never blow up. Live remains gated."
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
    # Human-authored beliefs only (empty = inject nothing into prompts)
    operator_card: str = field(default="", repr=False)
    # Deprecated: legacy web dashboard scan loop (abcxauto.web); Pro path ignores these.
    scan_enabled: bool = False
    scan_interval_s: int = 900
    # Max MDA symbols Grok may request per cycle via scan_request.
    scan_fetch_cap: int = 4

    # 0 = full NetLiq (default). Size/loss gates are always % of portfolio.
    trading_budget_usd: float = 0.0
    # Autonomous cycle cadence — long floors so model cost cannot eat a small book.
    cycle_sleep_s: float = 300.0
    grok_min_interval_s: float = 300.0
    pace_protect_s: float = 20.0
    pace_manage_s: float = 60.0
    pace_idle_s: float = 600.0

    # Background P&L monitor
    monitor_enabled: bool = True
    monitor_poll_s: int = 30
    monitor_review_s: int = 300
    monitor_extended_hours: bool = False

    # Deprecated: legacy web dashboard bind (abcxauto.web); Pro cockpit is the sole UI.
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # Walk-away floor — ON by default; agent may tighten, never weaken.
    risk_posture: str = "defensive"
    risk_gates_enabled: bool = True
    daily_loss_limit_pct: float = 2.0
    max_position_pct: float = 20.0
    auto_panic_on_breach: bool = True
    defined_risk_only: bool = True
    cash_only: bool = True
    max_peak_drawdown_pct: float = 8.0
    max_option_premium_pct: float = 5.0
    max_risk_per_trade_pct: float = 1.0
    max_open_positions: int = 15
    # Grokfolio: Autopilot-style book owner on an hourly/daily clock.
    grokfolio_enabled: bool = True
    grokfolio_cadence: str = "both"  # hourly | daily | both
    grokfolio_holdings: int = 15
    control_deliberation_pct: int = 40
    control_budget_pct: int = 25
    control_frequency_pct: int = 30
    control_entry_surface_pct: int = 50
    control_complexity_pct: int = 40
    control_rotation_pct: int = 40
    # Deprecated no-ops (kept so older tests/settings don't explode).
    max_daily_trades: int = 0
    min_reward_risk: float = 0.0
    control_options_pct: int = 50  # migrate → control_complexity_pct on load
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


def _grokfolio_cadence_env() -> str:
    raw = _env("ABCXAUTO_GROKFOLIO_CADENCE", "both").lower()
    return raw if raw in ("hourly", "daily", "both") else "both"


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


def load_operator_card() -> str:
    """Env ABCXAUTO_OPERATOR_CARD, else operator_card.txt, else empty."""
    import os

    env = (os.environ.get("ABCXAUTO_OPERATOR_CARD") or "").strip()
    if env:
        return env[:4000]
    path_raw = (os.environ.get("ABCXAUTO_OPERATOR_CARD_PATH") or "").strip()
    path = Path(path_raw) if path_raw else _REPO_ROOT / "operator_card.txt"
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()[:4000]
    except OSError:
        pass
    return ""


def format_operator_card_block(card: str | None = None) -> str:
    """Optional free-text Card (advanced). Prefer Controls tab dials."""
    text = (card if card is not None else load_operator_card()).strip()
    if not text:
        return ""
    return (
        "OPERATOR CARD (optional free-text — secondary to CONTROLS dials):\n"
        + text
    )


def _control_pct(cfg: Any, key: str, default: int = 50) -> int:
    try:
        raw = getattr(cfg, key, default)
        if raw is None:
            raw = default
        return int(max(0, min(100, int(float(raw)))))
    except (TypeError, ValueError):
        return default


def deliberation_requires_act(cfg: Any = None) -> bool:
    """True when Controls deliberation is S2-lean — no cheap Act-skip."""
    c = cfg if cfg is not None else get_config()
    return _control_pct(c, "control_deliberation_pct") >= DELIBERATION_REQUIRE_ACT_PCT


def effective_grok_min_interval_s(cfg: Any = None) -> float:
    """Grok spacing after Intelligence budget dial (higher budget → shorter wait).

    At budget 50 → base interval. At 0 → ~1.75× base. At 100 → ~0.4× base.
    Floor 5s so protect path still works under tests.
    """
    c = cfg if cfg is not None else get_config()
    try:
        base = float(getattr(c, "grok_min_interval_s", 120.0) or 120.0)
    except (TypeError, ValueError):
        base = 120.0
    budget = _control_pct(c, "control_budget_pct")
    mult = 1.75 - (budget / 100.0) * 1.35
    return max(5.0, base * mult)


def rotation_redeploy_lean(cfg: Any = None) -> bool:
    """True when Controls rotation dial authorizes redeploy pressure (process)."""
    c = cfg if cfg is not None else get_config()
    return _control_pct(c, "control_rotation_pct") >= ROTATION_REDEPLOY_PCT


def format_controls_block(cfg: Any = None) -> str:
    """Fact block: Controls tab — deliberation, budget, frequency, complexity, rotation, capacity."""
    c = cfg if cfg is not None else get_config()
    delib = _control_pct(c, "control_deliberation_pct")
    budget = _control_pct(c, "control_budget_pct")
    frequency = _control_pct(c, "control_frequency_pct")
    rotation = _control_pct(c, "control_rotation_pct")
    require_act = deliberation_requires_act(c)
    grok_min = effective_grok_min_interval_s(c)
    try:
        max_open = int(getattr(c, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        max_open = 0
    from abcxauto.structure_complexity import complexity_fact, entry_surface_fact

    lines = [
        "CONTROLS (agent-owned — Fact of current self_tune; not operator dials):",
        f"- deliberation={delib} (0=System1 lean … 100=System2 mega-worker; "
        f"require_act={require_act})",
        f"- intelligence_budget={budget} (0=protect API $ … 100=more frequent Grok; "
        f"effective_grok_min_s={grok_min:.0f})",
        f"- trade_frequency={frequency} (0=patient … 100=higher trade rate OK — "
        f"process/streams only; never bypasses unprotected or book capacity)",
        f"- capital_rotation={rotation} (0=hold protected book OK … 100=redeploy/"
        f"trim/exit to free cash for better setups OK — process only; shell never "
        f"auto-sells)",
        f"- {entry_surface_fact(c)}",
        f"- {complexity_fact(c)}",
        f"- book_capacity max_open_positions={max_open} "
        f"(0=unlimited; Controls-owned hard gate)",
    ]
    if require_act:
        lines.append(
            "HEURISTIC (labeled; not a shell hold gate): System2 lean — show "
            "base-rate / pre-mortem / alternatives in rationale when acting; "
            "shell will not skip Act after manage/idle hold."
        )
    if rotation_redeploy_lean(c):
        lines.append(
            "HEURISTIC (labeled; not a shell sell gate): capital_rotation high — "
            f"when cash is thin (<{ROTATION_THIN_CASH_PCT:.0f}% of NL), prefer "
            "trim/exit/rotate weak or crowded names to free cash before forcing "
            "new risk; shell does not auto-sell."
        )
    try:
        from abcxauto.universe import universe_fact_block

        lines.append("")
        lines.append(universe_fact_block())
    except Exception:
        pass
    return "\n".join(lines)


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
        operator_card=load_operator_card(),
        scan_enabled=_env_bool("ABCXAUTO_SCAN_ENABLED", False),
        scan_interval_s=int(_env("ABCXAUTO_SCAN_INTERVAL_S", "900")),
        scan_fetch_cap=int(_env("ABCXAUTO_SCAN_FETCH_CAP", "4")),
        trading_budget_usd=float(
            _env("ABCXAUTO_TRADING_BUDGET_USD")
            or _env("ABCXAUTO_TARGET_CAPITAL")
            or "0"
        ),
        cycle_sleep_s=float(_env("ABCXAUTO_CYCLE_SLEEP_S", "300")),
        grok_min_interval_s=float(_env("ABCXAUTO_GROK_MIN_INTERVAL_S", "300")),
        pace_protect_s=float(_env("ABCXAUTO_PACE_PROTECT_S", "20")),
        pace_manage_s=float(_env("ABCXAUTO_PACE_MANAGE_S", "60")),
        pace_idle_s=float(_env("ABCXAUTO_PACE_IDLE_S", "600")),
        monitor_enabled=_env_bool("ABCXAUTO_MONITOR_ENABLED", True),
        monitor_poll_s=int(_env("ABCXAUTO_MONITOR_POLL_S", "30")),
        monitor_review_s=int(_env("ABCXAUTO_MONITOR_REVIEW_S", "300")),
        monitor_extended_hours=_env_bool("ABCXAUTO_MONITOR_EXTENDED_HOURS", False),
        web_host=_env("ABCXAUTO_WEB_HOST", "127.0.0.1"),
        web_port=int(_env("ABCXAUTO_WEB_PORT", "8000")),
        risk_posture=_normalize_posture(_env("ABCXAUTO_RISK_POSTURE", "defensive")),
        risk_gates_enabled=_env_bool("ABCXAUTO_RISK_GATES_ENABLED", True),
        daily_loss_limit_pct=float(_env("ABCXAUTO_DAILY_LOSS_LIMIT_PCT", "2")),
        max_position_pct=float(_env("ABCXAUTO_MAX_POSITION_PCT", "20")),
        max_open_positions=int(_env("ABCXAUTO_MAX_OPEN_POSITIONS", "15")),
        grokfolio_enabled=_env_bool("ABCXAUTO_GROKFOLIO_ENABLED", True),
        grokfolio_cadence=_grokfolio_cadence_env(),
        grokfolio_holdings=max(
            1, min(30, int(_env("ABCXAUTO_GROKFOLIO_HOLDINGS", "15") or "15"))
        ),
        auto_panic_on_breach=_env_bool("ABCXAUTO_AUTO_PANIC_ON_BREACH", True),
        defined_risk_only=_env_bool("ABCXAUTO_DEFINED_RISK_ONLY", True),
        cash_only=_env_bool("ABCXAUTO_CASH_ONLY", True),
        max_peak_drawdown_pct=float(_env("ABCXAUTO_MAX_PEAK_DRAWDOWN_PCT", "8")),
        max_option_premium_pct=float(_env("ABCXAUTO_MAX_OPTION_PREMIUM_PCT", "5")),
        max_risk_per_trade_pct=float(_env("ABCXAUTO_MAX_RISK_PER_TRADE_PCT", "1")),
        control_deliberation_pct=int(
            float(
                _env("ABCXAUTO_CONTROL_DELIBERATION_PCT")
                or _env("ABCXAUTO_CONTROL_MANAGE_PCT", "40")
            )
        ),
        control_budget_pct=int(float(_env("ABCXAUTO_CONTROL_BUDGET_PCT", "25"))),
        control_frequency_pct=int(float(_env("ABCXAUTO_CONTROL_FREQUENCY_PCT", "30"))),
        control_entry_surface_pct=int(
            float(_env("ABCXAUTO_CONTROL_ENTRY_SURFACE_PCT", "50"))
        ),
        control_complexity_pct=int(
            float(
                _env("ABCXAUTO_CONTROL_COMPLEXITY_PCT")
                or _env("ABCXAUTO_CONTROL_OPTIONS_PCT", "40")
            )
        ),
        control_rotation_pct=int(float(_env("ABCXAUTO_CONTROL_ROTATION_PCT", "40"))),
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
    if key == "max_open_positions":
        return int(max(0, min(10_000, int(float(value)))))
    if key in CONTROL_KEYS and key.startswith("control_"):
        return int(max(0, min(100, int(float(value)))))
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
        # Ignore removed knobs (max_daily_trades, min_reward_risk, …).
        if key not in PERSISTED_OPERATOR_KEYS and key not in (
            "control_options_pct",
            "control_manage_pct",
        ):
            continue
        try:
            if key in ("control_options_pct", "control_manage_pct"):
                continue  # migrated below
            cleaned[key] = _coerce_risk_value(key, value)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid risk setting %s=%r", key, value)
    # Migrate legacy dials.
    if "control_deliberation_pct" not in cleaned and "control_manage_pct" in raw:
        try:
            cleaned["control_deliberation_pct"] = _coerce_risk_value(
                "control_deliberation_pct", raw["control_manage_pct"]
            )
        except (TypeError, ValueError):
            pass
    if "control_complexity_pct" not in cleaned and "control_options_pct" in raw:
        try:
            cleaned["control_complexity_pct"] = _coerce_risk_value(
                "control_complexity_pct", raw["control_options_pct"]
            )
        except (TypeError, ValueError):
            pass
    # Split legacy single complexity dial into entry surface + option complexity.
    # Old <40 = stock-only → entry=stock. Old ≥40 = stock+options → entry=mixed
    # (never auto-promote to options-only — that must be an explicit operator choice).
    if "control_entry_surface_pct" not in cleaned:
        try:
            old_c = int(
                cleaned.get(
                    "control_complexity_pct",
                    raw.get("control_complexity_pct", raw.get("control_options_pct", 50)),
                )
            )
        except (TypeError, ValueError):
            old_c = 50
        cleaned["control_entry_surface_pct"] = 20 if old_c < 40 else 50
    return cleaned


def load_risk_settings(path: Path | None = None) -> dict[str, Any]:
    """Load persisted Risk + Controls knobs from disk into ``_file_overrides``."""
    global _file_overrides
    _file_overrides = _read_risk_file(path or _risk_settings_path())
    return dict(_file_overrides)


def save_risk_settings(
    values: dict[str, Any],
    *,
    path: Path | None = None,
) -> Path:
    """Merge ``values`` into the on-disk settings file (Risk and/or Controls keys)."""
    global _file_overrides
    settings_path = path or _risk_settings_path()
    current = _read_risk_file(settings_path)
    for key, value in values.items():
        if key not in PERSISTED_OPERATOR_KEYS:
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
    """Env-backed config plus file-persisted risk knobs, agent_state, session overrides.

    Precedence: ``.env`` defaults < ``risk_settings.json`` < ``agent_state.json``
    < session overrides.
    """
    base = _load_env_config()
    agent_extra: dict[str, Any] = {}
    try:
        from abcxauto.self_tune import load_agent_state

        raw = load_agent_state()
        allowed = {
            "cycle_sleep_s",
            "grok_min_interval_s",
            "pace_protect_s",
            "pace_manage_s",
            "pace_idle_s",
            "scan_fetch_cap",
            "system_prompt_extra",
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
    """Apply Risk-tab capital overrides only (never Controls keys).

    Pass ``persist=False`` for session-only. SET_RISK_KEYS clamped to posture
    envelope when posture is set (unless ``_skip_clamp=True``).
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


def update_controls_config(**kwargs: Any) -> Config:
    """Apply Controls-tab overrides only (never Risk capital keys)."""
    persist = bool(kwargs.pop("persist", True))
    unknown = set(kwargs) - CONTROL_KEYS
    if unknown:
        raise ValueError(f"Unknown controls config keys: {sorted(unknown)}")
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
            logger.exception("Failed to persist controls settings")
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
    """Seed capital knobs, then clamp to the walk-away floor.

    Operator UI no longer uses this. Kept for tests / emergency. Cannot weaken
    daily-loss, size, defined-risk, or other floor gates.
    """
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
    from abcxauto.self_tune import clamp_risk_to_floor

    clamped_seed: dict[str, Any] = {}
    for key, value in seed.items():
        new_v, _note = clamp_risk_to_floor(key, value)
        clamped_seed[key] = new_v if new_v is not None else value
    payload: dict[str, Any] = {
        "risk_posture": "defensive",  # walk-away floor identity
        "risk_gates_enabled": True,
        "auto_panic_on_breach": True,
        "defined_risk_only": True,
        "cash_only": True,
        **clamped_seed,
    }
    return update_risk_config(**payload, persist=persist, _skip_clamp=True)


def set_risk_knobs(
    values: dict[str, Any],
    *,
    persist: bool = True,
) -> dict[str, Any]:
    """Agent self-tune path (set_risk alias). No operator approval. Floor-clamped."""
    from abcxauto.self_tune import apply_self_tune

    return apply_self_tune(values or {}, persist=persist)


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
    """Current effective Risk + Controls knobs for Pro UI hydrate.

    Pass ``reload=True`` to re-read ``risk_settings.json`` from disk.
    """
    if reload:
        load_risk_settings()
    cfg = get_config()
    return {k: getattr(cfg, k) for k in sorted(PERSISTED_OPERATOR_KEYS)}


def controls_config_snapshot(*, reload: bool = False) -> dict[str, Any]:
    if reload:
        load_risk_settings()
    cfg = get_config()
    return {k: getattr(cfg, k) for k in sorted(CONTROL_KEYS)}


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
