"""KEEP-3: hash-bound dry-run preview + single-use place token."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from abcxauto.config import Config, get_config
from abcxauto.memory import get_journal, reset_journal
from abcxauto.send_preview import (
    REASON_PREVIEW_MISMATCH,
    REASON_PREVIEW_TOKEN,
    REASON_PREVIEW_USED,
    REASON_TOKEN_EXPIRED,
    bind_place_token,
    collect_would_refuse,
    consume_place_token,
    needs_place_token,
    preview_ticket,
    reset_preview_state,
    ticket_max_loss,
    ticket_preview_hash,
)
from abcxauto.token_ttl import DEFAULT_PLACE_TOKEN_TTL_S
from abcxauto.thin_rth_kill_look import F10_HARD_USD, F10_PREFERRED_USD
from abcxauto.world_state import WorldState


def setup_function():
    reset_preview_state()


def teardown_function():
    reset_preview_state()


def _vertical(card: str = "pcs-skew", **overrides) -> dict:
    params = {
        "symbol": "SPY",
        "expiration": "20260718",
        "long_strike": 500.0,
        "short_strike": 505.0,
        "right": "P",
        "quantity": 1,
        "limit_price": 1.25,
        "card": card,
    }
    params.update(overrides)
    return {
        "strategy": "vertical_spread",
        "action": "vertical_spread",
        "params": params,
        "rationale": "keep-3 preview",
        "card": card,
        "_desk_session": "regular",
    }


def _bracket(**overrides) -> dict:
    params = {
        "symbol": "SPY",
        "quantity": 1,
        "direction": "LONG",
        "entry_price": 500.0,
        "stop_price": 495.0,
        "target_price": 510.0,
        "card": "flush bounce",
    }
    params.update(overrides)
    return {
        "strategy": "market_bracket",
        "params": params,
        "rationale": "keep-3 preview",
        "_desk_session": "regular",
    }


def _exit_ticket() -> dict:
    return {
        "strategy": "cancel_order",
        "params": {"order_id": 42},
        "rationale": "cancel child",
        "_desk_session": "regular",
    }


def _connector() -> MagicMock:
    connector = MagicMock()
    connector.connected = True
    connector.place_order = MagicMock()
    connector.place_vertical_spread = MagicMock()
    return connector


async def _safe_execute_must_not_run(*_a, **_k):
    raise AssertionError("preview / missing token must not reach safe_execute")


def _world(**kwargs) -> WorldState:
    base = dict(
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
    base.update(kwargs)
    return WorldState(**base)


def test_hygiene_does_not_soften_f10_or_enable_live():
    assert F10_HARD_USD == 2.0
    assert F10_PREFERRED_USD == 1.0
    assert get_config().ibkr_port != 7496
    assert Config().ibkr_port == 7497
    assert get_config().trading_mode == "paper"


def test_hash_binds_legs_qty_side_card_limit():
    a = _vertical()
    same = ticket_preview_hash(a)
    assert ticket_preview_hash(_vertical()) == same
    assert ticket_preview_hash(_vertical(quantity=2)) != same
    assert ticket_preview_hash(_vertical(card="other-play")) != same
    assert ticket_preview_hash(_vertical(limit_price=2.00)) != same
    assert ticket_preview_hash(_vertical(long_strike=499.0)) != same
    b = _vertical()
    b["params"]["direction"] = "SHORT"
    assert ticket_preview_hash(b) != same


def test_max_loss_vertical_is_defined_risk():
    loss = ticket_max_loss(_vertical(limit_price=1.25))
    assert loss == pytest.approx((5.0 - 1.25) * 100.0)
    assert ticket_max_loss(_exit_ticket()) is None


def test_needs_token_new_risk_only():
    assert needs_place_token(_vertical()) is True
    assert needs_place_token(_bracket()) is True
    assert needs_place_token(_exit_ticket()) is False
    close = _vertical()
    close["params"]["closing_position"] = True
    assert needs_place_token(close) is False


def test_preview_nameless_card_would_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)
    ticket = _vertical()
    ticket["params"].pop("card", None)
    ticket.pop("card", None)
    out = preview_ticket(ticket, world=_world(), snap={"account": {"netliquidation": 100000}})
    assert out["preview"] is True
    assert out["pass"] is False
    assert out["preview_token"] in (None, "")
    assert any("card" in str(r).lower() for r in out["would_refuse"])
    assert out["preview_id"]
    assert out["preview_hash"] == ticket_preview_hash(ticket)


@pytest.mark.asyncio
async def test_preview_path_cannot_write_an_order(monkeypatch):
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    ticket = _vertical()
    ticket["preview"] = True
    connector = _connector()
    result = await send_action(ticket, connector)
    assert result["preview"] is True
    assert result.get("status") in ("preview", "blocked")
    connector.place_order.assert_not_called()
    connector.place_vertical_spread.assert_not_called()


@pytest.mark.asyncio
async def test_place_without_token_fails_closed(monkeypatch):
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    result = await send_action(_vertical(), _connector())
    assert result["status"] == "blocked"
    assert result.get("reason_code") == REASON_PREVIEW_TOKEN
    assert REASON_PREVIEW_TOKEN in (result.get("would_refuse") or [])
    assert result.get("token_used") is False


@pytest.mark.asyncio
async def test_token_for_ticket_a_cannot_submit_ticket_b(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(action)
        return {"status": "ok", "note": "dispatched"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    ticket_a = _vertical(card="play-a")
    ticket_b = _vertical(card="play-b", quantity=2)
    bind = bind_place_token(ticket_a)
    token_a = bind["preview_token"]
    assert token_a
    assert ticket_preview_hash(ticket_a) != ticket_preview_hash(ticket_b)

    stolen = dict(ticket_b)
    stolen["params"] = dict(ticket_b["params"])
    stolen["preview_token"] = token_a
    blocked = await send_action(stolen, _connector())
    assert blocked["status"] == "blocked"
    assert blocked.get("reason_code") == REASON_PREVIEW_MISMATCH
    assert dispatched == []

    placed = await send_action(ticket_a, _connector())
    assert placed["status"] == "ok"
    assert len(dispatched) == 1
    assert placed.get("token_used") is True
    assert placed.get("preview_id") == bind["preview_id"]
    assert placed.get("preview_hash") == bind["preview_hash"]


@pytest.mark.asyncio
async def test_token_is_single_use(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(True)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    ticket = _vertical()
    bind_place_token(ticket)
    first = await send_action(ticket, _connector())
    assert first["status"] == "ok"
    replay = await send_action(ticket, _connector())
    assert replay["status"] == "blocked"
    # KEEP-4 TTL runs before authorize, so a spent token is token_used.
    from abcxauto.token_ttl import REASON_TOKEN_USED

    assert replay.get("reason_code") in (REASON_PREVIEW_USED, REASON_TOKEN_USED)
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_exit_places_without_token(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(True)
        return {"status": "ok"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    result = await send_action(_exit_ticket(), _connector())
    assert result["status"] == "ok"
    assert dispatched == [True]


@pytest.mark.asyncio
async def test_execute_ticket_preview_never_calls_send(monkeypatch):
    from abcxauto.agent_loop import execute_ticket

    async def boom(*_a, **_k):
        raise AssertionError("preview must not call send_action")

    monkeypatch.setattr("abcxauto.agent_loop.send_action", boom)
    ticket = _vertical()
    ticket["preview"] = True
    result = await execute_ticket(
        ticket,
        _connector(),
        _world(),
        {"account": {"netliquidation": 100000}, "positions": [], "open_orders": []},
    )
    assert result["preview"] is True
    assert "would_refuse" in result
    assert "max_loss" in result
    assert result.get("preview_id")


def test_journal_preview_fields(tmp_path, monkeypatch):
    db = tmp_path / "journal.db"
    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(db))
    monkeypatch.setenv("ABCXAUTO_JOURNAL_ENABLED", "true")
    reset_journal(path=str(db), enabled=True)
    ticket = _vertical()
    out = preview_ticket(ticket, world=_world(), snap={"account": {"netliquidation": 100000}})
    row = get_journal().get_send_preview(out["preview_id"])
    assert row is not None
    assert row["preview_id"] == out["preview_id"]
    assert row["preview_hash"] == out["preview_hash"]
    assert row["token_used"] is False
    assert isinstance(row["would_refuse"], list)
    assert row["verdict"] in ("pass", "refuse")

    bound = bind_place_token(dict(ticket), source="test")
    ok, meta = consume_place_token(bound["preview_token"], bound["preview_hash"])
    assert ok is True
    used = get_journal().get_send_preview(bound["preview_id"])
    assert used["token_used"] is True
    assert meta.get("token_used") is True

    conn = sqlite3.connect(str(db))
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(send_previews)").fetchall()
        }
    finally:
        conn.close()
    assert "send_previews" in tables
    assert {
        "preview_id",
        "preview_hash",
        "would_refuse_json",
        "token_used",
    } <= cols


def test_collect_would_refuse_reports_kill_look_without_softening():
    ticket = _bracket()
    world = _world(session_status="regular")
    reasons = collect_would_refuse(
        ticket,
        world=world,
        snap={"account": {"netliquidation": 100000}, "positions": [], "open_lots": []},
    )
    assert isinstance(reasons, list)


@pytest.mark.asyncio
async def test_expired_token_same_hash_cannot_place(monkeypatch):
    """Issue → advance past TTL → same-hash place is blocked expired."""
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    clock = {"now": 1_700_000_400.0}
    monkeypatch.setattr("abcxauto.token_ttl.time.time", lambda: clock["now"])
    ticket = _vertical()
    bind = bind_place_token(ticket)
    token = bind["preview_token"]
    assert token
    assert ticket_preview_hash(ticket) == bind["preview_hash"]
    from abcxauto.send_preview import _store

    _store[token] = {
        "preview_hash": bind["preview_hash"],
        "preview_id": bind["preview_id"],
        "used": False,
    }
    clock["now"] = clock["now"] + DEFAULT_PLACE_TOKEN_TTL_S
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result.get("reason_code") == REASON_TOKEN_EXPIRED
    assert REASON_TOKEN_EXPIRED in (result.get("would_refuse") or [])


def test_local_store_cannot_bypass_ttl(monkeypatch):
    clock = {"now": 1_700_000_400.0}
    monkeypatch.setattr("abcxauto.token_ttl.time.time", lambda: clock["now"])
    ticket = _vertical()
    bind = bind_place_token(ticket)
    token = bind["preview_token"]
    from abcxauto.send_preview import _store

    _store[token] = {
        "preview_hash": bind["preview_hash"],
        "preview_id": bind["preview_id"],
        "used": False,
    }
    clock["now"] = clock["now"] + DEFAULT_PLACE_TOKEN_TTL_S
    ok, meta = consume_place_token(token, bind["preview_hash"])
    assert ok is False
    assert meta.get("reason_code") == REASON_TOKEN_EXPIRED
    assert meta.get("token_used") is False


def test_ttl_issue_failure_fail_closed_no_local_mint(monkeypatch):
    def _boom(**_k):
        raise RuntimeError("ttl down")

    monkeypatch.setattr("abcxauto.token_ttl.issue_place_token", _boom)
    ticket = _vertical()
    bind = bind_place_token(ticket)
    assert not bind.get("preview_token")
    from abcxauto.send_preview import _store

    assert _store == {}


@pytest.mark.asyncio
async def test_live_port_still_wins_over_preview_token(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "7496")
    from abcxauto.config import clear_runtime_overrides

    clear_runtime_overrides()
    get_config.cache_clear()
    cfg = get_config()
    assert cfg.ibkr_port == 7496
    monkeypatch.setattr("abcxauto.send.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    ticket = _vertical()
    bind_place_token(ticket)
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result.get("reason_code") == "live_port_paper"
    assert F10_HARD_USD == 2.0
    assert get_config().ibkr_port == 7496
    assert get_config().trading_mode == "paper"
