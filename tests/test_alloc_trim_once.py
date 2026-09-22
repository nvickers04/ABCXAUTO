"""Alloc trim must not sell the same excess twice in one process."""

from __future__ import annotations

import pytest

from abcxauto import brain


@pytest.fixture(autouse=True)
def _clear_trim_sent():
    brain._ALLOC_TRIM_SENT.clear()
    yield
    brain._ALLOC_TRIM_SENT.clear()


def test_far_target_does_not_count_as_trim_cover() -> None:
    """AVGO target 369.85 of 89 is not the bid trim; must not block."""
    orders = [
        {
            "symbol": "AVGO",
            "action": "SELL",
            "type": "LMT",
            "lmt": 369.85,
            "quantity": 89,
        },
        {
            "symbol": "AVGO",
            "action": "SELL",
            "type": "STP",
            "quantity": 89,
        },
    ]
    assert brain._working_limit_sell_qty(orders, "AVGO", at_or_below=363.2) == 0
    assert brain._working_limit_sell_qty(orders, "AVGO", at_or_below=364.35) == 0


def test_bid_limit_sell_covers_excess() -> None:
    orders = [
        {
            "symbol": "AVGO",
            "action": "SELL",
            "type": "LMT",
            "lmt": 363.2,
            "quantity": 67,
        },
        {
            "symbol": "AVGO",
            "action": "SELL",
            "type": "LMT",
            "lmt": 369.85,
            "quantity": 89,
        },
    ]
    assert brain._working_limit_sell_qty(orders, "AVGO", at_or_below=363.2) == 67


@pytest.mark.asyncio
async def test_trim_records_sent_and_skips_stale_second_look(monkeypatch) -> None:
    """success+filled:false still records; next look with qty 89 must not re-sell."""
    positions = [
        {
            "symbol": "AVGO",
            "secType": "STK",
            "conId": "313130367",
            "quantity": 89,
        }
    ]
    orders = [
        {
            "symbol": "AVGO",
            "action": "SELL",
            "type": "LMT",
            "lmt": 369.85,
            "quantity": 89,
        },
        {
            "symbol": "AVGO",
            "action": "SELL",
            "type": "STP",
            "quantity": 89,
        },
    ]
    sized = {
        "AVGO": {
            "held": 89,
            "sized": 22,
            "excess": 67,
            "last": 363.2,
            "stop": None,
        }
    }
    allocation = {
        "names": {"AVGO": {"bid": 363.2, "bars": []}},
        "asof": "2026-09-22T18:17:00Z",
        "bar_date": "2026-09-22",
    }
    calls: list[dict] = []

    async def fake_alloc(*_a, **_k):
        return allocation

    async def fake_exec(act, *_a, **_k):
        calls.append(act)
        # Place returns success with filled:false (IBKR limit just placed / race).
        return {
            "success": True,
            "filled": False,
            "status": "Submitted",
            "order_id": 23242,
            "symbol": "AVGO",
        }

    class Conn:
        _executions: dict = {}

        async def get_positions(self):
            return list(positions)

    monkeypatch.setattr(
        "abcxauto.alloc_snapshot.build_allocation_snapshot", fake_alloc
    )
    monkeypatch.setattr("abcxauto.alloc_rank.score_name", lambda *_a, **_k: {})
    monkeypatch.setattr("abcxauto.alloc_rank.rank_board", lambda *_a, **_k: [])
    monkeypatch.setattr("abcxauto.alloc_rank.heat_groups", lambda *_a, **_k: {})
    monkeypatch.setattr("abcxauto.alloc_size.sized_book", lambda *_a, **_k: sized)
    monkeypatch.setattr("abcxauto.alloc_trim.trim_tickets", lambda *_a, **_k: [
        {
            "symbol": "AVGO",
            "action": "SELL",
            "quantity": 67,
            "limit_price": 363.2,
            "reason": "alloc_excess",
        }
    ])
    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", fake_exec)

    snap1: dict = {
        "positions": list(positions),
        "open_orders": list(orders),
        "account": {},
    }
    await brain.apply_pre_model_look_systems(
        connector=Conn(),
        world=object(),
        snap=snap1,
        day={},
    )
    assert len(calls) == 1
    assert calls[0]["target_conId"] == "313130367"
    assert calls[0]["params"]["target_conId"] == "313130367"
    assert snap1["alloc_trim_sent"]["AVGO"] == 67
    assert brain._ALLOC_TRIM_SENT["AVGO"] == 67

    # Next look: stale book still says 89; must not sell again.
    snap2: dict = {
        "positions": list(positions),
        "open_orders": list(orders),
        "account": {},
    }
    await brain.apply_pre_model_look_systems(
        connector=Conn(),
        world=object(),
        snap=snap2,
        day={},
    )
    assert len(calls) == 1
    assert snap2["alloc_trim_sent"]["AVGO"] == 67


