"""Walk-away floor + agent self_tune (no approval). Size is % of NetLiq."""

from abcxauto.config import (
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
    set_risk_knobs,
)
from abcxauto.self_tune import (
    MAX_OPEN_POSITIONS_RANGE,
    PACING_FLOORS,
    RISK_FLOOR,
    apply_self_tune,
    clamp_risk_to_floor,
    ensure_immutable_floor,
    is_self_tune_strategy,
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
    assert not is_self_tune_strategy("bracket")


def test_defaults_are_1k_floor():
    cfg = get_config()
    assert cfg.risk_posture == "defensive"
    assert cfg.daily_loss_limit_pct == 2.0
    assert cfg.max_position_pct == 20.0
    assert cfg.max_risk_per_trade_pct == 1.0
    assert cfg.max_open_positions == 15
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True
    assert cfg.auto_panic_on_breach is True
    assert cfg.cycle_sleep_s == 300.0
    assert cfg.grok_min_interval_s == 300.0
    assert cfg.pace_idle_s == 600.0
    assert cfg.scan_fetch_cap == 4
    assert cfg.trading_budget_usd == 0.0
    assert cfg.grokfolio_enabled is True
    assert cfg.grokfolio_cadence == "both"
    assert cfg.grokfolio_holdings == 15


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


def test_cannot_disable_grokfolio():
    out = apply_self_tune({"grokfolio_enabled": False}, persist=False)
    assert get_config().grokfolio_enabled is True
    assert "grokfolio_enabled" in (out.get("rejected") or {}) or out["status"] == "blocked"


def test_can_tighten_risk():
    out = apply_self_tune({"max_risk_per_trade_pct": 0.5}, persist=False)
    assert out["status"] == "ok"
    assert get_config().max_risk_per_trade_pct == 0.5


def test_cannot_shorten_cycle_sleep():
    out = apply_self_tune({"cycle_sleep_s": 5}, persist=False)
    assert out["status"] == "ok"
    assert get_config().cycle_sleep_s == PACING_FLOORS["cycle_sleep_s"]


def test_can_lengthen_pacing():
    out = apply_self_tune({"cycle_sleep_s": 480, "pace_idle_s": 900}, persist=False)
    assert out["status"] == "ok"
    assert get_config().cycle_sleep_s == 480.0
    assert get_config().pace_idle_s == 900.0


def test_can_retune_controls():
    out = apply_self_tune(
        {"controls": {"control_budget_pct": 10, "control_frequency_pct": 20}},
        persist=False,
    )
    assert out["status"] == "ok"
    assert get_config().control_budget_pct == 10
    assert get_config().control_frequency_pct == 20


def test_cannot_switch_to_live():
    out = apply_self_tune({"trading_mode": "live"}, persist=False)
    assert get_config().trading_mode == "paper"
    assert "trading_mode" in (out.get("rejected") or {}) or out["status"] == "blocked"


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


def test_prompt_extra():
    out = apply_self_tune({"prompt_extra": "Prefer cheap defined-risk verticals."}, persist=False)
    assert out["status"] == "ok"
    assert "Prefer cheap" in get_config().system_prompt_extra


def test_ensure_floor_repairs_weak_settings(tmp_path, monkeypatch):
    from abcxauto.config import update_risk_config, update_controls_config

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
    update_controls_config(max_open_positions=0, persist=False)
    ensure_immutable_floor(persist=False)
    cfg = get_config()
    assert cfg.daily_loss_limit_pct == 2.0
    assert cfg.defined_risk_only is True
    assert cfg.max_open_positions == 15


def test_clamp_risk_helper():
    v, note = clamp_risk_to_floor("max_position_pct", 99)
    assert v == RISK_FLOOR["max_position_pct"][1]
    assert note is not None


def test_get_config_clamps_weak_file(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    path.write_text(
        '{"max_open_positions": 25, "daily_loss_limit_pct": 9, '
        '"defined_risk_only": false, "risk_posture": "aggressive", '
        '"trading_budget_usd": 1000}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto.config import load_risk_settings

    load_risk_settings(path)
    cfg = get_config()
    assert cfg.max_open_positions == 15
    assert cfg.daily_loss_limit_pct == 2.0
    assert cfg.defined_risk_only is True
    assert cfg.risk_posture == "defensive"
    assert cfg.trading_budget_usd == 0.0


def test_get_config_repairs_old_max_open_floor_of_two(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    path.write_text('{"max_open_positions": 2}\n', encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto.config import load_risk_settings

    load_risk_settings(path)
    cfg = get_config()
    assert cfg.max_open_positions == 15


def test_idle_stance_allows_self_tune():
    from abcxauto.agent_loop import check_intent_coherence

    ok, reason = check_intent_coherence(
        {
            "stance": "idle",
            "intent": {"kind": "idle"},
        },
        "self_tune",
        {"strategy": "self_tune", "params": {"cycle_sleep_s": 400}},
    )
    assert ok, reason
