"""Layer-1 vol fact: taped names only, no GARCH, no SYSTEM_PROMPT growth."""

from __future__ import annotations

import ast
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.vol_fact import (
    BOOK_VOL_NAMES,
    WAKE_VOL_CHARS,
    banned_vol_prompt_terms,
    collect_vol_facts,
    compact_bars,
    fact_for_name,
    iv_minus_rv,
    iv_rank,
    iv_to_pct,
    publish_vol_facts,
    realized_state,
    taped_symbols,
    wake_vol_bit,
)
from abcxauto.world_state import WorldState, day_facts, format_wake

SYSTEM_PROMPT_LOCK = (
    "You own an Interactive Brokers {mode} book. Strategy is yours.\n"
    "Live only follows a promoted playbook. Risk is code.\n"
    "send tickets that match ORDER EXAMPLES.\n"
    "Size vs max_risk_per_trade_pct of NetLiq.\n"
)


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def _daily_bars(*, last_ret: float, quiet: float = 0.005, n: int = 22) -> list[dict]:
    px = 100.0
    start = date(2026, 1, 5)
    bars: list[dict] = []
    for i in range(n):
        if i == 0:
            pass
        elif i == n - 1:
            px *= 1.0 + last_ret
        else:
            px *= 1.0 + quiet
        bars.append({"t": (start + timedelta(days=i)).isoformat(), "c": round(px, 4)})
    return bars


def _chain(symbol: str = "IWM", *, ivs=None) -> dict:
    row = {
        "symbol": symbol,
        "source": "ibkr",
        "expirations": ["20260918"],
        "strikes": [220, 230, 240],
        "n_strikes": 3,
    }
    if ivs is not None:
        row["ivs"] = list(ivs)
        row["iv"] = ivs[len(ivs) // 2]
    return row


def _taped_snap(symbol: str = "IWM", *, last_ret: float = 0.04, ivs=None) -> dict:
    return {
        "positions": [{"symbol": symbol, "quantity": 1, "secType": "OPT"}],
        "candle_bars": {symbol: {"bars": _daily_bars(last_ret=last_ret), "resolution": "D"}},
        "option_chains": {symbol: _chain(symbol, ivs=ivs or [0.15, 0.22, 0.35])},
    }


def test_system_prompt_is_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert banned_vol_prompt_terms(SYSTEM_PROMPT) == []


def test_no_garch_import_or_prompt_lecture():
    from abcxauto.agent_loop import AWARENESS_HEART

    root = Path(__file__).resolve().parents[1] / "abcxauto"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "garch" not in alias.name.lower()
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "garch" not in node.module.lower()
    for text in (SYSTEM_PROMPT, AWARENESS_HEART):
        assert banned_vol_prompt_terms(text) == []


def test_fact_present_for_taped_name_with_candles_and_chain():
    snap = _taped_snap("IWM", last_ret=0.04)
    world = _world(positions=snap["positions"])
    rows = collect_vol_facts(world, snap)
    assert len(rows) == 1
    row = rows[0]
    assert row["sym"] == "IWM"
    assert row["rv"] == "high"
    assert row["iv"] == 22.0
    assert row["ivr"] == 35
    assert row["iv_rv"].startswith("+")


def test_fact_absent_when_no_tape():
    snap = {
        "ibkr_live_quotes": {"SPY": 500.0, "VIX": 15.0},
        "candle_bars": {},
        "option_chains": {},
    }
    world = _world(positions=[], scan_fetched=[])
    assert taped_symbols(world, snap) == []
    assert collect_vol_facts(world, snap) == []
    day = day_facts(world, {})
    assert day["vol"] == []
    assert day["vol_bit"] == ""
    text = format_wake(
        cycle=1, session="regular", flat=True, unprotected=[], ibkr_up=True, day=day
    )
    assert "vol=" not in text


def test_candles_or_chain_alone_is_not_a_fact():
    pos = [{"symbol": "IWM", "quantity": 1}]
    world = _world(positions=pos)
    bars_only = {
        "positions": pos,
        "candle_bars": {"IWM": {"bars": _daily_bars(last_ret=0.04)}},
    }
    chain_only = {
        "positions": pos,
        "option_chains": {"IWM": _chain("IWM", ivs=[0.2])},
    }
    assert collect_vol_facts(world, bars_only) == []
    assert collect_vol_facts(world, chain_only) == []


def test_canned_universe_is_not_taped():
    world = _world(positions=[])
    snap = {"ibkr_live_quotes": {"SPY": 500.0, "QQQ": 400.0, "IWM": 220.0}}
    assert "SPY" not in taped_symbols(world, snap)
    assert collect_vol_facts(world, snap) == []


def test_realized_vol_high_mid_low():
    assert realized_state(_daily_bars(last_ret=0.04, quiet=0.005))["rv"] == "high"
    assert realized_state(_daily_bars(last_ret=0.005, quiet=0.005))["rv"] == "mid"
    assert realized_state(_daily_bars(last_ret=0.001, quiet=0.01))["rv"] == "low"
    assert realized_state([{"t": "2026-01-01", "c": 10}]) is None


def test_iv_minus_rv_is_sign_and_rough_size():
    assert iv_minus_rv(22.0, 8.0) == "+14"
    assert iv_minus_rv(10.0, 18.0) == "-8"
    assert iv_minus_rv(12.1, 12.0) == "~0"
    assert iv_to_pct(0.22) == 22.0
    assert iv_to_pct(22.0) == 22.0
    assert iv_rank(22.0, [15.0, 22.0, 35.0]) == 35
    assert iv_rank(22.0, [22.0]) is None


def test_strike_list_is_not_iv():
    row = fact_for_name(
        "IWM",
        bars=_daily_bars(last_ret=0.01),
        chain=_chain("IWM"),
    )
    assert row is not None
    assert row["rv"] in ("high", "mid", "low")
    assert "iv" not in row


def test_day_facts_and_wake_and_book_paint_clipped_vol():
    from abcxauto.brain import _book_facts  # noqa: PLC0415

    snap = _taped_snap("IWM", last_ret=0.04)
    world = _world(positions=snap["positions"], flat=False)
    publish_vol_facts(world, snap)
    day = day_facts(world, {})
    assert day["vol"][0]["sym"] == "IWM"
    assert day["vol_bit"].startswith("IWM rv=high")
    text = format_wake(
        cycle=4, session="regular", flat=False, unprotected=[], ibkr_up=True, day=day
    )
    assert "vol=IWM rv=high" in text
    assert "iv=22.0" in text
    assert "iv-rv=" in text
    book = _book_facts(world)
    assert book["vol"][0]["sym"] == "IWM"
    assert len(json.dumps(book["vol"])) < 400


def test_wake_vol_is_clipped():
    rows = [
        {"sym": f"N{i}", "rv": "mid", "iv": 20 + i, "iv_rv": "+3"}
        for i in range(12)
    ]
    bit = wake_vol_bit(rows)
    assert bit.count(" rv=") <= 2
    assert len(bit) <= WAKE_VOL_CHARS
    from abcxauto.vol_fact import clip_vol_facts

    assert len(clip_vol_facts(rows)) <= BOOK_VOL_NAMES


def test_compact_bars_drop_fat():
    fat = [{"t": "2026-01-01", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9, "t_unix": 1, "t_iso": "x"}]
    slim = compact_bars(fat)
    assert slim == [{"c": 1.5, "t": "2026-01-01"}]


def test_write_last_turn_does_not_persist_vol(tmp_path, monkeypatch):
    from abcxauto import think_stream as ts

    monkeypatch.setattr(ts, "LAST_TURN_PATH", tmp_path / "last_turn.json")
    monkeypatch.setattr(ts, "DESK_BRIEF_PATH", tmp_path / "desk_brief.json")
    ts.write_last_turn(
        {
            "strat": "watching",
            "rationale": "watching IWM",
            "vol_facts": [{"sym": "IWM", "rv": "high"}],
            "world_state": {"vol_facts": [{"sym": "IWM", "rv": "high"}], "flat": True},
        }
    )
    last = json.loads((tmp_path / "last_turn.json").read_text(encoding="utf-8"))
    assert "vol_facts" not in last
    assert "vol" not in last
    snap: dict = {"candle_source": "none"}
    ts.seed_snap_from_last_turn(snap)
    assert "vol_facts" not in snap


def test_mda_asof_is_not_a_trigger_clock():
    from datetime import datetime, timezone

    from abcxauto.think_stream import _scan_hits_asof_age_s, scan_tape_age_s

    now = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)
    row = {
        "ts": "2026-08-28T13:50:00Z",
        "scan_hits": {
            "rows": [
                {
                    "symbol": "NKE",
                    "mda": {"asof_iso": "2026-08-28T10:00:00Z"},
                    "asof_iso": "2026-08-28T10:00:00Z",
                }
            ]
        },
    }
    assert _scan_hits_asof_age_s(row, now=now) is None
    age = scan_tape_age_s(row, now=now)
    assert age is not None
    assert 500 < age < 700


