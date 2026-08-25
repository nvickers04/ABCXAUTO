"""Account returns from IBKR journal NAV history."""

from datetime import datetime, timedelta, timezone

from abcxauto.account_returns import compute_account_returns
from abcxauto.memory.journal import TradeJournal

FAT_BOOK = [
    {
        "symbol": "NVDA",
        "sec_type": "STK",
        "quantity": 100,
        "unrealizedPNL": 50_000.0,
        "marketValue": 180_000.0,
        "avgCost": 1300.0,
        "mtm_pct": 38.0,
    },
    {
        "symbol": "IWM",
        "sec_type": "OPT",
        "quantity": 12,
        "unrealizedPNL": -8_400.0,
        "marketValue": 6_000.0,
        "mtm_pct": -58.0,
    },
]


def _nav_journal(tmp_path, *, positions, name: str = "nav.db") -> TradeJournal:
    db = tmp_path / name
    j = TradeJournal(path=str(db), enabled=True)
    now = datetime.now(timezone.utc)
    j.record_snapshot(
        account={"NetLiquidation": 90_000.0, "DailyPnL": 1.0},
        positions=positions,
        open_orders=[],
        ts=(now - timedelta(days=10)).isoformat(),
    )
    j.record_snapshot(
        account={"NetLiquidation": 100_000.0, "DailyPnL": 5.0},
        positions=positions,
        open_orders=[],
        ts=now.isoformat(),
    )
    return j


def test_compute_overlays_live_equity(tmp_path):
    j = _nav_journal(tmp_path, positions=[])
    perf = compute_account_returns(equity=101_500.0, daily_pnl=12.0, journal=j)
    assert perf["source"] == "ibkr_nav"
    assert perf["net_liquidation"] == 101_500.0
    assert perf["daily_pnl"] == 12.0
    assert abs(perf["ret_1w"] - (100_000 / 90_000 - 1)) < 1e-9
    assert perf["ret_3m"] is None
    assert perf["ret_1y"] is None
    assert perf["history_start"] is not None
    assert perf["history_days"] is not None
    assert perf["history_days"] >= 9


def test_compute_empty_journal(tmp_path):
    db = tmp_path / "empty.db"
    j = TradeJournal(path=str(db), enabled=True)
    perf = compute_account_returns(equity=50_000.0, daily_pnl=0.0, journal=j)
    assert perf["source"] == "none"
    assert perf["net_liquidation"] == 50_000.0
    assert perf["daily_pnl"] == 0.0
    assert perf["ret_1w"] is None
    assert perf["ret_3m"] is None
    assert perf["ret_1y"] is None


def test_empty_book_matches_nav_only(tmp_path):
    """Empty book and omitted positions both use journal NAV, not lot MTM."""
    j = _nav_journal(tmp_path, positions=[])
    kwargs = {"equity": 101_500.0, "daily_pnl": 12.0, "journal": j}
    nav_only = compute_account_returns(**kwargs)
    empty_book = compute_account_returns(positions=[], **kwargs)
    assert empty_book == nav_only
    assert nav_only["source"] == "ibkr_nav"
    assert abs(nav_only["ret_1w"] - (100_000 / 90_000 - 1)) < 1e-9
    assert nav_only["ret_3m"] is None
    assert nav_only["ret_1y"] is None
    assert nav_only["net_liquidation"] == 101_500.0


def test_open_lot_mtm_does_not_change_horizons(tmp_path):
    """Same NAV history: fat open lots (snapshot or arg) must not move returns."""
    empty_j = _nav_journal(tmp_path, positions=[], name="empty-book.db")
    fat_j = _nav_journal(tmp_path, positions=FAT_BOOK, name="fat-book.db")
    kwargs = {"equity": 101_500.0, "daily_pnl": 12.0}
    from_empty = compute_account_returns(**kwargs, journal=empty_j)
    from_fat_snap = compute_account_returns(**kwargs, journal=fat_j)
    from_fat_arg = compute_account_returns(
        **kwargs, positions=FAT_BOOK, journal=empty_j
    )
    for key in (
        "ret_1w",
        "ret_3m",
        "ret_1y",
        "source",
        "net_liquidation",
        "daily_pnl",
    ):
        assert from_empty[key] == from_fat_snap[key] == from_fat_arg[key]
    assert from_empty["net_liquidation"] == 101_500.0
    assert from_empty["source"] == "ibkr_nav"
    assert abs(from_empty["ret_1w"] - (100_000 / 90_000 - 1)) < 1e-9
    assert from_empty["ret_1w"] != (101_500 / 90_000 - 1)


def test_positions_without_nav_cannot_invent_horizons():
    class DeadJournal:
        pass

    perf = compute_account_returns(positions=FAT_BOOK, journal=DeadJournal())
    assert perf["source"] == "none"
    assert perf["net_liquidation"] is None
    assert perf["daily_pnl"] is None
    assert perf["ret_1w"] is None
    assert perf["ret_3m"] is None
    assert perf["ret_1y"] is None


def test_book_mtm_journal_blob_is_not_an_account_return():
    class BookMtmJournal:
        def account_performance(self):
            return {
                "source": "book_mtm",
                "net_liquidation": 186_000.0,
                "daily_pnl": 41_600.0,
                "ret_1w": 0.42,
                "ret_3m": 1.1,
                "ret_1y": 2.2,
                "as_of": "2026-08-25",
                "history_start": "2025-08-25",
                "history_days": 365,
                "open_upnl": 41_600.0,
                "mtm_pct": 22.0,
            }

    perf = compute_account_returns(
        equity=100_000.0, daily_pnl=5.0, positions=FAT_BOOK, journal=BookMtmJournal()
    )
    assert perf["source"] == "none"
    assert perf["net_liquidation"] == 100_000.0
    assert perf["daily_pnl"] == 5.0
    assert perf["ret_1w"] is None
    assert perf["ret_3m"] is None
    assert perf["ret_1y"] is None
    assert perf["as_of"] is None
    assert perf["history_start"] is None
    assert perf["history_days"] is None
    assert "open_upnl" not in perf
    assert "mtm_pct" not in perf
    assert "book_mtm" not in perf


def test_sourceless_horizon_is_not_trusted():
    class SourcelessJournal:
        def account_performance(self):
            return {"ret_1w": 0.42, "net_liquidation": 186_000.0}

    perf = compute_account_returns(journal=SourcelessJournal())
    assert perf["source"] == "none"
    assert perf["ret_1w"] is None
    assert perf["net_liquidation"] is None


def test_nav_blob_does_not_leak_mtm_keys():
    class NavPlusMtmJournal:
        def account_performance(self):
            return {
                "source": "ibkr_nav",
                "net_liquidation": 100_000.0,
                "daily_pnl": 5.0,
                "ret_1w": 0.05,
                "ret_3m": None,
                "ret_1y": None,
                "open_upnl": 999.0,
                "book_mtm": 12_000.0,
                "mtm_pct": 9.9,
            }

    perf = compute_account_returns(journal=NavPlusMtmJournal())
    assert perf["source"] == "ibkr_nav"
    assert perf["ret_1w"] == 0.05
    assert perf["net_liquidation"] == 100_000.0
    assert "open_upnl" not in perf
    assert "book_mtm" not in perf
    assert "mtm_pct" not in perf
