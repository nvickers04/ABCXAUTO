"""KEEP-4: unused dry-run / approval tokens expire fail-closed. No 7496."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from abcxauto.config import Config, get_config
from abcxauto.thin_rth_kill_look import F10_HARD_USD, F10_PREFERRED_USD
from abcxauto.token_ttl import (
    DEFAULT_PLACE_TOKEN_TTL_S,
    MAX_PLACE_TOKEN_TTL_S,
    MIN_PLACE_TOKEN_TTL_S,
    REASON_TOKEN_EXPIRED,
    REASON_TOKEN_INVALID,
    REASON_TOKEN_USED,
    TOKEN_TTL_ENV,
    consume_place_token,
    evaluate_place_token,
    expires_at,
    issue_place_token,
    peek_place_token,
    place_token_block,
    reset_place_tokens_for_tests,
    stamp_token,
    ticket_is_exit,
    token_expired,
    token_ttl_seconds,
)


def setup_function():
    reset_place_tokens_for_tests()


def teardown_function():
    reset_place_tokens_for_tests()
    from abcxauto.config import clear_runtime_overrides

    clear_runtime_overrides()
    get_config.cache_clear()


def _placeable_ticket() -> dict:
    return {
        "strategy": "market_bracket",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "side": "BUY",
            "entry_price": 500.0,
            "stop_price": 495.0,
            "target_price": 510.0,
        },
        "rationale": "regression",
        "_desk_session": "regular",
    }


def _exit_ticket() -> dict:
    return {
        "strategy": "vertical_spread",
        "params": {
            "symbol": "SPY",
            "quantity": 1,
            "closing_position": True,
        },
        "rationale": "exit",
        "_desk_session": "regular",
    }


def _connector() -> MagicMock:
    connector = MagicMock()
    connector.connected = True
    connector.place_order = MagicMock()
    return connector


async def _safe_execute_must_not_run(*_a, **_k):
    raise AssertionError("expired token must not reach safe_execute")


def test_hygiene_does_not_soften_f10_or_enable_live():
    assert F10_HARD_USD == 15.0
    assert F10_PREFERRED_USD == 10.0
    assert get_config().ibkr_port != 7496
    assert Config().ibkr_port == 7497
    assert DEFAULT_PLACE_TOKEN_TTL_S == 120.0
    assert MIN_PLACE_TOKEN_TTL_S == 5.0
    assert MAX_PLACE_TOKEN_TTL_S == 900.0
    assert DEFAULT_PLACE_TOKEN_TTL_S <= MAX_PLACE_TOKEN_TTL_S
    assert token_ttl_seconds() == DEFAULT_PLACE_TOKEN_TTL_S


def test_ttl_env_clamped_and_fail_closed(monkeypatch):
    monkeypatch.setenv(TOKEN_TTL_ENV, "60")
    assert token_ttl_seconds() == 60.0
    monkeypatch.setenv(TOKEN_TTL_ENV, "0")
    assert token_ttl_seconds() == DEFAULT_PLACE_TOKEN_TTL_S
    monkeypatch.setenv(TOKEN_TTL_ENV, "-1")
    assert token_ttl_seconds() == DEFAULT_PLACE_TOKEN_TTL_S
    monkeypatch.setenv(TOKEN_TTL_ENV, "off")
    assert token_ttl_seconds() == DEFAULT_PLACE_TOKEN_TTL_S
    monkeypatch.setenv(TOKEN_TTL_ENV, "999999")
    assert token_ttl_seconds() == MAX_PLACE_TOKEN_TTL_S
    monkeypatch.setenv(TOKEN_TTL_ENV, "1")
    assert token_ttl_seconds() == MIN_PLACE_TOKEN_TTL_S
    assert token_ttl_seconds(45) == 45.0
    assert token_ttl_seconds(0) == DEFAULT_PLACE_TOKEN_TTL_S
    assert token_ttl_seconds(float("inf")) == DEFAULT_PLACE_TOKEN_TTL_S


def test_token_expired_at_boundary_fail_closed():
    issued = 1_700_000_000.0
    ttl = 120.0
    assert token_expired(issued, now=issued + 119.0, ttl_s=ttl) is False
    assert token_expired(issued, now=issued + ttl, ttl_s=ttl) is True
    assert token_expired(issued, now=issued + ttl + 1.0, ttl_s=ttl) is True
    assert expires_at(issued, ttl) == issued + 120.0
    assert token_expired(None, now=issued) is True
    assert token_expired(issued, expires_at=issued + 10, now=issued + 10) is True
    assert token_expired("bogus", now=issued) is True
    assert token_expired(issued, now=float("nan"), ttl_s=ttl) is True


def test_stamp_token_keeps_keep3_record_shape():
    now = 1_700_000_100.0
    stamped = stamp_token({"kind": "dry_run", "hint": "SPY"}, now=now, ttl_s=60)
    assert stamped["kind"] == "dry_run"
    assert stamped["hint"] == "SPY"
    assert stamped["issued_at"] == now
    assert stamped["ttl_s"] == 60.0
    assert stamped["expires_at"] == now + 60.0
    assert token_expired(
        stamped["issued_at"],
        expires_at=stamped["expires_at"],
        now=now + 59,
    ) is False
    assert token_expired(
        stamped["issued_at"],
        expires_at=stamped["expires_at"],
        now=now + 60,
    ) is True


def test_store_fresh_then_expired_cannot_place():
    now = 1_700_000_200.0
    rec = issue_place_token(kind="dry_run", ttl_s=60, now=now)
    assert rec["kind"] == "dry_run"
    assert rec["id"]
    assert peek_place_token(rec["id"])["used"] is False
    fresh = consume_place_token(rec["id"], now=now + 1)
    assert fresh["ok"] is True
    assert peek_place_token(rec["id"])["used"] is True
    used = consume_place_token(rec["id"], now=now + 2)
    assert used["ok"] is False
    assert used["reason"] == REASON_TOKEN_USED

    dead = issue_place_token(kind="approval", ttl_s=60, now=now)
    expired = consume_place_token(dead["id"], now=now + 60)
    assert expired["ok"] is False
    assert expired["reason"] == REASON_TOKEN_EXPIRED
    block = place_token_block(
        {**_placeable_ticket(), "place_token": dead["id"]},
        now=now + 60,
    )
    assert block is not None
    assert block["status"] == "blocked"
    assert block["reason_code"] == REASON_TOKEN_EXPIRED


def test_inline_and_unknown_tokens_fail_closed():
    now = 1_700_000_300.0
    inline = stamp_token({"kind": "preview"}, now=now, ttl_s=30)
    assert evaluate_place_token(inline, now=now + 1)["ok"] is True
    assert evaluate_place_token(inline, now=now + 30)["ok"] is False
    assert evaluate_place_token("", now=now)["reason"] == REASON_TOKEN_INVALID
    assert evaluate_place_token("missing-id", now=now)["reason"] == REASON_TOKEN_INVALID
    assert evaluate_place_token(None, now=now)["reason"] == REASON_TOKEN_INVALID
    assert place_token_block(
        {**_placeable_ticket(), "dry_run_token": ""},
        now=now,
    )["reason_code"] == REASON_TOKEN_INVALID
    assert ticket_is_exit(_exit_ticket()) is True
    assert ticket_is_exit(_placeable_ticket()) is False
    assert place_token_block(
        {**_exit_ticket(), "approval_token": "expired-junk"},
        now=now,
    ) is None


@pytest.mark.asyncio
async def test_send_expired_token_cannot_place(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    now = 1_700_000_400.0
    rec = issue_place_token(kind="preview", ttl_s=60, now=now)
    ticket = _placeable_ticket()
    ticket["preview_token"] = rec["id"]
    # Freeze the gate clock past TTL.
    monkeypatch.setattr(
        "abcxauto.token_ttl.time.time",
        lambda: now + 60,
    )
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result["reason_code"] == REASON_TOKEN_EXPIRED


@pytest.mark.asyncio
async def test_send_fresh_token_still_dispatches(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(action)
        return {"status": "ok", "note": "dispatched"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action
    from abcxauto.send_preview import bind_place_token

    clock = {"now": 1_700_000_500.0}
    monkeypatch.setattr("abcxauto.token_ttl.time.time", lambda: clock["now"])
    ticket = _placeable_ticket()
    bind_place_token(ticket)
    clock["now"] = clock["now"] + 1
    result = await send_action(ticket, _connector())
    assert result["status"] == "ok"
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_send_without_token_fails_closed_keep3(monkeypatch):
    """KEEP-3 merged — new risk requires a hash-bound preview token."""
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action
    from abcxauto.send_preview import REASON_PREVIEW_TOKEN

    result = await send_action(_placeable_ticket(), _connector())
    assert result["status"] == "blocked"
    assert result["reason_code"] == REASON_PREVIEW_TOKEN


@pytest.mark.asyncio
async def test_send_exit_never_blocked_by_expired_token(monkeypatch):
    dispatched = []

    async def _record(action, connector):
        dispatched.append(True)
        return {"status": "ok", "note": "exit"}

    monkeypatch.setattr("abcxauto.send.safe_execute", _record)
    from abcxauto.send import send_action

    ticket = _exit_ticket()
    ticket["approval_token"] = "dead"
    result = await send_action(ticket, _connector())
    assert result["status"] == "ok"
    assert dispatched == [True]


@pytest.mark.asyncio
async def test_live_port_still_wins_over_fresh_token(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "7496")
    from abcxauto.config import clear_runtime_overrides, get_config

    clear_runtime_overrides()
    get_config.cache_clear()
    cfg = get_config()
    assert cfg.ibkr_port == 7496
    monkeypatch.setattr("abcxauto.send.get_config", lambda: cfg)
    monkeypatch.setattr("abcxauto.send.safe_execute", _safe_execute_must_not_run)
    from abcxauto.send import send_action

    rec = issue_place_token(kind="place")
    ticket = _placeable_ticket()
    ticket["place_token"] = rec["id"]
    result = await send_action(ticket, _connector())
    assert result["status"] == "blocked"
    assert result["reason_code"] == "live_port_paper"
    assert F10_HARD_USD == 15.0
    assert get_config().ibkr_port == 7496