def test_news_facts_text_is_color_not_trigger():
    from abcxauto.brain import AGENT_TOOLS  # noqa: PLC0415
    from abcxauto.news_feed import format_news_for_prompt
    from abcxauto.prints import USE_MDA_NEWS

    assert USE_MDA_NEWS == "color_not_trigger"
    text = format_news_for_prompt(
        [{"symbol": "IWM", "headline": "Fund flow note"}]
    )
    assert "color only" in text
    assert "not a trigger" in text
    assert "garch" not in text.lower()
    catalog = json.dumps(AGENT_TOOLS, default=str)
    assert "Color only, never a trigger" in catalog
    assert "+15 minutes is already in the price" in catalog
    assert banned_vol_prompt_terms(catalog) == []


@pytest.mark.asyncio
async def test_candles_then_chain_stamps_vol_on_taped_name():
    from abcxauto.brain import BrainTurn, _run_tool

    bars = _daily_bars(last_ret=0.04)

    class Conn:
        async def get_historical_bars(self, symbol, *, resolution="D", countback=60):
            return {
                "symbol": symbol,
                "bars": bars,
                "source": "ibkr",
                "freshness": "ibkr_rth",
                "resolution": "D",
            }

        async def get_realtime_bars(self, symbol, **_k):
            raise AssertionError("hist answered")

        async def get_option_chain(self, symbol, min_dte=7, max_dte=45):
            return _chain(symbol, ivs=[0.15, 0.22, 0.35])

    world = _world(positions=[{"symbol": "IWM", "quantity": 1, "secType": "OPT"}], flat=False)
    snap: dict = {"positions": world.positions}
    await _run_tool(
        "candles",
        {"symbol": "IWM", "resolution": "D", "countback": 40},
        connector=Conn(),
        world=world,
        snap=snap,
        turn=BrainTurn(),
    )
    assert snap.get("vol_facts") in (None, [])
    await _run_tool(
        "option_chain",
        {"symbol": "IWM"},
        connector=Conn(),
        world=world,
        snap=snap,
        turn=BrainTurn(),
    )
    rows = snap.get("vol_facts") or world.vol_facts
    assert rows and rows[0]["sym"] == "IWM"
    assert rows[0]["rv"] == "high"
    day = day_facts(world, {})
    assert day["vol"][0]["sym"] == "IWM"
    assert "vol=IWM" in format_wake(
        cycle=1, session="regular", flat=False, unprotected=[], ibkr_up=True, day=day
    )
