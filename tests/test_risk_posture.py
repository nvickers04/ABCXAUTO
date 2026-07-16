"""Risk posture envelopes, set_risk clamp, live clamp."""

from abcxauto.config import (
    apply_risk_posture,
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


def test_apply_risk_posture_seeds(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    cfg = apply_risk_posture("balanced", persist=True)
    assert cfg.risk_posture == "balanced"
    assert cfg.max_risk_per_trade_pct == 1.5
    assert cfg.daily_loss_limit_pct == 5.0
    assert cfg.max_position_pct == 12.0
    assert cfg.max_open_positions == 6
    assert cfg.max_daily_trades == 12
    assert cfg.auto_panic_on_breach is True
    assert path.is_file()


def test_clamp_risk_knobs_ceiling(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    apply_risk_posture("defensive", persist=True)

    applied, notes = clamp_risk_knobs({"max_risk_per_trade_pct": 99.0})
    assert applied["max_risk_per_trade_pct"] == 2.0
    assert "max_risk_per_trade_pct" in notes


def test_set_risk_requires_posture(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    clear_runtime_overrides()

    out = set_risk_knobs({"max_risk_per_trade_pct": 1.0}, persist=False)
    assert out["status"] == "blocked"


def test_set_risk_within_envelope(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    apply_risk_posture("balanced", persist=True)

    out = set_risk_knobs({"max_risk_per_trade_pct": 3.5, "max_daily_trades": 20})
    assert out["status"] == "ok"
    assert out["applied"]["max_risk_per_trade_pct"] == 3.5
    assert get_config().max_risk_per_trade_pct == 3.5
    assert get_config().max_daily_trades == 20


def test_set_risk_clamps_over_ceiling(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    apply_risk_posture("balanced", persist=True)

    out = set_risk_knobs({"max_risk_per_trade_pct": 50.0})
    assert out["status"] == "ok"
    assert out["applied"]["max_risk_per_trade_pct"] == 4.0
    assert out["clamped"]


def test_executor_set_risk(tmp_path, monkeypatch):
    import asyncio

    from abcxauto.executor import safe_execute

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    apply_risk_posture("balanced", persist=True)

    class _C:
        connected = True

    result = asyncio.run(
        safe_execute(
            {"strategy": "set_risk", "params": {"max_risk_per_trade_pct": 2.0}},
            _C(),
        )
    )
    assert result["status"] == "ok"
    assert get_config().max_risk_per_trade_pct == 2.0


def test_live_aggressive_effective_balanced(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    set_trading_mode("live", live_confirm="I_UNDERSTAND_LIVE_TRADING_RISK")
    apply_risk_posture("aggressive", persist=True)
    cfg = get_config()
    assert cfg.risk_posture == "aggressive"
    assert cfg.effective_risk_posture == "balanced"
    # Seeds use effective (balanced) values
    assert cfg.max_risk_per_trade_pct == 1.5
    set_trading_mode("paper")
