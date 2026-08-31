"""Universe watchlist — scan seed, not a send sandbox."""

from __future__ import annotations

import pytest

from abcxauto.world_state import WorldState
from abcxauto.universe import (
    filter_to_legal,
    is_common_equity_symbol,
    is_legal_symbol,
    legal_symbols,
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


def test_new_entry_is_not_limited_to_watchlist(tmp_path, monkeypatch):
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
            "params": {
                "symbol": "ZZZZ",
                "quantity": 1,
                "direction": "LONG",
                "card": "off-watchlist breakout",
            },
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


@pytest.mark.asyncio
async def test_pull_one_screen_ibkr_empty_no_mda_fallback(monkeypatch):
    """IBKR connected + empty scanner → empty. Do not dump ARENA_CATALOG names."""
    from abcxauto.universe import ARENA_CATALOG, pull_one_screen

    async def empty_scan(_connector, _spec):
        return []

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", empty_scan)
    catalog = list(ARENA_CATALOG["mega_cap"]["mda_fallback"] or [])
    assert catalog  # fixture: arena has catalog names we must not return

    class Conn:
        connected = True

    out = await pull_one_screen(Conn(), arena="mega_cap")
    assert out["ok"] is True
    assert out["source"] == "empty"
    assert out["symbols"] == []
    assert out["persisted"] is False
    for name in catalog:
        assert name not in out["symbols"]


@pytest.mark.asyncio
async def test_pull_one_screen_no_ibkr_may_use_mda_seed():
    """No IBKR connector → MDA industry seed still allowed this look."""
    from abcxauto.universe import ARENA_CATALOG, pull_one_screen

    out = await pull_one_screen(None, arena="technology")
    seed = list(ARENA_CATALOG["technology"]["mda_fallback"] or [])
    assert out["ok"] is True
    assert out["source"] == "mda_seed"
    assert out["symbols"]
    assert out["symbols"][0] == seed[0]
    assert "AAPL" in out["symbols"]
    assert out["persisted"] is False


_CANNED_TAPE = ("SPY", "QQQ", "IWM", "DIA")


def test_legal_symbols_empty_persist_does_not_dump_canned_tape():
    """Empty persist stays empty — no catalog dump, no SPY/QQQ/IWM invent."""
    from abcxauto.universe import ARENA_CATALOG

    al = load_allowlist()
    assert al["legal_symbols"] == []
    got = legal_symbols()
    assert got == []
    for name in _CANNED_TAPE:
        assert name not in got
    for arena_id in ("index_etfs", "mega_cap"):
        for name in ARENA_CATALOG[arena_id]["mda_fallback"] or []:
            assert name not in got
    assert not is_legal_symbol("SPY")
    assert not is_legal_symbol("QQQ")


def test_legal_symbols_caches_empty_without_dump():
    """[] is a cache hit. A later persist write must not leak through until miss."""
    assert legal_symbols() == []
    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "legal_symbols": ["SPY", "QQQ", "IWM"],
        }
    )
    assert legal_symbols() == []
    assert legal_symbols(use_cache=False) == ["SPY", "QQQ", "IWM"]


@pytest.mark.asyncio
async def test_refresh_empty_does_not_invent_index_defaults():
    """most_active has no MDA seed. Empty must not persist SPY/QQQ/IWM."""
    save_allowlist(
        {
            "enabled_arenas": ["most_active"],
            "custom_symbols": [],
            "exclude_symbols": [],
        }
    )
    al = await refresh_legal_set(connector=None, persist=True)
    assert al["legal_symbols"] == []
    assert al["membership"] == []
    assert al["source"] == "empty"
    for name in _CANNED_TAPE:
        assert name not in al["legal_symbols"]
    assert legal_symbols() == []
    persisted = load_allowlist()
    assert persisted["legal_symbols"] == []


@pytest.mark.asyncio
async def test_refresh_ibkr_empty_no_mda_catalog_dump(monkeypatch):
    """Connected IBKR + empty mega_cap screen → persist empty, not catalog names."""
    from abcxauto.universe import ARENA_CATALOG

    async def empty_scan(_connector, _spec):
        return {"ok": True, "symbols": []}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", empty_scan)
    catalog = list(ARENA_CATALOG["mega_cap"]["mda_fallback"] or [])
    assert catalog
    save_allowlist(
        {
            "enabled_arenas": ["mega_cap"],
            "custom_symbols": [],
            "exclude_symbols": [],
        }
    )

    class Conn:
        connected = True

    al = await refresh_legal_set(Conn(), persist=True)
    assert al["legal_symbols"] == []
    assert al["source"] == "empty"
    for name in catalog:
        assert name not in al["legal_symbols"]
    assert legal_symbols() == []
