"""KEEP-3: hash-bound dry-run preview + single-use place token."""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from abcxauto.config import Config, get_config
from abcxauto.memory import get_journal, reset_journal
from abcxauto.mode_size import MODE_SIZE_CEILING_EXPLORE
from abcxauto.send_preview import (
    REASON_PREVIEW_MISMATCH,
    REASON_PREVIEW_TOKEN,
    REASON_PREVIEW_USED,
    REASON_TOKEN_EXPIRED,
    authorize_place,
    bind_place_token,
    collect_would_refuse,
    consume_place_token,
    needs_place_token,
    preview_ticket,
    reset_preview_state,
    ticket_max_loss,
    ticket_preview_hash,
)
from abcxauto.token_ttl import DEFAULT_PLACE_TOKEN_TTL_S, issue_place_token
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
    assert F10_HARD_USD == 15.0
    assert F10_PREFERRED_USD == 10.0
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
    """vertical_spread with no params.card refuses like gate_ticket; exits do not."""
    from abcxauto.risk_gates import new_risk_card_error

    monkeypatch.setenv("ABCXAUTO_JOURNAL_PATH", str(tmp_path / "journal.db"))
    reset_journal(path=str(tmp_path / "journal.db"), enabled=True)
    want = new_risk_card_error("")
    assert want == "new risk requires params.card naming a play"

    ticket = _vertical()
    ticket["params"].pop("card", None)
    ticket.pop("card", None)
    # world=None: still refuse via always-armed path (not invent a card).
    reasons = collect_would_refuse(ticket, world=None, snap={})
    assert want in reasons

    out = preview_ticket(ticket, world=_world(), snap={"account": {"netliquidation": 100000}})
    assert out["preview"] is True
    assert out["pass"] is False
    assert out["preview_token"] in (None, "")
    assert want in out["would_refuse"]
    assert out["preview_id"]
    assert out["preview_hash"] == ticket_preview_hash(ticket)

    exit_reasons = collect_would_refuse(_exit_ticket(), world=None, snap={})
    assert want not in exit_reasons
    close = _vertical()
    close["params"]["closing_position"] = True
    close["params"].pop("card", None)
    close.pop("card", None)
    assert want not in collect_would_refuse(close, world=None, snap={})


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


def test_preview_mirrors_always_armed_refusals_when_gates_off(monkeypatch):
    """Paper risk_gates_enabled=False still previews the #200 always-armed refuses."""
    from dataclasses import replace

    cfg = replace(
        get_config(),
        risk_gates_enabled=False,
        sizing_floors=False,
        defined_risk_only=True,
        cash_only=True,
        daily_loss_limit_pct=25.0,
        trading_mode="paper",
        ibkr_port=7497,
    )
    monkeypatch.setattr("abcxauto.config.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "abcxauto.look_snapshot.check_ticket_numbers",
        lambda *_a, **_k: (True, "", ""),
    )

    class _Gate:
        is_halted = False
        halt_reason = ""

        def halt(self, *_a, **_k):
            raise AssertionError("preview must not trip the halt latch")

    gate = _Gate()
    monkeypatch.setattr("abcxauto.risk_gates.get_risk_gate", lambda: gate)

    healthy = {
        "account": {
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 100_000.0,
            "AvailableFunds": 100_000.0,
        },
        "positions": [],
        "open_lots": [],
    }
    world = _world(session_status="regular")

    unlimited = {
        "strategy": "ratio_spread",
        "params": {
            "symbol": "SPY",
            "expiration": "20260718",
            "long_strike": 500.0,
            "short_strike": 510.0,
            "right": "C",
            "ratio": 2,
            "quantity": 1,
            "card": "preview-ratio",
        },
        "rationale": "keep-3 preview",
        "_desk_session": "regular",
    }
    defined = collect_would_refuse(unlimited, world=world, snap=healthy)
    assert any("defined_risk_only" in str(r) for r in defined)

    gate.is_halted = True
    gate.halt_reason = "daily loss breach"
    halted = collect_would_refuse(_vertical(), world=world, snap=healthy)
    assert any("halted" in str(r).lower() for r in halted)
    gate.is_halted = False

    loss_snap = {
        "account": {
            "netliquidation": 100_000.0,
            "dailypnl": -25_000.0,
            "TotalCashValue": 100_000.0,
        },
        "positions": [],
        "open_lots": [],
    }
    daily = collect_would_refuse(_vertical(), world=world, snap=loss_snap)
    assert any("daily_loss" in str(r).lower() for r in daily)

    cash_snap = {
        "account": {
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 50.0,
        },
        "positions": [],
        "open_lots": [],
    }
    cash = collect_would_refuse(_vertical(), world=world, snap=cash_snap)
    assert any("cash" in str(r).lower() for r in cash)

    short = collect_would_refuse(
        _bracket(direction="SHORT"), world=world, snap=healthy
    )
    assert any("short" in str(r).lower() or "cash-only" in str(r).lower() for r in short)

    exits = collect_would_refuse(_exit_ticket(), world=world, snap=loss_snap)
    assert not any("daily_loss" in str(r).lower() for r in exits)
    assert not any("defined_risk" in str(r).lower() for r in exits)
    assert not any("cash" in str(r).lower() for r in exits)
    assert not any("halted" in str(r).lower() for r in exits)


