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
    exploit_may_widen,
    implied_size_pct_nl,
    live_marks_match_paper,
    max_risk_per_trade_off,
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


# Production 2026-09-02: Noah set max_risk=0. Grok sized a named vertical.
# agent_loop passed underlying last (~570) into implied_size_pct_nl with
# option multiplier 100 → ~85% of NL. Ceiling was self_tune size_pct_nl
# 0.5 — a shadow cap, not leftover max_risk. Named gate mode_size_ticket_error.
_PROD_NL = 67_000.0
_PROD_UNDERLYING = 570.0
_PROD_PREMIUM = 2.50
_PROD_QTY = 5
_SHADOW = 0.5


def _named_vertical(**over) -> dict:
    p = {
        "card": "spy debit vertical",
        "symbol": "SPY",
        "expiration": "20260718",
        "long_strike": 500.0,
        "short_strike": 505.0,
        "right": "C",
        "quantity": _PROD_QTY,
        "limit_price": 5.0,
    }
    p.update(over)
    return p


def _arm_shadow_cap_max_risk_off():
    from abcxauto.config import update_risk_config
    from abcxauto.self_tune import apply_self_tune

    update_risk_config(
        max_risk_per_trade_pct=0,
        defined_risk_only=True,
        persist=True,
        _skip_clamp=True,
    )
    out = apply_self_tune({"size_pct_nl": _SHADOW}, persist=True)
    assert out["status"] == "ok"
    assert out["applied"]["size_pct_nl"] == _SHADOW
    assert get_config().max_risk_per_trade_pct == 0.0
    assert get_config().defined_risk_only is True
    assert get_config().ibkr_port == 7497
    assert get_config().trading_mode == "paper"
    assert working_size_ceiling() == _SHADOW
    assert max_risk_per_trade_off() is True


def test_option_implied_units_are_premium_not_underlying_notional():
    """Wrong units: qty × underlying × 100 / NL × 100 ≈ 85. Right: premium × 100."""
    from abcxauto.send import option_size_mark

    wrong = implied_size_pct_nl(
        1, _PROD_NL, _PROD_UNDERLYING, multiplier=100.0
    )
    right = implied_size_pct_nl(1, _PROD_NL, _PROD_PREMIUM, multiplier=100.0)
    assert wrong == pytest.approx(85.0746, rel=1e-4)
    assert right == pytest.approx(0.37313, rel=1e-3)
    assert wrong > MODE_SIZE_CEILING_EXPLORE
    assert right < _SHADOW

    px, mult = option_size_mark(
        "vertical_spread",
        _named_vertical(quantity=1, limit_price=_PROD_PREMIUM),
        _PROD_UNDERLYING,
    )
    assert px == _PROD_PREMIUM
    assert mult == 100.0
    # Missing premium: do not fall back to the stock last.
    none_px, none_mult = option_size_mark(
        "vertical_spread",
        {"symbol": "SPY", "quantity": 1, "expiration": "20260718"},
        _PROD_UNDERLYING,
    )
    assert none_px is None
    assert none_mult == 100.0
    stk_px, stk_mult = option_size_mark(
        "market_bracket", {"symbol": "AAPL", "quantity": 10}, 50.0
    )
    assert stk_px == 50.0
    assert stk_mult == 1.0


def test_option_premium_not_underlying_when_max_risk_on():
    """Units fix: a 1-lot debit must not look like 85% of NL just because last is 570."""
    from abcxauto.config import update_risk_config

    update_risk_config(max_risk_per_trade_pct=2.0, persist=True, _skip_clamp=True)
    apply_self_tune({"size_pct_nl": _SHADOW}, persist=True)
    assert max_risk_per_trade_off() is False
    assert working_size_ceiling() == _SHADOW
    note = mode_size_ticket_error(
        _named_vertical(quantity=1, limit_price=_PROD_PREMIUM),
        net_liq=_PROD_NL,
        price=_PROD_UNDERLYING,
        strategy="vertical_spread",
    )
    assert note == ""
    # Same ticket without premium still must not invent 85% from the stock last.
    no_prem = mode_size_ticket_error(
        {"card": "spy debit vertical", "symbol": "SPY", "quantity": 1},
        net_liq=_PROD_NL,
        price=_PROD_UNDERLYING,
        strategy="vertical_spread",
    )
    assert no_prem == ""


def test_mode_size_does_not_veto_defined_risk_option_when_max_risk_off():
    """size_pct_nl 0.5 must not stand in as a clerk max-risk while the knob is 0."""
    _arm_shadow_cap_max_risk_off()
    grok_sized = implied_size_pct_nl(
        _PROD_QTY, _PROD_NL, 5.0, multiplier=100.0
    )
    assert grok_sized > _SHADOW
    assert grok_sized < MODE_SIZE_CEILING_EXPLORE

    for name in (
        "vertical_spread",
        "calendar_spread",
        "butterfly",
        "iron_condor",
        "iron_butterfly",
    ):
        note = mode_size_ticket_error(
            _named_vertical(),
            net_liq=_PROD_NL,
            price=_PROD_UNDERLYING,
            strategy=name,
        )
        assert note == "", f"{name}: {note}"

    # apply must not shrink Grok's contracts either.
    params = _named_vertical()
    apply_note = apply_size_pct_nl(
        params,
        net_liq=_PROD_NL,
        price=_PROD_UNDERLYING,
        strategy="vertical_spread",
    )
    assert apply_note is None
    assert params["quantity"] == _PROD_QTY


