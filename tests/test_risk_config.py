"""Runtime + persisted risk config (Risk tab)."""

from abcxauto.config import (
    clear_risk_settings,
    clear_runtime_overrides,
    get_config,
    load_risk_settings,
    risk_config_snapshot,
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


def test_update_risk_config_session_override():
    update_risk_config(daily_loss_limit_pct=3.0, max_open_positions=4, persist=False)
    cfg = get_config()
    assert cfg.daily_loss_limit_pct == 3.0
    assert cfg.max_open_positions == 4
    clear_runtime_overrides()
    assert get_config().daily_loss_limit_pct == 0.0


def test_update_risk_config_persists(tmp_path, monkeypatch):
    path = tmp_path / "persist_risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    clear_risk_settings(path=path)
    load_risk_settings(path)

    update_risk_config(daily_loss_limit_pct=2.5, auto_panic_on_breach=True)
    assert path.is_file()
    clear_runtime_overrides()
    # File knobs survive clearing session overrides
    assert get_config().daily_loss_limit_pct == 2.5
    assert get_config().auto_panic_on_breach is True

    # Simulate restart: drop in-memory file cache, reload from disk
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
