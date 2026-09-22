"""Allocation snapshot: forming bars, date alignment, scan last ignored."""

from __future__ import annotations

import pytest

from abcxauto.alloc_snapshot import build_allocation_snapshot, drop_forming_bars


def test_drop_forming_bars_drops_today_without_connector():
    bars = [
        {"date": "2026-09-18", "close": 10.0},
        {"t": "2026-09-19", "c": 11.0},
        {"date": "2026-09-22", "close": 12.0},
        {"t": "20260922", "c": 12.5},
    ]
    kept = drop_forming_bars(bars, "2026-09-22")
    dates = [
        (b.get("date") or b.get("t") or "")[:10]
        if "-" in str(b.get("date") or b.get("t") or "")
        else str(b.get("date") or b.get("t") or "")
        for b in kept
    ]
    assert "2026-09-22" not in dates
    assert "20260922" not in dates
    assert len(kept) == 2
    assert kept[0]["close"] == 10.0
    assert kept[1]["c"] == 11.0


@pytest.mark.asyncio
async def test_odd_last_completed_date_is_not_aligned():
    class Conn:
        async def get_live_quote(self, symbol, *, fresh=False):
            return {
                "symbol": symbol,
                "last": 100.0,
                "bid": 99.5,
                "asof_iso": "2026-09-22T14:30:00Z",
            }

        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            if symbol == "SPY":
                return {
                    "bars": [
                        {"date": "2026-09-17", "close": 500.0},
                        {"date": "2026-09-18", "close": 501.0},
                        {"date": "2026-09-19", "close": 502.0},
                        {"date": "2026-09-22", "close": 503.0},
                    ]
                }
            if symbol == "HALT":
                return {
                    "bars": [
                        {"date": "2026-09-16", "close": 40.0},
                        {"date": "2026-09-17", "close": 41.0},
                        {"date": "2026-09-18", "close": 42.0},
                    ]
                }
            return {
                "bars": [
                    {"date": "2026-09-17", "close": 90.0},
                    {"date": "2026-09-18", "close": 91.0},
                    {"date": "2026-09-19", "close": 92.0},
                ]
            }

    snap = await build_allocation_snapshot(
        Conn(),
        positions=[{"symbol": "HALT", "quantity": 10}],
        orders=[],
        account={"NetLiquidation": 100_000.0, "TotalCashValue": 20_000.0},
        scan_symbols=["AVGO"],
        today="2026-09-22",
    )
    assert snap["ok"] is True
    assert snap["bar_date"] == "2026-09-19"
    assert snap["names"]["SPY"]["aligned"] is True
    assert snap["names"]["AVGO"]["aligned"] is True
    assert snap["names"]["HALT"]["aligned"] is False
    assert snap["names"]["HALT"]["bars"][-1]["date"] == "2026-09-18"
    # Missing date must not inherit SPY's close.
    halt_dates = {b["date"] for b in snap["names"]["HALT"]["bars"]}
    assert "2026-09-19" not in halt_dates


@pytest.mark.asyncio
async def test_scan_last_on_the_side_is_ignored():
    class Conn:
        async def get_live_quote(self, symbol, *, fresh=False):
            return {
                "symbol": symbol,
                "last": 180.0,
                "bid": 179.5,
                "asof_iso": "2026-09-22T15:00:00Z",
            }

        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "bars": [
                    {"date": "2026-09-18", "close": 170.0},
                    {"date": "2026-09-19", "close": 175.0},
                    {"date": "2026-09-22", "close": 999.0},
                ]
            }

    poisonous = {
        "symbol": "AVGO",
        "last": 1.0,
        "open": 2.0,
        "gap": 50.0,
        "bid": 0.5,
    }
    snap = await build_allocation_snapshot(
        Conn(),
        positions=[{"symbol": "AVGO", "qty": 89}],
        orders=[
            {
                "symbol": "AVGO",
                "orderType": "STP",
                "auxPrice": 176.0,
                "status": "Submitted",
            }
        ],
        account={
            "NetLiquidation": 50_000.0,
            "TotalCashValue": 5_000.0,
            "AvailableFunds": 4_000.0,
        },
        scan_symbols=[poisonous, "NVDA"],
        today="2026-09-22",
    )
    assert snap["ok"] is True
    assert snap["names"]["AVGO"]["last"] == 180.0
    assert snap["names"]["AVGO"]["bid"] == 179.5
    assert snap["names"]["AVGO"]["stop"] == 176.0
    assert snap["names"]["AVGO"]["qty"] == 89
    assert snap["names"]["AVGO"]["bars"][-1]["close"] != 999.0
    assert snap["names"]["NVDA"]["last"] == 180.0
    assert snap["names"]["NVDA"]["qty"] == 0
    # Scan row poison never becomes the snapshot last.
    assert snap["names"]["AVGO"]["last"] != poisonous["last"]


@pytest.mark.asyncio
async def test_missing_nl_marks_ok_false_and_empty_asof():
    class Conn:
        async def get_live_quote(self, symbol, *, fresh=False):
            return {
                "symbol": symbol,
                "last": 10.0,
                "bid": 9.9,
                "asof_iso": "2026-09-22T12:00:00Z",
            }

        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {"bars": [{"date": "2026-09-19", "close": 10.0}]}

    snap = await build_allocation_snapshot(
        Conn(),
        positions=[],
        orders=[],
        account={},
        scan_symbols=[],
        today="2026-09-22",
    )
    assert snap["ok"] is False
    assert snap["asof"] == ""
    assert snap["nl"] is None
    assert "SPY" in snap["names"]
