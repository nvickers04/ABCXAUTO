"""Wake paints measured diversification, size, and order-type facts."""

from __future__ import annotations

import pytest

from abcxauto.world_state import format_wake


def test_wake_fills_book_numbers_and_leaves_f_blank() -> None:
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "allocation": {
                "nl": 32541.48,
                "leftover_usd": 24768.78,
                "lots": [
                    {
                        "symbol": "XOM",
                        "qty": 48,
                        "last": 161.11,
                        "stop": 159.15,
                        "target": 164.94,
                    }
                ],
            },
            "cash_only": True,
            "max_risk_per_trade_pct": 20.0,
            "board_line": "board XOM open.",
        },
    )
    assert "book names=1." in text
    assert "XOM long" in text
    assert "H=1.000 N_eff=1.00." in text
    assert "heat=no pair." in text
    assert "board XOM open." in text
    assert "q = floor(NL * f / |entry - stop|)" in text
    assert "f= entry=" in text
    assert "q is not solved here" in text
    assert "long_stk loss_per_unit=|entry-stop|=1.96" in text
    assert "short_stk unavailable cash_only=on" in text
    assert "q=floor(NL * f / loss_per_unit)" in text
    assert "f=?" in text
    lower = text.lower()
    assert "should" not in lower
    assert "must" not in lower
    assert "sell" not in lower


def test_wake_without_allocation_stays_quiet() -> None:
    text = format_wake(
        cycle=1,
        session="regular",
        flat=True,
        unprotected=[],
        ibkr_up=True,
        day={"vol_bit": "SPY rv20=0.12"},
    )
    assert "book names=" not in text
    assert "long_stk " not in text


def test_quote_math_fills_debit_and_leaves_f_blank() -> None:
    from abcxauto.world_state import attach_tool_math

    class World:
        net_liquidation = 32541.48
        positions = [
            {
                "symbol": "XOM",
                "secType": "STK",
                "quantity": 48,
                "avgCost": 161.36,
            }
        ]
        open_orders = [
            {
                "symbol": "XOM",
                "orderType": "STP",
                "action": "SELL",
                "totalQuantity": 48,
                "auxPrice": 159.15,
            }
        ]
        ibkr_live_quotes = {"XOM": {"last": 161.11}}

    row = {
        "symbol": "XOM",
        "strike": 160,
        "right": "P",
        "ibkr": {
            "bid": 1.2,
            "ask": 1.4,
            "last": 1.3,
            "delta": -0.4,
            "theta": -0.05,
            "vega": 0.12,
            "iv": 0.22,
        },
    }
    attach_tool_math(row, World())
    assert "f=" in row["size_page"]
    assert "floor(" in row["size_page"]
    assert "q=48" not in row["size_page"]
    assert "loss_per_unit=debit*100=130" in row["order_type"]
    assert "delta(risk-neutral)=-0.4" in row["order_type"]
    assert "f=?" in row["order_type"]
    # Option bid/ask must not fill the execution stock spread.
    exec_ln = next(ln for ln in row["order_type"].splitlines() if ln.startswith("execution"))
    assert "spread=(ask-bid)/mid=" not in exec_ln


def test_wake_prints_spread_from_the_stock_quote() -> None:
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day={
            "allocation": {
                "nl": 32541.48,
                "leftover_usd": 24768.78,
                "lots": [
                    {
                        "symbol": "XOM",
                        "qty": 48,
                        "last": 161.11,
                        "stop": 159.15,
                    }
                ],
            },
            "cash_only": True,
            "stock_bid": 161.05,
            "stock_ask": 161.2,
        },
    )
    assert "spread=(ask-bid)/mid=" in text
    # Stock quote must not invent an option debit / long_opt line.
    assert "long_opt" not in text


