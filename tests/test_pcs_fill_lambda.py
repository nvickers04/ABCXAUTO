"""PCS fill-λ: formulas, mid-never-fill, stop_1x, journal events."""

from __future__ import annotations

import inspect
import sqlite3

import pytest

from abcxauto.llm import SYSTEM_PROMPT
from abcxauto.memory import get_journal
from abcxauto.pcs_fill_lambda import (
    EVENT_FILL,
    EVENT_LIFECYCLE_END,
    EVENT_QUOTE_SNAP,
    EVENT_SCORE_MARK,
    EVENT_TICKET_SUBMIT,
    LAMBDA_DECLARED,
    LAMBDA_STRESS,
    MANAGE_STOP_MULT,
    QUOTE_IBKR_BAG,
    QUOTE_IBKR_LEGS,
    QUOTE_UNAVAILABLE,
    capture_pcs_vertical_quote,
    c_score,
    d_score,
    include_in_pnl_mean,
    is_pcs_ticket,
    journal_pcs_post_send,
    journal_pcs_pre_send,
    kill_evidence_valid,
    lambda_implied_open,
    legs_sum_credit_quote,
    manage_hits,
    manage_levels,
    mid_or_lambda0_fill,
    model_cost_allocation_ok,
    net_combo_fill,
    quote_source_of,
    score_mark_payload,
)
from tests.test_no_clerk_process import SYSTEM_PROMPT_LOCK

PCS_OPEN = {
    "symbol": "SPY",
    "expiration": "20260918",
    "long_strike": 500.0,
    "short_strike": 505.0,
    "right": "P",
    "quantity": 1,
    "order_type": "LMT",
    "limit_price": 0.85,
    "card": "pcs-skew",
}


def test_system_prompt_is_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_module_does_not_import_risk():
    import abcxauto.pcs_fill_lambda as mod

    src = inspect.getsource(mod)
    assert "risk_gates" not in src
    assert "self_tune" not in src
    assert "from abcxauto.protect" not in src
    assert "from abcxauto.config" not in src


def test_is_pcs_ticket_is_put_vertical_pcs_skew_only():
    assert is_pcs_ticket("vertical_spread", PCS_OPEN) is True
    call = dict(PCS_OPEN, right="C")
    assert is_pcs_ticket("vertical_spread", call) is False
    other = dict(PCS_OPEN, card="other-card")
    assert is_pcs_ticket("vertical_spread", other) is False
    assert is_pcs_ticket("cash_secured_put", PCS_OPEN) is False
    assert is_pcs_ticket("close_option", PCS_OPEN) is False


def test_lambda_implied_open_and_null_s():
    assert lambda_implied_open(1.00, 0.90, 0.10) == pytest.approx(1.0)
    assert lambda_implied_open(1.00, 1.00, 0.10) == pytest.approx(0.0)
    assert lambda_implied_open(1.00, 0.90, None) is None
    assert lambda_implied_open(1.00, 0.90, 0.0) is None


def test_c_score_is_min_actual_vs_declared_lambda():
    # Paper mid fill C=M=1.00, S=0.10 → conservative 0.95, stress 0.90
    assert c_score(1.00, 1.00, 0.10, lam=LAMBDA_DECLARED) == pytest.approx(0.95)
    assert c_score(1.00, 1.00, 0.10, lam=LAMBDA_STRESS) == pytest.approx(0.90)
    # Fill at bid is already conservative
    assert c_score(0.90, 1.00, 0.10, lam=LAMBDA_DECLARED) == pytest.approx(0.90)
    assert c_score(1.00, None, 0.10) is None


def test_d_score_is_max_actual_vs_lambda_tax():
    assert d_score(1.00, 1.00, 0.10, lam=LAMBDA_DECLARED) == pytest.approx(1.05)
    assert d_score(1.00, 1.00, 0.10, lam=LAMBDA_STRESS) == pytest.approx(1.10)
    assert d_score(1.12, 1.00, 0.10, lam=LAMBDA_DECLARED) == pytest.approx(1.12)


def test_mid_and_lambda0_cannot_graduate_as_fill():
    assert mid_or_lambda0_fill(1.00, 1.00, 0.0) is True
    assert mid_or_lambda0_fill(0.90, 1.00, 1.0) is False
    assert kill_evidence_valid(
        quote_source=QUOTE_IBKR_BAG, mid=1.00, has_real_fill=False
    ) is False
    assert kill_evidence_valid(
        quote_source=QUOTE_UNAVAILABLE, mid=None, has_real_fill=False
    ) is False
    assert kill_evidence_valid(
        quote_source=QUOTE_IBKR_BAG,
        mid=1.00,
        has_real_fill=True,
        fill_price=1.00,
        lambda_implied=0.0,
    ) is False
    assert kill_evidence_valid(
        quote_source=QUOTE_UNAVAILABLE,
        mid=None,
        has_real_fill=True,
        fill_price=0.80,
        lambda_implied=None,
    ) is True
    assert kill_evidence_valid(
        quote_source=QUOTE_IBKR_BAG,
        mid=1.00,
        has_real_fill=True,
        fill_price=0.90,
        lambda_implied=1.0,
    ) is True


