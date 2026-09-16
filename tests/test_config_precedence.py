"""Settings / risk_settings.json > env > default. scan_fetch_cap via get_config()."""

from __future__ import annotations

from abcxauto.config import (
    CONFIG_PRECEDENCE,
    get_config,
    save_risk_settings,
)


def test_config_precedence_tuple():
    assert CONFIG_PRECEDENCE == ("settings", "env", "default")


def test_settings_beats_env_beats_default(monkeypatch, tmp_path):
    monkeypatch.delenv("ABCXAUTO_MODEL", raising=False)
    get_config.cache_clear()
    assert get_config().model == "grok-4.6"

    monkeypatch.setenv("ABCXAUTO_MODEL", "grok-from-env")
    get_config.cache_clear()
    assert get_config().model == "grok-from-env"

    save_risk_settings({"model": "grok-from-settings"})
    get_config.cache_clear()
    assert get_config().model == "grok-from-settings"


def test_scan_fetch_cap_uses_get_config_not_env_short_circuit(monkeypatch):
    from abcxauto.opportunity_scan import scan_fetch_cap
    from abcxauto.self_tune import apply_self_tune

    monkeypatch.setenv("ABCXAUTO_SCAN_FETCH_CAP", "6")
    get_config.cache_clear()
    assert scan_fetch_cap() == 6
    assert get_config().scan_fetch_cap == 6

    apply_self_tune({"scan_fetch_cap": 2}, persist=True)
    assert get_config().scan_fetch_cap == 2
    assert scan_fetch_cap() == 2


def test_get_config_doc_does_not_claim_full_agent_state_merge():
    doc = get_config.__doc__ or ""
    assert "only" in doc.lower() and "scan_fetch_cap" in doc
    assert "agent_state.json`` <" not in doc
