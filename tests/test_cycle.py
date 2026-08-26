"""Cycle shim — inventory helpers stay; launcher / sit-loop / hunt-window do not."""

import pytest

from abcxauto.cycle import (
    ALLOWED_ACTIONS,
    equity_of,
    format_position_inventory,
    normalize_action,
    pnl_of,
    risk_label,
    run_cycle,
    snap,
)


class FakeConnector:
    connected = True

    async def connect(self):
        return True

    async def get_positions(self):
        return [{"symbol": "AAPL", "quantity": 10, "sec_type": "STK", "unrealized_pnl": 5.0}]

    async def get_open_orders(self):
        return []

    async def get_account_summary(self):
        return {"netliquidation": 50000, "unrealizedpnl": 12.5}

    async def get_recent_executions(self):
        return []


@pytest.mark.asyncio
async def test_snap_is_retired_noop(monkeypatch):
    """cycle.snap must not launch a book look or arm a wake clock."""
    started: list[str] = []

    async def boom(*_a, **_k):
        started.append("async")
        raise AssertionError("cycle.snap must not look or send")

    def boom_sync(*_a, **_k):
        started.append("sync")
        raise AssertionError("cycle.snap must not arm a nap clock")

    monkeypatch.setattr("abcxauto.agent_loop._tool", boom)
    monkeypatch.setattr("abcxauto.agent_loop.snap", boom)
    monkeypatch.setattr("abcxauto.park_clock.set_wake", boom_sync)
    monkeypatch.setattr("abcxauto.park_clock.ensure_next_look", boom_sync)

    out = await snap(FakeConnector())
    assert started == []
    assert out.get("book_unreliable") is True
    assert "cycle_snap_retired" in str(out.get("validation") or "")


@pytest.mark.asyncio
async def test_run_cycle_is_retired_noop(monkeypatch):
    """Import-safe leftover: must not start a think cycle or a nap clock."""
    started: list[str] = []

    async def boom(*_a, **_k):
        started.append("async")
        raise AssertionError("cycle shim must not start think, snap, or send")

    def boom_sync(*_a, **_k):
        started.append("sync")
        raise AssertionError("cycle shim must not arm a nap clock")

    monkeypatch.setattr("abcxauto.agent_loop.grok_turn", boom)
    monkeypatch.setattr("abcxauto.agent_loop.snap", boom)
    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom)
    monkeypatch.setattr("abcxauto.brain.grok_turn", boom)
    monkeypatch.setattr("abcxauto.park_clock.set_wake", boom_sync)
    monkeypatch.setattr("abcxauto.park_clock.ensure_next_look", boom_sync)
    monkeypatch.setattr("abcxauto.pacing.wait_for_pace", boom)
    monkeypatch.setattr("asyncio.sleep", boom)

    out = await run_cycle(1, FakeConnector(), None, [], 0.0)
    assert started == []
    assert out["strat"] == "skipped"
    assert out["sends"] == 0
    assert out["result"]["status"] == "skipped"
    assert "cycle_shim_retired" in str(out["result"].get("note") or "")
    assert out["inventory"] == format_position_inventory([])


def test_cycle_has_no_sit_loop_or_hunt_window():
    from abcxauto import cycle

    for name in (
        "sit_loop",
        "run_sit",
        "hunt_window",
        "SYSTEM_PROMPT",
        "apply_tweak",
        "TWEAKS",
        "grok",
        "grok_turn",
        "execute_ticket",
        "safe_execute",
        "gate_ticket",
    ):
        assert not hasattr(cycle, name)
    assert "run_cycle" in cycle.__all__
    assert "format_position_inventory" in cycle.__all__
    assert cycle.run_cycle is not None


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
    from abcxauto import agent_loop, cycle

    assert not hasattr(agent_loop, "TWEAKS")
    assert not hasattr(cycle, "apply_tweak")
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
