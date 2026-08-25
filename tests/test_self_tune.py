"""Walk-away floor + agent self_tune (no approval). Size is % of NetLiq."""

import math
from types import SimpleNamespace

from abcxauto.config import (
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
    set_risk_knobs,
)
from abcxauto.self_tune import (
    MAX_OPEN_POSITIONS_RANGE,
    RISK_FLOOR,
    apply_self_tune,
    clamp_risk_to_floor,
    ensure_immutable_floor,
    floor_clamp_config_fields,
    is_self_tune_strategy,
    levers_snapshot,
)

_CEILING_KEYS = (
    "max_risk_per_trade_pct",
    "max_position_pct",
    "daily_loss_limit_pct",
)


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def test_is_self_tune_alias():
    assert is_self_tune_strategy("self_tune")
    assert is_self_tune_strategy("set_risk")
    assert not is_self_tune_strategy("set_controls")
    assert not is_self_tune_strategy("set_self")
    assert not is_self_tune_strategy("bracket")


def test_defaults_are_1k_floor():
    cfg = get_config()
    assert cfg.risk_posture == "defensive"
    assert cfg.daily_loss_limit_pct == 25.0
    assert cfg.max_position_pct == 25.0
    assert cfg.max_risk_per_trade_pct == 25.0
    assert cfg.max_peak_drawdown_pct == 25.0
    assert cfg.max_open_positions == 15
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True
    assert cfg.auto_panic_on_breach is True
    assert cfg.scan_fetch_cap == 8
    assert cfg.trading_budget_usd == 0.0
    assert not hasattr(cfg, "cycle_sleep_s")
    assert not hasattr(cfg, "control_budget_pct")


