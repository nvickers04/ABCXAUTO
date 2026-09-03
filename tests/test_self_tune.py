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
    slot_cap_armed,
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
    assert cfg.max_open_positions == 0
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True
    assert cfg.auto_panic_on_breach is True
    assert cfg.scan_fetch_cap == 8
    assert cfg.session_look_cap == 160
    assert cfg.session_token_cap == 2_500_000
    assert cfg.trading_budget_usd == 0.0
    assert not hasattr(cfg, "cycle_sleep_s")
    assert not hasattr(cfg, "control_budget_pct")


def test_cannot_weaken_daily_loss(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    from abcxauto.config import update_risk_config

    update_risk_config(daily_loss_limit_pct=25.0, persist=True)
    out = apply_self_tune({"daily_loss_limit_pct": 50.0}, persist=True)
    assert get_config().daily_loss_limit_pct == 25.0
    assert "daily_loss_limit_pct" in (out.get("rejected") or {})
    assert "daily_loss_limit_pct" not in (out.get("applied") or {})


def test_cannot_disable_defined_risk():
    out = apply_self_tune({"defined_risk_only": False}, persist=False)
    assert get_config().defined_risk_only is True
    assert out["status"] == "blocked" or "defined_risk_only" in (out.get("rejected") or {})


def test_paper_self_tune_mop_is_not_capped_at_25():
    """Operator mop is not shoved into a baked 25. Grok cannot rewrite it. 0 stays off."""
    from abcxauto.config import update_capacity_config

    update_capacity_config(max_open_positions=99, persist=True)
    out = apply_self_tune({"max_open_positions": 8}, persist=True)
    assert "max_open_positions" in (out.get("rejected") or {})
    assert get_config().max_open_positions == 99
    assert get_config().max_open_positions != 25
    update_capacity_config(max_open_positions=0, persist=True)
    off = apply_self_tune({"max_open_positions": 8}, persist=True)
    assert "max_open_positions" in (off.get("rejected") or {})
    assert get_config().max_open_positions == 0


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
    assert snap["max_risk_per_trade_pct"]["off"] == 0
    assert snap["max_position_pct"]["off"] == 0
    assert snap["max_option_premium_pct"]["off"] == 0
    assert "off" not in snap["daily_loss_limit_pct"]
    assert snap["max_open_positions"]["min"] == MAX_OPEN_POSITIONS_RANGE[0]
    assert snap["max_open_positions"]["min"] == 0
    assert snap["max_open_positions"]["off"] == 0
    # Do not invent 25 as a ceiling on paper or live.
    assert "max" not in snap["max_open_positions"]
    assert snap["max_open_positions"]["pick"] == "this book"
    assert snap["max_open_positions"]["with"] == "size_pct_nl"
    assert "not pick-one" in snap["together"]
    assert snap["change"] == "self_tune"


def test_live_levers_snapshot_does_not_invent_25():
    cfg = SimpleNamespace(
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        max_open_positions=0,
        max_risk_per_trade_pct=25.0,
        max_option_premium_pct=25.0,
        max_position_pct=25.0,
        daily_loss_limit_pct=25.0,
        max_peak_drawdown_pct=25.0,
    )
    snap = levers_snapshot(cfg)
    assert snap["max_open_positions"]["min"] == 0
    assert snap["max_open_positions"]["now"] == 0
    assert snap["max_open_positions"]["off"] == 0
    assert "max" not in snap["max_open_positions"]


def test_can_tighten_risk():
    """Grok cannot persist over operator max_risk. File wins. 0 stays 0."""
    from abcxauto.config import update_risk_config

    update_risk_config(max_risk_per_trade_pct=0, persist=True, _skip_clamp=True)
    out = apply_self_tune({"max_risk_per_trade_pct": 0.5}, persist=True)
    assert "max_risk_per_trade_pct" in (out.get("rejected") or {})
    assert "max_risk_per_trade_pct" not in (out.get("applied") or {})
    assert get_config().max_risk_per_trade_pct == 0.0


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
    assert v == 99
    assert note is None
    live = SimpleNamespace(trading_mode="live", ibkr_port=7496, is_paper=False)
    v_live, note_live = clamp_risk_to_floor("max_open_positions", 99, cfg=live)
    assert v_live == 99
    assert note_live is None
    v0, note0 = clamp_risk_to_floor("max_open_positions", 0, cfg=live)
    assert v0 == 0
    assert note0 is None


def test_zero_off_pct_ceilings_survive_get_config(tmp_path, monkeypatch):
    """0 = off for risk/position/premium. Not a 0.25 floor, not unsupervised 25%."""
    from abcxauto.config import update_risk_config

    keys = (
        "max_risk_per_trade_pct",
        "max_position_pct",
        "max_option_premium_pct",
    )
    for key in keys:
        v, note = clamp_risk_to_floor(key, 0)
        assert v == 0.0
        assert note is None

    v, note = clamp_risk_to_floor("max_risk_per_trade_pct", 0.1)
    assert v == 0.25
    assert note is not None
    v, note = clamp_risk_to_floor("max_risk_per_trade_pct", 2.0)
    assert v == 2.0
    assert note is None
    v, note = clamp_risk_to_floor("max_option_premium_pct", 2.0)
    assert v == 2.0
    assert note is None

    cfg0 = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=0.0,
        max_risk_per_trade_pct=0.0,
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=0.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=0,
        risk_gates_enabled=True,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        sizing_floors=False,
    )
    fixes = floor_clamp_config_fields(cfg0)
    for key in keys:
        assert key not in fixes

    cfg_neg = SimpleNamespace(**{**vars(cfg0), "max_risk_per_trade_pct": -1.0})
    assert floor_clamp_config_fields(cfg_neg)["max_risk_per_trade_pct"] == 25.0

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_risk_config(
        max_risk_per_trade_pct=0,
        max_position_pct=0,
        max_option_premium_pct=0,
        persist=True,
        _skip_clamp=True,
    )
    cfg = get_config()
    assert cfg.max_risk_per_trade_pct == 0.0
    assert cfg.max_position_pct == 0.0
    assert cfg.max_option_premium_pct == 0.0
    ensure_immutable_floor(persist=True)
    cfg = get_config()
    assert cfg.max_risk_per_trade_pct == 0.0
    assert cfg.max_position_pct == 0.0
    assert cfg.max_option_premium_pct == 0.0

    off = apply_self_tune(
        {
            "max_risk_per_trade_pct": 0.5,
            "max_position_pct": 5.0,
            "max_option_premium_pct": 1.0,
        },
        persist=True,
    )
    rejected = off.get("rejected") or {}
    assert "max_risk_per_trade_pct" in rejected
    assert "max_position_pct" in rejected
    assert "max_option_premium_pct" in rejected
    assert get_config().max_risk_per_trade_pct == 0.0
    assert get_config().max_position_pct == 0.0
    assert get_config().max_option_premium_pct == 0.0
    two = apply_self_tune({"max_risk_per_trade_pct": 2.0}, persist=True)
    assert "max_risk_per_trade_pct" in (two.get("rejected") or {})
    assert get_config().max_risk_per_trade_pct == 0.0
    lift = apply_self_tune({"max_risk_per_trade_pct": 0.1}, persist=True)
    assert "max_risk_per_trade_pct" in (lift.get("rejected") or {})
    assert get_config().max_risk_per_trade_pct == 0.0


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
    from abcxauto.config import update_capacity_config, update_risk_config

    update_risk_config(
        max_risk_per_trade_pct=0.5,
        max_position_pct=5.0,
        daily_loss_limit_pct=1.0,
        persist=True,
        _skip_clamp=True,
    )
    update_capacity_config(max_open_positions=2, persist=True)

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
    assert cfg.max_risk_per_trade_pct == 0.5
    assert cfg.max_position_pct == 5.0
    assert cfg.daily_loss_limit_pct == 1.0
    # Operator mop is not shoved into 25, and Grok cannot rewrite it.
    assert cfg.max_open_positions == 2
    assert math.isfinite(cfg.max_risk_per_trade_pct)
    assert math.isfinite(cfg.max_position_pct)
    assert math.isfinite(cfg.daily_loss_limit_pct)
    assert cfg.max_risk_per_trade_pct != 99
    assert cfg.max_position_pct != 1e9
    assert cfg.daily_loss_limit_pct != 100
    assert cfg.max_open_positions != 25
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
    # Nested operator knobs are ignored — defaults stay, mop is not 99.
    assert cfg.max_risk_per_trade_pct == RISK_FLOOR["max_risk_per_trade_pct"][1]
    assert cfg.max_position_pct == RISK_FLOOR["max_position_pct"][1]
    assert cfg.daily_loss_limit_pct == RISK_FLOOR["daily_loss_limit_pct"][1]
    assert cfg.max_open_positions == 0
    rejected = out.get("rejected") or {}
    assert "trading_mode" in rejected
    assert "ibkr_port" in rejected
    applied = out.get("applied") or {}
    assert "max_risk_per_trade_pct" not in applied
    assert "max_open_positions" not in applied


