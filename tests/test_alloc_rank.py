"""Tests for abcxauto.alloc_rank — pure RS-style score, rank, heat groups."""

from __future__ import annotations

import pytest

from abcxauto.alloc_rank import heat_groups, rank_board, score_name


def _dates(n: int, start: str = "2024-01-02") -> list[str]:
    """Monotonic ISO-ish session labels (not calendar-aware; order only)."""
    y, m, d = (int(x) for x in start.split("-"))
    out: list[str] = []
    for i in range(n):
        day = d + i
        mm = m
        yy = y
        while day > 28:
            day -= 28
            mm += 1
            if mm > 12:
                mm = 1
                yy += 1
        out.append(f"{yy:04d}-{mm:02d}-{day:02d}")
    return out


def _flat(dates: list[str], px: float) -> dict[str, float]:
    return {d: px for d in dates}


def test_known_two_date_roc():
    """ROC(63) from two known closes 63 sessions apart."""
    dates = _dates(64)  # indices 0..63 → ROC(63) exists
    name = {d: 100.0 for d in dates}
    name[dates[0]] = 100.0
    name[dates[-1]] = 110.0
    spy = _flat(dates, 400.0)

    out = score_name(name, spy)
    assert out["windows"] == [63]
    assert out["partial"] is True
    assert out["score"] == pytest.approx(0.10)
    assert out["vs_spy"] == pytest.approx(0.10)


def test_partial_when_only_63_sessions():
    dates = _dates(64)
    name = {d: 100.0 + i * 0.01 for i, d in enumerate(dates)}
    spy = {d: 200.0 + i * 0.005 for i, d in enumerate(dates)}
    out = score_name(name, spy)
    assert out["partial"] is True
    assert out["windows"] == [63]
    assert 126 not in out["windows"]
    assert 252 not in out["windows"]


def test_full_year_not_partial():
    dates = _dates(253)  # t_idx = 252 → all four windows
    name = {d: 100.0 * (1.001**i) for i, d in enumerate(dates)}
    spy = {d: 200.0 * (1.0005**i) for i, d in enumerate(dates)}
    out = score_name(name, spy)
    assert out["partial"] is False
    assert out["windows"] == [63, 126, 189, 252]


def test_rank_order_by_vs_spy():
    scores = {
        "AAA": {"vs_spy": 0.05, "partial": False, "score": 0.1},
        "BBB": {"vs_spy": 0.20, "partial": True, "score": 0.3},
        "CCC": {"vs_spy": -0.01, "partial": False, "score": 0.0},
    }
    board = rank_board(scores)
    assert [r["symbol"] for r in board] == ["BBB", "AAA", "CCC"]
    assert [r["rank"] for r in board] == [1, 2, 3]
    assert all(r["universe"] == 3 for r in board)
    assert board[0]["vs_spy"] == 0.20
    assert board[0]["partial"] is True


def test_heat_perfect_corr_share_group():
    rets = [0.01, -0.02, 0.03, 0.0, 0.01, -0.01]
    groups = heat_groups({"AAA": rets, "BBB": list(rets), "CCC": [-x for x in rets]})
    paired = [g for g in groups if set(g) == {"AAA", "BBB"}]
    assert len(paired) == 1
    assert ["CCC"] in groups


def test_no_rs_rating_or_ibd_1_99_field():
    dates = _dates(64)
    name = _flat(dates, 100.0)
    name[dates[-1]] = 105.0
    spy = _flat(dates, 400.0)
    scored = score_name(name, spy)
    board = rank_board({"XYZ": scored})
    banned = {"rs_rating", "rating", "rs", "ibd_rating", "relative_strength"}
    for blob in (scored, *board):
        assert banned.isdisjoint(blob.keys())
        for k, v in blob.items():
            if k == "rank":
                continue
            # No IBD-style 1–99 integer score field
            assert not (
                isinstance(v, int)
                and not isinstance(v, bool)
                and 1 <= v <= 99
                and k.endswith("rating")
            )