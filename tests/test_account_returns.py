"""Account returns from IBKR journal NAV history."""

from datetime import datetime, timedelta, timezone

from abcxauto.account_returns import compute_account_returns
from abcxauto.memory.journal import TradeJournal


def test_compute_overlays_live_equity(tmp_path):
    db = tmp_path / "nav.db"
    j = TradeJournal(path=str(db), enabled=True)
    now = datetime.now(timezone.utc)
    j.record_snapshot(
        account={"NetLiquidation": 90_000.0, "DailyPnL": 1.0},
        positions=[],
        open_orders=[],
        ts=(now - timedelta(days=10)).isoformat(),
    )
    j.record_snapshot(
        account={"NetLiquidation": 100_000.0, "DailyPnL": 5.0},
        positions=[],
        open_orders=[],
        ts=now.isoformat(),
    )
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