def test_floor_clamp_repairs_nonfinite():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=float("nan"),
        max_position_pct=float("inf"),
        max_risk_per_trade_pct=float("-inf"),
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
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
    out = set_risk_knobs({"max_peak_drawdown_pct": 8.0}, persist=False)
    assert out["status"] == "ok"
    assert get_config().max_peak_drawdown_pct == 8.0
    blocked = set_risk_knobs({"max_risk_per_trade_pct": 0.6}, persist=False)
    assert "max_risk_per_trade_pct" in (blocked.get("rejected") or {})
    assert get_config().max_risk_per_trade_pct != 0.6


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
    # Paper: 0 is unlimited, not shoved back to leftover 15.
    assert cfg.max_open_positions == 0


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


def test_paper_start_grok_mop_survives(tmp_path, monkeypatch):
    """Grok-chosen mop survives get_config + ensure_immutable_floor on paper."""
    from abcxauto.config import load_risk_settings, update_capacity_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "7497")
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_capacity_config(max_open_positions=40, persist=True)
    assert get_config().max_open_positions == 40
    ensure_immutable_floor(persist=True)
    assert get_config().max_open_positions == 40
    assert get_config().defined_risk_only is True
    assert get_config().cash_only is True
    clear_runtime_overrides()
    load_risk_settings(path)
    get_config.cache_clear()
    assert get_config().max_open_positions == 40


