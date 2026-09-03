"""Operator disk knobs are source of truth. Grok self_tune cannot persist over them.

2026-09-03 paper 7497 reload tax: operator set mop/risk/pos/prem = 0 (off),
daily_loss_limit_pct = 25, session_token_cap = 5_000_000. Grok self_tune
rewrote RAM (and sometimes disk) to mop 8, risk 0.25/0.5, pos 5, prem 1,
dll 3/5, peak DD 12. Each restore required a live reload and killed a
thinking look. File wins. 0 stays 0. dll 25 stays 25.
"""

from __future__ import annotations

import json
import logging

from abcxauto.config import (
    get_config,
    load_risk_settings,
    risk_settings_path,
    update_agent_config,
    update_capacity_config,
    update_risk_config,
)
from abcxauto.mode_size import (
    max_risk_per_trade_off,
    mode_size_ticket_error,
)
from abcxauto.self_tune import (
    OPERATOR_DISK_KEYS,
    _OPERATOR_DISK_REJECT,
    apply_self_tune,
    is_operator_disk_key,
    load_agent_state,
)
from abcxauto.send import apply_size_pct_nl


# 2026-09-03 operator disk on paper 7497.
_TAX_MOP = 0
_TAX_RISK = 0.0
_TAX_POS = 0.0
_TAX_PREM = 0.0
_TAX_DLL = 25.0
_TAX_TOKEN = 5_000_000
_GROK_TUNE = {
    "max_open_positions": 8,
    "max_risk_per_trade_pct": 0.5,
    "max_position_pct": 5,
    "max_option_premium_pct": 1,
    "daily_loss_limit_pct": 3,
}


def _seed_operator_disk() -> None:
    update_capacity_config(max_open_positions=_TAX_MOP, persist=True)
    update_risk_config(
        max_risk_per_trade_pct=_TAX_RISK,
        max_position_pct=_TAX_POS,
        max_option_premium_pct=_TAX_PREM,
        daily_loss_limit_pct=_TAX_DLL,
        sizing_floors=False,
        defined_risk_only=True,
        cash_only=True,
        persist=True,
        _skip_clamp=True,
    )
    update_agent_config(session_token_cap=_TAX_TOKEN, persist=True)


def _assert_operator_disk_holds(*, reload: bool = False) -> None:
    if reload:
        from abcxauto.config import clear_runtime_overrides

        clear_runtime_overrides()
        load_risk_settings(risk_settings_path())
        get_config.cache_clear()
    cfg = get_config()
    assert cfg.max_open_positions == _TAX_MOP
    assert cfg.max_risk_per_trade_pct == _TAX_RISK
    assert cfg.max_position_pct == _TAX_POS
    assert cfg.max_option_premium_pct == _TAX_PREM
    assert cfg.daily_loss_limit_pct == _TAX_DLL
    assert cfg.session_token_cap == _TAX_TOKEN
    assert cfg.sizing_floors is False
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7497
    raw = json.loads(risk_settings_path().read_text(encoding="utf-8"))
    assert raw["max_open_positions"] == _TAX_MOP
    assert raw["max_risk_per_trade_pct"] == _TAX_RISK
    assert raw["max_position_pct"] == _TAX_POS
    assert raw["max_option_premium_pct"] == _TAX_PREM
    assert raw["daily_loss_limit_pct"] == _TAX_DLL
    assert raw["session_token_cap"] == _TAX_TOKEN
    assert raw["sizing_floors"] is False
    assert raw["defined_risk_only"] is True
    assert raw["cash_only"] is True
    assert "ibkr_port" not in raw
    assert "trading_mode" not in raw
    assert "7496" not in risk_settings_path().read_text(encoding="utf-8")


def test_operator_disk_key_set():
    for key in (
        "max_open_positions",
        "max_risk_per_trade_pct",
        "max_position_pct",
        "max_option_premium_pct",
        "daily_loss_limit_pct",
        "session_token_cap",
        "sizing_floors",
        "defined_risk_only",
        "cash_only",
        "ibkr_port",
        "trading_mode",
    ):
        assert is_operator_disk_key(key)
        assert key in OPERATOR_DISK_KEYS
    assert not is_operator_disk_key("size_pct_nl")
    assert not is_operator_disk_key("max_peak_drawdown_pct")
    assert not is_operator_disk_key("scan_fetch_cap")
    assert not is_operator_disk_key("session_look_cap")