def test_cannot_weaken_daily_loss(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    out = apply_self_tune({"daily_loss_limit_pct": 50.0}, persist=True)
    assert out["status"] == "ok"
    hi = RISK_FLOOR["daily_loss_limit_pct"][1]
    assert get_config().daily_loss_limit_pct == hi
    assert "daily_loss_limit_pct" in out["clamped"]


def test_cannot_disable_defined_risk():
    out = apply_self_tune({"defined_risk_only": False}, persist=False)
    assert get_config().defined_risk_only is True
    assert out["status"] == "blocked" or "defined_risk_only" in (out.get("rejected") or {})


def test_cannot_raise_max_open_positions():
    out = apply_self_tune({"max_open_positions": 99}, persist=False)
    assert out["status"] == "ok"
    assert get_config().max_open_positions == MAX_OPEN_POSITIONS_RANGE[1]


def test_cannot_set_trading_budget_sleeve():
    out = apply_self_tune({"trading_budget_usd": 50_000}, persist=False)
    assert get_config().trading_budget_usd == 0.0
    assert "trading_budget_usd" in (out.get("rejected") or {})


def test_size_ceiling_is_25_pct_of_nl():
    v, note = clamp_risk_to_floor("max_risk_per_trade_pct", 50)
    assert v == 25.0
    assert note is not None
    v2, _ = clamp_risk_to_floor("max_option_premium_pct", 50)
    assert v2 == 25.0
    v3, _ = clamp_risk_to_floor("max_position_pct", 50)
    assert v3 == 25.0
    v4, _ = clamp_risk_to_floor("daily_loss_limit_pct", 50)
    assert v4 == 25.0
    v5, _ = clamp_risk_to_floor("max_peak_drawdown_pct", 50)
    assert v5 == 25.0


def test_levers_snapshot_shows_now_and_range():
    snap = levers_snapshot()
    risk = snap["max_risk_per_trade_pct"]
    assert risk["min"] == 0.25
    assert risk["max"] == 25.0
    assert risk["now"] is not None
    assert snap["max_open_positions"]["min"] == MAX_OPEN_POSITIONS_RANGE[0]
    assert snap["max_open_positions"]["max"] == MAX_OPEN_POSITIONS_RANGE[1]
    assert snap["change"] == "self_tune"


def test_can_tighten_risk():
    out = apply_self_tune({"max_risk_per_trade_pct": 0.5}, persist=False)
    assert out["status"] == "ok"
    assert get_config().max_risk_per_trade_pct == 0.5


def test_dead_pacing_and_control_dials_are_rejected():
    out = apply_self_tune(
        {
            "cycle_sleep_s": 480,
            "pace_idle_s": 900,
            "control_budget_pct": 10,
            "control_frequency_pct": 20,
        },
        persist=False,
    )
    rejected = out.get("rejected") or {}
    assert "cycle_sleep_s" in rejected
    assert "pace_idle_s" in rejected
    assert "control_budget_pct" in rejected
    assert "control_frequency_pct" in rejected
    assert out["status"] == "blocked" or not (out.get("applied") or {})


def test_cannot_switch_to_live():
    out = apply_self_tune({"trading_mode": "live"}, persist=False)
    assert get_config().trading_mode == "paper"
    assert "trading_mode" in (out.get("rejected") or {}) or out["status"] == "blocked"


def test_clamp_risk_to_floor_cannot_raise_past_ceiling():
    for key in _CEILING_KEYS:
        hi = RISK_FLOOR[key][1]
        for raw in (hi + 1.0, 99.0, 1e9, "50"):
            v, note = clamp_risk_to_floor(key, raw)
            assert v is not None
            assert v <= hi
            assert note is not None
    v, note = clamp_risk_to_floor("max_open_positions", 99)
    assert v == MAX_OPEN_POSITIONS_RANGE[1]
    assert note is not None


def test_clamp_risk_to_floor_rejects_nonfinite():
    for key in _CEILING_KEYS:
        assert clamp_risk_to_floor(key, float("nan")) == (None, None)
        assert clamp_risk_to_floor(key, float("inf")) == (None, None)
        assert clamp_risk_to_floor(key, float("-inf")) == (None, None)
        assert clamp_risk_to_floor(key, "NaN") == (None, None)
    assert clamp_risk_to_floor("max_open_positions", float("nan")) == (None, None)
    assert clamp_risk_to_floor("max_open_positions", float("inf")) == (None, None)


def test_malicious_self_tune_cannot_weaken_floor_or_go_live(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    apply_self_tune(
        {
            "max_risk_per_trade_pct": 0.5,
            "max_position_pct": 5.0,
            "daily_loss_limit_pct": 1.0,
            "max_open_positions": 2,
        },
        persist=True,
    )

    payload = {
        "trading_mode": "live",
        "live_confirm": "I_UNDERSTAND_LIVE_TRADING_RISK",
        "ibkr_port": 7496,
        "is_paper": False,
        "max_risk_per_trade_pct": 99,
        "max_position_pct": 1e9,
        "daily_loss_limit_pct": "100",
        "max_open_positions": 10_000,
        "defined_risk_only": False,
        "cash_only": False,
        "risk_gates_enabled": False,
        "auto_panic_on_breach": False,
        "sizing_floors": False,
        "trading_budget_usd": 50_000,
        "risk_posture": "aggressive",
        "risk": {
            "trading_mode": "live",
            "live_confirm": "I_UNDERSTAND_LIVE_TRADING_RISK",
            "ibkr_port": 7496,
            "max_risk_per_trade_pct": float("nan"),
            "max_position_pct": float("inf"),
            "daily_loss_limit_pct": 50,
            "max_open_positions": 99,
        },
    }
    out = apply_self_tune(payload, persist=True)
    cfg = get_config()
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7497
    assert cfg.is_paper is True
    assert not cfg.live_confirm
    assert cfg.max_risk_per_trade_pct <= RISK_FLOOR["max_risk_per_trade_pct"][1]
    assert cfg.max_position_pct <= RISK_FLOOR["max_position_pct"][1]
    assert cfg.daily_loss_limit_pct <= RISK_FLOOR["daily_loss_limit_pct"][1]
    assert cfg.max_open_positions <= MAX_OPEN_POSITIONS_RANGE[1]
    assert math.isfinite(cfg.max_risk_per_trade_pct)
    assert math.isfinite(cfg.max_position_pct)
    assert math.isfinite(cfg.daily_loss_limit_pct)
    assert cfg.max_risk_per_trade_pct != 99
    assert cfg.max_position_pct != 1e9
    assert cfg.daily_loss_limit_pct != 100
    assert cfg.max_open_positions != 10_000
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True
    assert cfg.risk_gates_enabled is True
    assert cfg.auto_panic_on_breach is True
    assert cfg.trading_budget_usd == 0.0
    rejected = out.get("rejected") or {}
    assert "trading_mode" in rejected
    assert "live_confirm" in rejected
    assert "ibkr_port" in rejected
    applied = out.get("applied") or {}
    assert "trading_mode" not in applied
    assert "live_confirm" not in applied
    assert "ibkr_port" not in applied
    if path.is_file():
        persisted = path.read_text(encoding="utf-8")
        assert "7496" not in persisted
        assert '"trading_mode"' not in persisted


def test_nonfinite_self_tune_cannot_poison_floor_or_go_live():
    before = get_config()
    out = apply_self_tune(
        {
            "max_risk_per_trade_pct": float("nan"),
            "max_position_pct": float("inf"),
            "daily_loss_limit_pct": float("-inf"),
            "max_open_positions": float("nan"),
            "trading_mode": "live",
            "ibkr_port": 7496,
        },
        persist=False,
    )
    cfg = get_config()
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7497
    assert cfg.max_risk_per_trade_pct == before.max_risk_per_trade_pct
    assert cfg.max_position_pct == before.max_position_pct
    assert cfg.daily_loss_limit_pct == before.daily_loss_limit_pct
    assert cfg.max_open_positions == before.max_open_positions
    assert math.isfinite(cfg.max_risk_per_trade_pct)
    rejected = out.get("rejected") or {}
    assert "max_risk_per_trade_pct" in rejected
    assert "max_position_pct" in rejected
    assert "daily_loss_limit_pct" in rejected
    assert "max_open_positions" in rejected
    assert "trading_mode" in rejected
    assert out["status"] == "blocked" or not (out.get("applied") or {})


def test_nested_malicious_risk_blob_clamps_and_stays_paper():
    out = apply_self_tune(
        {
            "risk": {
                "trading_mode": "live",
                "live_confirm": "I_UNDERSTAND_LIVE_TRADING_RISK",
                "ibkr_port": 7496,
                "max_risk_per_trade_pct": 50,
                "max_position_pct": 50,
                "daily_loss_limit_pct": 50,
                "max_open_positions": 99,
            }
        },
        persist=False,
    )
    cfg = get_config()
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7497
    assert cfg.max_risk_per_trade_pct == RISK_FLOOR["max_risk_per_trade_pct"][1]
    assert cfg.max_position_pct == RISK_FLOOR["max_position_pct"][1]
    assert cfg.daily_loss_limit_pct == RISK_FLOOR["daily_loss_limit_pct"][1]
    assert cfg.max_open_positions == MAX_OPEN_POSITIONS_RANGE[1]
    rejected = out.get("rejected") or {}
    assert "trading_mode" in rejected
    assert "ibkr_port" in rejected
    applied = out.get("applied") or {}
    assert applied.get("max_risk_per_trade_pct") == RISK_FLOOR["max_risk_per_trade_pct"][1]


def test_floor_clamp_repairs_nonfinite():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=float("nan"),
        max_position_pct=float("inf"),
        max_risk_per_trade_pct=float("-inf"),
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_open_positions=15,
        risk_gates_enabled=True,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="paper",
        sizing_floors=False,
    )
    fixes = floor_clamp_config_fields(cfg)
    assert fixes["daily_loss_limit_pct"] == 25.0
    assert fixes["max_position_pct"] == 25.0
    assert fixes["max_risk_per_trade_pct"] == 25.0


def test_set_risk_alias_no_approval():
    out = set_risk_knobs({"max_risk_per_trade_pct": 0.6}, persist=False)
    assert out["status"] == "ok"
    assert get_config().max_risk_per_trade_pct == 0.6


def test_nested_self_tune_universe(tmp_path, monkeypatch):
    uni = tmp_path / "uni.json"
    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(uni))
    out = apply_self_tune(
        {"universe": {"enabled_arenas": ["index_etfs"], "custom_symbols": ["SPY"]}},
        persist=False,
    )
    assert out["status"] == "ok"
    assert "universe" in out["applied"]


