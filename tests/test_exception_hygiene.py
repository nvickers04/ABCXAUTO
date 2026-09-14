"""Class-(a) exception hygiene: fail-closed gates must log and not send."""

from __future__ import annotations

import logging

import pytest

from abcxauto.world_state import WorldState


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=False,
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
        gates={"max_risk_per_trade_pct": 1.0},
        envelope={},
        regime={"session_phase": "mid", "trend_bias": "mixed", "vol_proxy": "normal"},
        portfolio_risk={"n_positions": 0, "top_symbol": "", "top_concentration_pct": 0},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
    )
    base.update(kwargs)
    return WorldState(**base)


def _close_act() -> dict:
    return {
        "action": "market_order",
        "strategy": "market_order",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "conId": 99,
            "closing_position": True,
        },
        "rationale": "exit",
    }


def _new_entry_act() -> dict:
    return {
        "action": "market_bracket",
        "strategy": "market_bracket",
        "params": {
            "symbol": "QQQ",
            "quantity": 1,
            "direction": "LONG",
            "stop_price": 90.0,
            "target_price": 110.0,
            "card": "index momo",
        },
        "rationale": "new entry",
    }


def test_new_risk_halted_unreadable_gate_fails_closed(monkeypatch, caplog):
    from abcxauto.agent_loop import _new_risk_halted

    def boom():
        raise RuntimeError("gate down")

    monkeypatch.setattr("abcxauto.risk_gates.get_risk_gate", boom)
    with caplog.at_level(logging.WARNING, logger="abcxauto.agent_loop"):
        assert _new_risk_halted(_world()) is True
    assert any("halt check failed closed" in r.getMessage() for r in caplog.records)


