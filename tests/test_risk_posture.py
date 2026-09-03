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
    assert "max_risk_per_trade_pct" in (out.get("rejected") or {})
    assert "max_risk_per_trade_pct" not in (out.get("applied") or {})
    assert get_config().max_risk_per_trade_pct != 0.5


def test_set_risk_tightens_inside_floor(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    out = set_risk_knobs({"max_risk_per_trade_pct": 0.75, "max_peak_drawdown_pct": 5.0})
    assert out["status"] == "ok"
    assert "max_risk_per_trade_pct" not in (out.get("applied") or {})
    assert "max_risk_per_trade_pct" in (out.get("rejected") or {})
    assert get_config().max_risk_per_trade_pct != 0.75
    assert get_config().max_peak_drawdown_pct == 5.0


def test_set_risk_clamps_over_ceiling(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    out = set_risk_knobs({"max_risk_per_trade_pct": 50.0})
    assert "max_risk_per_trade_pct" in (out.get("rejected") or {})
    assert "max_risk_per_trade_pct" not in (out.get("applied") or {})
    assert get_config().max_risk_per_trade_pct == 25.0


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
            {"strategy": "set_risk", "params": {"max_peak_drawdown_pct": 6.0}},
            _C(),
        )
    )
    assert result["status"] == "ok"
    assert get_config().max_peak_drawdown_pct == 6.0
    assert get_config().max_risk_per_trade_pct == 25.0


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


def test_operator_may_persist_aggressive_posture_on_paper(tmp_path, monkeypatch):
    from abcxauto.config import update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    cfg = update_risk_config(risk_posture="aggressive", persist=True)
    assert cfg.trading_mode == "paper" or cfg.is_paper
    assert cfg.risk_posture == "aggressive"
    assert get_config().risk_posture == "aggressive"
    assert get_config().effective_risk_posture == "aggressive"
    clear_runtime_overrides()
    load_risk_settings(path)
    assert get_config().risk_posture == "aggressive"
    for posture in ("defensive", "balanced", "aggressive"):
        update_risk_config(risk_posture=posture, persist=True)
        assert get_config().risk_posture == posture


def test_live_maps_stored_aggressive_to_balanced(tmp_path, monkeypatch):
    from abcxauto.config import update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_risk_config(risk_posture="aggressive", persist=True)
    set_trading_mode("live", live_confirm="I_UNDERSTAND_LIVE_TRADING_RISK")
    cfg = get_config()
    assert cfg.risk_posture == "aggressive"
    assert cfg.effective_risk_posture == "balanced"
    set_trading_mode("paper")