def test_2026_09_03_self_tune_cannot_rewrite_operator_disk(caplog):
    """Tune mop=8 risk=0.5 pos=5 prem=1 dll=3 leaves disk+loaded at 0/0/0/0/25."""
    _seed_operator_disk()
    _assert_operator_disk_holds()
    with caplog.at_level(logging.WARNING, logger="abcxauto.self_tune"):
        out = apply_self_tune(dict(_GROK_TUNE), persist=True)
    assert out["status"] == "blocked" or not (out.get("applied") or {})
    rejected = out.get("rejected") or {}
    for key in _GROK_TUNE:
        assert rejected[key] == _OPERATOR_DISK_REJECT
        assert key not in (out.get("applied") or {})
    hits = [r for r in caplog.records if "operator disk" in r.getMessage()]
    assert len(hits) == 1
    msg = hits[0].getMessage()
    assert "max_open_positions" in msg
    assert "daily_loss_limit_pct" in msg
    _assert_operator_disk_holds()
    _assert_operator_disk_holds(reload=True)
    assert get_config().daily_loss_limit_pct == 25.0
    assert get_config().daily_loss_limit_pct != 0.5
    assert get_config().daily_loss_limit_pct != 3
    assert get_config().max_risk_per_trade_pct == 0.0
    assert get_config().max_open_positions == 0


def test_size_pct_nl_0_5_does_not_reimpose_clerk_cap_when_max_risk_off():
    """size_pct_nl 0.5 from tune is not a clerk 0.5% max-risk while the knob is 0."""
    from tests.test_mode_size import (
        _PROD_NL,
        _PROD_QTY,
        _PROD_UNDERLYING,
        _SHADOW,
        _named_vertical,
    )

    _seed_operator_disk()
    out = apply_self_tune({**_GROK_TUNE, "size_pct_nl": _SHADOW}, persist=True)
    assert out["status"] == "ok"
    assert out["applied"]["size_pct_nl"] == _SHADOW
    for key in _GROK_TUNE:
        assert key in (out.get("rejected") or {})
        assert key not in (out.get("applied") or {})
    _assert_operator_disk_holds()
    assert get_config().max_risk_per_trade_pct == 0.0
    assert max_risk_per_trade_off() is True
    assert load_agent_state().get("size_pct_nl") == _SHADOW
    note = mode_size_ticket_error(
        _named_vertical(),
        net_liq=_PROD_NL,
        price=_PROD_UNDERLYING,
        strategy="vertical_spread",
    )
    assert note == ""
    params = _named_vertical()
    apply_note = apply_size_pct_nl(
        params,
        net_liq=_PROD_NL,
        price=_PROD_UNDERLYING,
        strategy="vertical_spread",
    )
    assert apply_note is None
    assert params["quantity"] == _PROD_QTY
    _assert_operator_disk_holds(reload=True)
    assert get_config().max_risk_per_trade_pct == 0.0
    assert load_agent_state().get("size_pct_nl") == _SHADOW


