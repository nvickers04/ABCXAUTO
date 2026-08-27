"""send.py is the broker path — paper must not place on a live IBKR port."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from abcxauto.config import Config, clear_runtime_overrides, get_config


def setup_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def teardown_function():
    clear_runtime_overrides()
    get_config.cache_clear()


def _cfg(**kwargs) -> Config:
    base = get_config()
    return Config(**{**base.__dict__, **kwargs})


def _placeable_ticket() -> dict:
    return {
        "strategy": "market_bracket",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "side": "BUY",
            "entry_price": 500.0,
            "stop_price": 495.0,
            "target_price": 510.0,
        },
        "rationale": "regression",
    }


def _connector() -> MagicMock:
    connector = MagicMock()
    connector.connected = True
    connector.place_order = MagicMock()
    return connector


async def _safe_execute_must_not_run(*_a, **_k):
    raise AssertionError("safe_execute must not run on paper + live IBKR port")


async def _assert_paper_live_port_does_not_place(monkeypatch, port: int) -> None:
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", str(port))
    cfg = _cfg(trading_mode="paper", ibkr_port=port)
    monkeypatch.setattr("abcxauto.send.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)

    from abcxauto.send import send_action

    connector = _connector()
    result = await send_action(_placeable_ticket(), connector)

    assert result["status"] == "blocked"
    assert result.get("reason_code") == "live_port_paper"
    assert str(port) in str(result.get("note") or "")
    connector.place_order.assert_not_called()
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == port


@pytest.mark.asyncio
async def test_port_7496_trading_mode_paper_must_not_place(monkeypatch):
    """Port 7496 with TRADING_MODE=paper must not place."""
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "7496")
    clear_runtime_overrides()
    get_config.cache_clear()
    cfg = get_config()
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7496
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)

    from abcxauto.send import send_action

    connector = _connector()
    result = await send_action(_placeable_ticket(), connector)

    assert result["status"] == "blocked"
    assert result.get("reason_code") == "live_port_paper"
    assert "7496" in str(result.get("note") or "")
    connector.place_order.assert_not_called()
    assert get_config().trading_mode == "paper"
    assert get_config().ibkr_port == 7496


@pytest.mark.asyncio
async def test_port_4001_trading_mode_paper_must_not_place(monkeypatch):
    """Gateway 4001 with TRADING_MODE=paper must not place."""
    await _assert_paper_live_port_does_not_place(monkeypatch, 4001)


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [7497, 4002])
async def test_paper_ports_still_dispatch(monkeypatch, port):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", str(port))
    monkeypatch.setattr(
        "abcxauto.send.get_config",
        lambda: _cfg(trading_mode="paper", ibkr_port=port),
    )
    dispatched = []

    async def _record(action, connector):
        dispatched.append((action, connector))
        return {"status": "ok", "note": "dispatched"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)

    from abcxauto.send import send_action

    connector = _connector()
    ticket = _placeable_ticket()
    result = await send_action(ticket, connector)

    assert result["status"] == "ok"
    assert len(dispatched) == 1
    assert dispatched[0][0] is ticket
    assert dispatched[0][1] is connector


@pytest.mark.asyncio
async def test_live_mode_is_not_enabled_by_send(monkeypatch):
    """trading_mode==live is the blocked live path — send does not enable it."""
    monkeypatch.setattr(
        "abcxauto.send.get_config",
        lambda: _cfg(trading_mode="live", ibkr_port=7496),
    )
    dispatched = []

    async def _record(action, connector):
        dispatched.append(True)
        return {"status": "blocked", "note": "live path stays blocked"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)

    from abcxauto.send import send_action

    connector = _connector()
    result = await send_action(_placeable_ticket(), connector)

    assert dispatched == [True]
    assert result["status"] == "blocked"
    connector.place_order.assert_not_called()
    # send must not flip the process onto live
    assert get_config().trading_mode == "paper"


def test_size_pct_nl_notional_from_current_nl():
    """Grok's size is % of current NL — not a dollar sleeve or a 1% card law."""
    from abcxauto.send import (
        apply_size_pct_nl,
        notional_from_size_pct_nl,
        qty_from_size_pct_nl,
    )

    nl = 100_000.0
    assert notional_from_size_pct_nl(2.0, nl) == 2000.0
    assert qty_from_size_pct_nl(2.0, nl, 50.0) == 40
    assert qty_from_size_pct_nl(1.0, 37_000.0, 500.0) is None  # 370 < 500
    assert qty_from_size_pct_nl(5.0, 37_000.0, 500.0) == 3
    params = {"symbol": "AAPL", "size_pct_nl": 2.0}
    note = apply_size_pct_nl(params, net_liq=nl, price=50.0, strategy="market_bracket")
    assert note is not None
    assert note["notional"] == 2000.0
    assert note["net_liq"] == nl
    assert params["quantity"] == 40
    # Valid qty is left alone — Grok already chose contracts.
    kept = {"quantity": 7, "size_pct_nl": 2.0}
    assert apply_size_pct_nl(kept, net_liq=nl, price=50.0) is None
    assert kept["quantity"] == 7
