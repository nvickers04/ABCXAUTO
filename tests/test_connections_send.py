"""Smoke + contract tests for connections / send façades."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def test_connection_status_keys():
    from abcxauto.connections import connection_status

    connector = MagicMock()
    connector.connected = False
    status = connection_status(connector)
    assert set(status.keys()) >= {
        "ibkr_connected",
        "ibkr_host",
        "ibkr_port",
        "ibkr_client_id",
        "mda_configured",
        "xai_configured",
        "trading_mode",
    }
    assert status["ibkr_connected"] is False
    assert isinstance(status["mda_configured"], bool)
    assert isinstance(status["xai_configured"], bool)
    assert isinstance(status["trading_mode"], str)


@pytest.mark.asyncio
async def test_send_action_hold_no_dispatch():
    from abcxauto.send import send_action

    connector = MagicMock()
    connector.connected = True
    # If hold leaked to the executor, it must not touch the broker.
    connector.place_order = MagicMock()
    connector.get_positions = MagicMock()

    result = await send_action({"strategy": "hold", "params": {}}, connector)

    assert result["status"] in ("held", "blocked")
    connector.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_send_action_noop_held():
    from abcxauto.send import send_action

    connector = MagicMock()
    connector.connected = True
    result = await send_action({"action": "noop"}, connector)
    assert result["status"] == "held"


def _tape_seed_boom(*_a, **_k):
    raise AssertionError("snapshot_positions must not seed tape / universe names")


@pytest.mark.asyncio
async def test_snapshot_positions_returns_broker_rows_only(monkeypatch):
    from abcxauto.connections import snapshot_positions

    broker = [
        {"symbol": "AAPL", "quantity": 5, "conId": 1, "secType": "STK"},
        {"symbol": "XLE", "quantity": -1, "conId": 2, "secType": "OPT"},
    ]
    connector = MagicMock()
    connector.get_positions = AsyncMock(return_value=list(broker))
    monkeypatch.setattr("abcxauto.opportunity_scan.tape_seed_symbols", _tape_seed_boom)
    monkeypatch.setattr("abcxauto.universe.legal_symbols", _tape_seed_boom)

    rows = await snapshot_positions(connector)
    assert rows == broker
    connector.get_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_positions_empty_broker_stays_empty(monkeypatch):
    from abcxauto.connections import snapshot_positions

    connector = MagicMock()
    connector.get_positions = AsyncMock(return_value=[])
    monkeypatch.setattr("abcxauto.opportunity_scan.tape_seed_symbols", _tape_seed_boom)
    monkeypatch.setattr("abcxauto.universe.legal_symbols", _tape_seed_boom)

    rows = await snapshot_positions(connector)
    assert rows == []
    symbols = [r.get("symbol") for r in rows]
    for name in ("SPY", "QQQ", "IWM", "DIA"):
        assert name not in symbols


@pytest.mark.asyncio
async def test_snapshot_positions_non_list_is_empty():
    from abcxauto.connections import snapshot_positions

    connector = MagicMock()
    connector.get_positions = AsyncMock(return_value={"error": "no book"})
    assert await snapshot_positions(connector) == []

    connector.get_positions = AsyncMock(return_value=None)
    assert await snapshot_positions(connector) == []

    connector.get_positions = None
    assert await snapshot_positions(connector) == []