@pytest.mark.asyncio
async def test_trim_records_when_execution_captured_despite_failed_flag(
    monkeypatch,
) -> None:
    """filled:false + success false, but SLD execution landed → still record."""
    positions = [
        {
            "symbol": "AVGO",
            "secType": "STK",
            "conId": "313130367",
            "quantity": 89,
        }
    ]
    sized = {
        "AVGO": {
            "held": 89,
            "sized": 22,
            "excess": 67,
            "last": 364.35,
            "stop": None,
        }
    }
    allocation = {
        "names": {"AVGO": {"bid": 364.35, "bars": []}},
        "asof": "2026-09-22T18:17:00Z",
        "bar_date": "2026-09-22",
    }
    calls: list[dict] = []

    class Conn:
        def __init__(self) -> None:
            self._executions: dict = {}

        async def get_positions(self):
            return list(positions)

    conn = Conn()

    async def fake_alloc(*_a, **_k):
        return allocation

    async def fake_exec(act, *_a, **_k):
        calls.append(act)
        conn._executions.setdefault("AVGO", []).append(
            {"side": "SLD", "shares": 67, "price": 364.35}
        )
        return {"success": False, "filled": False, "status": "error"}

    monkeypatch.setattr(
        "abcxauto.alloc_snapshot.build_allocation_snapshot", fake_alloc
    )
    monkeypatch.setattr("abcxauto.alloc_rank.score_name", lambda *_a, **_k: {})
    monkeypatch.setattr("abcxauto.alloc_rank.rank_board", lambda *_a, **_k: [])
    monkeypatch.setattr("abcxauto.alloc_rank.heat_groups", lambda *_a, **_k: {})
    monkeypatch.setattr("abcxauto.alloc_size.sized_book", lambda *_a, **_k: sized)
    monkeypatch.setattr("abcxauto.alloc_trim.trim_tickets", lambda *_a, **_k: [
        {
            "symbol": "AVGO",
            "action": "SELL",
            "quantity": 67,
            "limit_price": 364.35,
            "reason": "alloc_excess",
        }
    ])
    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", fake_exec)

    snap: dict = {
        "positions": list(positions),
        "open_orders": [],
        "account": {},
    }
    await brain.apply_pre_model_look_systems(
        connector=conn,
        world=object(),
        snap=snap,
        day={},
    )
    assert len(calls) == 1
    assert snap["alloc_trim_sent"]["AVGO"] == 67


@pytest.mark.asyncio
async def test_after_send_refresh_skips_when_held_at_sized(monkeypatch) -> None:
    """After a trim fill, refreshed qty at sized blocks a further ticket."""
    positions_start = [
        {
            "symbol": "AVGO",
            "secType": "STK",
            "conId": "1",
            "quantity": 89,
        },
        {
            "symbol": "MSFT",
            "secType": "STK",
            "conId": "2",
            "quantity": 15,
        },
    ]
    positions_after = [
        {
            "symbol": "AVGO",
            "secType": "STK",
            "conId": "1",
            "quantity": 22,
        },
        {
            "symbol": "MSFT",
            "secType": "STK",
            "conId": "2",
            "quantity": 10,
        },
    ]
    sized = {
        "AVGO": {"held": 89, "sized": 22, "excess": 67, "last": 360.0, "stop": None},
        "MSFT": {"held": 15, "sized": 10, "excess": 5, "last": 400.0, "stop": None},
    }
    tickets = [
        {
            "symbol": "AVGO",
            "action": "SELL",
            "quantity": 67,
            "limit_price": 360.0,
            "reason": "alloc_excess",
        },
        {
            "symbol": "MSFT",
            "action": "SELL",
            "quantity": 5,
            "limit_price": 400.0,
            "reason": "alloc_excess",
        },
    ]
    allocation = {
        "names": {
            "AVGO": {"bid": 360.0, "bars": []},
            "MSFT": {"bid": 400.0, "bars": []},
        },
        "asof": "2026-09-22T18:17:00Z",
        "bar_date": "2026-09-22",
    }
    calls: list[str] = []
    live = {"rows": list(positions_start)}

    class Conn:
        _executions: dict = {}

        async def get_positions(self):
            return list(live["rows"])

    async def fake_alloc(*_a, **_k):
        return allocation

    async def fake_exec(act, *_a, **_k):
        sym = str((act.get("params") or {}).get("symbol") or "")
        calls.append(sym)
        if sym == "AVGO":
            live["rows"] = list(positions_after)
        return {"success": True, "filled": False, "status": "Submitted"}

    monkeypatch.setattr(
        "abcxauto.alloc_snapshot.build_allocation_snapshot", fake_alloc
    )
    monkeypatch.setattr("abcxauto.alloc_rank.score_name", lambda *_a, **_k: {})
    monkeypatch.setattr("abcxauto.alloc_rank.rank_board", lambda *_a, **_k: [])
    monkeypatch.setattr("abcxauto.alloc_rank.heat_groups", lambda *_a, **_k: {})
    monkeypatch.setattr("abcxauto.alloc_size.sized_book", lambda *_a, **_k: sized)
    monkeypatch.setattr(
        "abcxauto.alloc_trim.trim_tickets", lambda *_a, **_k: list(tickets)
    )
    monkeypatch.setattr("abcxauto.agent_loop.execute_ticket", fake_exec)

    snap: dict = {
        "positions": list(positions_start),
        "open_orders": [],
        "account": {},
    }
    await brain.apply_pre_model_look_systems(
        connector=Conn(),
        world=object(),
        snap=snap,
        day={},
    )
    assert calls == ["AVGO"]
    assert snap["alloc_trim_sent"]["AVGO"] == 67
    assert "MSFT" not in snap["alloc_trim_sent"]
