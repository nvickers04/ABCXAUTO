"""Optional IBKR scan() filters this look — clerk allowlist, applied echo."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("stub_agent_loop_import")

from abcxauto.universe import (
    ARENA_CATALOG,
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


def test_expanded_screen_keys_resolve_and_unknown_still_rejected():
    from abcxauto.universe import known_scan_codes, known_screen_keys

    codes = known_scan_codes()
    assert "HOT_BY_VOLUME" in codes
    assert "TOP_TRADE_COUNT" in codes
    assert "HOT_BY_PRICE" in codes
    assert "TOP_OPEN_PERC_GAIN" in codes
    # Invented / non-catalog codes stay rejected.
    bad = resolve_screen(scan_code="FAKE_MOONSHOT_SCAN")
    assert bad["ok"] is False
    assert "unknown" in bad["error"].lower()

    for arena, code in (
        ("hot_by_volume", "HOT_BY_VOLUME"),
        ("top_trade_count", "TOP_TRADE_COUNT"),
        ("hot_by_price", "HOT_BY_PRICE"),
        ("most_active", "MOST_ACTIVE"),
    ):
        by_name = resolve_screen(arena=arena)
        by_code = resolve_screen(scan_code=code)
        assert by_name["ok"] is True
        assert by_code["ok"] is True
        assert by_name["scan_code"] == code
        assert by_code["ibkr"]["scanCode"] == code
        # Zero filter args: standing screen still has IBKR spec.
        assert by_name["ibkr"] is not None

    keys = known_screen_keys()
    assert "hot_by_volume" in keys
    assert "HOT_BY_VOLUME" in keys
    assert "top_trade_count" in keys
    assert "TOP_TRADE_COUNT" in keys
    # No sort= knob — screen name is the sort.
    assert "sort" not in keys



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


def test_both_selectors_compose_universe_and_sort():
    """arena is the universe; scan_code is the sort. Dropping the sort made
    mega_cap+TOP_PERC_LOSE run HOT_BY_VOLUME and the look spent itself retrying.
    """
    from abcxauto.tool_args import normalize_tool_call
    from abcxauto.universe import resolve_screen

    _name, args = normalize_tool_call(
        "scan", {"arena": "mega_cap", "scan_code": "TOP_PERC_LOSE"}
    )
    assert args.get("arena") == "mega_cap"
    assert args.get("scan_code") == "TOP_PERC_LOSE"

    composed = resolve_screen(arena="mega_cap", scan_code="TOP_PERC_LOSE")
    assert composed["ok"] is True
    assert composed["arena_id"] == "mega_cap"
    assert composed["scan_code"] == "TOP_PERC_LOSE"
    assert composed["ibkr"]["scanCode"] == "TOP_PERC_LOSE"
    assert composed["ibkr"]["marketCapAbove"] == 200_000_000_000

    # One selector on its own is untouched, either way round.
    _n2, only_code = normalize_tool_call("scan", {"scan_code": "TOP_PERC_LOSE"})
    assert only_code.get("scan_code") == "TOP_PERC_LOSE"
    assert not str(only_code.get("arena") or "").strip()
    _n3, only_arena = normalize_tool_call("scan", {"arena": "most_active"})
    assert only_arena.get("arena") == "most_active"


def test_scan_tool_schema_advertises_filters_and_keeps_system_clean():
    """Filters live in the tool schema. SYSTEM stays free of a tag catalog."""
    from abcxauto.brain import AGENT_TOOLS
    from abcxauto.llm import SYSTEM_PROMPT
    from abcxauto.universe import known_scan_codes, known_screen_keys

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
        if hasattr(raw_params, "model_dump"):
            params = raw_params.model_dump()
    props = params.get("properties") or {}
    desc = str(getattr(fn, "description", "") or "")
    assert "market_cap_above" in props
    assert "above_price" in props
    assert "above_volume" in props
    assert "average_option_volume_above" in props
    assert "usdMarketCapAbove" in props
    assert "optVolumeAbove" in props
    assert "avgVolumeAbove" in props
    assert "peRatioAbove" in props
    assert "peRatioBelow" in props
    assert "industry" in props
    assert "sector" in props
    assert "stock_type" in props
    assert "xml" in str((props.get("peRatioAbove") or {}).get("description") or "").lower()
    arena = props.get("arena") or {}
    code = props.get("scan_code") or {}
    assert arena.get("enum") == known_screen_keys()
    assert code.get("enum") == known_scan_codes()
    assert "skip_class" in desc.lower()
    assert "ranked" in desc.lower()
    assert "symbols[]" in desc.lower() or "symbols" in desc.lower()
    # Kill: no SYSTEM catalog of IBKR tags / guessed P/E / XML dump.
    assert "usdMarketCapAbove" not in SYSTEM_PROMPT
    assert "peRatio" not in SYSTEM_PROMPT
    assert "TagValue" not in SYSTEM_PROMPT
    assert "reqScannerParameters" not in SYSTEM_PROMPT
    assert "<ScanCode>" not in SYSTEM_PROMPT


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
    assert data.get("thin") is True
    assert data.get("sort") == "MOST_ACTIVE"
    assert all("bid" not in h and "ask" not in h for h in data["hits"])
    from abcxauto.opportunity_scan import RANKED_ROW_KEYS

    for hit in data["hits"]:
        assert set(hit) <= RANKED_ROW_KEYS
        assert hit.get("source") == "ibkr"
        assert "skip_class" in hit


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
    # 1e10 raw USD → 10000 million. Native 1e10 was an impossible cap.
    assert sub.marketCapAbove == 10_000.0
    assert sub.averageOptionVolumeAbove == 500
    tags = {t.tag: t.value for t in captured["filter_opts"]}
    assert tags["usdMarketCapAbove"] == "10000"
    assert tags["marketCapAbove1e6"] == "10000"
    assert tags["optVolumeAbove"] == "1000"
    assert tags["avgVolumeAbove"] == "500000"


def test_usd_cap_converts_to_ibkr_millions():
    from abcxauto.universe import _usd_to_scanner_millions, resolve_screen

    assert _usd_to_scanner_millions(200_000_000_000) == 200_000.0
    assert _usd_to_scanner_millions(10_000_000_000) == 10_000.0
    mega = resolve_screen(arena="mega_cap", scan_code="TOP_PERC_LOSE")
    assert mega["ibkr"]["marketCapAbove"] == 200_000_000_000
    assert _usd_to_scanner_millions(mega["ibkr"]["marketCapAbove"]) == 200_000.0


@pytest.mark.asyncio
async def test_empty_mega_screen_echoes_the_cap_filter(monkeypatch):
    """An empty mega sort must still show the $200B floor so it is not a silent miss."""
    from abcxauto.universe import pull_one_screen

    async def empty(_connector, spec):
        assert spec["marketCapAbove"] == 200_000_000_000
        return {"ok": True, "symbols": [], "rows": []}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", empty)

    class Conn:
        connected = True

    out = await pull_one_screen(Conn(), arena="mega_cap", scan_code="TOP_PERC_LOSE")
    assert out["ok"] is True
    assert out["source"] == "empty"
    assert out["applied"]["market_cap_above"] == 200_000_000_000


@pytest.mark.asyncio
async def test_ibkr_scanner_error_returns_error_not_names(monkeypatch):
    from abcxauto.universe import pull_one_screen

    async def boom(_connector, _spec):
        return {"ok": False, "error": "Error 162: Historical market data Service error", "symbols": []}

    monkeypatch.setattr("abcxauto.universe._ibkr_scan", boom)

    class Conn:
        connected = True

    out = await pull_one_screen(Conn(), arena="mega_cap")
    assert out["ok"] is False
    assert "162" in str(out.get("error") or "")
    for name in ("AAPL", "MSFT", "NVDA", "SPY"):
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


def test_tool_args_hoists_stock_type_and_industry():
    from abcxauto.tool_args import normalize_tool_call

    name, args = normalize_tool_call(
        "scan",
        {
            "arena": "most_active",
            "stockType": "ETF",
            "industry": "Technology",
        },
    )
    assert name == "scan"
    assert args["stock_type"] == "ETF"
    assert args["industry"] == "Technology"
    assert "stockType" not in args


def test_flush_default_is_the_card_trio_with_large_mega_tags():
    from abcxauto.universe import (
        ARENA_CATALOG,
        flush_cap_filters,
        flush_default_jobs,
        is_flush_default_screen,
        _usd_to_scanner_millions,
    )

    jobs = flush_default_jobs()
    assert [(j["arena"], j["scan_code"]) for j in jobs] == [
        ("most_active", "MOST_ACTIVE"),
        ("top_losers", "TOP_PERC_LOSE"),
        ("top_gainers", "TOP_PERC_GAIN"),
    ]
    assert is_flush_default_screen() is True
    assert is_flush_default_screen(scan_code="TOP_PERC_LOSE") is True
    assert is_flush_default_screen(arena="most_active") is True
    assert is_flush_default_screen(arena="mega_cap", scan_code="TOP_PERC_LOSE") is False
    assert is_flush_default_screen(arena="hot_by_volume") is False
    cap = flush_cap_filters()
    floor = float(ARENA_CATALOG["large_cap"]["ibkr"]["marketCapAbove"])
    assert cap["native"]["marketCapAbove"] == floor
    assert cap["native"]["marketCapAbove"] < 200_000_000_000
    # Same millions path as mega/large arenas — not raw USD that emptied the tape.
    assert _usd_to_scanner_millions(cap["native"]["marketCapAbove"]) == 10_000.0
    kept = flush_cap_filters(
        {"ok": True, "native": {"marketCapAbove": 5e9}, "tags": {}, "applied": {}}
    )
    assert kept["native"]["marketCapAbove"] == 5e9


def test_stock_type_filter_is_native():
    both = parse_scan_filters({"arena": "most_active", "stock_type": "both"})
    assert both["ok"] is True
    assert both["native"]["stockTypeFilter"] == "CORP,ETF"
    assert both["applied"]["stock_type"] == "CORP,ETF"
    corp = parse_scan_filters({"stock_type": "CORP"})
    assert corp["native"]["stockTypeFilter"] == "CORP"
    etf = parse_scan_filters({"stock_type": "etf"})
    assert etf["native"]["stockTypeFilter"] == "ETF"
    bad = parse_scan_filters({"stock_type": "warrant"})
    assert bad["ok"] is False
    assert "stock_type" in bad["error"]


def test_industry_rejected_when_xml_unverified():
    from abcxauto.universe import _industry_tags_from_xml

    assert _industry_tags_from_xml("maybe industry sometime") == frozenset()
    assert _industry_tags_from_xml("<ScannerParameters></ScannerParameters>") == frozenset()
    out = parse_scan_filters(
        {"arena": "most_active", "industry": "Technology"},
        industry_tags=frozenset(),
    )
    assert out["ok"] is False
    assert "industry" in out["error"]


def test_industry_accepted_only_when_xml_lists_the_code():
    from abcxauto.universe import _industry_tags_from_xml

    xml = (
        "<ScannerParameters><AbstractField>"
        "<code>industry</code></AbstractField>"
        "<AbstractField><code>sector</code></AbstractField>"
        "</ScannerParameters>"
    )
    found = _industry_tags_from_xml(xml)
    assert found == frozenset({"industry", "sector"})
    # category is not an industry/sector code name — do not invent it.
    mixed = (
        "<ScannerParameters><AbstractField>"
        "<code>category</code></AbstractField></ScannerParameters>"
    )
    assert _industry_tags_from_xml(mixed) == frozenset()

    out = parse_scan_filters(
        {"arena": "most_active", "industry": "Technology"},
        industry_tags=frozenset({"industry"}),
    )
    assert out["ok"] is True
    assert out["tags"]["industry"] == "Technology"
    assert out["applied"]["industry"] == "Technology"
    spec, applied = merge_scan_filters_into_spec(
        resolve_screen(arena="most_active")["ibkr"],
        out,
    )
    assert spec["filterTags"]["industry"] == "Technology"
    assert "industry" not in (ARENA_CATALOG["most_active"].get("ibkr") or {})


def test_catalog_specs_do_not_hardcode_unverified_industry():
    for meta in ARENA_CATALOG.values():
        ibkr = meta.get("ibkr") or {}
        blob = json.dumps(ibkr).lower()
        assert "industry" not in blob
        assert "sector" not in blob


@pytest.mark.asyncio
async def test_verified_industry_tags_uses_live_xml_or_stays_empty(monkeypatch):
    from abcxauto.universe import reset_industry_tag_cache, verified_industry_tags

    reset_industry_tag_cache()
    assert await verified_industry_tags(None) == frozenset()

    class FakeIB:
        async def reqScannerParametersAsync(self):
            return (
                "<ScannerParameters><AbstractField>"
                "<code>stockIndustry</code></AbstractField></ScannerParameters>"
            )

    class Conn:
        connected = True
        ib = FakeIB()

        class _Lock:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        async_lock = _Lock()

    found = await verified_industry_tags(Conn())
    assert found == frozenset({"stockIndustry"})
    # Unverified friendly name still rejected.
    parsed = parse_scan_filters(
        {"industry": "Technology"},
        industry_tags=found,
    )
    assert parsed["ok"] is False
    assert "industry" in parsed["error"]
    ok = parse_scan_filters(
        {"stockIndustry": "Technology"},
        industry_tags=found,
    )
    assert ok["ok"] is True
    assert ok["tags"]["stockIndustry"] == "Technology"