def test_stop_1x_is_not_stop_2x():
    assert MANAGE_STOP_MULT == 1.0
    levels = manage_levels(1.00)
    assert levels["stop_1x"] == pytest.approx(1.00)
    assert levels["tp50"] == pytest.approx(0.50)
    assert "stop_2x" not in levels
    first, hits = manage_hits(debit_mark=1.00, fill_credit=1.00, dte=30)
    assert first == "stop_1x"
    assert "stop_1x" in hits
    assert "stop_2x" not in hits
    # 2× debit is still stop_1x (already through 1.0×), never a 2× rule
    first2, hits2 = manage_hits(debit_mark=2.00, fill_credit=1.00, dte=30)
    assert first2 == "stop_1x"
    assert "stop_2x" not in hits2
    first_pt, hits_pt = manage_hits(debit_mark=0.40, fill_credit=1.00, dte=30)
    assert first_pt == "tp50"
    first_dte, hits_dte = manage_hits(debit_mark=0.70, fill_credit=1.00, dte=21)
    assert first_dte == "dte21"
    assert "dte21" in hits_dte


def test_unfilled_excluded_from_pnl_mean_model_cost_hook_stays():
    assert include_in_pnl_mean(filled=False, has_real_fill=False) is False
    assert include_in_pnl_mean(filled=True, has_real_fill=True) is True
    assert model_cost_allocation_ok(False) is True
    assert model_cost_allocation_ok(True) is True


def test_legs_sum_and_quote_source():
    summed = legs_sum_credit_quote(
        {"bid": 0.40, "ask": 0.50, "last": 0.45},
        {"bid": 1.20, "ask": 1.30, "last": 1.25},
    )
    assert summed["quote_source"] == QUOTE_IBKR_LEGS
    assert summed["bid"] == pytest.approx(0.70)
    assert summed["ask"] == pytest.approx(0.90)
    assert summed["mid"] == pytest.approx(0.80)
    assert summed["half_spread"] == pytest.approx(0.10)
    assert quote_source_of(bag={"bid": 0.80, "ask": 0.90}) == QUOTE_IBKR_BAG
    assert quote_source_of(bag={}, legs_ok=True) == QUOTE_IBKR_LEGS
    assert quote_source_of(bag={"mid": 0.85}, legs_ok=False) == QUOTE_UNAVAILABLE


def test_score_mark_open_rejects_mid_fill():
    quote = {
        "quote_source": QUOTE_IBKR_BAG,
        "bid": 0.80,
        "ask": 0.90,
        "mid": 0.85,
        "half_spread": 0.05,
    }
    mid_fill = score_mark_payload(
        side="open",
        quote=quote,
        fill={"credit": 0.85, "fill_price": 0.85, "has_real_fill": True},
    )
    assert mid_fill["lambda_implied"] == pytest.approx(0.0)
    assert mid_fill["c_score"] == pytest.approx(0.825)
    assert mid_fill["c_score_stress"] == pytest.approx(0.80)
    assert mid_fill["evidence_valid"] is False
    assert mid_fill["include_in_pnl_mean"] is True  # filled, but evidence still invalid

    bid_fill = score_mark_payload(
        side="open",
        quote=quote,
        fill={"credit": 0.80, "fill_price": 0.80, "has_real_fill": True},
    )
    assert bid_fill["lambda_implied"] == pytest.approx(1.0)
    assert bid_fill["evidence_valid"] is True


def test_net_combo_fill_from_opt_legs():
    net = net_combo_fill(
        [
            {
                "exec_id": "a",
                "order_id": 77,
                "sec_type": "OPT",
                "side": "SLD",
                "quantity": 1,
                "price": 1.20,
                "commission": 0.65,
            },
            {
                "exec_id": "b",
                "order_id": 77,
                "sec_type": "OPT",
                "side": "BOT",
                "quantity": 1,
                "price": 0.40,
                "commission": 0.65,
            },
        ]
    )
    assert net is not None
    assert net["credit"] == pytest.approx(0.80)
    assert net["commission"] == pytest.approx(1.30)
    assert net["has_real_fill"] is True


