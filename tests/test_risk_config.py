"""Runtime + persisted risk/controls config (disjoint ownership)."""

from abcxauto.config import (
    CONTROL_KEYS,
    RISK_CONFIG_KEYS,
    clear_risk_settings,
    clear_runtime_overrides,
    deliberation_requires_act,
    effective_grok_min_interval_s,
    format_controls_block,
    get_config,
    load_risk_settings,
    risk_config_snapshot,
    update_controls_config,
    update_risk_config,
)


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def test_defaults_off():
    snap = risk_config_snapshot()
    assert snap["daily_loss_limit_pct"] == 0.0
    assert snap["max_position_pct"] == 0.0
    assert snap["auto_panic_on_breach"] is False


def test_risk_and_controls_keys_disjoint():
    assert CONTROL_KEYS.isdisjoint(RISK_CONFIG_KEYS)


def test_update_risk_rejects_controls_keys():
    try:
        update_risk_config(control_deliberation_pct=80, persist=False)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Unknown" in str(e)


def test_update_controls_rejects_risk_keys():
    try:
        update_controls_config(daily_loss_limit_pct=3.0, persist=False)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Unknown" in str(e)


def test_update_risk_config_session_override():
    update_risk_config(daily_loss_limit_pct=3.0, persist=False)
    cfg = get_config()
    assert cfg.daily_loss_limit_pct == 3.0
    clear_runtime_overrides()
    assert get_config().daily_loss_limit_pct == 0.0


def test_update_controls_capacity(tmp_path, monkeypatch):
    path = tmp_path / "persist_controls.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_controls_config(max_open_positions=12, control_complexity_pct=80)
    assert get_config().max_open_positions == 12
    assert get_config().control_complexity_pct == 80


def test_update_risk_config_persists(tmp_path, monkeypatch):
    path = tmp_path / "persist_risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    update_risk_config(daily_loss_limit_pct=2.5, auto_panic_on_breach=True)
    assert path.is_file()
    clear_runtime_overrides()
    assert get_config().daily_loss_limit_pct == 2.5
    assert get_config().auto_panic_on_breach is True

    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(path)
    assert get_config().daily_loss_limit_pct == 2.5
    assert get_config().auto_panic_on_breach is True


def test_unknown_key_rejected():
    try:
        update_risk_config(symbol_allowlist=frozenset({"SPY"}))  # type: ignore[arg-type]
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Unknown" in str(e)


def test_controls_defaults_mid():
    snap = risk_config_snapshot()
    assert snap["control_deliberation_pct"] == 50
    assert snap["control_budget_pct"] == 50
    assert snap["control_complexity_pct"] == 50
    assert snap["control_frequency_pct"] == 50
    assert snap["control_rotation_pct"] == 50


def test_controls_persist_and_clamp(tmp_path, monkeypatch):
    path = tmp_path / "persist_controls.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    update_controls_config(
        control_deliberation_pct=80,
        control_budget_pct=20,
        control_frequency_pct=75,
        control_rotation_pct=90,
        control_complexity_pct=150,  # clamp to 100
    )
    cfg = get_config()
    assert cfg.control_deliberation_pct == 80
    assert cfg.control_budget_pct == 20
    assert cfg.control_frequency_pct == 75
    assert cfg.control_rotation_pct == 90
    assert cfg.control_complexity_pct == 100
    assert deliberation_requires_act(cfg) is True

    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(path)
    cfg2 = get_config()
    assert cfg2.control_deliberation_pct == 80
    block = format_controls_block(cfg2)
    assert "CONTROLS" in block
    assert "deliberation=80" in block
    assert "intelligence_budget=20" in block
    assert "trade_frequency=75" in block
    assert "capital_rotation=90" in block
    assert "structure_complexity=100" in block or "option_complexity=100" in block
    assert "entry_surface=" in block
    assert "require_act=True" in block
    assert "redeploy" in block.lower() or "free cash" in block.lower()


def test_legacy_options_migrates_to_complexity(tmp_path, monkeypatch):
    path = tmp_path / "legacy_controls.json"
    path.write_text(
        '{\n  "control_manage_pct": 90,\n  "control_options_pct": 40\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(path)
    cfg = get_config()
    assert cfg.control_deliberation_pct == 90
    assert cfg.control_complexity_pct == 40
    # Old options=40 → mixed entry surface (not stock-only, not options-only).
    assert cfg.control_entry_surface_pct == 50


def test_legacy_stock_complexity_migrates_entry_surface(tmp_path, monkeypatch):
    path = tmp_path / "legacy_stock.json"
    path.write_text('{"control_complexity_pct": 20}\n', encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(path)
    cfg = get_config()
    assert cfg.control_entry_surface_pct == 20
    assert cfg.control_complexity_pct == 20


def test_budget_scales_grok_min():
    update_controls_config(control_budget_pct=50, persist=False)
    mid = effective_grok_min_interval_s(get_config())
    update_controls_config(control_budget_pct=0, persist=False)
    low = effective_grok_min_interval_s(get_config())
    update_controls_config(control_budget_pct=100, persist=False)
    high = effective_grok_min_interval_s(get_config())
    assert low > mid > high >= 5.0


def test_controls_not_in_set_risk_keys():
    from abcxauto.config import CONTROL_KEYS, SET_RISK_KEYS

    assert CONTROL_KEYS.isdisjoint(SET_RISK_KEYS)


def test_set_trading_mode_paper_live_roundtrip():
    from abcxauto.config import set_trading_mode

    live = set_trading_mode("live", live_confirm="I_UNDERSTAND_LIVE_TRADING_RISK")
    assert live.trading_mode == "live"
    assert live.ibkr_port in (7496, 4001)
    assert live.is_paper is False
    paper = set_trading_mode("paper")
    assert paper.trading_mode == "paper"
    assert paper.is_paper is True


def test_set_trading_mode_live_requires_phrase():
    from abcxauto.config import set_trading_mode

    try:
        set_trading_mode("live", live_confirm="nope")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "confirm" in str(e).lower() or "I_UNDERSTAND" in str(e)
