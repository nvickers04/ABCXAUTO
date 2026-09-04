"""Operator agent settings — brain / pacing / link knobs from Pro Settings.

The walk-away floor and the paper/live switch are not reachable from here.
"""

import pytest

from abcxauto.config import (
    AGENT_CONFIG_KEYS,
    AGENT_LOCKED_KEYS,
    CAPACITY_KEYS,
    PERSISTED_SETTINGS_KEYS,
    RISK_CONFIG_KEYS,
    agent_config_snapshot,
    clamp_agent_knobs,
    get_config,
    load_risk_settings,
    risk_settings_path,
    set_agent_knobs,
    update_agent_config,
)


def test_agent_keys_are_disjoint_from_the_floor():
    assert AGENT_CONFIG_KEYS.isdisjoint(RISK_CONFIG_KEYS)
    assert AGENT_CONFIG_KEYS.isdisjoint(CAPACITY_KEYS)
    assert AGENT_CONFIG_KEYS.isdisjoint(AGENT_LOCKED_KEYS)
    assert AGENT_CONFIG_KEYS <= PERSISTED_SETTINGS_KEYS


def test_update_agent_config_applies_and_persists():
    cfg = update_agent_config(model="grok-4.6-fast", temperature=0.9, max_tokens=4096)
    assert cfg.model == "grok-4.6-fast"
    assert cfg.temperature == 0.9
    assert cfg.max_tokens == 4096
    path = risk_settings_path()
    assert path.is_file()

    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(path)
    reread = get_config()
    assert reread.model == "grok-4.6-fast"
    assert reread.temperature == 0.9
    assert reread.max_tokens == 4096


def test_update_agent_config_session_only_does_not_write():
    path = risk_settings_path()
    cfg = update_agent_config(monitor_poll_s=45, persist=False)
    assert cfg.monitor_poll_s == 45
    assert not path.is_file()


def test_numeric_knobs_are_clamped_not_accepted_raw():
    cfg = update_agent_config(
        temperature=9.0,
        monitor_poll_s=1,
        monitor_review_s=2,
        max_tokens=1,
    )
    assert cfg.temperature == 2.0
    assert cfg.monitor_poll_s == 5
    assert cfg.monitor_review_s == 30
    assert cfg.max_tokens == 1024


def test_session_caps_persist_and_clamp():
    cfg = update_agent_config(session_look_cap=80, session_token_cap=500_000)
    assert cfg.session_look_cap == 80
    assert cfg.session_token_cap == 500_000
    from abcxauto import config as cfg_mod

    cfg_mod._file_overrides = {}
    cfg_mod._runtime_overrides.clear()
    load_risk_settings(risk_settings_path())
    reread = get_config()
    assert reread.session_look_cap == 80
    assert reread.session_token_cap == 500_000
    assert update_agent_config(session_look_cap=0).session_look_cap == 1
    assert update_agent_config(session_token_cap=1).session_token_cap == 50_000
    assert update_agent_config(session_look_cap=9_999).session_look_cap == 400
    assert update_agent_config(session_token_cap=99_000_000).session_token_cap == 10_000_000


def test_scan_fetch_cap_stays_with_self_tune():
    """agent_state beats risk_settings, so an operator write here would be a lie."""
    with pytest.raises(ValueError):
        update_agent_config(scan_fetch_cap=4)


def test_disconnect_halt_cannot_be_disabled():
    # 0 turns the post-disconnect entry halt off — that weakens the floor.
    cfg = update_agent_config(disconnect_halt_s=0)
    assert cfg.disconnect_halt_s == 1.0
    assert update_agent_config(disconnect_halt_s=30).disconnect_halt_s == 30.0


def test_booleans_round_trip_from_text():
    assert update_agent_config(monitor_enabled="off").monitor_enabled is False
    assert update_agent_config(monitor_extended_hours="yes").monitor_extended_hours is True