def test_mode_size_still_rejects_lottery_stk_when_max_risk_is_on():
    from abcxauto.config import update_risk_config

    update_risk_config(max_risk_per_trade_pct=2.0, persist=True, _skip_clamp=True)
    apply_self_tune({"size_pct_nl": _SHADOW}, persist=True)
    assert max_risk_per_trade_off() is False
    note = mode_size_ticket_error(
        {"card": "learn", "size_pct_nl": 12.0, "quantity": 240},
        net_liq=100_000.0,
        price=50.0,
        strategy="market_bracket",
    )
    assert note
    assert "mode_size" in note


@pytest.mark.asyncio
async def test_execute_ticket_named_vertical_not_blocked_by_size_pct_nl_shadow(
    monkeypatch,
):
    from abcxauto.agent_loop import execute_ticket
    from abcxauto.look_snapshot import begin_look, record_look_tool

    _arm_shadow_cap_max_risk_off()
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda _s: True)

    sent: list[dict] = []

    async def capture(action, _conn):
        sent.append(action)
        return {"status": "ok", "order_id": 7001}

    async def underlying_last(_act, _snap, connector=None):
        return _PROD_UNDERLYING

    monkeypatch.setattr("abcxauto.agent_loop.send_action", capture)
    monkeypatch.setattr("abcxauto.agent_loop._quote_for_action", underlying_last)

    snap = {
        "account": {"netliquidation": _PROD_NL},
        "positions": [],
        "open_orders": [],
        "ibkr_live_quotes": {"SPY": _PROD_UNDERLYING},
    }
    begin_look(snap)
    record_look_tool(
        snap,
        "option_quote",
        {
            "symbol": "SPY",
            "ibkr": {"last": 5.0, "bid": 4.9, "ask": 5.1, "mid": 5.0},
        },
    )
    result = await execute_ticket(
        {
            "action": "vertical_spread",
            "strategy": "vertical_spread",
            "params": _named_vertical(),
            "rationale": "spy debit vertical",
        },
        object(),
        _world(),
        snap,
    )
    assert result.get("status") == "ok", result
    assert sent
    assert sent[0]["strategy"] == "vertical_spread"
    assert int(sent[0]["params"]["quantity"]) == _PROD_QTY
    assert "mode_size" not in str(result.get("note") or "")
    assert get_config().defined_risk_only is True
    assert get_config().max_risk_per_trade_pct == 0.0
    assert get_config().ibkr_port == 7497


def test_defined_risk_only_still_rejects_stk_and_allows_last_stop_when_max_risk_off(
    monkeypatch,
):
    from abcxauto.proposals import validate_proposal
    from abcxauto.risk_gates import check_defined_risk_only
    from tests.test_proposals import RATIONALE, VALID_PAYLOADS

    _arm_shadow_cap_max_risk_off()
    monkeypatch.setattr("abcxauto.risk_gates.get_config", get_config)
    monkeypatch.setattr("abcxauto.proposals.get_config", get_config)

    mb = validate_proposal(
        "market_bracket",
        {
            "symbol": "SIRI",
            "quantity": 10,
            "direction": "LONG",
            "stop_price": 28.50,
            "target_price": 31.00,
        },
        RATIONALE,
        quote_last=29.75,
    )
    ok, why = check_defined_risk_only(mb)
    assert ok is False
    assert "defined_risk_only" in why

    oca = validate_proposal(
        "oca",
        {
            "symbol": "SIRI",
            "quantity": 10,
            "direction": "LONG",
            "stop_price": 28.50,
            "target_price": 31.00,
        },
        RATIONALE,
        quote_last=29.75,
    )
    ok_oca, why_oca = check_defined_risk_only(oca)
    assert ok_oca is True, why_oca

    stop = validate_proposal(
        "stop_order",
        {
            "symbol": "CNH",
            "action": "SELL",
            "quantity": 5,
            "stop_price": 10.0,
            "closing_position": True,
        },
        RATIONALE,
    )
    ok_stop, why_stop = check_defined_risk_only(stop)
    assert ok_stop is True, why_stop

    for name in (
        "vertical_spread",
        "calendar_spread",
        "butterfly",
        "iron_condor",
        "iron_butterfly",
    ):
        prop = validate_proposal(name, VALID_PAYLOADS[name], RATIONALE)
        ok_n, why_n = check_defined_risk_only(prop)
        assert ok_n is True, f"{name}: {why_n}"
    assert get_config().defined_risk_only is True
    assert get_config().max_risk_per_trade_pct == 0.0


@pytest.mark.asyncio
async def test_paper_must_not_place_on_7496_with_shadow_cap_off(monkeypatch):
    from abcxauto.send import send_action

    _arm_shadow_cap_max_risk_off()
    cfg = Config(
        **{
            **get_config().__dict__,
            "trading_mode": "paper",
            "ibkr_port": 7496,
            "defined_risk_only": True,
            "max_risk_per_trade_pct": 0.0,
        }
    )
    monkeypatch.setattr("abcxauto.send.get_config", lambda: cfg)

    async def _must_not_execute(*_a, **_k):
        raise AssertionError("safe_execute must not run on paper + 7496")

    monkeypatch.setattr("abcxauto.send.safe_execute", _must_not_execute)

    class Conn:
        connected = True

        def place_order(self, *a, **k):
            raise AssertionError("7496 must not place")

        async def place_vertical_spread(self, **k):
            raise AssertionError("7496 must not place")

    result = await send_action(
        {
            "strategy": "vertical_spread",
            "params": _named_vertical(),
            "rationale": "must not reach live",
        },
        Conn(),
    )
    assert result["status"] == "blocked"
    assert result.get("reason_code") == "live_port_paper"
    assert "7496" in str(result.get("note") or "")
    assert cfg.defined_risk_only is True
    assert cfg.trading_mode == "paper"
    assert cfg.ibkr_port == 7496
    assert cfg.max_risk_per_trade_pct == 0.0
