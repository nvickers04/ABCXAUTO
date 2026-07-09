"""Brutal order suite — place/validate/cancel or dry-run for all types."""

import asyncio

from abcxauto.brutal_suite import format_brutal_summary, run_brutal_suite
from abcxauto.proposals import STRATEGIES


class _PaperConn:
    connected = True

    async def place_limit_order(self, **kw):
        return {"success": True, "order_id": 101, "status": "Submitted", "kwargs": kw}

    async def place_market_order(self, **kw):
        return {"success": True, "order_id": 102, "status": "Submitted"}

    async def place_stop_order(self, **kw):
        return {"success": True, "order_id": 103}

    async def place_stop_limit(self, **kw):
        return {"success": True, "order_id": 104}

    async def place_bracket_order(self, **kw):
        return {"success": True, "order_id": 105}

    async def place_market_bracket(self, **kw):
        return {"success": True, "order_id": 106}

    async def place_oca(self, **kw):
        return {"success": True, "order_id": 107}

    async def place_trailing_stop(self, **kw):
        return {"success": True, "order_id": 108}

    async def place_trailing_stop_limit(self, **kw):
        return {"success": True, "order_id": 109}

    async def cancel_order(self, order_id):
        return {"success": True, "order_id": order_id}

    async def modify_stop_price(self, **kw):
        return {"success": True}

    async def modify_target_price(self, **kw):
        return {"success": True}

    async def modify_order(self, **kw):
        return {"success": True}

    async def close_option_position(self, **kw):
        return {"success": True, "order_id": 110}

    async def place_vertical_spread(self, **kw):
        return {"success": True, "order_id": 111}

    async def place_iron_condor(self, **kw):
        return {"success": True, "order_id": 112}

    async def place_straddle(self, **kw):
        return {"success": True, "order_id": 113}

    async def place_strangle(self, **kw):
        return {"success": True, "order_id": 114}

    async def place_iron_butterfly(self, **kw):
        return {"success": True, "order_id": 115}

    async def place_butterfly(self, **kw):
        return {"success": True, "order_id": 116}

    async def place_calendar_spread(self, **kw):
        return {"success": True, "order_id": 117}

    async def place_diagonal_spread(self, **kw):
        return {"success": True, "order_id": 118}

    async def place_covered_call(self, **kw):
        return {"success": True, "order_id": 119}

    async def place_protective_put(self, **kw):
        return {"success": True, "order_id": 120}

    async def place_collar(self, **kw):
        return {"success": True, "order_id": 121}

    async def place_ratio_spread(self, **kw):
        return {"success": True, "order_id": 122}

    async def place_jade_lizard(self, **kw):
        return {"success": True, "order_id": 123}


def test_brutal_suite_dry_run_never_idle():
    report = asyncio.run(run_brutal_suite(source="test", force_dry=True))
    assert report["idle_prevented"] is True
    assert report["paper_only"] is True
    assert report["strategies_tested"] >= len(STRATEGIES) * 0.9
    assert report["pass_rate"] >= 0.85
    names = {r["strategy"] for r in report["results"]}
    for core in (
        "market_order",
        "limit_order",
        "stop_order",
        "stop_limit",
        "bracket",
        "oca",
        "trailing_stop",
    ):
        assert core in names
    assert any(r.get("strategy") == "panic_flatten_leg" for r in report["results"])
    assert "brutal suite" in format_brutal_summary(report)


def test_brutal_suite_paper_place_cancel_mock():
    conn = _PaperConn()
    report = asyncio.run(
        run_brutal_suite(connector=conn, source="paper_mock", force_dry=False)
    )
    assert report["mode"] == "paper"
    placed = [r for r in report["results"] if r.get("placed") and r.get("mode") == "paper"]
    assert len(placed) >= 5
    # Cancelled or cancel_intent for placed rows
    for r in placed:
        assert r.get("cancel_intent") is True
        assert r.get("pass") is True


def test_startup_source_marker():
    report = asyncio.run(run_brutal_suite(source="startup", force_dry=True))
    assert report["source"] == "startup"
    assert report["idle_prevented"] is True