@pytest.mark.parametrize(
    "payload",
    [
        {"trading_mode": "live"},
        {"ibkr_port": 7496},
        {"live_confirm": "I_UNDERSTAND_LIVE_TRADING_RISK"},
        {"xai_api_key": "sk-nope"},
        {"marketdata_token": "nope"},
    ],
)
def test_writer_refuses_mode_port_and_secrets(payload):
    with pytest.raises(ValueError) as err:
        update_agent_config(**payload)
    assert "set_trading_mode" in str(err.value)
    cfg = get_config()
    assert cfg.trading_mode == "paper"
    assert cfg.is_paper is True


def test_writer_refuses_risk_and_unknown_keys():
    for payload in ({"daily_loss_limit_pct": 1.0}, {"max_open_positions": 3}, {"nope": 1}):
        with pytest.raises(ValueError) as err:
            update_agent_config(**payload)
        assert "Unknown agent config keys" in str(err.value)


def test_text_knobs_reject_blanks_and_whitespace():
    for bad in ("", "   ", "grok 4.6"):
        with pytest.raises(ValueError):
            update_agent_config(model=bad)
    assert get_config().model


def test_session_models_empty_falls_back_and_persists():
    cfg = update_agent_config(model_rth="", model_research="grok-4.6-fast")
    assert cfg.model_rth == ""
    assert cfg.model_research == "grok-4.6-fast"
    from abcxauto.desk_mode import session_model

    assert session_model("regular", cfg) == cfg.model
    assert session_model("premarket", cfg) == "grok-4.6-fast"
    with pytest.raises(ValueError):
        update_agent_config(model_research="grok 4.6")


def test_cache_is_invalidated_so_env_edits_go_live(monkeypatch):
    before = get_config().temperature
    monkeypatch.setenv("ABCXAUTO_TEMPERATURE", str(before + 0.4))
    # Cached env config still holds the old value.
    assert get_config().temperature == before
    cfg = update_agent_config(max_tokens=2048)
    assert cfg.temperature == pytest.approx(before + 0.4)


def test_set_agent_knobs_reports_instead_of_raising():
    res = set_agent_knobs(
        {
            "temperature": 9.0,
            "trading_mode": "live",
            "ibkr_port": 7496,
            "model": "",
            "nope": 1,
            "monitor_poll_s": 60,
        }
    )
    assert res["applied"] == {"temperature": 2.0, "monitor_poll_s": 60}
    assert res["clamped"]["temperature"] == {"raw": 9.0, "clamped": 2.0}
    assert set(res["rejected"]) == {"trading_mode", "ibkr_port", "model", "nope"}
    for key in ("trading_mode", "ibkr_port"):
        assert "set_trading_mode" in res["rejected"][key]
    cfg = get_config()
    assert cfg.trading_mode == "paper"
    assert cfg.temperature == 2.0


def test_clamp_agent_knobs_is_pure():
    before = get_config().monitor_poll_s
    applied, notes, rejected = clamp_agent_knobs({"monitor_poll_s": 9999})
    assert applied == {"monitor_poll_s": 900}
    assert notes["monitor_poll_s"]["raw"] == 9999
    assert rejected == {}
    assert get_config().monitor_poll_s == before


def test_snapshot_covers_every_agent_knob():
    snap = agent_config_snapshot(reload=True)
    assert set(snap) == set(AGENT_CONFIG_KEYS)
    assert snap["ibkr_host"]
    assert "ibkr_port" not in snap
    assert "trading_mode" not in snap


def test_settings_file_holds_risk_and_agent_keys_together():
    import json

    update_agent_config(model="grok-4.6", monitor_poll_s=15)
    from abcxauto.config import update_risk_config

    update_risk_config(daily_loss_limit_pct=2.0)
    raw = json.loads(risk_settings_path().read_text(encoding="utf-8"))
    assert raw["model"] == "grok-4.6"
    assert raw["monitor_poll_s"] == 15
    assert raw["daily_loss_limit_pct"] == 2.0
    # A settings file cannot smuggle in a live port.
    assert "ibkr_port" not in raw
    assert "trading_mode" not in raw