def test_stash_quote_inputs_uses_the_held_name() -> None:
    from abcxauto.brain import _stash_quote_inputs

    day = {"allocation": {"lots": [{"symbol": "XOM", "qty": 48, "last": 161.0}]}}
    _stash_quote_inputs(
        day,
        {
            "names": {
                "SPY": {"qty": 0, "bid": 500.0, "ask": 500.2, "last": 500.1},
                "XOM": {
                    "qty": 48,
                    "bid": 161.05,
                    "ask": 161.2,
                    "iv": 0.22,
                    "last": 161.11,
                },
            }
        },
    )
    assert day["stock_bid"] == 161.05
    assert day["stock_ask"] == 161.2
    assert day["stock_iv"] == 0.22
    assert "bid" not in day
    assert "delta" not in day
    assert "iv" not in day
    assert day["allocation"]["lots"][0]["last"] == 161.0


@pytest.mark.asyncio
async def test_one_held_option_quote_fills_debit() -> None:
    from abcxauto.brain import _stash_option_quotes

    class Conn:
        async def get_live_option_quote(self, symbol, expiration, strike, right):
            assert symbol == "XOM"
            assert expiration == "20261016"
            assert strike == 160.0
            assert right == "P"
            return {
                "symbol": symbol,
                "bid": 1.2,
                "ask": 1.4,
                "last": 1.3,
                "delta": -0.4,
                "theta": -0.05,
                "iv": 0.22,
            }

    day = {
        "allocation": {
            "nl": 32541.48,
            "leftover_usd": 20000.0,
            "lots": [{"symbol": "XOM", "qty": 1, "last": 161.11}],
        },
        "cash_only": True,
        "stock_bid": 161.05,
        "stock_ask": 161.2,
    }
    await _stash_option_quotes(
        Conn(),
        [
            {
                "symbol": "XOM",
                "secType": "OPT",
                "right": "P",
                "strike": 160,
                "expiration": "2026-10-16",
                "quantity": 1,
            }
        ],
        day,
    )
    assert day["stock_bid"] == 161.05
    assert day["debit"] == (1.2 + 1.4) / 2.0
    assert day["strike"] == 160.0
    assert day["delta"] == -0.4
    assert day["bid"] == 1.2
    text = format_wake(
        cycle=1,
        session="regular",
        flat=False,
        unprotected=[],
        ibkr_up=True,
        day=day,
    )
    assert "long_opt" in text
    assert "loss_per_unit=debit*100=130" in text
    assert "delta(risk-neutral)=-0.4" in text
    stock_mid = (161.2 + 161.05) / 2.0
    stock_spr = (161.2 - 161.05) / stock_mid
    opt_mid = (1.4 + 1.2) / 2.0
    opt_spr = (1.4 - 1.2) / opt_mid
    stock_spr_s = f"{stock_spr:.6f}".rstrip("0").rstrip(".")
    opt_spr_s = f"{opt_spr:.6f}".rstrip("0").rstrip(".")
    exec_ln = next(ln for ln in text.splitlines() if ln.startswith("execution"))
    assert f"spread=(ask-bid)/mid={stock_spr_s}" in exec_ln
    assert f"spread=(ask-bid)/mid={opt_spr_s}" not in exec_ln
    long_opt = next(ln for ln in text.splitlines() if ln.startswith("long_opt"))
    # Option spread may appear on long_opt; it must not be the execution spread.
    if f"spread=(ask-bid)/mid={opt_spr_s}" in text:
        assert f"spread=(ask-bid)/mid={opt_spr_s}" in long_opt


@pytest.mark.asyncio
async def test_two_option_lots_do_not_invent_a_debit() -> None:
    from abcxauto.brain import _stash_option_quotes

    class Conn:
        async def get_live_option_quote(self, symbol, expiration, strike, right):
            raise AssertionError("two legs are not one debit")

    day: dict = {"bid": 161.05}
    await _stash_option_quotes(
        Conn(),
        [
            {
                "symbol": "XOM",
                "secType": "OPT",
                "right": "P",
                "strike": 155,
                "expiration": "20261016",
                "quantity": -1,
            },
            {
                "symbol": "XOM",
                "secType": "OPT",
                "right": "P",
                "strike": 160,
                "expiration": "20261016",
                "quantity": 1,
            },
        ],
        day,
    )
    assert "debit" not in day
    assert day["bid"] == 161.05
