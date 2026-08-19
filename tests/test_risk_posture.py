"""Walk-away floor clamp and live posture identity."""

from abcxauto.config import (
    clamp_risk_knobs,
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
    resolve_effective_posture,
    set_risk_knobs,
    set_trading_mode,
)


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def test_resolve_effective_posture_live_clamps_aggressive():
    assert resolve_effective_posture("aggressive", "paper") == "aggressive"
    assert resolve_effective_posture("aggressive", "live") == "balanced"
    assert resolve_effective_posture("", "paper") == ""


def test_no_posture_seed_api():
    import abcxauto.config as cfg

    assert not hasattr(cfg, "apply_risk_posture")
    assert not hasattr(cfg, "_POSTURE_SEEDS")
    assert not hasattr(cfg, "_POSTURE_PROMPT_BIAS")


def test_clamp_risk_knobs_ceiling(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    applied, notes = clamp_risk_knobs({"max_risk_per_trade_pct": 99.0})
    assert applied["max_risk_per_trade_pct"] == 25.0
    assert "max_risk_per_trade_pct" in notes


def test_set_risk_no_approval_needed(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    clear_runtime_overrides()

    out = set_risk_knobs({"max_risk_per_trade_pct": 0.5}, persist=False)
    assert out["status"] == "ok"
    assert get_config().max_risk_per_trade_pct == 0.5


def test_set_risk_tightens_inside_floor(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    out = set_risk_knobs({"max_risk_per_trade_pct": 0.75, "max_peak_drawdown_pct": 5.0})
    assert out["status"] == "ok"
    assert out["applied"]["max_risk_per_trade_pct"] == 0.75
    assert get_config().max_risk_per_trade_pct == 0.75
    assert get_config().max_peak_drawdown_pct == 5.0


def test_set_risk_clamps_over_ceiling(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    out = set_risk_knobs({"max_risk_per_trade_pct": 50.0})
    assert out["status"] == "ok"
    assert out["applied"]["max_risk_per_trade_pct"] == 25.0
    assert out["clamped"]


def test_executor_set_risk(tmp_path, monkeypatch):
    import asyncio

    from abcxauto.executor import safe_execute

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    class _C:
        connected = True

    result = asyncio.run(
        safe_execute(
            {"strategy": "set_risk", "params": {"max_risk_per_trade_pct": 0.5}},
            _C(),
        )
    )
    assert result["status"] == "ok"
    assert get_config().max_risk_per_trade_pct == 0.5


def test_live_keeps_defensive_identity(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    set_trading_mode("live", live_confirm="I_UNDERSTAND_LIVE_TRADING_RISK")
    cfg = get_config()
    assert cfg.risk_posture == "defensive"
    assert cfg.effective_risk_posture == "defensive"
    set_trading_mode("paper")
