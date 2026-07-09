"""Decision space catalog + cancel_order_id + paper exercise (mock)."""

import asyncio

from abcxauto.decision_space import (
    cancel_order_id,
    list_decision_space,
    paper_exercise_order_types,
)


def test_list_decision_space_has_catalog_and_strategies():
    space = list_decision_space()
    assert "decisions" in space and len(space["decisions"]) >= 10
    ids = {d["id"] for d in space["decisions"]}
    for need in (
        "market_order",
        "limit_order",
        "stop_order",
        "bracket",
        "oca",
        "cancel_order",
        "vertical_spread",
    ):
        assert need in ids
    assert "market_order" in space["agent_strategies_registered"]
    assert space["individual_cancel"]["tool"] == "cancel_order_id"
    assert any(e["api_value"] == "MKT" for e in space["ibkr_order_type_enum"])


def test_cancel_order_id_mock():
    class C:
        connected = True

        async def cancel_order(self, oid):
            return {"success": True, "order_id": oid}

    out = asyncio.run(cancel_order_id(C(), 42))
    assert out["ok"] is True
    assert out["order_id"] == 42
    assert out["mode"] == "individual_cancel"


def test_paper_exercise_mock_place_and_individual_cancel():
    cancelled: list[int] = []

    class C:
        connected = True

        async def connect(self):
            return True

        async def place_market_order(self, *a, **k):
            return {"success": True, "order_id": 1, "filled": True}

        async def place_limit_order(self, *a, **k):
            return {"success": True, "order_id": 2, "filled": True}

        async def place_stop_order(self, *a, **k):
            return {"success": True, "order_id": 3}

        async def place_stop_limit(self, *a, **k):
            return {"success": True, "order_id": 4}

        async def place_trailing_stop(self, *a, **k):
            return {"success": True, "order_id": 5}

        async def place_oca(self, *a, **k):
            return {
                "success": True,
                "stop_order_id": 6,
                "target_order_id": 7,
            }

        async def place_bracket_order(self, *a, **k):
            return {"success": True, "order_id": 8}

        async def place_market_bracket(self, *a, **k):
            return {"success": True, "order_id": 9, "stop_order_id": 10, "target_order_id": 11}

        async def get_open_orders(self):
            return [
                {"order_id": 3, "symbol": "SPY"},
                {"order_id": 4, "symbol": "SPY"},
                {"order_id": 5, "symbol": "SPY"},
                {"order_id": 6, "symbol": "SPY"},
                {"order_id": 7, "symbol": "SPY"},
            ]

        async def cancel_order(self, oid):
            cancelled.append(int(oid))
            return {"success": True, "order_id": oid}

        async def get_positions(self):
            return [{"symbol": "SPY", "sec_type": "STK", "quantity": 2}]

        async def get_quote(self, symbol):
            return {"last": 100.0}

    report = asyncio.run(paper_exercise_order_types(C(), symbol="SPY", quantity=1))
    assert report["paper_only"] is True
    assert len(report["placed"]) >= 5
    assert report["cancels_ok"] >= 3
    # Individual cancels — each open order cancelled by id
    assert 3 in cancelled and 4 in cancelled and 5 in cancelled
    assert report["flatten"] is not None
