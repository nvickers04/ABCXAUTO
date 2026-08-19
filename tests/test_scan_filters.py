"""Optional IBKR scan() filters this look — clerk allowlist, applied echo."""

from __future__ import annotations

import json

import pytest

from abcxauto.universe import (
    _pe_tags_from_xml,
    merge_scan_filters_into_spec,
    parse_scan_filters,
    reset_pe_tag_cache,
    resolve_screen,
)


def setup_function():
    reset_pe_tag_cache()


def teardown_function():
    reset_pe_tag_cache()


def test_parse_native_filters_allowlist():
    out = parse_scan_filters(
        {
            "arena": "most_active",
            "above_price": 5,
            "above_volume": 1_000_000,
            "market_cap_above": 50_000_000_000,
            "below_price": 500,
            "average_option_volume_above": 1000,
        }
    )
    assert out["ok"] is True
    assert out["native"]["abovePrice"] == 5.0
    assert out["native"]["aboveVolume"] == 1_000_000
    assert out["native"]["marketCapAbove"] == 50_000_000_000.0
    assert out["native"]["belowPrice"] == 500.0
    assert out["native"]["averageOptionVolumeAbove"] == 1000
    assert out["applied"]["above_price"] == 5.0
    assert out["applied"]["market_cap_above"] == 50_000_000_000.0


def test_parse_tagvalue_allowlist():
    out = parse_scan_filters(
        {
            "scan_code": "MOST_ACTIVE",
            "usdMarketCapAbove": "10000",
            "optVolumeAbove": "1000",
            "avgVolumeAbove": "500000",
        }
    )
    assert out["ok"] is True
    assert out["tags"] == {
        "usdMarketCapAbove": "10000",
        "optVolumeAbove": "1000",
        "avgVolumeAbove": "500000",
    }
    assert out["applied"]["usdMarketCapAbove"] == "10000"


def test_unknown_keys_rejected():
    out = parse_scan_filters({"arena": "most_active", "volumeAbove": 1})
    assert out["ok"] is False
    assert "unknown" in out["error"].lower()
    assert "volumeAbove" in out["error"]


def test_arbitrary_tag_rejected():
    out = parse_scan_filters({"arena": "most_active", "priceAbove": "1"})
    assert out["ok"] is False
    assert "priceAbove" in out["error"]


def test_pe_omitted_when_unverified():
    out = parse_scan_filters(
        {"arena": "most_active", "peRatioAbove": "15"},
        pe_tags=frozenset(),
    )
    assert out["ok"] is False
    assert "peRatioAbove" in out["error"]


def test_pe_accepted_only_when_xml_verified():
    out = parse_scan_filters(
        {"arena": "most_active", "peRatioAbove": "20"},
        pe_tags=frozenset({"peRatioAbove"}),
    )
    assert out["ok"] is True
    assert out["tags"]["peRatioAbove"] == "20"
    assert out["applied"]["peRatioAbove"] == "20"


def test_pe_tags_from_xml_requires_code_element():
    xml = "<ScannerParameters><AbstractField><code>peRatioAbove</code></AbstractField></ScannerParameters>"
    assert _pe_tags_from_xml(xml) == frozenset({"peRatioAbove"})
    # Do not guess from prose / unrelated text.
    assert _pe_tags_from_xml("maybe pe ratio above sometime") == frozenset()


def test_merge_filters_into_arena_spec():
    resolved = resolve_screen(arena="most_active")
    assert resolved["ok"] is True
    filt = parse_scan_filters(
        {
            "above_price": 10,
            "above_volume": 2_000_000,
            "market_cap_above": 1e11,
            "usdMarketCapAbove": "50000",
        }
    )
    spec, applied = merge_scan_filters_into_spec(resolved["ibkr"], filt)
    assert spec is not None
    assert spec["scanCode"] == "MOST_ACTIVE"
    assert spec["abovePrice"] == 10.0
    assert spec["aboveVolume"] == 2_000_000
    assert spec["marketCapAbove"] == 1e11
    assert spec["filterTags"]["usdMarketCapAbove"] == "50000"
    assert applied["above_price"] == 10.0
    assert applied["usdMarketCapAbove"] == "50000"