@pytest.mark.asyncio
async def test_capture_prefers_bag_then_legs():
    class BagFirst:
        async def get_live_vertical_bag_quote(self, *_a, **_k):
            return {"bid": 0.80, "ask": 0.90, "last": 0.84}

        async def get_live_option_quote(self, *_a, **_k):
            raise AssertionError("legs must not run when BAG printed")

    bag = await capture_pcs_vertical_quote(BagFirst(), PCS_OPEN)
    assert bag["quote_source"] == QUOTE_IBKR_BAG
    assert bag["mid"] == pytest.approx(0.85)

    class LegsOnly:
        async def get_live_vertical_bag_quote(self, *_a, **_k):
            return {"error": "no IBKR tick yet"}

        async def get_live_option_quote(self, symbol, expiration, strike, right):
            if float(strike) == 500.0:
                return {"bid": 0.40, "ask": 0.50, "last": 0.45}
            return {"bid": 1.20, "ask": 1.30, "last": 1.25}

    legs = await capture_pcs_vertical_quote(LegsOnly(), PCS_OPEN)
    assert legs["quote_source"] == QUOTE_IBKR_LEGS
    assert legs["mid"] == pytest.approx(0.80)

    class Dead:
        pass

    dead = await capture_pcs_vertical_quote(Dead(), PCS_OPEN)
    assert dead["quote_source"] == QUOTE_UNAVAILABLE
    assert dead["mid"] is None


def test_journal_events_open_mid_fill_not_evidence(tmp_path):
    journal = get_journal()
    quote = {
        "quote_source": QUOTE_IBKR_BAG,
        "bid": 0.80,
        "ask": 0.90,
        "mid": 0.85,
        "half_spread": 0.05,
        "last": 0.85,
    }

    class Prop:
        strategy = "vertical_spread"
        card = "pcs-skew"
        params = PCS_OPEN

    lid = journal_pcs_pre_send(journal, Prop(), quote, proposal_id=9)
    assert lid
    journal_pcs_post_send(
        journal,
        Prop(),
        quote,
        {"success": True, "order_id": 77, "avg_fill_price": 0.85, "filled": True},
        True,
        proposal_id=9,
        lifecycle_id=lid,
    )
    events = list(reversed(journal.pcs_events(lifecycle_id=lid, limit=20)))
    names = [e["event"] for e in events]
    assert names[0] == EVENT_QUOTE_SNAP
    assert EVENT_TICKET_SUBMIT in names
    assert EVENT_FILL in names
    assert EVENT_SCORE_MARK in names
    mark = next(e for e in events if e["event"] == EVENT_SCORE_MARK)
    assert mark["evidence_valid"] is False
    assert mark["lambda_implied"] == pytest.approx(0.0)
    assert mark["c_score"] == pytest.approx(0.825)
    with sqlite3.connect(journal.path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM pcs_fill_events").fetchone()[0]
    assert n >= 4


@pytest.mark.asyncio
async def test_execute_proposal_snaps_pcs_bag_not_stk_mid(monkeypatch):
    from abcxauto.config import Config, get_config
    from abcxauto.executor import execute_proposal
    from abcxauto.proposals import validate_proposal

    base = get_config()
    monkeypatch.setattr(
        "abcxauto.executor.get_config",
        lambda: Config(
            **{
                **base.__dict__,
                "risk_gates_enabled": False,
                "max_arena_concentration_pct": 0,
                "defined_risk_only": False,
            }
        ),
    )

    class Gateway:
        connected = True

        async def get_live_vertical_bag_quote(self, *_a, **_k):
            return {"bid": 0.80, "ask": 0.90, "last": 0.85, "mid": 0.85}

        async def get_live_quote(self, symbol, fresh=False):
            raise AssertionError("PCS must not use underlying STK last")

        async def place_vertical_spread(self, **kwargs):
            assert "card" not in kwargs
            return {
                "success": True,
                "order_id": 77,
                "note": "IBKR combo (BAG)",
                "avg_fill_price": 0.80,
                "filled": True,
            }

    proposal = validate_proposal("vertical_spread", dict(PCS_OPEN), "pcs open")
    result = await execute_proposal(proposal, Gateway())
    assert result["success"] is True
    rows = get_journal().pcs_events(limit=20)
    assert any(r["event"] == EVENT_QUOTE_SNAP for r in rows)
    mark = next(r for r in rows if r["event"] == EVENT_SCORE_MARK)
    assert mark["quote_source"] == QUOTE_IBKR_BAG
    assert mark["lambda_implied"] == pytest.approx(1.0)
    assert mark["evidence_valid"] is True
    send = get_journal().recent_send_marks()[0]
    assert send["bid"] == 0.80
    assert send["ask"] == 0.90
    assert send["fill_price"] == 0.80


def test_unfilled_lifecycle_keeps_model_cost_hook(tmp_path):
    journal = get_journal()

    class Prop:
        strategy = "vertical_spread"
        card = "pcs-skew"
        params = PCS_OPEN

    quote = {"quote_source": QUOTE_UNAVAILABLE, "bid": None, "ask": None, "mid": None}
    lid = journal_pcs_pre_send(journal, Prop(), quote, proposal_id=3)
    journal_pcs_post_send(
        journal,
        Prop(),
        quote,
        {"error": "not filled", "status": "rejected"},
        False,
        proposal_id=3,
        lifecycle_id=lid,
    )
    end = next(
        e for e in journal.pcs_events(lifecycle_id=lid) if e["event"] == EVENT_LIFECYCLE_END
    )
    assert end["include_in_pnl_mean"] is False
    assert end.get("model_cost_ok") is True
