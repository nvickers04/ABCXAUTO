from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from abcxauto.agent_loop import execute_ticket, gate_ticket, is_new_risk
from abcxauto.look_snapshot import REASON_CODE, begin_look
from abcxauto.world_state import WorldState

# RTH sendable enum after fix/sendable-structures-on-rth (71f5f81).
# new-risk-on-open is the is_new_risk classification with closing_position
# omitted / false. roll_option and oca stay out of _NEW_RISK on purpose:
# roll is lifecycle of an existing contract; oca is a grouping ticket that
# can be entry or management. Do not guess them into the entry set.
RTH_SENDABLE_NEW_RISK_ON_OPEN = (
    ('vertical_spread', True),
    ('iron_condor', True),
    ('iron_butterfly', True),
    ('butterfly', True),
    ('calendar_spread', True),
    ('diagonal_spread', True),
    ('cash_secured_put', True),
    ('covered_call', True),
    ('protective_put', True),
    ('collar', True),
    ('roll_option', False),
    ('buy_option', True),
    ('straddle', True),
    ('strangle', True),
    ('bracket', True),
    ('market_bracket', True),
    ('oca', False),
)

OVERLAY_OPENS = ('covered_call', 'collar', 'protective_put')


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status='regular',
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=37000.0,
        daily_pnl=0.0,
        positions=[],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture='balanced',
        effective_posture='balanced',
        gates={},
        envelope={},
        regime={},
        portfolio_risk={},
        working_thesis='',
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def _overlay_params(strat: str, *, closing: bool, card: str | None, limit_price: float | None = 0.55) -> dict:
    params: dict = {
        'symbol': 'SPY',
        'expiration': '20260718',
        'shares': 100,
    }
    if strat == 'collar':
        params['put_strike'] = 490.0
        params['call_strike'] = 510.0
    elif strat == 'covered_call':
        params['strike'] = 510.0
    else:
        params['strike'] = 490.0
    if card is not None:
        params['card'] = card
    if limit_price is not None:
        params['limit_price'] = limit_price
    if closing:
        params['closing_position'] = True
    return params


def _overlay_ticket(strat: str, *, closing: bool = False, card: str | None = 'overlay play', limit_price: float | None = 0.55) -> dict:
    return {
        'action': strat,
        'strategy': strat,
        'params': _overlay_params(strat, closing=closing, card=card, limit_price=limit_price),
        'rationale': 'overlay ticket',
    }


def _long_spy() -> list[dict]:
    return [{
        'symbol': 'SPY',
        'secType': 'STK',
        'sec_type': 'STK',
        'quantity': 100,
        'conId': 7,
    }]


def _empty_look_snap(positions: list[dict]) -> dict:
    snap = {
        'account': {'netliquidation': 37000.0},
        'positions': positions,
        'open_orders': [],
        'book_unreliable': False,
    }
    begin_look(snap)
    return snap


def _capture_send(monkeypatch) -> list:
    sent: list = []

    async def capture(action, _conn):
        sent.append(action)
        return {'status': 'ok'}

    monkeypatch.setattr('abcxauto.agent_loop.send_action', capture)
    monkeypatch.setattr(
        'abcxauto.thin_rth_kill_look.kill_look_send_block',
        lambda *_a, **_k: None,
    )
    return sent


@pytest.mark.parametrize('strat', OVERLAY_OPENS)
def test_overlay_open_is_new_risk(strat: str):
    assert is_new_risk(strat) is True
    assert is_new_risk(strat, {}) is True
    assert is_new_risk(strat, {'closing_position': False}) is True


@pytest.mark.parametrize('strat', OVERLAY_OPENS)
def test_overlay_close_is_not_new_risk(strat: str):
    assert is_new_risk(strat, {'closing_position': True}) is False


@pytest.mark.parametrize('strat, expect', RTH_SENDABLE_NEW_RISK_ON_OPEN)
def test_rth_sendable_enum_new_risk_on_open(strat: str, expect: bool):
    assert is_new_risk(strat) is expect
    assert is_new_risk(strat, {'closing_position': False}) is expect
    assert is_new_risk(strat, {'closing_position': True}) is False


@pytest.mark.parametrize('strat', OVERLAY_OPENS)
def test_overlay_open_requires_named_card(strat: str):
    world = _world()
    blocked, forced = gate_ticket(_overlay_ticket(strat, card=None), world)
    assert blocked == 'blocked'
    note = str((forced or {}).get('note') or '')
    assert 'params.card' in note


@pytest.mark.parametrize('strat', OVERLAY_OPENS)
def test_overlay_open_blocked_when_book_unreliable(strat: str):
    world = _world(gates={'book_unreliable': True})
    blocked, forced = gate_ticket(_overlay_ticket(strat), world)
    assert blocked == 'blocked'
    assert 'unreliable' in str((forced or {}).get('note') or '').lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('strat', OVERLAY_OPENS)
async def test_overlay_open_empty_cache_is_number_gated(monkeypatch, strat: str):
    pos = _long_spy()
    sent = _capture_send(monkeypatch)
    result = await execute_ticket(
        _overlay_ticket(strat, closing=False, card='overlay play', limit_price=0.55),
        MagicMock(),
        _world(positions=pos),
        _empty_look_snap(pos),
    )
    assert result.get('status') == 'blocked'
    assert result.get('reason_code') == REASON_CODE == 'stale_or_invented_number'
    note = str(result.get('note') or '')
    assert 'limit_price=0.55' in note
    assert 'closing_position=false' in note
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('strat', OVERLAY_OPENS)
async def test_overlay_close_empty_cache_is_not_number_gated(monkeypatch, strat: str):
    pos = _long_spy()
    sent = _capture_send(monkeypatch)
    result = await execute_ticket(
        _overlay_ticket(strat, closing=True, card=None, limit_price=0.55),
        MagicMock(),
        _world(positions=pos),
        _empty_look_snap(pos),
    )
    assert result.get('reason_code') != REASON_CODE
    assert 'stale_or_invented_number' not in str(result.get('note') or '')
    # Later inventory / geometry may still refuse; this gate must not.
    assert sent == [] or result.get('status') == 'ok'
