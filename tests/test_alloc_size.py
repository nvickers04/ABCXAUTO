"""Allocation share targets — 1% risk / 25% notional, not the 20% max_risk knob."""

from __future__ import annotations

import math

from abcxauto.alloc_size import (
    HEAT_CAP_PCT,
    NOTIONAL_CAP_PCT,
    RISK_PCT,
    heat_pct,
    heat_pct_of,
    sized_book,
    target_shares,
)

NL = 32581.0
LAST = 363.36
STOP = 356.35
QTY = 89


def test_constants():
    assert RISK_PCT == 1.0
    assert NOTIONAL_CAP_PCT == 25.0
    assert HEAT_CAP_PCT == 6.0


def test_avgo_notional_cap_wins():
    risk = math.floor(NL * RISK_PCT / 100.0 / abs(LAST - STOP))
    notional = math.floor(NL * NOTIONAL_CAP_PCT / 100.0 / LAST)
    assert notional == 22
    assert risk == 46
    sized = target_shares(nl=NL, last=LAST, stop=STOP)
    assert sized == 22
    assert sized == min(risk, notional)


def test_stop_missing_uses_notional_only():
    assert target_shares(nl=NL, last=LAST, stop=None) == 22


def test_sized_book_avgo_positive_vs_spy():
    snap = {
        "nl": NL,
        "names": {
            "AVGO": {
                "last": LAST,
                "stop": STOP,
                "qty": QTY,
                "aligned": True,
            },
        },
    }
    scores = {"AVGO": {"vs_spy": 0.12}}
    out = sized_book(snap, scores)
    row = out["AVGO"]
    assert row["held"] == 89
    assert row["sized"] == 22
    assert row["excess"] == 67
    assert row["excess"] != 0
    assert row["vs_spy"] == 0.12
    assert row["last"] == LAST
    assert row["stop"] == STOP


def test_sized_book_avgo_negative_vs_spy_keeps_target():
    snap = {
        "nl": NL,
        "names": {
            "AVGO": {
                "last": LAST,
                "stop": STOP,
                "qty": QTY,
                "aligned": True,
            },
        },
    }
    scores = {"AVGO": {"vs_spy": -0.05, "partial": False}}
    out = sized_book(snap, scores)
    row = out["AVGO"]
    assert row["sized"] == 22
    assert row["excess"] == 67
    assert row["vs_spy"] == -0.05


def test_unaligned_sized_none():
    snap = {
        "nl": NL,
        "names": {
            "AVGO": {
                "last": LAST,
                "stop": STOP,
                "qty": QTY,
                "aligned": False,
            },
        },
    }
    scores = {"AVGO": {"vs_spy": 0.12}}
    out = sized_book(snap, scores)
    row = out["AVGO"]
    assert row["sized"] is None
    assert row["excess"] is None
    assert row["held"] == 89


def test_heat_of_22_share_lot_under_heat_cap():
    lots = [{"qty": 22, "last": LAST, "stop": STOP}]
    heat = heat_pct_of(lots, NL)
    assert heat == heat_pct(lots, NL)
    assert heat < HEAT_CAP_PCT
    expect = 100.0 * 22 * abs(LAST - STOP) / NL
    assert heat == expect


def test_does_not_read_risk_config_or_floors():
    import abcxauto.alloc_size as mod

    assert not hasattr(mod, "get_config")
    assert "abcxauto.config" not in getattr(mod, "__dict__", {})
    assert "abcxauto.risk_gates" not in getattr(mod, "__dict__", {})
    src = open(mod.__file__, encoding="utf-8").read()
    assert "get_config" not in src
    assert "sizing_floors" not in src
    assert "max_risk_per_trade_pct" not in src