def test_collect_would_refuse_kill_look_exception_fail_closes(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("kill-look exploded")

    monkeypatch.setattr("abcxauto.thin_rth_kill_look.kill_look_send_block", boom)
    reasons = collect_would_refuse(
        _exit_ticket(),
        world=_world(session_status="regular"),
        snap={"account": {"netliquidation": 100000}, "positions": [], "open_lots": []},
    )
    assert any("kill-look gate failed closed" in str(r) for r in reasons)


@pytest.mark.asyncio
async def test_h_ttl_expired_same_hash_cannot_place(monkeypatch):
    """H-TTL: issue → past TTL → same-hash place is blocked expired."""
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action
    import abcxauto.send_preview as sp

    assert not hasattr(sp, "_store")
    clock = {"now": 1_700_000_400.0}
    monkeypatch.setattr("abcxauto.token_ttl.time.time", lambda: clock["now"])
    ticket = _vertical()
    bind = bind_place_token(ticket)
    token = bind["preview_token"]
    assert token
    assert ticket_preview_hash(ticket) == bind["preview_hash"]
    clock["now"] = clock["now"] + DEFAULT_PLACE_TOKEN_TTL_S
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result.get("reason_code") == REASON_TOKEN_EXPIRED
    assert REASON_TOKEN_EXPIRED in (result.get("would_refuse") or [])


def test_h_ttl_no_local_store_and_expiry_refuses_before_place(monkeypatch):
    """H-TTL: no send_preview token table; KEEP-4 expiry refuses consume."""
    import abcxauto.send_preview as sp

    assert not hasattr(sp, "_store")
    clock = {"now": 1_700_000_400.0}
    monkeypatch.setattr("abcxauto.token_ttl.time.time", lambda: clock["now"])
    ticket = _vertical()
    bind = bind_place_token(ticket)
    token = bind["preview_token"]
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
    import abcxauto.send_preview as sp

    assert not hasattr(sp, "_store")


@pytest.mark.asyncio
async def test_expired_keep4_token_cannot_place(monkeypatch):
    """H0: KEEP-4 issue_place_token past TTL cannot place."""
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    now = 1_700_000_400.0
    ticket = _vertical()
    digest = ticket_preview_hash(ticket)
    rec = issue_place_token(
        kind="preview",
        payload={"preview_hash": digest, "preview_id": "prv_keep4_expired"},
        ttl_s=60,
        now=now,
    )
    ticket["preview_token"] = rec["id"]
    monkeypatch.setattr("abcxauto.token_ttl.time.time", lambda: now + 60)
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result.get("reason_code") == REASON_TOKEN_EXPIRED
    assert REASON_TOKEN_EXPIRED in (result.get("would_refuse") or [])


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
    assert F10_HARD_USD == 15.0
    assert get_config().ibkr_port == 7496
    assert get_config().trading_mode == "paper"


def test_market_bracket_no_entry_is_not_a_notional_refuse(monkeypatch):
    """88 shares is not a mode_size refuse. Cash is the spend cap."""
    from abcxauto.config import update_risk_config
    from abcxauto.self_tune import apply_self_tune

    update_risk_config(max_risk_per_trade_pct=2.0, persist=True, _skip_clamp=True)
    apply_self_tune({"size_pct_nl": MODE_SIZE_CEILING_EXPLORE}, persist=True)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "abcxauto.look_snapshot.check_ticket_numbers",
        lambda *_a, **_k: (True, "", ""),
    )
    monkeypatch.setattr(
        "abcxauto.desk_mode.new_risk_research_error",
        lambda *_a, **_k: None,
    )

    nl = 32_361.08
    last = 359.40
    # qty 88 ≈ 98% of NL. Notional percent does not refuse it.
    ticket = _bracket(
        symbol="AVGO",
        quantity=88,
        stop_price=356.2,
        target_price=366.8,
        card="avgo long",
    )
    ticket["params"].pop("entry_price", None)
    assert "entry_price" not in ticket["params"]
    assert "limit_price" not in ticket["params"]
    assert "price_hint" not in ticket["params"]

    world = _world(net_liquidation=nl, session_status="regular")
    snap = {
        "account": {"netliquidation": nl},
        "positions": [],
        "open_lots": [],
        "ibkr_live_quotes": {"AVGO": last},
    }
    reasons = collect_would_refuse(ticket, world=world, snap=snap)
    joined = " ".join(str(r) for r in reasons)
    assert "mode_size" not in joined, reasons
    assert ticket["params"]["quantity"] == 88


