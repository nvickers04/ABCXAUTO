"""Order suite — schema dry-run + gated paper place→cancel."""

import asyncio

from abcxauto.order_suite import (
    SUITE_STRATEGIES,
    clear_cached_suite,
    format_order_suite_summary,
    get_cached_suite,
    paper_place_enabled,
    run_order_suite,
)
from abcxauto.strategy_params import OPTION_STRATEGIES


class _PaperConn:
    connected = True

    def __init__(self) -> None:
        self.place_calls: list[str] = []
        self.cancel_calls: list[int] = []
        self._oid = 100

    def __getattr__(self, name: str):
        if not name.startswith(("place_", "buy_", "sell_", "close_", "roll_")):
            raise AttributeError(name)

        async def _method(**kwargs):
            self.place_calls.append(name)
            self._oid += 1
            return {"success": True, "order_id": self._oid, "method": name}

        return _method

    async def cancel_order(self, order_id):
        self.cancel_calls.append(int(order_id))
        return {"success": True, "order_id": order_id}


def test_order_suite_dry_run_never_idle(tmp_path, monkeypatch):
    clear_cached_suite()
    monkeypatch.chdir(tmp_path)
    report = asyncio.run(run_order_suite(source="test", force_dry=True))
    assert report["idle_prevented"] is True
    assert report["paper_only"] is True
    assert report["mode"] == "dry_run"
    assert report["strategies_tested"] >= len(SUITE_STRATEGIES)
    assert report["pass_rate"] >= 0.85
    names = {r["strategy"] for r in report["results"]}
    for core in (
        "market_order",
        "limit_order",
        "stop_order",
        "bracket",
        "oca",
        "close_option",
        "covered_call",
        "roll_option",
    ):
        assert core in names
    assert any(r.get("strategy") == "panic_flatten_leg" for r in report["results"])
    assert "order suite" in format_order_suite_summary(report)
    cached = get_cached_suite()
    assert cached.get("pass_rate") == report["pass_rate"]
    assert cached.get("source") == "test"


def test_startup_suite_default_zero_place_calls(monkeypatch):
    clear_cached_suite()
    monkeypatch.delenv("ABCXAUTO_SUITE_PAPER_PLACE", raising=False)
    from abcxauto.config import get_config

    get_config.cache_clear()
    conn = _PaperConn()
    report = asyncio.run(run_order_suite(connector=conn, source="startup", force_dry=True))
    assert report["mode"] == "dry_run"
    assert conn.place_calls == []


def test_paper_place_cancels_orders(monkeypatch):
    """force_dry=False + connected connector → place→cancel for placeable strategies."""
    monkeypatch.setenv("ABCXAUTO_SUITE_PAPER_PLACE", "true")
    from abcxauto.config import get_config

    get_config.cache_clear()
    assert paper_place_enabled() is True
    conn = _PaperConn()
    report = asyncio.run(
        run_order_suite(connector=conn, source="paper_mock", force_dry=False)
    )
    assert report["mode"] == "paper"
    assert conn.place_calls, "expected paper place calls"
    assert conn.cancel_calls, "expected cancel after place"
    assert len(conn.cancel_calls) == len(conn.place_calls)
    option_rows = [
        r for r in report["results"]
        if r.get("strategy") in OPTION_STRATEGIES and r.get("phase") == "paper_place_cancel"
    ]
    assert option_rows, "option strategies should paper-place"
    assert all(r.get("placed") for r in option_rows if r.get("pass"))


def test_paper_flag_off_still_dry_when_force_dry(monkeypatch):
    monkeypatch.setenv("ABCXAUTO_SUITE_PAPER_PLACE", "false")
    from abcxauto.config import get_config

    get_config.cache_clear()
    assert paper_place_enabled() is False
    conn = _PaperConn()
    report = asyncio.run(
        run_order_suite(connector=conn, source="manual", force_dry=True)
    )
    assert report["mode"] == "dry_run"
    assert conn.place_calls == []


def test_paper_place_disabled_in_live_mode():
    from abcxauto.config import clear_runtime_overrides, set_trading_mode

    clear_runtime_overrides()
    set_trading_mode("live", live_confirm="I_UNDERSTAND_LIVE_TRADING_RISK")
    assert paper_place_enabled() is False
    clear_runtime_overrides()
    assert paper_place_enabled() is True


def test_startup_source_marker():
    report = asyncio.run(run_order_suite(source="startup", force_dry=True))
    assert report["source"] == "startup"
    assert report["idle_prevented"] is True


def test_closed_session_still_schema_validates():
    pulse = {
        "session": {"status": "closed"},
        "data_freshness": {"spy_last": 500},
        "narrative": "closed",
        "position_ledger": [],
    }
    report = asyncio.run(
        run_order_suite(pulse=pulse, source="closed_test", force_dry=True)
    )
    for r in report["results"]:
        if r.get("strategy") == "panic_flatten_leg":
            continue
        assert r.get("schema_validated") is True or r.get("phase") == "schema"
        if r.get("mode") == "dry_run" and r.get("pass"):
            assert r.get("schema_validated") is True
            assert r.get("gateway") or r.get("schema_detail") is not None