def test_scan_tool_schema_has_no_pe_and_no_tag_catalog():
    from abcxauto.brain import AGENT_TOOLS
    from abcxauto.llm import SYSTEM_PROMPT

    scan = None
    for t in AGENT_TOOLS:
        fn = getattr(t, "function", None)
        name = str(getattr(fn, "name", None) or getattr(t, "name", "") or "")
        if name == "scan":
            scan = t
            break
    assert scan is not None
    fn = getattr(scan, "function", None)
    raw_params = getattr(fn, "parameters", None) or {}
    if isinstance(raw_params, str):
        params = json.loads(raw_params)
    else:
        params = dict(raw_params)
    props = params.get("properties") or {}
    blob = json.dumps(props).lower()
    assert "peratio" not in blob
    assert "pe_ratio" not in blob
    assert "market_cap_above" in props
    assert "above_price" in props
    assert "usdMarketCapAbove" in props
    # Kill: no SYSTEM catalog of IBKR tags / guessed P/E.
    assert "usdMarketCapAbove" not in SYSTEM_PROMPT
    assert "peRatio" not in SYSTEM_PROMPT
    assert "TagValue" not in SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_scan_filters_echo_applied_and_reach_ibkr_spec(monkeypatch):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    seen: dict = {}

    async def fake_pull(connector=None, *, arena=None, scan_code=None, filters=None):
        seen["arena"] = arena
        seen["filters"] = filters
        spec, applied = merge_scan_filters_into_spec(
            resolve_screen(arena=arena).get("ibkr"),
            filters,
        )
        seen["spec"] = spec
        return {
            "ok": True,
            "arena_id": "most_active",
            "scan_code": "MOST_ACTIVE",
            "source": "ibkr",
            "symbols": ["TSLA", "AMD"],
            "applied": applied,
            "persisted": False,
        }

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", fake_pull)

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=1.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    data = json.loads(
        await _run_tool(
            "scan",
            {
                "arena": "most_active",
                "above_price": 5,
                "above_volume": 1_000_000,
                "market_cap_above": 200_000_000_000,
            },
            connector=object(),
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data["ok"] is True
    assert data["symbols"] == ["TSLA", "AMD"]
    assert data["applied"]["above_price"] == 5.0
    assert data["applied"]["above_volume"] == 1_000_000
    assert data["applied"]["market_cap_above"] == 200_000_000_000.0
    assert seen["spec"]["abovePrice"] == 5.0
    assert seen["spec"]["aboveVolume"] == 1_000_000
    assert seen["spec"]["marketCapAbove"] == 200_000_000_000.0
    assert data["persisted"] is False
    assert data["ranked"] is False
    assert all("last" not in h and "bid" not in h for h in data["hits"])


@pytest.mark.asyncio
async def test_scan_unknown_filter_key_errors(monkeypatch):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def boom(*_a, **_k):
        raise AssertionError("must not pull screen on unknown key")

    monkeypatch.setattr("abcxauto.universe.pull_one_screen", boom)

    world = WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=1.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    data = json.loads(
        await _run_tool(
            "scan",
            {"arena": "most_active", "magicFilter": 1},
            connector=None,
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    assert data.get("ok") is False
    assert "unknown" in str(data.get("error") or "").lower()


@pytest.mark.asyncio
async def test_ibkr_scan_passes_native_and_tag_filters(monkeypatch):
    from abcxauto.universe import _ibkr_scan

    captured: dict = {}

    class FakeIB:
        async def reqScannerDataAsync(self, sub, opts=None, filter_opts=None):
            captured["sub"] = sub
            captured["filter_opts"] = list(filter_opts or [])
            return []

        def cancelScannerSubscription(self, _sub):
            return None

    class Conn:
        connected = True
        ib = FakeIB()

        class _Lock:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        async_lock = _Lock()

    out = await _ibkr_scan(
        Conn(),
        {
            "scanCode": "MOST_ACTIVE",
            "locationCode": "STK.US.MAJOR",
            "rows": 10,
            "abovePrice": 5.0,
            "belowPrice": 100.0,
            "aboveVolume": 1_000_000,
            "marketCapAbove": 1e10,
            "averageOptionVolumeAbove": 500,
            "filterTags": {
                "usdMarketCapAbove": "10000",
                "optVolumeAbove": "1000",
                "avgVolumeAbove": "500000",
            },
        },
    )
    assert out["ok"] is True
    assert out["symbols"] == []
    sub = captured["sub"]
    assert sub.abovePrice == 5.0
    assert sub.belowPrice == 100.0
    assert sub.aboveVolume == 1_000_000
    assert sub.marketCapAbove == 1e10
    assert sub.averageOptionVolumeAbove == 500
    tags = {t.tag: t.value for t in captured["filter_opts"]}
    assert tags["usdMarketCapAbove"] == "10000"
    assert tags["optVolumeAbove"] == "1000"
    assert tags["avgVolumeAbove"] == "500000"


@pytest.mark.asyncio
async def test_ibkr_scanner_error_returns_error_not_names(monkeypatch):
    from abcxauto.universe import ARENA_CATALOG, pull_one_screen

    async def boom(_connector, _spec):
        return {"ok": False, "error": "Error 162: Historical market data Service error", "symbols": []}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", boom)
    catalog = list(ARENA_CATALOG["mega_cap"]["mda_fallback"] or [])

    class Conn:
        connected = True

    out = await pull_one_screen(Conn(), arena="mega_cap")
    assert out["ok"] is False
    assert "162" in str(out.get("error") or "")
    for name in catalog[:3]:
        assert name not in str(out)


@pytest.mark.asyncio
async def test_tool_args_hoists_camel_filter_aliases():
    from abcxauto.tool_args import normalize_tool_call

    name, args = normalize_tool_call(
        "scan",
        {
            "arena": "most_active",
            "abovePrice": 5,
            "aboveVolume": 1000000,
            "marketCapAbove": 1e9,
            "usd_market_cap_above": "10000",
        },
    )
    assert name == "scan"
    assert args["above_price"] == 5
    assert args["above_volume"] == 1000000
    assert args["market_cap_above"] == 1e9
    assert args["usdMarketCapAbove"] == "10000"
    assert "abovePrice" not in args
    assert "usd_market_cap_above" not in args