def test_preview_hash_stable_when_quantity_not_rewritten():
    """Preview path must not rewrite qty — hash stays bound to the ticket."""
    ticket = _bracket(
        symbol="AVGO",
        quantity=88,
        stop_price=356.2,
        target_price=366.8,
        card="avgo long",
    )
    ticket["params"].pop("entry_price", None)
    before = ticket_preview_hash(ticket)
    digest = ticket_preview_hash(
        {
            "strategy": "market_bracket",
            "params": dict(ticket["params"]),
        }
    )
    assert digest == before
    assert ticket["params"]["quantity"] == 88
    assert ticket_preview_hash(ticket) == before


def test_mismatch_includes_place_hash_different_from_preview_hash():
    """On preview_token_mismatch the model must see both hashes."""
    previewed = _bracket(quantity=88, card="avgo long")
    bind = bind_place_token(previewed)
    assert bind["preview_token"]
    preview_hash = bind["preview_hash"]
    assert preview_hash == ticket_preview_hash(previewed)

    placed = _bracket(quantity=7, card="avgo long")
    placed["preview_token"] = bind["preview_token"]
    place_hash = ticket_preview_hash(placed)
    assert place_hash != preview_hash

    blocked = authorize_place(placed)
    assert blocked is not None
    assert blocked.get("reason_code") == REASON_PREVIEW_MISMATCH
    assert blocked.get("preview_hash") == preview_hash
    assert blocked.get("place_hash") == place_hash
    assert blocked.get("place_hash") != blocked.get("preview_hash")
    assert blocked.get("token_used") is False

    # Mismatch must not spend the token.
    ok, _meta = consume_place_token(bind["preview_token"], preview_hash)
    assert ok is True


