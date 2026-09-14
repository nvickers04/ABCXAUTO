"""Live connect needs an on-disk promoted playbook, not just .env."""

from __future__ import annotations

import pytest

from abcxauto.broker.connection import (
    LIVE_CONFIRM_PHRASE,
    TradingModePortError,
    assert_connect_allowed,
    assert_live_playbook,
    playbook_live_promoted,
    validate_trading_mode_port,
)
from abcxauto.config import Config, get_config, set_trading_mode


def test_playbook_live_missing_is_not_promoted(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "missing.json"))
    assert playbook_live_promoted() is False
    with pytest.raises(TradingModePortError, match="promoted playbook"):
        assert_live_playbook()


def test_playbook_live_promoted_true(tmp_path, monkeypatch):
    p = tmp_path / "playbook_live.json"
    p.write_text('{"promoted": true, "promoted_at": "2026-09-14"}', encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(p))
    assert playbook_live_promoted() is True
    assert_live_playbook()


def test_playbook_live_garbage_is_not_promoted(tmp_path, monkeypatch):
    p = tmp_path / "playbook_live.json"
    p.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(p))
    assert playbook_live_promoted() is False


def test_assert_connect_allowed_live_needs_playbook(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "none.json"))
    base = get_config()
    cfg = Config(
        **{
            **base.__dict__,
            "trading_mode": "live",
            "ibkr_port": 7496,
            "live_confirm": LIVE_CONFIRM_PHRASE,
        }
    )
    monkeypatch.setattr("abcxauto.broker.connection.get_config", lambda: cfg)
    with pytest.raises(TradingModePortError, match="promoted playbook"):
        assert_connect_allowed()
    p = tmp_path / "none.json"
    p.write_text('{"promoted": true}', encoding="utf-8")
    assert_connect_allowed()


def test_set_trading_mode_live_needs_playbook(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "none.json"))
    with pytest.raises(TradingModePortError, match="promoted playbook"):
        set_trading_mode("live", live_confirm=LIVE_CONFIRM_PHRASE)
    (tmp_path / "none.json").write_text('{"promoted": true}', encoding="utf-8")
    cfg = set_trading_mode("live", live_confirm=LIVE_CONFIRM_PHRASE)
    assert cfg.trading_mode == "live"
    paper = set_trading_mode("paper")
    assert paper.trading_mode == "paper"


def test_validate_port_still_only_checks_phrase():
    validate_trading_mode_port("live", 7496, LIVE_CONFIRM_PHRASE)