def test_prompt_extra_is_gone():
    out = apply_self_tune({"prompt_extra": "Prefer cheap defined-risk verticals."}, persist=False)
    assert "prompt_extra" in (out.get("rejected") or {})


def test_ensure_floor_repairs_weak_settings(tmp_path, monkeypatch):
    from abcxauto.config import update_capacity_config, update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(
        daily_loss_limit_pct=0.0,
        defined_risk_only=False,
        persist=False,
        _skip_clamp=True,
    )
    update_capacity_config(max_open_positions=0, persist=False)
    ensure_immutable_floor(persist=False)
    cfg = get_config()
    assert cfg.daily_loss_limit_pct == 25.0
    assert cfg.defined_risk_only is True
    assert cfg.max_open_positions == 15


def test_grok_cannot_self_tune_risk_posture(tmp_path, monkeypatch):
    from abcxauto.config import update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(risk_posture="aggressive", persist=True)
    out = apply_self_tune({"risk_posture": "defensive"}, persist=True)
    assert get_config().risk_posture == "aggressive"
    assert "risk_posture" in (out.get("rejected") or {})
    assert out["status"] == "blocked" or not (out.get("applied") or {})


def test_ensure_floor_does_not_bounce_operator_posture(tmp_path, monkeypatch):
    from abcxauto.config import update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(risk_posture="aggressive", persist=True)
    ensure_immutable_floor(persist=True)
    cfg = get_config()
    assert cfg.risk_posture == "aggressive"
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True


