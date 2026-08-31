"""explore/exploit is a MODE BIT that sizes, not a personality label.

Paper gates-off (#110) must still clamp lottery % of NL. Exploit without
graduated cards must not widen. Paper start must not restore
25% as the working send size. Live still forces gates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from abcxauto.config import Config, get_config
from abcxauto.mode_size import (
    MODE_SIZE_CEILING_EXPLORE,
    MODE_SIZE_FLOOR,
    exploit_learning_card_error,
    exploit_may_widen,
    live_marks_match_paper,
    mode_size_ceiling,
    mode_size_ticket_error,
    working_size_ceiling,
)
from abcxauto.self_tune import (
    apply_self_tune,
    ensure_immutable_floor,
    floor_clamp_config_fields,
    load_agent_state,
)
from abcxauto.send import apply_size_pct_nl, qty_from_size_pct_nl
from abcxauto.world_state import WorldState




def _paper_cfg(**overrides) -> Config:
    base = get_config()
    return Config(
        **{
            **base.__dict__,
            "trading_mode": "paper",
            "ibkr_port": 7497,
            "risk_gates_enabled": False,
            "defined_risk_only": True,
            "risk_posture": "balanced",
            **overrides,
        }
    )


def _world() -> WorldState:
    return WorldState(
        cycle=1,
        session_status="regular",
        flat=True,
        needs_protection=False,
        unprotected=[],
        net_liquidation=100_000.0,
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
        capacity={
            "open_count": 0,
            "max_open_positions": 6,
            "slots_left": 6,
            "allows_new_risk": True,
        },
    )


def _floor_cfg(*, live: bool, max_risk: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        daily_loss_limit_pct=25.0,
        max_position_pct=25.0,
        max_risk_per_trade_pct=max_risk,
        max_peak_drawdown_pct=25.0,
        max_option_premium_pct=5.0,
        max_symbol_concentration_pct=25.0,
        max_arena_concentration_pct=25.0,
        max_open_positions=15,
        risk_gates_enabled=False,
        auto_panic_on_breach=True,
        defined_risk_only=True,
        cash_only=True,
        scan_fetch_cap=8,
        trading_budget_usd=0.0,
        trading_mode="live" if live else "paper",
        ibkr_port=7496 if live else 7497,
        is_paper=not live,
        sizing_floors=False,
    )


def test_explore_clamps_12pct_learning_card_send_when_paper_gates_off():
    """A learning card cannot send a live-intolerable % of NL — even gates-off."""
    card = "learn flush"
    params = {"card": card, "symbol": "AAPL", "size_pct_nl": 12.0}
    note = apply_size_pct_nl(
        params, net_liq=100_000.0, price=50.0, strategy="market_bracket"
    )
    assert note is not None
    assert note.get("clamped") is True
    assert note["raw_size_pct_nl"] == 12.0
    assert note["size_pct_nl"] == MODE_SIZE_CEILING_EXPLORE
    assert params["size_pct_nl"] == MODE_SIZE_CEILING_EXPLORE
    lottery = qty_from_size_pct_nl(12.0, 100_000.0, 50.0)
    assert params["quantity"] == qty_from_size_pct_nl(
        MODE_SIZE_CEILING_EXPLORE, 100_000.0, 50.0
    )
    assert params["quantity"] < lottery
    assert MODE_SIZE_CEILING_EXPLORE < 10
    assert MODE_SIZE_CEILING_EXPLORE != 1.0
    assert MODE_SIZE_CEILING_EXPLORE != 25.0


def test_explore_clamps_lottery_qty_without_size_pct_nl():
    params = {"card": "learn flush", "symbol": "AAPL", "quantity": 240}
    note = apply_size_pct_nl(
        params, net_liq=100_000.0, price=50.0, strategy="market_bracket"
    )
    assert note is not None
    assert note.get("clamped") is True
    assert params["quantity"] == qty_from_size_pct_nl(
        MODE_SIZE_CEILING_EXPLORE, 100_000.0, 50.0
    )
    assert params["quantity"] < 240


@pytest.mark.asyncio
async def test_execute_ticket_explore_clamps_12pct_when_paper_gates_off(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    card = "learn flush"
    cfg = _paper_cfg()
    monkeypatch.setattr("abcxauto.agent_loop.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.executor.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.send.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.proposals.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    result = await execute_ticket(
        {
            "action": "market_bracket",
            "strategy": "market_bracket",
            "params": {
                "card": card,
                "symbol": "AAPL",
                "direction": "LONG",
                "stop_price": 48.0,
                "target_price": 55.0,
                "size_pct_nl": 12.0,
            },
            "rationale": card,
        },
        object(),
        _world(),
        {
            "account": {"netliquidation": 100_000.0},
            "positions": [],
            "open_orders": [],
            "ibkr_live_quotes": {"AAPL": 50.0},
        },
    )
    assert result.get("status") in ("ok", "blocked"), result
    if result.get("status") == "blocked":
        note = str(result.get("note") or "")
        assert "mode_size" in note or "12" in note
        assert sent == []
        return
    assert sent
    pct = float(sent[0]["params"]["size_pct_nl"])
    qty = int(sent[0]["params"]["quantity"])
    assert pct <= MODE_SIZE_CEILING_EXPLORE + 1e-6
    assert pct < 10
    lottery = qty_from_size_pct_nl(12.0, 100_000.0, 50.0)
    assert qty < lottery
    assert qty == qty_from_size_pct_nl(pct, 100_000.0, 50.0)


def test_exploit_without_graduated_cards_does_not_widen():
    card = "still learning"
    assert mode_size_ceiling() == MODE_SIZE_CEILING_EXPLORE
    assert mode_size_ceiling(card=card, type="market_bracket") == MODE_SIZE_CEILING_EXPLORE
    assert working_size_ceiling() == MODE_SIZE_CEILING_EXPLORE
    assert exploit_may_widen() is False
    params = {"card": card, "size_pct_nl": 12.0}
    note = apply_size_pct_nl(
        params, net_liq=100_000.0, price=50.0, strategy="market_bracket"
    )
    assert note is not None
    assert note["size_pct_nl"] == MODE_SIZE_CEILING_EXPLORE
    assert params["size_pct_nl"] == MODE_SIZE_CEILING_EXPLORE
    out = apply_self_tune({"size_pct_nl": 12.0}, persist=True)
    assert out["status"] == "ok"
    assert out["applied"]["size_pct_nl"] == MODE_SIZE_CEILING_EXPLORE
    assert out["clamped"]["size_pct_nl"]["raw"] == 12.0



def test_exploit_with_graduated_cards_opens_walkaway_not_working_size(monkeypatch):
    """Exploit + graduated opens the walk-away ceiling. Working default stays single-digit."""
    monkeypatch.setattr("abcxauto.mode_size.graduated_names", lambda book=None: ["grad"])
    monkeypatch.setattr(
        "abcxauto.mode_size.card_is_graduated",
        lambda *_a, **_k: True,
    )
    assert live_marks_match_paper() is False
    assert exploit_may_widen() is True
    assert mode_size_ceiling(card="grad") == 25.0
    assert working_size_ceiling(card="grad") == MODE_SIZE_CEILING_EXPLORE
    assert working_size_ceiling(card="grad") != 25.0
    assert working_size_ceiling(card="grad") < 10


def test_self_tune_may_move_inside_the_band_not_only_down():
    first = apply_self_tune({"size_pct_nl": 3.0}, persist=True)
    assert first["applied"]["size_pct_nl"] == 3.0
    second = apply_self_tune({"size_pct_nl": 6.0}, persist=True)
    assert second["applied"]["size_pct_nl"] == 6.0
    assert working_size_ceiling() == 6.0
    tight = apply_self_tune({"size_pct_nl": 0.1}, persist=True)
    assert tight["applied"]["size_pct_nl"] == MODE_SIZE_FLOOR


def test_paper_start_does_not_restore_25pct_working_size():
    assert get_config().trading_mode == "paper"
    assert get_config().ibkr_port == 7497
    assert working_size_ceiling() != 25.0
    assert working_size_ceiling() < 10
    assert working_size_ceiling() == MODE_SIZE_CEILING_EXPLORE
    ensure_immutable_floor(persist=True)
    assert working_size_ceiling() != 25.0
    assert working_size_ceiling() < 10
    state = load_agent_state()
    assert state.get("size_pct_nl") not in (25, 25.0)
    paper = _floor_cfg(live=False, max_risk=1.0)
    assert "max_risk_per_trade_pct" not in floor_clamp_config_fields(paper)
    assert "max_option_premium_pct" not in floor_clamp_config_fields(paper)


def test_live_still_forces_gates_and_old_1pct_walkaway():
    live = _floor_cfg(live=True, max_risk=1.0)
    live.defined_risk_only = False
    live.cash_only = False
    live.auto_panic_on_breach = False
    fixes = floor_clamp_config_fields(live)
    assert fixes.get("risk_gates_enabled") is True
    assert fixes.get("defined_risk_only") is True
    assert fixes.get("cash_only") is True
    assert fixes.get("auto_panic_on_breach") is True
    assert fixes.get("sizing_floors") is True
    assert fixes.get("max_risk_per_trade_pct") == 25.0


def test_live_start_repairs_gates_off(tmp_path, monkeypatch):
    from abcxauto.config import clear_risk_settings, load_risk_settings, update_risk_config

    path = tmp_path / "risk.json"
    monkeypatch.setenv("ABCXAUTO_RISK_SETTINGS_PATH", str(path))
    monkeypatch.setenv("ABCXAUTO_AGENT_STATE_PATH", str(tmp_path / "agent.json"))
    monkeypatch.setenv("TRADING_MODE", "live")
    clear_risk_settings(path=path)
    load_risk_settings(path)
    get_config.cache_clear()
    update_risk_config(
        risk_gates_enabled=False,
        defined_risk_only=False,
        sizing_floors=False,
        persist=True,
        _skip_clamp=True,
    )
    ensure_immutable_floor(persist=True)
    cfg = get_config()
    assert cfg.risk_gates_enabled is True
    assert cfg.defined_risk_only is True
    assert cfg.sizing_floors is True
    assert working_size_ceiling() != 25.0


def test_mode_size_ticket_error_rejects_unclamped_12pct():
    raw = {"card": "learn", "size_pct_nl": 12.0, "quantity": 240}
    note = mode_size_ticket_error(raw, net_liq=100_000.0, price=50.0)
    assert note
    assert "mode_size" in note


def test_size_and_slots_stay_together_on_the_band():
    from abcxauto.mode_size import mode_size_band
    from abcxauto.self_tune import levers_snapshot

    band = mode_size_band()
    assert band["with"] == "max_open_positions"
    assert band["unit"] == "pct_nl"
    assert band["max"] == MODE_SIZE_CEILING_EXPLORE
    snap = levers_snapshot()
    assert snap["size_pct_nl"]["with"] == "max_open_positions"
    assert snap["max_open_positions"]["with"] == "size_pct_nl"
    assert "not pick-one" in snap["together"]


def test_system_prompt_does_not_bake_the_mode_number():
    from abcxauto.llm import SYSTEM_PROMPT
    from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK
    assert "8" not in SYSTEM_PROMPT
    assert "explore" not in SYSTEM_PROMPT
    assert "exploit" not in SYSTEM_PROMPT
