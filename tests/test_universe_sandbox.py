"""Live IBKR screens — no persisted watchlist, no seed dump."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from abcxauto.universe import (
    ARENA_CATALOG,
    is_common_equity_symbol,
    known_screen_keys,
    pull_one_screen,
    resolve_screen,
    scan_skip_class,
)


_DELETED_SCREENS = (
    "index_etfs",
    "commodities",
    "technology",
    "healthcare",
    "energy",
    "financials",
)
_CANNED = ("SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA")


def test_catalog_is_ibkr_screens_only():
    assert "index_etfs" not in ARENA_CATALOG
    assert "financials" not in ARENA_CATALOG
    assert "technology" not in ARENA_CATALOG
    for screen_id, meta in ARENA_CATALOG.items():
        assert meta.get("ibkr"), screen_id
        assert "mda_fallback" not in meta
        assert (meta.get("ibkr") or {}).get("scanCode")
    assert "mega_cap" in ARENA_CATALOG
    assert "large_cap" in ARENA_CATALOG
    assert "mid_cap" in ARENA_CATALOG
    assert "most_active" in ARENA_CATALOG


def test_universe_module_cannot_write_a_watchlist():
    import abcxauto.universe as universe

    src = inspect.getsource(universe)
    assert "universe_allowlist" not in src
    assert "ABCXAUTO_UNIVERSE_PATH" not in src
    assert "save_allowlist" not in src
    assert "load_allowlist" not in src
    assert "mda_fallback" not in src
    assert "legal_symbols" not in src
    assert "write_text" not in src
    assert not hasattr(universe, "save_allowlist")
    assert not hasattr(universe, "load_allowlist")
    assert not hasattr(universe, "legal_symbols")


def test_new_entry_is_not_limited_to_a_watchlist():
    import abcxauto.universe as universe

    assert not hasattr(universe, "is_legal_symbol")
    assert not hasattr(universe, "filter_to_legal")
    assert not hasattr(universe, "legal_symbols")


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


def test_tape_seed_is_book_only_never_alphabetized_or_invented():
    from abcxauto.opportunity_scan import _universe

    seed = _universe([{"symbol": "NVDA"}, {"symbol": "ZZZZ"}, {"symbol": "AAPL"}], cap=10)
    assert seed[0] == "NVDA"
    assert seed == ["NVDA", "ZZZZ", "AAPL"]
    assert seed != sorted(seed)
    assert _universe([]) == []
    for name in ("SPY", "QQQ", "IWM"):
        assert name not in _universe([])


def test_deleted_screens_rejected_and_error_names_valid():
    for dead in _DELETED_SCREENS:
        out = resolve_screen(arena=dead)
        assert out["ok"] is False
        err = str(out.get("error") or "")
        assert "unknown screen" in err
        assert dead in err
        assert "valid=" in err
        for name in ("mega_cap", "most_active", "top_gainers"):
            assert name in err
        assert "financials" not in (out.get("screens") or [])
        assert "mega_cap" in (out.get("screens") or known_screen_keys())


@pytest.mark.asyncio
async def test_pull_one_screen_ibkr_empty_stays_empty_and_labeled(monkeypatch):
    async def empty_scan(_connector, _spec):
        return {"ok": True, "symbols": [], "rows": [], "ibkr_rows": 0, "kept": 0}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", empty_scan)

    class Conn:
        connected = True

    out = await pull_one_screen(Conn(), arena="mega_cap")
    assert out["ok"] is True
    assert out["source"] == "empty"
    assert out["empty"] is True
    assert out["symbols"] == []
    assert out["rows"] == []
    assert out["kept"] == 0
    for name in _CANNED:
        assert name not in out["symbols"]
        assert name not in json.dumps(out)


@pytest.mark.asyncio
async def test_pull_one_screen_without_ibkr_does_not_dump_catalog():
    out = await pull_one_screen(None, arena="mega_cap")
    assert out["ok"] is False
    assert "IBKR" in str(out.get("error") or "")
    blob = json.dumps(out)
    for name in _CANNED:
        assert name not in blob


@pytest.mark.asyncio
async def test_deleted_screen_pull_is_rejected(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("deleted screen must not hit IBKR")

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", boom)
    out = await pull_one_screen(object(), arena="financials")
    assert out["ok"] is False
    assert "unknown screen" in str(out.get("error") or "")
    assert "most_active" in str(out.get("error") or "")


@pytest.mark.asyncio
async def test_pull_one_screen_does_not_write_watchlist(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    before = {
        p: (p.stat().st_mtime_ns if p.is_file() else None)
        for p in (repo_root / "universe_allowlist.json", tmp_path / "universe_allowlist.json")
    }

    async def empty_scan(_connector, _spec):
        return {"ok": True, "symbols": [], "rows": [], "ibkr_rows": 0, "kept": 0}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", empty_scan)

    class Conn:
        connected = True

    await pull_one_screen(Conn(), arena="most_active")
    assert not (tmp_path / "universe_allowlist.json").is_file()
    live = repo_root / "universe_allowlist.json"
    after = live.stat().st_mtime_ns if live.is_file() else None
    assert after == before[live]


def test_scan_skip_class_labels_levered_and_clean():
    assert scan_skip_class({"symbol": "TQQQ"}) == "levered"
    assert scan_skip_class({"symbol": "AAPL", "last": 230.0}) == ""
    assert scan_skip_class({"symbol": "PSQL", "last": 4.2}) == "micro"