def test_clamp_risk_helper():
    v, note = clamp_risk_to_floor("max_position_pct", 99)
    assert v == RISK_FLOOR["max_position_pct"][1]
    assert note is not None


def test_get_config_clamps_weak_file(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    path.write_text(
        '{"max_open_positions": 25, "daily_loss_limit_pct": 50, '
        '"defined_risk_only": false, "risk_posture": "aggressive", '
        '"trading_budget_usd": 1000}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto.config import load_risk_settings

    load_risk_settings(path)
    cfg = get_config()
    assert cfg.max_open_positions == 25
    assert cfg.daily_loss_limit_pct == 25.0
    assert cfg.defined_risk_only is True
    assert cfg.risk_posture == "aggressive"
    assert cfg.trading_budget_usd == 0.0


def test_floor_clamp_does_not_force_defensive_posture():
    from abcxauto.config import update_risk_config
    from abcxauto.self_tune import floor_clamp_config_fields

    update_risk_config(risk_posture="aggressive", persist=False)
    cfg = get_config()
    assert cfg.risk_posture == "aggressive"
    assert "risk_posture" not in floor_clamp_config_fields(cfg)


def test_grok_may_set_open_positions_to_two(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    path.write_text('{"max_open_positions": 2}\n', encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto.config import load_risk_settings

    load_risk_settings(path)
    cfg = get_config()
    assert cfg.max_open_positions == 2


def test_can_self_tune_open_positions_to_three():
    out = apply_self_tune({"max_open_positions": 3}, persist=False)
    assert out["status"] == "ok"
    assert get_config().max_open_positions == 3


def test_self_tune_is_sendable():
    from abcxauto.agent_loop import ALLOWED_ACTIONS, normalize_action

    strat, forced = normalize_action(
        {"strategy": "self_tune", "params": {"max_risk_per_trade_pct": 0.5}}
    )
    assert strat == "self_tune"
    assert forced is None
    assert "self_tune" in ALLOWED_ACTIONS