def test_paper_start_gates_off_stays_off(tmp_path, monkeypatch):
    """Operator-off on paper survives get_config + ensure_immutable_floor."""
    from abcxauto.config import load_risk_settings, update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "7497")
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_risk_config(risk_gates_enabled=False, persist=True, _skip_clamp=True)
    assert get_config().risk_gates_enabled is False
    ensure_immutable_floor(persist=True)
    assert get_config().risk_gates_enabled is False
    assert get_config().defined_risk_only is True
    assert get_config().cash_only is True
    clear_runtime_overrides()
    load_risk_settings(path)
    get_config.cache_clear()
    assert get_config().risk_gates_enabled is False
    persisted = path.read_text(encoding="utf-8")
    assert '"risk_gates_enabled": false' in persisted


def test_live_start_gates_off_repaired_on(tmp_path, monkeypatch):
    """Live start still forces gates, defined-risk, and sizing floors."""
    from abcxauto.config import load_risk_settings, update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("TRADING_MODE", "live")
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_risk_config(
        risk_gates_enabled=False,
        defined_risk_only=False,
        sizing_floors=False,
        persist=True,
        _skip_clamp=True,
    )
    ensure_immutable_floor(persist=True)
    cfg = get_config()
    assert cfg.risk_gates_enabled is True
    assert cfg.defined_risk_only is True
    assert cfg.sizing_floors is True


def test_live_start_mop_zero_stays_off(tmp_path, monkeypatch):
    """Live start does not invent a 15/25 slot cap. Gates and sizing floors stay on."""
    from abcxauto.config import load_risk_settings, update_capacity_config, update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("TRADING_MODE", "live")
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_capacity_config(max_open_positions=0, persist=True)
    update_risk_config(
        risk_gates_enabled=False,
        sizing_floors=False,
        persist=True,
        _skip_clamp=True,
    )
    ensure_immutable_floor(persist=True)
    cfg = get_config()
    assert cfg.max_open_positions == 0
    assert cfg.risk_gates_enabled is True
    assert cfg.sizing_floors is True
    assert cfg.defined_risk_only is True


def test_live_start_keeps_grok_mop_above_25(tmp_path, monkeypatch):
    """Live start does not cap a chosen mop at 25. Gates and sizing floors stay on."""
    from abcxauto.config import load_risk_settings, update_capacity_config, update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("TRADING_MODE", "live")
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_capacity_config(max_open_positions=99, persist=True)
    update_risk_config(
        risk_gates_enabled=False,
        sizing_floors=False,
        persist=True,
        _skip_clamp=True,
    )
    ensure_immutable_floor(persist=True)
    cfg = get_config()
    assert cfg.max_open_positions == 99
    assert cfg.max_open_positions != 25
    assert cfg.risk_gates_enabled is True
    assert cfg.sizing_floors is True
    assert cfg.defined_risk_only is True


def test_floor_clamp_paper_keeps_grok_mop_above_25():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=25.0,
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=40,
        risk_gates_enabled=False,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        sizing_floors=False,
    )
    assert "max_open_positions" not in floor_clamp_config_fields(cfg)


def test_floor_clamp_live_does_not_cap_mop_at_25():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=25.0,
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=40,
        risk_gates_enabled=True,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        sizing_floors=True,
    )
    assert "max_open_positions" not in floor_clamp_config_fields(cfg)


def test_floor_clamp_live_does_not_restore_zero_to_15():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=25.0,
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=0,
        risk_gates_enabled=True,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        sizing_floors=True,
    )
    assert "max_open_positions" not in floor_clamp_config_fields(cfg)


