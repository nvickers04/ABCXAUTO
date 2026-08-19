"""Runtime + persisted risk/capacity config (disjoint ownership)."""

from abcxauto.config import (
    CAPACITY_KEYS,
    RISK_CONFIG_KEYS,
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
    risk_config_snapshot,
    update_capacity_config,
    update_risk_config,
)


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def test_defaults_1k_floor():
    snap = risk_config_snapshot()
    assert snap["daily_loss_limit_pct"] == 25.0
    assert snap["max_position_pct"] == 25.0
    assert snap["max_peak_drawdown_pct"] == 25.0
    assert snap["auto_panic_on_breach"] is True
    assert snap["defined_risk_only"] is True
    assert snap["max_open_positions"] == 15


def test_risk_and_capacity_keys_disjoint():
    assert CAPACITY_KEYS.isdisjoint(RISK_CONFIG_KEYS)


def test_update_risk_rejects_capacity_keys():
    try:
        update_risk_config(max_open_positions=8, persist=False)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Unknown" in str(e)


def test_update_capacity_rejects_risk_keys():
    try:
        update_capacity_config(daily_loss_limit_pct=3.0, persist=False)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Unknown" in str(e)


def test_update_risk_config_session_override():
    update_risk_config(daily_loss_limit_pct=50.0, persist=False)
    cfg = get_config()
    # Immutable floor: cannot raise daily-loss above 25%
    assert cfg.daily_loss_limit_pct == 25.0
    clear_runtime_overrides()
    assert get_config().daily_loss_limit_pct == 25.0


def test_update_capacity(tmp_path, monkeypatch):
    path = tmp_path / "persist_capacity.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)
    update_capacity_config(max_open_positions=12)
    assert get_config().max_open_positions == 12
    update_capacity_config(max_open_positions=99)
    assert get_config().max_open_positions == 25


def test_update_risk_config_persists(tmp_path, monkeypatch):
    path = tmp_path / "persist_risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    update_risk_config(daily_loss_limit_pct=1.5, auto_panic_on_breach=True)
    assert path.is_file()
    clear_runtime_overrides()
    assert get_config().daily_loss_limit_pct == 1.5
    assert get_config().auto_panic_on_breach is True

    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(path)
    assert get_config().daily_loss_limit_pct == 1.5
    assert get_config().auto_panic_on_breach is True


def test_unknown_key_rejected():
    try:
        update_risk_config(symbol_allowlist=frozenset({"SPY"}))  # type: ignore[arg-type]
        assert False, "expected ValueError"
    except ValueError as e:
        assert "Unknown" in str(e)


def test_stale_control_dials_are_ignored(tmp_path, monkeypatch):
    path = tmp_path / "legacy_controls.json"
    path.write_text(
        '{\n  "control_manage_pct": 90,\n  "control_options_pct": 40,\n'
        '  "max_open_positions": 8\n}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(path)
    cfg = get_config()
    assert cfg.max_open_positions == 8
    assert not hasattr(cfg, "control_budget_pct")


def test_capacity_not_in_set_risk_keys():
    from abcxauto.config import CAPACITY_KEYS, SET_RISK_KEYS

    assert CAPACITY_KEYS.isdisjoint(SET_RISK_KEYS)
    assert CAPACITY_KEYS == frozenset({"max_open_positions"})
    assert "risk_posture" not in SET_RISK_KEYS


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
