"""Grok chooses enabled_arenas via self_tune; scan labels the catalog."""

from __future__ import annotations

import json

import pytest

from abcxauto.config import clear_runtime_overrides, get_config
from abcxauto.self_tune import apply_self_tune
from abcxauto.universe import (
    ARENA_CATALOG,
    load_allowlist,
    refresh_legal_set,
    reset_universe_cache,
    save_allowlist,
)


_HAND_PICKED = [
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "MDY",
    "GLD",
    "SLV",
    "USO",
    "UNG",
    "TLT",
    "TIP",
    "UUP",
]


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_UNIVERSE_PATH", str(tmp_path / "universe.json"))
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(tmp_path / "risk.json"))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    reset_universe_cache()
    clear_runtime_overrides()
    get_config.cache_clear()
    yield
    reset_universe_cache()
    clear_runtime_overrides()
    get_config.cache_clear()


def _tool_props(name: str) -> dict:
    from abcxauto.brain import AGENT_TOOLS

    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        nm = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        if nm != name:
            continue
        params = getattr(fn, "parameters", None) or {}
        if isinstance(params, str):
            params = json.loads(params)
        elif hasattr(params, "model_dump"):
            params = params.model_dump()
        return dict((params or {}).get("properties") or {})
    raise AssertionError(f"tool {name} missing")


def test_self_tune_enabled_arenas_persists():
    out = apply_self_tune(
        {"enabled_arenas": ["index_etfs", "commodities"]},
        persist=True,
    )
    assert out["status"] == "ok"
    assert (out.get("applied") or {}).get("universe", {}).get("enabled_arenas") == [
        "index_etfs",
        "commodities",
    ]
    al = load_allowlist()
    assert al["enabled_arenas"] == ["index_etfs", "commodities"]


def test_self_tune_rejects_unknown_arena_and_names_catalog():
    prior = load_allowlist()
    out = apply_self_tune(
        {"enabled_arenas": ["index_etfs", "not_a_real_arena"]},
        persist=True,
    )
    rejected = out.get("rejected") or {}
    err = str(rejected.get("enabled_arenas") or rejected.get("universe") or "")
    assert err, out
    assert "not_a_real_arena" in err
    for name in ARENA_CATALOG:
        assert name in err
    al = load_allowlist()
    assert "not_a_real_arena" not in al["enabled_arenas"]
    assert al["enabled_arenas"] == prior["enabled_arenas"]


def test_self_tune_nested_universe_validates_arenas():
    out = apply_self_tune(
        {"universe": {"enabled_arenas": ["bogus_bucket"]}},
        persist=True,
    )
    rejected = out.get("rejected") or {}
    err = str(rejected.get("enabled_arenas") or rejected.get("universe") or "")
    assert "bogus_bucket" in err
    assert "mega_cap" in err


@pytest.mark.asyncio
async def test_refresh_does_not_drop_custom_or_exclude():
    save_allowlist(
        {
            "enabled_arenas": ["technology"],
            "custom_symbols": list(_HAND_PICKED),
            "exclude_symbols": ["INTC"],
        }
    )
    al = await refresh_legal_set(connector=None, persist=True)
    assert al["custom_symbols"] == _HAND_PICKED
    assert al["exclude_symbols"] == ["INTC"]
    for name in _HAND_PICKED:
        assert name in al["legal_symbols"], name
    assert "INTC" not in al["legal_symbols"]
    persisted = load_allowlist()
    assert persisted["custom_symbols"] == _HAND_PICKED
    assert persisted["exclude_symbols"] == ["INTC"]
    for name in _HAND_PICKED:
        assert name in persisted["legal_symbols"]


@pytest.mark.asyncio
async def test_scan_hits_carry_arena_label(monkeypatch):
    from abcxauto.opportunity_scan import criteria_scan

    async def fake_pull(_connector=None, **_k):
        return {
            "ok": True,
            "arena_id": "mega_cap",
            "scan_code": "HOT_BY_VOLUME",
            "source": "ibkr",
            "symbols": ["AAPL", "MSFT"],
            "rows": [
                {"symbol": "AAPL", "rank": 0, "distance": "1.2"},
                {"symbol": "MSFT", "rank": 1, "distance": "0.8"},
            ],
            "applied": {},
        }

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)
    out = await criteria_scan(arena="mega_cap", connector=object())
    assert out["ok"] is True
    assert out.get("arena") == "mega_cap"
    assert (out.get("criteria") or {}).get("arena") == "mega_cap"
    assert out["hits"]
    for row in out["hits"]:
        assert row.get("arena") == "mega_cap"
        assert "last" not in row
        assert "bid" not in row


def test_self_tune_schema_exposes_arena_catalog():
    props = _tool_props("self_tune")
    items = (props.get("enabled_arenas") or {}).get("items") or {}
    enum = items.get("enum")
    assert enum is not None
    assert set(enum) == set(ARENA_CATALOG)
    assert list(enum) == list(ARENA_CATALOG.keys())


def test_self_tune_arenas_cannot_weaken_floor_or_go_live():
    out = apply_self_tune(
        {
            "enabled_arenas": ["index_etfs"],
            "trading_mode": "live",
            "daily_loss_limit_pct": 50.0,
            "defined_risk_only": False,
        },
        persist=True,
    )
    rejected = out.get("rejected") or {}
    assert "trading_mode" in rejected
    assert "daily_loss_limit_pct" in rejected
    assert get_config().trading_mode == "paper"
    assert get_config().daily_loss_limit_pct == 25.0
    assert get_config().defined_risk_only is True
    al = load_allowlist()
    assert al["enabled_arenas"] == ["index_etfs"]


@pytest.mark.asyncio
async def test_mda_arena_membership_is_not_send_geometry():
    from abcxauto.look_snapshot import REASON_CODE, begin_look, check_ticket_numbers

    save_allowlist(
        {
            "enabled_arenas": ["index_etfs"],
            "custom_symbols": [],
            "exclude_symbols": [],
        }
    )
    al = await refresh_legal_set(connector=None, persist=True)
    assert al["membership"]
    for row in al["membership"]:
        assert row.get("source") == "mda_fallback"
        assert "last" not in row
        assert "bid" not in row
        assert "ask" not in row
        assert set(row) <= {"symbol", "arena", "source"}
    snap: dict = {}
    begin_look(snap)
    snap["scan_hits"] = {
        "source": "mda_seed",
        "arena": "index_etfs",
        "rows": [{"symbol": "SPY", "arena": "index_etfs"}],
    }
    ok, code, _msg = check_ticket_numbers(
        "market_bracket",
        {"symbol": "SPY", "price_hint": 500.12, "stop_price": 495.0},
        snap,
    )
    assert ok is False
    assert code == REASON_CODE
