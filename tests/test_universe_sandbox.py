"""Universe sandbox + entry surface / option complexity gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.structure_complexity import (
    allowed_strategies,
    complexity_band,
    entry_surface_band,
    reject_reason,
    strategy_allowed,
)
from abcxauto.universe import (
    filter_to_legal,
    is_common_equity_symbol,
    is_legal_symbol,
    load_allowlist,
    refresh_legal_set,
    reset_universe_cache,
    save_allowlist,
)


@pytest.fixture(autouse=True)
def _iso_universe(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe.json"))
    reset_universe_cache()
    yield
    reset_universe_cache()


def test_entry_surface_bands():
    assert entry_surface_band(20) == "stock"
    assert "market_bracket" in allowed_strategies(entry_pct=20, complexity=90)
    assert "vertical_spread" not in allowed_strategies(entry_pct=20, complexity=90)

    assert entry_surface_band(50) == "mixed"
    assert "market_bracket" in allowed_strategies(entry_pct=50, complexity=50)
    assert "vertical_spread" in allowed_strategies(entry_pct=50, complexity=50)
    assert "jade_lizard" not in allowed_strategies(entry_pct=50, complexity=50)

    assert entry_surface_band(80) == "options"
    assert "market_bracket" not in allowed_strategies(entry_pct=80, complexity=90)
    assert "vertical_spread" in allowed_strategies(entry_pct=80, complexity=50)
    assert "jade_lizard" in allowed_strategies(entry_pct=80, complexity=90)


def test_option_complexity_bands():
    assert complexity_band(50) == "defined"
    assert complexity_band(90) == "full"


def test_strategy_allowed_respects_config(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: SimpleNamespace(
            control_entry_surface_pct=80,
            control_complexity_pct=50,
            control_options_pct=50,
        ),
    )
    assert strategy_allowed("market_bracket") is False
    assert strategy_allowed("vertical_spread") is True
    assert "entry_surface=options" in (reject_reason("market_bracket") or "")


def test_exits_always_allowed(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.config.get_config",
        lambda: SimpleNamespace(
            control_entry_surface_pct=80,
            control_complexity_pct=20,
            control_options_pct=20,
        ),
    )
    assert strategy_allowed("modify_stop") is True
    assert strategy_allowed("close_option") is True
    assert strategy_allowed("hold") is True


@pytest.mark.asyncio
async def test_refresh_legal_offline_fallback():
    from abcxauto.universe import membership_rows, universe_glance_line

    save_allowlist(
        {
            "enabled_arenas": ["index_etfs", "commodities"],
            "custom_symbols": ["ROKU"],
            "exclude_symbols": ["USO"],
        }
    )
    al = await refresh_legal_set(connector=None, persist=True)
    legal = set(al["legal_symbols"])
    assert "SPY" in legal
    assert "GLD" in legal
    assert "ROKU" in legal
    assert "USO" not in legal
    assert is_legal_symbol("SPY")
    assert not is_legal_symbol("USO")
    assert filter_to_legal(["SPY", "ZZZZ", "GLD"]) == ["SPY", "GLD"]
    mem = {r["symbol"]: r for r in al.get("membership") or []}
    assert mem["SPY"]["source"] == "mda_fallback"
    assert mem["ROKU"]["arena"] == "custom"
    assert membership_rows(query="spy")[0]["symbol"] == "SPY"
    assert "legal" in universe_glance_line().lower()


def test_hunt_outside_sandbox_rejected(tmp_path, monkeypatch):
    from abcxauto.agent_loop import validate_judgment
    from abcxauto.world_state import WorldState

    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": [],
            "exclude_symbols": [],
            "legal_symbols": ["SPY", "QQQ", "IWM"],
        }
    )
    reset_universe_cache()
    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[{"symbol": "ZZZZ", "source": "mda"}],
        news_items=[],
        risk_posture="aggressive",
        effective_posture="aggressive",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        capacity={
            "open_count": 0,
            "max_open_positions": 6,
            "slots_left": 6,
            "allows_new_risk": True,
        },
    )
    ok, reason, _ = validate_judgment(
        {
            "stance": "hunt",
            "thesis": "edge",
            "focus": "ZZZZ",
            "dismissed": "",
            "intent": {"kind": "hunt", "symbol": "ZZZZ", "direction": "LONG"},
            "risk_budget_pct": 1.0,
            "regime_fit": True,
            "setup_grade": "A",
        },
        world,
    )
    assert ok is False
    assert "sandbox" in reason.lower() or "universe" in reason.lower()


def test_load_default_arenas():
    al = load_allowlist()
    assert al["enabled_arenas"]


def test_rejects_unit_warrant_junk_tickers():
    assert is_common_equity_symbol("AAPL")
    assert is_common_equity_symbol("BRK.B")
    assert is_common_equity_symbol("LOW")
    assert is_common_equity_symbol("MU")
    assert is_common_equity_symbol("MSTU")  # 4-char ETF â€” IBKR stockTypeFilter owns this
    assert not is_common_equity_symbol("AACOU")
    assert not is_common_equity_symbol("DMAAR")
    assert not is_common_equity_symbol("MESHU")
    assert not is_common_equity_symbol("IACOU")


def test_tape_seed_not_alphabetized(monkeypatch):
    from abcxauto.opportunity_scan import _universe

    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": [],
            "exclude_symbols": [],
            "legal_symbols": ["ZZZZ", "AAPL", "MSFT"],
        }
    )
    reset_universe_cache()
    # Persist may re-normalize; force cache order used by legal_symbols.
    monkeypatch.setattr(
        "abcxauto.universe.legal_symbols",
        lambda use_cache=True: ["ZZZZ", "AAPL", "MSFT"],
    )
    seed = _universe([{"symbol": "NVDA"}], cap=10)
    assert seed[0] == "NVDA"
    assert seed[1:] == ["ZZZZ", "AAPL", "MSFT"]
    assert seed != sorted(seed)