def test_flat_streak_unreadable_fails_closed(monkeypatch, caplog):
    from abcxauto.agent_loop import gate_ticket

    def boom():
        raise OSError("flat streak unreadable")

    monkeypatch.setattr("abcxauto.trade_plan.load_flat_streak", boom)
    monkeypatch.setattr("abcxauto.universe.is_legal_symbol", lambda s: True)
    with caplog.at_level(logging.WARNING, logger="abcxauto.agent_loop"):
        strat, forced = gate_ticket(_new_entry_act(), _world())
    assert strat == "blocked"
    assert "unconfirmed" in str((forced or {}).get("note") or "").lower()
    assert any("flat-streak gate failed closed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_inventory_validation_exception_fails_closed(monkeypatch, caplog):
    from abcxauto.agent_loop import execute_ticket

    def boom(*_a, **_k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr("abcxauto.agent_loop.validate_action_against_inventory", boom)
    monkeypatch.setattr("abcxauto.desk_mode.is_research_session", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.gate_ticket",
        lambda act, world: ("market_order", None),
    )
    monkeypatch.setattr(
        "abcxauto.look_snapshot.check_ticket_numbers",
        lambda *_a, **_k: (True, "", ""),
    )
    monkeypatch.setattr(
        "abcxauto.trade_playbook.check_overlay_shares",
        lambda *_a, **_k: (True, "", ""),
    )

    pos = [{"symbol": "SPY", "quantity": 1, "conId": 99, "secType": "STK"}]
    with caplog.at_level(logging.WARNING, logger="abcxauto.agent_loop"):
        result = await execute_ticket(
            _close_act(),
            None,
            _world(positions=pos),
            {"positions": pos},
        )
    assert result.get("status") == "validated_block"
    assert "inventory_validation_failed" in str(result.get("reason") or "")
    assert any(
        "inventory validation failed closed" in r.getMessage() for r in caplog.records
    )


def test_preview_inventory_exception_fails_closed(monkeypatch, caplog):
    from abcxauto.send_preview import collect_would_refuse

    def boom(*_a, **_k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr("abcxauto.agent_loop.validate_action_against_inventory", boom)
    monkeypatch.setattr("abcxauto.desk_mode.is_research_session", lambda *_a, **_k: False)
    monkeypatch.setattr(
        "abcxauto.thin_rth_kill_look.kill_look_send_block",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "abcxauto.agent_loop.gate_ticket",
        lambda act, world: ("market_order", None),
    )
    monkeypatch.setattr(
        "abcxauto.look_snapshot.check_ticket_numbers",
        lambda *_a, **_k: (True, "", ""),
    )
    monkeypatch.setattr(
        "abcxauto.trade_playbook.check_overlay_shares",
        lambda *_a, **_k: (True, "", ""),
    )
    with caplog.at_level(logging.WARNING, logger="abcxauto.send_preview"):
        reasons = collect_would_refuse(_close_act(), world=_world(), snap={})
    assert any("inventory_validation_failed" in str(r) for r in reasons)
    assert any(
        "preview inventory check failed closed" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_pre_trade_account_exception_logs_and_fails_closed(monkeypatch, caplog):
    from abcxauto.risk_gates import reset_risk_gate
    from tests.test_risk_gates import FakeConnector, _bracket, _cfg

    gate = reset_risk_gate()
    monkeypatch.setattr(
        "abcxauto.risk_gates.get_config",
        lambda: _cfg(
            risk_gates_enabled=True,
            daily_loss_limit_pct=2.0,
            max_position_pct=10.0,
            max_open_positions=5,
        ),
    )
    conn = FakeConnector(account_error=RuntimeError("timeout"))
    with caplog.at_level(logging.WARNING, logger="abcxauto.risk_gates"):
        ok, reason = await gate.pre_trade_check(_bracket(), conn)
    assert ok is False
    assert "fail-closed" in reason.lower()
    assert any("cannot read account summary" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_cancel_last_stop_unreadable_orders_logs_and_fails_closed(caplog):
    from abcxauto.executor import _verify_cancel_not_last_stop
    from abcxauto.proposals import validate_proposal
    from tests.test_proposals import RATIONALE, VALID_PAYLOADS

    class Boom:
        async def get_open_orders(self):
            raise RuntimeError("orders down")

        async def get_positions(self):
            return []

    proposal = validate_proposal("cancel_order", VALID_PAYLOADS["cancel_order"], RATIONALE)
    with caplog.at_level(logging.WARNING, logger="abcxauto.executor"):
        rejection = await _verify_cancel_not_last_stop(proposal, Boom())
    assert rejection and "fail-closed" in str(rejection.get("error") or "").lower()
    assert any("cannot read open orders" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_exit_verify_unreadable_positions_logs_and_fails_closed(monkeypatch, caplog):
    from abcxauto.config import Config, get_config
    from abcxauto.executor import execute_proposal
    from abcxauto.proposals import validate_proposal
    from tests.test_proposals import RATIONALE, VALID_PAYLOADS

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
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: Config(**{**base.__dict__, "defined_risk_only": False}),
    )

    class Boom:
        async def get_positions(self):
            raise RuntimeError("positions down")

        async def get_account_summary(self):
            return {"netliquidation": 100_000.0, "dailypnl": 0.0}

        async def get_open_orders(self):
            return []

    proposal = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
    with caplog.at_level(logging.WARNING, logger="abcxauto.executor"):
        result = await execute_proposal(proposal, Boom())
    assert "error" in result
    assert "could not verify position" in str(result.get("error") or "").lower()
    assert any("cannot read positions" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_send_marks_quote_failure_logs_and_still_dispatches(monkeypatch, caplog):
    from abcxauto.config import Config, get_config
    from abcxauto.executor import execute_proposal
    from abcxauto.proposals import validate_proposal
    from tests.test_executor import FakeGateway
    from tests.test_proposals import RATIONALE, VALID_PAYLOADS

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
    monkeypatch.setattr(
        "abcxauto.proposals.get_config",
        lambda: Config(**{**base.__dict__, "defined_risk_only": False, "risk_posture": "balanced"}),
    )

    async def boom(*_a, **_k):
        raise RuntimeError("quote stamp down")

    monkeypatch.setattr("abcxauto.send_marks.capture_send_quote", boom)
    gateway = FakeGateway()
    proposal = validate_proposal("market_order", VALID_PAYLOADS["market_order"], RATIONALE)
    with caplog.at_level(logging.WARNING, logger="abcxauto.executor"):
        result = await execute_proposal(proposal, gateway)
    assert result.get("error") is None or result.get("success") is not False
    assert gateway.calls, result
    assert any("send_marks quote failed" in r.getMessage() for r in caplog.records)


def test_book_peak_equity_update_failure_logs(monkeypatch, caplog):
    from abcxauto.book import _peak_dd_pct

    class Gate:
        peak_equity = 10_000.0

        def update_equity(self, _nl):
            raise RuntimeError("equity write failed")

    monkeypatch.setattr("abcxauto.risk_gates.get_risk_gate", lambda: Gate())
    with caplog.at_level(logging.WARNING, logger="abcxauto.book"):
        dd = _peak_dd_pct(9_000.0)
    assert dd is not None
    assert any("peak-equity update failed" in r.getMessage() for r in caplog.records)


def test_book_halt_gate_unavailable_logs(monkeypatch, caplog):
    from abcxauto.book import _trades_today_and_halt

    def boom():
        raise RuntimeError("gate down")

    monkeypatch.setattr("abcxauto.risk_gates.get_risk_gate", boom)

    class DeadJournal:
        def daily_summary(self):
            raise RuntimeError("journal down")

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: DeadJournal())
    with caplog.at_level(logging.WARNING, logger="abcxauto.book"):
        trades, halted, _reason = _trades_today_and_halt()
    assert trades is None
    assert halted is None
    assert any("halt/trade-count gate unavailable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_market_bracket_unfilled_cancel_failure_logs(caplog):
    from abcxauto.broker.orders import IBKROrdersMixin

    class FakeOrders(IBKROrdersMixin):
        async def place_market_order(self, *_a, **_k):
            return {
                "success": True,
                "filled": False,
                "order_id": 44,
                "fill_status": "Submitted",
            }

        async def _reconcile_market_fill(self, **_k):
            return {"filled": False}

        async def cancel_order(self, _oid):
            raise RuntimeError("cancel refused")

    with caplog.at_level(logging.WARNING, logger="abcxauto.broker.orders"):
        result = await FakeOrders().place_market_bracket("SPY", 1, "LONG", 400.0, 420.0)
    assert result.get("success") is False
    assert result.get("filled") is False
    assert any("cancel of unfilled entry failed" in r.getMessage() for r in caplog.records)
    assert any("order_id=44" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_abort_fuse_unreadable_orders_logs_and_fails_closed(caplog):
    from abcxauto.abort_fuse import _read_book

    class Boom:
        async def get_open_orders(self):
            raise RuntimeError("orders down")

        async def get_positions(self):
            return []

    with caplog.at_level(logging.WARNING, logger="abcxauto.abort_fuse"):
        orders, _lots, err = await _read_book(Boom(), positions=[], open_orders=None)
    assert orders is None
    assert "get_open_orders failed" in err
    assert any("cannot read open orders" in r.getMessage() for r in caplog.records)


def test_flat_streak_corrupt_json_logs(tmp_path, monkeypatch, caplog):
    from abcxauto.trade_plan import _flat_streak_state, load_flat_streak

    path = tmp_path / "flat.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(path))
    with caplog.at_level(logging.WARNING, logger="abcxauto.trade_plan"):
        st = _flat_streak_state()
        assert load_flat_streak() == 0
    assert st["empty_count"] == 0
    assert any("flat streak state unreadable" in r.getMessage() for r in caplog.records)


def test_session_billed_tokens_journal_failure_logs(monkeypatch, caplog):
    from abcxauto.session_caps import billed_tokens_now

    def boom():
        raise RuntimeError("journal dark")

    monkeypatch.setattr("abcxauto.memory.get_journal", boom)
    with caplog.at_level(logging.WARNING, logger="abcxauto.session_caps"):
        assert billed_tokens_now() == 0
    assert any("billed-token read failed" in r.getMessage() for r in caplog.records)


def test_config_floor_clamp_failure_logs(monkeypatch, caplog):
    from abcxauto.config import get_config

    def boom(_cfg):
        raise RuntimeError("clamp exploded")

    monkeypatch.setattr("abcxauto.self_tune.floor_clamp_config_fields", boom)
    get_config.cache_clear()
    with caplog.at_level(logging.WARNING, logger="abcxauto.config"):
        cfg = get_config()
    assert cfg is not None
    assert any("floor clamp of config fields failed" in r.getMessage() for r in caplog.records)
    get_config.cache_clear()