def test_agent_state_size_pct_nl_cannot_paint_max_risk(tmp_path, monkeypatch):
    """Hunch check: dirty agent_state 0.5 is not Config.max_risk while disk is 0."""
    _seed_operator_disk()
    state = tmp_path / "agent_state.json"
    state.write_text(
        json.dumps(
            {
                "size_pct_nl": 0.5,
                "max_risk_per_trade_pct": 0.5,
                "max_open_positions": 8,
                "daily_loss_limit_pct": 3,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(state))
    overlay = load_agent_state()
    assert overlay.get("size_pct_nl") == 0.5
    assert "max_risk_per_trade_pct" not in overlay
    assert "max_open_positions" not in overlay
    assert "daily_loss_limit_pct" not in overlay
    from abcxauto.config import clear_runtime_overrides

    clear_runtime_overrides()
    get_config.cache_clear()
    load_risk_settings(risk_settings_path())
    cfg = get_config()
    assert cfg.max_risk_per_trade_pct == 0.0
    assert cfg.max_open_positions == 0
    assert cfg.daily_loss_limit_pct == 25.0


def test_peak_dd_tune_does_not_arm_sizing_floors():
    _seed_operator_disk()
    out = apply_self_tune(
        {
            **_GROK_TUNE,
            "max_peak_drawdown_pct": 12.0,
            "max_symbol_concentration_pct": 8.0,
            "max_arena_concentration_pct": 10.0,
        },
        persist=True,
    )
    assert out["status"] == "ok"
    assert out["applied"]["max_peak_drawdown_pct"] == 12.0
    assert out["applied"]["max_symbol_concentration_pct"] == 8.0
    assert out["applied"]["max_arena_concentration_pct"] == 10.0
    for key in _GROK_TUNE:
        assert key not in (out.get("applied") or {})
    cfg = get_config()
    assert cfg.max_peak_drawdown_pct == 12.0
    assert cfg.sizing_floors is False
    assert cfg.max_open_positions == 0
    assert cfg.max_risk_per_trade_pct == 0.0
    assert cfg.daily_loss_limit_pct == 25.0
    _assert_operator_disk_holds()
    raw = json.loads(risk_settings_path().read_text(encoding="utf-8"))
    assert raw["max_peak_drawdown_pct"] == 12.0
    assert raw["sizing_floors"] is False


def test_defined_risk_cash_only_7496_still_locked():
    _seed_operator_disk()
    out = apply_self_tune(
        {
            "defined_risk_only": False,
            "cash_only": False,
            "trading_mode": "live",
            "ibkr_port": 7496,
            "live_confirm": "I_UNDERSTAND_LIVE_TRADING_RISK",
            "sizing_floors": True,
            "session_token_cap": 100_000,
        },
        persist=True,
    )
    rejected = out.get("rejected") or {}
    assert rejected["defined_risk_only"] == _OPERATOR_DISK_REJECT
    assert rejected["cash_only"] == _OPERATOR_DISK_REJECT
    assert rejected["trading_mode"] == _OPERATOR_DISK_REJECT
    assert rejected["ibkr_port"] == _OPERATOR_DISK_REJECT
    assert rejected["sizing_floors"] == _OPERATOR_DISK_REJECT
    assert rejected["session_token_cap"] == _OPERATOR_DISK_REJECT
    cfg = get_config()
    assert cfg.defined_risk_only is True
    assert cfg.cash_only is True
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7497
    assert cfg.sizing_floors is False
    assert cfg.session_token_cap == _TAX_TOKEN
    _assert_operator_disk_holds()
    _assert_operator_disk_holds(reload=True)


def test_legitimate_tune_kept_when_operator_keys_present():
    _seed_operator_disk()
    out = apply_self_tune(
        {
            **_GROK_TUNE,
            "size_pct_nl": 3.0,
            "scan_fetch_cap": 4,
            "session_look_cap": 80,
        },
        persist=True,
    )
    assert out["status"] == "ok"
    assert out["applied"]["size_pct_nl"] == 3.0
    assert out["applied"]["scan_fetch_cap"] == 4
    assert out["applied"]["session_look_cap"] == 80
    assert "session_token_cap" not in (out.get("applied") or {})
    _assert_operator_disk_holds()
    assert get_config().scan_fetch_cap == 4
    assert get_config().session_look_cap == 80
    assert get_config().session_token_cap == _TAX_TOKEN


def test_zero_stays_zero_dll_25_not_clamped_to_floor_min():
    _seed_operator_disk()
    apply_self_tune(
        {
            "max_open_positions": 8,
            "max_risk_per_trade_pct": 0.25,
            "max_position_pct": 5,
            "max_option_premium_pct": 1,
            "daily_loss_limit_pct": 0.5,
        },
        persist=True,
    )
    cfg = get_config()
    assert cfg.max_open_positions == 0
    assert cfg.max_risk_per_trade_pct == 0.0
    assert cfg.max_position_pct == 0.0
    assert cfg.max_option_premium_pct == 0.0
    assert cfg.daily_loss_limit_pct == 25.0
    assert cfg.daily_loss_limit_pct != 0.5
