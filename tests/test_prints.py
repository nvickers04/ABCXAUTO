"""IBKR + MDA prints share one clock, one nest, no mixed last."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from abcxauto.prints import (
    attach_mda_news,
    asof_fields,
    bar_time_fields,
    ibkr_block,
    live_limit_px,
    mda_worth_asking,
    merge_mda_metrics,
    note_mda_miss,
    parse_asof,
    reset_mda_miss_cache,
    stamp,
)


def setup_function():
    reset_mda_miss_cache()


def teardown_function():
    reset_mda_miss_cache()


def test_parse_asof_unix_and_iso_match():
    dt = parse_asof(1_777_000_000)
    iso = parse_asof("2026-05-01T00:00:00Z")
    assert dt is not None and iso is not None
    assert asof_fields(1_777_000_000)["asof"] == 1_777_000_000
    assert asof_fields("2026-05-01T00:00:00Z")["asof_iso"] == "2026-05-01T00:00:00Z"


def test_parse_asof_rejects_bar_indexes():
    assert parse_asof(59) is None
    assert asof_fields(59) == {}


def test_bar_time_fields_add_unix_beside_original_t():
    row = bar_time_fields("2026-08-18T18:00:05")
    assert row["t"]
    assert row["t_unix"] > 1e9
    assert row["t_iso"].endswith("Z")


def test_parse_asof_ibkr_compact_is_new_york_wall():
    assert asof_fields("20260825 09:35:00")["asof_iso"] == "2026-08-25T13:35:00Z"
    assert asof_fields("20260825  09:35:00")["asof_iso"] == "2026-08-25T13:35:00Z"
    naive = datetime(2026, 8, 25, 9, 35)
    row = bar_time_fields(naive)
    assert row["t_iso"] == "2026-08-25T13:35:00Z"


def test_stamp_and_ibkr_block_are_live():
    q = stamp(
        {"symbol": "SPY", "last": 500.0, "bid": 499.9, "ask": 500.1},
        source="ibkr",
        freshness="live",
        use="ibkr_live_for_decisions",
        fallback_now=True,
    )
    assert q["source"] == "ibkr"
    assert q["asof"]
    live = ibkr_block(q)
    assert live["last"] == 500.0
    assert live["source"] == "ibkr"
    assert live["freshness"] == "live"
    assert live["spread"] == pytest.approx(0.2)
    assert live["spread_pct"] == pytest.approx(0.04)


def test_live_limit_px_rejects_mda():
    assert live_limit_px({"last": 10.0, "mid": 10.0, "source": "mda"}) is None
    assert live_limit_px({"mid": 1.25, "source": "ibkr"}) == 1.25


def test_mda_miss_cache_skips_repeat_404():
    assert mda_worth_asking("SPY") is True
    note_mda_miss("SNXX")
    assert mda_worth_asking("SNXX") is False


def test_join_mda_metrics_and_news_on_ibkr_hit():
    rows = [{"symbol": "NVDA", "last": 181.5, "quote_source": "ibkr_live"}]
    n = merge_mda_metrics(
        rows,
        [{"symbol": "NVDA", "mda_last": 180.0, "sma20": 175.0, "last": 999.0, "source": "mda"}],
    )
    assert n == 1
    assert rows[0]["last"] == 181.5
    assert "last" not in rows[0]["mda"]
    assert rows[0]["mda"]["mda_last"] == 180.0
    attach_mda_news(rows, [{"symbol": "NVDA", "headline": "Chip note", "source": "mda"}])
    assert rows[0]["mda"]["news"][0]["headline"] == "Chip note"
    assert rows[0]["mda"]["source"] == "mda"


def test_attach_mda_news_skips_timeout_misses():
    rows = [{"symbol": "HEI", "last": 240.0}]
    n = attach_mda_news(
        rows,
        [{"symbol": "HEI", "headline": "(unavailable - timed out)", "error": "timed out"}],
    )
    assert n == 0
    assert "mda" not in rows[0]


def test_quote_from_ticker_has_asof():
    from abcxauto.broker.quotes import quote_from_ticker

    ticker = SimpleNamespace(
        last=10.0,
        bid=9.9,
        ask=10.1,
        close=9.5,
        open_=9.6,
        impliedVolatility=None,
        modelGreeks=None,
        time=None,
        contract=SimpleNamespace(symbol="AMD"),
    )
    q = quote_from_ticker(ticker)
    assert q["source"] == "ibkr"
    assert q["freshness"] == "live"
    assert q["asof"]
    assert q["asof_iso"].endswith("Z")


def test_bars_from_ibkr_carry_t_unix():
    from abcxauto.broker.bars import bars_from_ibkr

    rows = bars_from_ibkr(
        [SimpleNamespace(date="2026-08-18", open=1, high=2, low=0.5, close=1.5, volume=9)]
    )
    assert rows[0]["c"] == 1.5
    assert rows[0]["t_unix"] > 1e9
    assert rows[0]["t_iso"].startswith("2026-08-18")


@pytest.mark.asyncio
async def test_scan_with_metrics_nests_mda_not_last(monkeypatch):
    from abcxauto.brain import BrainTurn, _run_tool
    from abcxauto.world_state import WorldState

    async def fake_scan(**_k):
        return {
            "ok": True,
            "source": "ibkr",
            "symbols": ["NVDA"],
            "hits": [
                {
                    "symbol": "NVDA",
                    "last": 181.5,
                    "quote_source": "ibkr_live",
                    "ibkr": {"last": 181.5, "source": "ibkr", "freshness": "live"},
                }
            ],
            "quoted": 1,
            "ranked": True,
            "rank_meaning": "IBKR",
            "applied": {},
        }

    async def fake_metrics(symbols, **_k):
        return [
            {
                "symbol": "NVDA",
                "mda_last": 180.0,
                "sma20": 175.0,
                "dist20": 0.02,
                "source": "mda",
                "freshness": "delayed_daily",
            }
        ]

    monkeypatch.setattr("abcxauto.brain.criteria_scan", fake_scan)
    monkeypatch.setattr("abcxauto.opportunity_scan.fetch_scan_metrics", fake_metrics)

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
    import json

    data = json.loads(
        await _run_tool(
            "scan",
            {"arena": "most_active", "with": ["metrics"]},
            connector=object(),
            world=world,
            snap={},
            turn=BrainTurn(),
        )
    )
    hit = data["hits"][0]
    assert hit["last"] == 181.5
    assert hit["ibkr"]["source"] == "ibkr"
    assert hit["mda"]["mda_last"] == 180.0
    assert "last" not in hit["mda"]
    assert "delayed" in hit["mda"]["freshness"]


@pytest.mark.asyncio
async def test_underlying_price_is_ibkr_only(monkeypatch):
    from abcxauto.broker.options import IBKROptionsMixin

    class Host(IBKROptionsMixin):
        async def get_live_quote(self, symbol, **_k):
            return {"error": "no tick", "source": "ibkr"}

    def boom():
        raise AssertionError("MDA must not price send geometry")

    monkeypatch.setattr("abcxauto.marketdata.provider.get_data_provider", boom)
    assert await Host()._get_underlying_price("SPY") is None
