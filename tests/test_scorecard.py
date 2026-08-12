"""Book return vs model cost scorecard."""

from abcxauto.memory import get_journal
from abcxauto.scorecard import compute_scorecard, estimate_cost_usd, estimate_tokens


def test_estimate_tokens_and_cost():
    n = estimate_tokens("abcd" * 25)
    assert n >= 1
    cost = estimate_cost_usd(1_000_000, 1_000_000, in_rate=3.0, out_rate=15.0)
    assert abs(cost - 18.0) < 1e-9


def test_scorecard_beating_when_book_ahead():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 1100.0, "DailyPnL": 10.0})
    j.record_model_usage(stage="judge", input_tokens=100, output_tokens=50, cost_usd=0.01)
    sc = compute_scorecard(equity=1100.0, journal=j)
    assert sc["startup_cash"] == 1000.0
    assert sc["book_pnl"] == 100.0
    assert sc["model_cost_usd"] == 0.01
    assert sc["beating_model"] is True
    assert abs(sc["edge_usd"] - 99.99) < 1e-9


def test_scorecard_losing_to_model_bill():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 999.0, "DailyPnL": -1.0})
    j.record_model_usage(stage="act", input_tokens=10, output_tokens=10, cost_usd=5.0)
    sc = compute_scorecard(equity=999.0, journal=j)
    assert sc["beating_model"] is False
    assert sc["edge_usd"] < 0


def test_scorecard_return_uses_trading_budget_not_fat_nl():
    j = get_journal()
    j.record_snapshot(account={"NetLiquidation": 1_000_000.0, "DailyPnL": 0.0})
    j.record_snapshot(account={"NetLiquidation": 1_000_100.0, "DailyPnL": 100.0})
    sc = compute_scorecard(equity=1_000_100.0, journal=j, trading_budget=1000.0)
    assert sc["book_pnl"] == 100.0
    assert abs(sc["book_return_pct"] - 10.0) < 1e-9
    assert sc["trading_budget_usd"] == 1000.0


def test_scorecard_no_history_does_not_invent_pnl():
    j = get_journal()
    sc = compute_scorecard(equity=1_000_000.0, journal=j, trading_budget=1000.0)
    assert sc["book_pnl"] is None
    assert sc["startup_cash"] is None
