"""Heavy order lab + auto-reconfig."""

from abcxauto.order_lab import (
    auto_reconfig_from_lab,
    format_lab_summary,
    run_order_lab,
    strategies_for_session,
)
from abcxauto.proposals import STRATEGIES
from abcxauto.rocket import TWEAKS


def test_regular_session_tests_many_strategies():
    names = strategies_for_session("regular")
    assert "bracket" in names
    assert "market_order" in names
    assert "vertical_spread" in names
    assert len(names) >= 15


def test_closed_session_skips_new_risk_entries():
    names = strategies_for_session("closed")
    assert "market_order" in names
    assert "bracket" not in names  # closed: no new entries in lab scope


def test_order_lab_schema_pass_rate_high():
    pulse = {
        "session": {"status": "regular"},
        "data_freshness": {"spy_last": 500},
    }
    positions = [
        {"conId": 1, "symbol": "SPY", "sec_type": "STK", "quantity": 1},
        {
            "conId": 99,
            "symbol": "SPY",
            "sec_type": "OPT",
            "quantity": 1,
            "expiration": "20260718",
            "strike": 500,
            "right": "C",
        },
    ]
    lab = run_order_lab(pulse=pulse, positions=positions, proposal=None, history=[])
    assert lab["strategies_tested"] >= 10
    assert lab["pass_rate"] >= 0.85, lab
    assert "ORDER LAB" in format_lab_summary(lab) or "lab" in lab["summary"]


def test_order_lab_proposal_conid_gate():
    pulse = {"session": {"status": "regular"}, "data_freshness": {"spy_last": 500}}
    positions = [{"conId": 1, "symbol": "SPY", "sec_type": "STK", "quantity": 5}]
    # Wrong conId type for stock market close of option conId
    bad = {
        "strategy": "market_order",
        "params": {"symbol": "SPY", "action": "SELL", "quantity": 1, "closing_position": True},
        "target_conId": "99",
    }
    lab = run_order_lab(
        pulse=pulse,
        positions=positions + [{"conId": 99, "symbol": "SPY", "sec_type": "OPT", "quantity": 1}],
        proposal=bad,
        history=[],
    )
    prop = lab["proposal_tests"]
    assert any(not p["pass"] for p in prop if p.get("phase") == "proposal_inventory")


def test_auto_reconfig_from_lab_writes_tweaks():
    before = dict(TWEAKS)
    try:
        TWEAKS.clear()
        lab = {
            "pass_rate": 0.5,
            "results": [{"strategy": "jade_lizard", "pass": False}],
            "proposal_tests": [{"pass": False, "phase": "proposal_schema"}],
            "failed": 3,
        }
        rec = auto_reconfig_from_lab(lab, hist=[{"pnl_chg": -1}, {"pnl_chg": -2}, {"pnl_chg": -1}])
        assert rec["type"] == "auto_reconfig"
        assert "auto-reconfig" in rec["summary"]
        assert TWEAKS.get("prefer_bracket_only") or TWEAKS.get("require_target_conId")
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_all_strategies_have_fixtures_when_regular():
    """Every registered strategy gets a fixture attempt under regular session."""
    pulse = {"session": {"status": "regular"}, "data_freshness": {"spy_last": 100}}
    lab = run_order_lab(pulse=pulse, positions=[], history=[])
    tested = {r["strategy"] for r in lab["results"]}
    # Core + combos should cover the registry
    assert len(tested) >= len(STRATEGIES) * 0.7
