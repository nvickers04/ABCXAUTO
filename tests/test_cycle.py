"""Inventory helpers live on agent_loop. ``cycle.py`` and ``run_cycle`` are gone."""

from __future__ import annotations

import importlib

import pytest

from abcxauto.agent_loop import (
    ALLOWED_ACTIONS,
    equity_of,
    format_position_inventory,
    normalize_action,
    pnl_of,
    risk_label,
)


def test_cycle_module_and_run_cycle_are_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("abcxauto.cycle")
    import abcxauto.agent_loop as agent_loop

    assert not hasattr(agent_loop, "run_cycle")


def test_cycle_has_no_sit_loop_or_hunt_window():
    import abcxauto.agent_loop as agent_loop

    for name in (
        "sit_loop",
        "run_sit",
        "hunt_window",
        "SYSTEM_PROMPT",
        "apply_tweak",
        "TWEAKS",
        "run_cycle",
    ):
        assert not hasattr(agent_loop, name)
    assert hasattr(agent_loop, "format_position_inventory")
    assert hasattr(agent_loop, "snap")
    assert hasattr(agent_loop, "gate_ticket")
    assert hasattr(agent_loop, "execute_ticket")


def test_format_position_inventory_export():
    assert format_position_inventory([]) == "LIVE POSITION LEDGER: (none)\n"
    text = format_position_inventory(
        [
            {
                "conId": 42,
                "symbol": "SPY",
                "sec_type": "STK",
                "quantity": 3,
            },
            {
                "con_id": 99,
                "symbol": "SPY",
                "secType": "OPT",
                "quantity": -1,
                "expiration": "20260828",
                "strike": 500,
                "right": "C",
            },
        ]
    )
    assert "conId=42" in text
    assert "SPY STK" in text
    assert "pos=+3" in text
    assert "conId=99" in text
    assert "expiry=20260828" in text
    assert "right=C" in text


def test_normalize_action_rejects_unknown():
    strat, forced = normalize_action({"action": "hold_existing", "strategy": "hold_existing"})
    assert strat == "blocked"
    assert forced["status"] == "blocked"
    assert "invalid" in forced["note"] or "allowlist" in forced["note"]


def test_normalize_action_rejects_hold_and_noop():
    strat, forced = normalize_action({"action": "hold", "strategy": "hold"})
    assert strat == "blocked"
    assert forced is not None
    strat2, forced2 = normalize_action({"action": "noop", "strategy": "noop"})
    assert strat2 == "blocked"
    assert forced2 is not None


def test_normalize_action_hold_not_in_allowlist():
    assert "hold" not in ALLOWED_ACTIONS
    assert "trailing_stop" in ALLOWED_ACTIONS  # structure vocab for manage/protect
    strat, forced = normalize_action({"action": "trailing_stop", "strategy": "trailing_stop"})
    assert strat == "trailing_stop"
    assert forced is None


def test_no_dead_operator_tweak_surface():
    from abcxauto import agent_loop

    assert not hasattr(agent_loop, "TWEAKS")
    assert "lab_min_pass_rate" not in dir(agent_loop)
    assert "prefer_bracket_only" not in dir(agent_loop)


def test_pnl_and_equity():
    assert pnl_of({"dailypnl": -12.5, "unrealizedpnl": 99.0}) == -12.5
    assert pnl_of({"dailypnl": 0.0, "unrealizedpnl": -99.0}) == 0.0
    assert pnl_of({"unrealizedpnl": -12.5}) == 0.0
    assert equity_of({"netliquidation": 50000}) == 50000.0


def test_risk_label_compliant():
    assert risk_label({"protection": {"unprotected_symbols": []}}) == "COMPLIANT"


def test_config_has_no_metronome_fields():
    from abcxauto import park_clock
    from abcxauto.config import CAPACITY_KEYS, Config, get_config

    get_config.cache_clear()
    cfg = get_config()
    assert not hasattr(cfg, "trading_mandate")
    assert "cycle_sleep_s" not in Config.__dataclass_fields__
    assert "control_budget_pct" not in Config.__dataclass_fields__
    assert not hasattr(park_clock, "MAX_LOOK_OPEN_S")
    assert not hasattr(park_clock, "max_look_s")
    assert not hasattr(cfg, "idle_streak")
    assert CAPACITY_KEYS == frozenset({"max_open_positions"})
    get_config.cache_clear()
