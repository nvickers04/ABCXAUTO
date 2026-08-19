"""Smoke + contract tests for connections / send façades."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_import_smoke():
    from abcxauto.connections import (
        connect,
        connection_status,
        get_connector,
        get_quote,
        session_info,
        snapshot_account,
        snapshot_open_orders,
        snapshot_positions,
    )
    from abcxauto.send import send_action

    assert callable(get_connector)
    assert callable(connect)
    assert callable(snapshot_account)
    assert callable(snapshot_positions)
    assert callable(snapshot_open_orders)
    assert callable(get_quote)
    assert callable(session_info)
    assert callable(connection_status)
    assert callable(send_action)


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
