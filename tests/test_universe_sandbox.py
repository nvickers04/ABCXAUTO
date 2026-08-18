"""Universe watchlist — scan seed, not a send sandbox."""

from __future__ import annotations

import pytest

from abcxauto.world_state import WorldState
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
    assert filter_to_legal(["SPY", "ZZZZ", "GLD"]) == ["SPY", "ZZZZ", "GLD"]
    mem = {r["symbol"]: r for r in al.get("membership") or []}
    assert mem["SPY"]["source"] == "mda_fallback"
    assert mem["ROKU"]["arena"] == "custom"
    assert membership_rows(query="spy")[0]["symbol"] == "SPY"
    assert "legal" in universe_glance_line().lower()


def test_hunt_is_not_limited_to_watchlist(tmp_path, monkeypatch):
    from abcxauto.agent_loop import gate_ticket

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
    strat, forced = gate_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {"symbol": "ZZZZ", "quantity": 1, "direction": "LONG"},
            "rationale": "edge",
        },
        world,
    )
    note = str((forced or {}).get("note") or "").lower()
    assert "sandbox" not in note
    assert "universe" not in note
    assert strat == "market_bracket"
    assert forced is None


def test_load_default_arenas():
    al = load_allowlist()
    assert al["enabled_arenas"]


def test_rejects_unit_warrant_junk_tickers():
    assert is_common_equity_symbol("AAPL")
    assert is_common_equity_symbol("BRK.B")
    assert is_common_equity_symbol("LOW")
    assert is_common_equity_symbol("MU")
    assert is_common_equity_symbol("MSTU")
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
    monkeypatch.setattr(
        "abcxauto.universe.legal_symbols",
        lambda use_cache=True: ["ZZZZ", "AAPL", "MSFT"],
    )
    seed = _universe([{"symbol": "NVDA"}], cap=10)
    assert seed[0] == "NVDA"
    assert seed[1:] == ["ZZZZ", "AAPL", "MSFT"]
    assert seed != sorted(seed)
