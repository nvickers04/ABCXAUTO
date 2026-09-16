"""Suite config must not read the operator's live risk_settings.json or .env."""

from __future__ import annotations

import json

from abcxauto.config import (
    get_config,
    load_risk_settings,
    risk_settings_path,
    save_risk_settings,
)


def test_builtin_defaults_when_settings_file_absent():
    """No settings file -> code defaults, not the operator's live knobs."""
    cfg = get_config()
    assert not risk_settings_path().is_file()
    assert cfg.session_token_cap == 2_500_000
    assert cfg.session_look_cap == 160
    assert cfg.daily_loss_limit_pct == 25.0
    assert cfg.max_position_pct == 25.0
    assert cfg.risk_posture == "defensive"


def test_redirected_settings_file_is_honored():
    save_risk_settings({"session_token_cap": 654_321})
    get_config.cache_clear()
    assert get_config().session_token_cap == 654_321
    assert risk_settings_path().is_file()


def test_repo_root_settings_file_does_not_leak(tmp_path, monkeypatch):
    """The default repo-root path is ignored while the suite redirects."""
    poison = tmp_path / "operator_risk_settings.json"
    poison.write_text(
        json.dumps({"session_token_cap": 123456}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("abcxauto.config._DEFAULT_RISK_SETTINGS_PATH", poison)
    load_risk_settings()
    get_config.cache_clear()
    assert get_config().session_token_cap == 2_500_000
    assert risk_settings_path().resolve() != poison.resolve()


def test_cwd_dotenv_does_not_leak_token_cap(tmp_path, monkeypatch):
    """load_dotenv() must not re-inject the operator's ABCXAUTO_SESSION_TOKEN_CAP."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "ABCXAUTO_SESSION_TOKEN_CAP=123456\n",
        encoding="utf-8",
    )
    get_config.cache_clear()
    assert get_config().session_token_cap == 2_500_000