def test_floor_clamp_paper_does_not_force_gates_on():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=25.0,
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=15,
        risk_gates_enabled=False,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        sizing_floors=False,
    )
    fixes = floor_clamp_config_fields(cfg)
    assert "risk_gates_enabled" not in fixes


def test_floor_clamp_live_forces_gates_on():
    cfg = SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=25.0,
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=25.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=15,
        risk_gates_enabled=False,
        auto_panic_on_breach=True,
        defined_risk_only=False,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        sizing_floors=False,
    )
    fixes = floor_clamp_config_fields(cfg)
    assert fixes.get("risk_gates_enabled") is True
    assert fixes.get("defined_risk_only") is True
    assert fixes.get("sizing_floors") is True


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
    from abcxauto.config import update_capacity_config

    update_capacity_config(max_open_positions=2, persist=True)
    out = apply_self_tune({"max_open_positions": 3}, persist=False)
    assert "max_open_positions" in (out.get("rejected") or {})
    assert get_config().max_open_positions == 2


def test_self_tune_still_cannot_disable_live_floor_or_mode():
    out = apply_self_tune(
        {
            "trading_mode": "live",
            "ibkr_port": 7496,
            "risk_gates_enabled": False,
            "sizing_floors": False,
            "defined_risk_only": False,
            "cash_only": False,
        },
        persist=False,
    )
    cfg = get_config()
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7497
    rejected = out.get("rejected") or {}
    assert "trading_mode" in rejected
    assert "sizing_floors" in rejected
    assert "risk_gates_enabled" in rejected
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True


def test_book_width_is_not_a_baked_nl_formula():
    """Same leftover 12/25 is not the working width of a $1k and a $35k book."""
    import abcxauto.self_tune as st
    import abcxauto.trade_plan as tp
    import abcxauto.world_state as ws

    for mod in (st, tp, ws):
        assert not hasattr(mod, "slots_for_nl")
        assert not hasattr(mod, "slots_from_nl")
        assert not hasattr(mod, "nl_to_slots")

    from abcxauto.trade_plan import capacity_fact

    small = capacity_fact([], max_open_positions=6, net_liq=1_000, cap_armed=False)
    big = capacity_fact([], max_open_positions=40, net_liq=35_000, cap_armed=False)
    assert small["max_open_positions"] == 6
    assert big["max_open_positions"] == 40
    assert small["nl"] == 1_000
    assert big["nl"] == 35_000
    assert small["allows_new_risk"] is True
    assert big["allows_new_risk"] is True
    assert small["with_size"] == "size_pct_nl"
    assert big["with_size"] == "size_pct_nl"
    assert small["max_open_positions"] != big["max_open_positions"]
    assert 12 not in (small["max_open_positions"], big["max_open_positions"])
    assert 25 not in (small["max_open_positions"], big["max_open_positions"])


def test_self_tune_is_sendable():
    from abcxauto.agent_loop import ALLOWED_ACTIONS, normalize_action

    strat, forced = normalize_action(
        {"strategy": "self_tune", "params": {"max_risk_per_trade_pct": 0.5}}
    )
    assert strat == "self_tune"
    assert forced is None
    assert "self_tune" in ALLOWED_ACTIONS


def test_slot_cap_armed_is_positive_mop_only():
    """One rule: mop > 0 is a ceiling. 0 is off. No live-always / paper-gates fork."""
    paper_off = SimpleNamespace(
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        risk_gates_enabled=True,
        max_open_positions=0,
    )
    live_off = SimpleNamespace(
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        risk_gates_enabled=True,
        max_open_positions=0,
    )
    paper_on = SimpleNamespace(
        trading_mode="paper",
        ibkr_port=7497,
        is_paper=True,
        risk_gates_enabled=False,
        max_open_positions=4,
    )
    live_on = SimpleNamespace(
        trading_mode="live",
        ibkr_port=7496,
        is_paper=False,
        risk_gates_enabled=True,
        max_open_positions=4,
    )
    assert slot_cap_armed(paper_off) is False
    assert slot_cap_armed(live_off) is False
    assert slot_cap_armed(paper_on) is True
    assert slot_cap_armed(live_on) is True


def test_live_self_tune_mop_zero_stays_off(tmp_path, monkeypatch):
    """Live operator mop 0 stays off — Grok cannot restore 15 or cap at 25."""
    from abcxauto.config import update_capacity_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("IBKR_PORT", "7496")
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_capacity_config(max_open_positions=0, persist=True)
    out = apply_self_tune({"max_open_positions": 8}, persist=False)
    assert "max_open_positions" in (out.get("rejected") or {})
    assert get_config().max_open_positions == 0
    assert get_config().max_open_positions != 15
    assert get_config().max_open_positions != 25
