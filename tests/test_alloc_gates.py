"""Alloc size / heat refuses for new risk only — fit_qty, not sell/rotate."""

from __future__ import annotations

from types import SimpleNamespace

from abcxauto.risk_gates import check_alloc_gates, is_exit_or_management


def _buy(*, symbol: str = "AVGO", quantity: int = 89, **params) -> SimpleNamespace:
    base = {
        "symbol": symbol,
        "quantity": quantity,
        "direction": "LONG",
        "entry_price": 363.36,
        "stop_price": 356.35,
        "target_price": 380.0,
        "closing_position": False,
    }
    base.update(params)
    return SimpleNamespace(strategy="market_bracket", params=SimpleNamespace(**base))


def _exit_sell(*, symbol: str = "AVGO", quantity: int = 89) -> SimpleNamespace:
    return SimpleNamespace(
        strategy="market_order",
        params=SimpleNamespace(
            symbol=symbol,
            quantity=quantity,
            action="SELL",
            closing_position=True,
            entry_price=363.36,
            stop_price=356.35,
        ),
    )


def test_buy_above_sized_refused_with_fit_qty():
    proposal = _buy(quantity=89)
    snap = {
        "sized": {
            "AVGO": {
                "held": 0,
                "sized": 22,
                "excess": 0,
                "last": 363.36,
                "stop": 356.35,
            }
        }
    }
    ok, reason = check_alloc_gates(proposal, snap)
    assert ok is False
    assert "fit_qty=22" in reason
    assert "sell" not in reason.lower()
    assert "rotate" not in reason.lower()


def test_exit_sell_not_refused_by_alloc():
    proposal = _exit_sell(quantity=89)
    assert is_exit_or_management(proposal) is True
    snap = {
        "sized": {"AVGO": {"sized": 22}},
        "heat_pct": 9.0,
        "heat_block": True,
    }
    ok, reason = check_alloc_gates(proposal, snap)
    assert ok is True
    assert "fit_qty" not in reason
    assert "heat_block" not in reason


def test_heat_block_refuses_new_risk():
    proposal = _buy(quantity=10)
    snap = {
        "sized": {"AVGO": {"sized": 22}},
        "heat_pct": 7.5,
        "heat_block": True,
    }
    ok, reason = check_alloc_gates(proposal, snap)
    assert ok is False
    assert "heat_block" in reason
    assert "sell" not in reason.lower()
    assert "rotate" not in reason.lower()


def test_qty_at_sized_passes():
    proposal = _buy(quantity=22)
    snap = {"sized": {"AVGO": {"sized": 22}}}
    ok, reason = check_alloc_gates(proposal, snap)
    assert ok is True
    assert reason == "ok"


def test_missing_alloc_size_module_skips(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _block_alloc(name, *args, **kwargs):
        if name == "abcxauto.alloc_size" or name.startswith("abcxauto.alloc_size."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_alloc)
    proposal = _buy(quantity=89)
    snap = {"sized": {"AVGO": {"sized": 22}}}
    ok, reason = check_alloc_gates(proposal, snap)
    assert ok is True
    assert reason == "alloc_size_missing"