def test_size_pct_nl_fill_keeps_preview_hash():
    """Filling quantity from size_pct_nl must not break a passing preview."""
    ticket = _bracket(quantity=1, card="sized")
    ticket["params"].pop("quantity", None)
    ticket["params"]["size_pct_nl"] = 5.0
    assert ticket_preview_hash(ticket)
    bind = bind_place_token(ticket)
    placed = {
        "strategy": ticket["strategy"],
        "action": ticket.get("action"),
        "params": dict(ticket["params"]),
        "preview_token": bind["preview_token"],
        "_hash_as_sent_qty": True,
    }
    placed["params"]["quantity"] = 7
    assert ticket_preview_hash(placed) == bind["preview_hash"]
    assert authorize_place(placed) is None


def test_size_cash_refuse_names_print_and_fit_qty(monkeypatch):
    """Cash refuse must name the live print and a quantity that fits."""
    from dataclasses import replace

    cfg = replace(
        get_config(),
        risk_gates_enabled=False,
        sizing_floors=False,
        cash_only=True,
        trading_mode="paper",
        ibkr_port=7497,
    )
    monkeypatch.setattr("abcxauto.config.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "abcxauto.look_snapshot.check_ticket_numbers",
        lambda *_a, **_k: (True, "", ""),
    )
    monkeypatch.setattr(
        "abcxauto.desk_mode.new_risk_research_error",
        lambda *_a, **_k: None,
    )

    # market_bracket notional uses max(stop, target)=110 -> 11k; 5k cash -> fit_qty=45.
    ticket = _bracket(
        symbol="AVGO",
        quantity=100,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        card="cash fit",
    )
    world = _world(net_liquidation=100_000.0, session_status="regular")
    snap = {
        "account": {
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 5_000.0,
        },
        "positions": [],
        "open_lots": [],
        "ibkr_live_quotes": {"AVGO": 100.0},
    }
    reasons = collect_would_refuse(ticket, world=world, snap=snap)
    joined = " ".join(str(r) for r in reasons)
    assert "size_cash" in joined, reasons
    assert "print=100" in joined, reasons
    assert "fit_qty=45" in joined, reasons


def test_preview_cash_gate_is_total_cash_not_available_funds(monkeypatch):
    """Preview cash_only must match place: TotalCashValue only, never margin."""
    from dataclasses import replace

    cfg = replace(
        get_config(),
        risk_gates_enabled=False,
        sizing_floors=False,
        cash_only=True,
        trading_mode="paper",
        ibkr_port=7497,
    )
    monkeypatch.setattr("abcxauto.config.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.risk_gates.get_config", lambda: cfg)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "abcxauto.look_snapshot.check_ticket_numbers",
        lambda *_a, **_k: (True, "", ""),
    )
    monkeypatch.setattr(
        "abcxauto.desk_mode.new_risk_research_error",
        lambda *_a, **_k: None,
    )

    ticket = _bracket(
        symbol="AVGO",
        quantity=100,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        card="cash vs margin",
    )
    world = _world(net_liquidation=100_000.0, session_status="regular")
    # Margin covers 11k notional; cash does not.
    snap = {
        "account": {
            "netliquidation": 100_000.0,
            "dailypnl": 0.0,
            "TotalCashValue": 5_000.0,
            "AvailableFunds": 80_000.0,
        },
        "positions": [],
        "open_lots": [],
        "ibkr_live_quotes": {"AVGO": 100.0},
    }
    reasons = collect_would_refuse(ticket, world=world, snap=snap)
    joined = " ".join(str(r) for r in reasons)
    assert "size_cash" in joined, reasons
    assert "80000" not in joined, reasons
    assert "AvailableFunds" not in joined, reasons

    # Missing TotalCashValue: fail-closed even when AvailableFunds covers.
    snap["account"].pop("TotalCashValue", None)
    missing = collect_would_refuse(ticket, world=world, snap=snap)
    miss_joined = " ".join(str(r) for r in missing)
    assert "TotalCashValue" in miss_joined, missing
    assert "AvailableFunds" not in miss_joined, missing
    assert "size_cash" not in miss_joined, missing
