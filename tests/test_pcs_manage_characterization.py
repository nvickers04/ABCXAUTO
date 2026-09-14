"""Characterization: PCS look ingest, manage refresh, follow-after-act.

Pins current behavior of ``ingest_pcs_from_look``, ``refresh_pcs_manage``,
``follow_pcs_after_act``, and ``capture_pcs_vertical_quote`` before a module
split. These are a safety net, not a spec change.
"""

from __future__ import annotations

import inspect

import pytest

from abcxauto.memory import get_journal
from abcxauto.pcs_fill_lambda import (
    EVENT_COMMISSION,
    EVENT_FILL,
    EVENT_LIFECYCLE_END,
    EVENT_MANAGE_CHECK,
    EVENT_ORDER_UPDATE,
    EVENT_QUOTE_SNAP,
    EVENT_SCORE_MARK,
    EVENT_TICKET_SUBMIT,
    QUOTE_IBKR_BAG,
    QUOTE_IBKR_LEGS,
    QUOTE_UNAVAILABLE,
    capture_pcs_vertical_quote,
    dte_days,
    follow_pcs_after_act,
    ingest_pcs_from_look,
    journal_pcs_post_send,
    journal_pcs_pre_send,
    manage_hits,
    manage_levels,
    refresh_pcs_manage,
)

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

QUOTE_BAG = {
    "quote_source": QUOTE_IBKR_BAG,
    "bid": 0.80,
    "ask": 0.90,
    "mid": 0.85,
    "half_spread": 0.05,
    "last": 0.84,
}


class _Prop:
    strategy = "vertical_spread"
    card = "pcs-skew"
    params = PCS_OPEN


def _seed_open_pcs(journal, *, filled: bool = False, proposal_id: int = 9, order_id: int = 77):
    lid = journal_pcs_pre_send(journal, _Prop(), QUOTE_BAG, proposal_id=proposal_id)
    result = {"success": True, "order_id": order_id}
    if filled:
        result.update({"avg_fill_price": 0.80, "filled": True})
    journal_pcs_post_send(
        journal,
        _Prop(),
        QUOTE_BAG,
        result,
        True,
        proposal_id=proposal_id,
        lifecycle_id=lid,
    )
    return lid


def _pcs_positions():
    return [
        {
            "symbol": "SPY",
            "secType": "OPT",
            "right": "P",
            "expiration": "20260918",
            "strike": 500.0,
            "quantity": 1,
        },
        {
            "symbol": "SPY",
            "secType": "OPT",
            "right": "P",
            "expiration": "20260918",
            "strike": 505.0,
            "quantity": -1,
        },
    ]


def _event_names(journal, lid):
    return [row["event"] for row in journal.pcs_events(lifecycle_id=lid, limit=50)]


def test_ingest_pcs_from_look_none_journal_is_noop():
    ingest_pcs_from_look(None, {"positions": _pcs_positions()})


def test_ingest_no_open_pcs_lifecycle_is_noop():
    """Look with a PCS-shaped book but no open lifecycle writes nothing."""
    journal = get_journal()
    before = journal.pcs_events(limit=50)
    ingest_pcs_from_look(
        journal,
        {
            "positions": _pcs_positions(),
            "fills": [
                {
                    "order_id": 77,
                    "sec_type": "BAG",
                    "side": "SLD",
                    "price": 0.80,
                    "quantity": 1,
                }
            ],
            "open_orders": [{"order_id": 77}],
        },
    )
    assert journal.pcs_events(limit=50) == before


def test_ingest_look_with_pcs_vertical_writes_working_and_fill():
    """Open lifecycle + look with working BAG oid and OPT/BAG fills.

    Writes ``pcs_order_update`` (working) plus fill / commission / score /
    filled-update. Positions are present so the lifecycle stays open.
    """
    journal = get_journal()
    lid = _seed_open_pcs(journal, filled=False, order_id=77)
    before = set(_event_names(journal, lid))
    ingest_pcs_from_look(
        journal,
        {
            "positions": _pcs_positions(),
            "open_orders": [{"order_id": 77, "symbol": "SPY"}],
            "fills": [
                {
                    "exec_id": "bag-1",
                    "order_id": 77,
                    "sec_type": "BAG",
                    "side": "SLD",
                    "quantity": 1,
                    "price": 0.80,
                    "commission": 1.30,
                }
            ],
        },
    )
    names = _event_names(journal, lid)
    assert EVENT_ORDER_UPDATE in names
    working = [
        row
        for row in journal.pcs_events(lifecycle_id=lid, event=EVENT_ORDER_UPDATE)
        if row.get("status") == "working"
    ]
    assert working, names
    assert working[0]["order_id"] == 77
    assert working[0]["include_in_pnl_mean"] is False
    assert EVENT_FILL in names
    assert EVENT_COMMISSION in names
    assert EVENT_SCORE_MARK in names
    filled = [
        row
        for row in journal.pcs_events(lifecycle_id=lid, event=EVENT_ORDER_UPDATE)
        if row.get("status") == "filled"
    ]
    assert filled
    assert EVENT_LIFECYCLE_END not in names
    assert set(names) >= before | {
        EVENT_FILL,
        EVENT_COMMISSION,
        EVENT_SCORE_MARK,
    }


def test_ingest_filled_and_flat_ends_lifecycle_filled_closed():
    """After a real fill, a look with no matching lot and no working oid ends it."""
    journal = get_journal()
    lid = _seed_open_pcs(journal, filled=True, order_id=77)
    ingest_pcs_from_look(
        journal,
        {"positions": [], "open_orders": [], "fills": []},
    )
    ends = [
        row
        for row in journal.pcs_events(lifecycle_id=lid, event=EVENT_LIFECYCLE_END)
    ]
    assert ends
    assert ends[0]["reason"] == "filled_closed"
    assert ends[0]["include_in_pnl_mean"] is True


def test_ingest_stale_look_without_miss_does_not_end():
    """# NOTE: pins current behavior; see plan

    Submitted (unfilled) lifecycle + look missing the new BAG oid does not
    write ``pcs_lifecycle_end``. Only a missed send-mark or a prior fill ends it.
    """
    journal = get_journal()
    lid = _seed_open_pcs(journal, filled=False, order_id=77)
    ingest_pcs_from_look(
        journal,
        {"positions": [], "open_orders": [], "fills": []},
    )
    assert EVENT_LIFECYCLE_END not in _event_names(journal, lid)


@pytest.mark.asyncio
async def test_refresh_pcs_manage_writes_check_from_connector_bag_quote():
    """Open quote-snap row still has geometry, so manage can match the lot.

    Close mark is the BAG ask. ``ba_tax`` is 2× half_spread when present.
    """
    journal = get_journal()
    lid = journal_pcs_pre_send(journal, _Prop(), QUOTE_BAG, proposal_id=11)

    bag_calls: list[tuple] = []

    class _Conn:
        async def get_live_vertical_bag_quote(self, symbol, exp, long_k, short_k, right):
            bag_calls.append((symbol, exp, long_k, short_k, right))
            return {"bid": 0.80, "ask": 0.90, "last": 0.84}

        async def get_live_option_quote(self, *_a, **_k):
            raise AssertionError("legs must not run when BAG printed")

    await refresh_pcs_manage(
        journal,
        {"positions": _pcs_positions()},
        _Conn(),
    )
    assert bag_calls == [("SPY", "20260918", 500.0, 505.0, "P")]
    rows = journal.pcs_events(lifecycle_id=lid, event=EVENT_MANAGE_CHECK)
    assert len(rows) == 1
    row = rows[0]
    assert row["quote_source"] == QUOTE_IBKR_BAG
    assert row["bid"] == pytest.approx(0.80)
    assert row["ask"] == pytest.approx(0.90)
    assert row["mid"] == pytest.approx(0.85)
    assert row["debit"] == pytest.approx(0.90)
    assert row["credit"] is None
    assert row["ba_tax"] == pytest.approx(0.10)
    assert row["include_in_pnl_mean"] is False
    dte = dte_days("20260918")
    assert row["dte"] == dte
    first, hits = manage_hits(debit_mark=0.90, fill_credit=None, dte=dte)
    assert row["manage_rule"] == first
    assert row["manage_hits"] == hits
    assert row["manage_levels"] == manage_levels(None)
    assert row["fingerprint"] == f"{first}|{0.9}|{dte}|{QUOTE_IBKR_BAG}|{0.1}"


@pytest.mark.asyncio
async def test_refresh_pcs_manage_after_post_send_skips_without_geometry_on_latest():
    """# NOTE: pins current behavior; see plan

    ``pcs_open_lifecycles`` returns only the latest row. After post-send the
    latest row has no expiration/strikes, so ``_lot_matches`` is false and
    manage does not write even when the look book has the vertical.
    """
    journal = get_journal()
    lid = _seed_open_pcs(journal, filled=True, order_id=77)

    class _Conn:
        async def get_live_vertical_bag_quote(self, *_a, **_k):
            raise AssertionError("must not quote when lot does not match")

    await refresh_pcs_manage(
        journal,
        {"positions": _pcs_positions()},
        _Conn(),
    )
    assert journal.pcs_events(lifecycle_id=lid, event=EVENT_MANAGE_CHECK) == []


@pytest.mark.asyncio
async def test_refresh_pcs_manage_quote_unavailable_still_writes():
    """No connector, or a raising connector, still records manage with unavailable."""
    journal = get_journal()
    lid = journal_pcs_pre_send(journal, _Prop(), QUOTE_BAG, proposal_id=12)
    snap = {"positions": _pcs_positions()}
    await refresh_pcs_manage(journal, snap, connector=None)
    rows = journal.pcs_events(lifecycle_id=lid, event=EVENT_MANAGE_CHECK)
    assert len(rows) == 1
    assert rows[0]["quote_source"] == QUOTE_UNAVAILABLE
    assert rows[0]["debit"] is None
    assert rows[0]["bid"] is None
    assert rows[0]["ask"] is None

    class _Boom:
        def get_live_vertical_bag_quote(self, *_a, **_k):
            raise RuntimeError("ibkr down")

        def get_live_option_quote(self, *_a, **_k):
            raise RuntimeError("ibkr down")

    # Same fingerprint → second write is skipped.
    await refresh_pcs_manage(journal, snap, _Boom())
    assert len(journal.pcs_events(lifecycle_id=lid, event=EVENT_MANAGE_CHECK)) == 1

    # New lifecycle: raising connector is fail-closed to unavailable, not an error.
    lid2 = journal_pcs_pre_send(journal, _Prop(), QUOTE_BAG, proposal_id=13)
    await refresh_pcs_manage(journal, snap, _Boom())
    rows2 = journal.pcs_events(lifecycle_id=lid2, event=EVENT_MANAGE_CHECK)
    assert len(rows2) == 1
    assert rows2[0]["quote_source"] == QUOTE_UNAVAILABLE


@pytest.mark.asyncio
async def test_refresh_pcs_manage_none_journal_is_noop():
    await refresh_pcs_manage(None, {"positions": _pcs_positions()}, connector=None)


@pytest.mark.asyncio
async def test_follow_pcs_after_act_with_and_without_pcs_ticket():
    """# NOTE: pins current behavior; see plan

    ``act`` / ``result`` are ignored (``_ = (act, result)``). Both a PCS
    ticket and a hold still ingest + refresh from the look snap / journal.
    """
    journal = get_journal()
    lid = journal_pcs_pre_send(journal, _Prop(), QUOTE_BAG, proposal_id=21)
    snap = {
        "positions": _pcs_positions(),
        "open_orders": [],
        "fills": [],
    }

    class _Conn:
        async def get_live_vertical_bag_quote(self, *_a, **_k):
            return {"bid": 0.80, "ask": 0.90, "last": 0.84}

    pcs_act = {
        "strategy": "vertical_spread",
        "params": dict(PCS_OPEN),
    }
    await follow_pcs_after_act(
        pcs_act, {"success": True, "order_id": 77}, snap, _Conn(), journal
    )
    after_pcs = _event_names(journal, lid)
    assert EVENT_MANAGE_CHECK in after_pcs

    hold_journal_count = len(journal.pcs_events(lifecycle_id=lid, event=EVENT_MANAGE_CHECK))
    await follow_pcs_after_act(
        {"strategy": "hold", "params": {}}, {"status": "hold"}, snap, _Conn(), journal
    )
    # Duplicate fingerprint — hold does not add a second manage row.
    assert (
        len(journal.pcs_events(lifecycle_id=lid, event=EVENT_MANAGE_CHECK))
        == hold_journal_count
    )


@pytest.mark.asyncio
async def test_follow_pcs_after_act_without_open_lot_is_noop():
    journal = get_journal()
    before = journal.pcs_events(limit=50)
    await follow_pcs_after_act(
        {"strategy": "hold"},
        {"status": "hold"},
        {"positions": [], "open_orders": [], "fills": []},
        connector=None,
        journal=journal,
    )
    assert journal.pcs_events(limit=50) == before


@pytest.mark.asyncio
async def test_capture_pcs_vertical_quote_uses_connector_bag_or_legs_never_mda():
    """BAG first, then IBKR legs. MDA-shaped methods are never invoked."""
    src = inspect.getsource(capture_pcs_vertical_quote)
    assert "get_live_vertical_bag_quote" in src
    assert "get_live_option_quote" in src
    assert "fetch_option_facts" not in src
    assert "option_facts" not in src

    calls: list[tuple] = []

    class _Bag:
        async def get_live_vertical_bag_quote(self, *a):
            calls.append(("bag", a))
            return {"bid": 0.80, "ask": 0.90, "last": 0.84}

        async def get_live_option_quote(self, *a):
            calls.append(("leg", a))
            raise AssertionError("legs must not run when BAG printed")

        async def fetch_option_facts(self, *_a, **_k):
            calls.append(("mda_facts",))
            raise AssertionError("MDA must not run")

        async def get_option_facts(self, *_a, **_k):
            calls.append(("mda_facts",))
            raise AssertionError("MDA must not run")

    bag = await capture_pcs_vertical_quote(_Bag(), PCS_OPEN)
    assert bag["quote_source"] == QUOTE_IBKR_BAG
    assert bag["mid"] == pytest.approx(0.85)
    assert [c[0] for c in calls] == ["bag"]

    calls.clear()

    class _Legs:
        async def get_live_vertical_bag_quote(self, *a):
            calls.append(("bag", a))
            return {"error": "no IBKR tick yet"}

        async def get_live_option_quote(self, symbol, expiration, strike, right):
            calls.append(("leg", (symbol, expiration, strike, right)))
            if float(strike) == 500.0:
                return {"bid": 0.40, "ask": 0.50, "last": 0.45}
            return {"bid": 1.20, "ask": 1.30, "last": 1.25}

        async def fetch_option_facts(self, *_a, **_k):
            calls.append(("mda_facts",))
            raise AssertionError("MDA must not run")

    legs = await capture_pcs_vertical_quote(_Legs(), PCS_OPEN)
    assert legs["quote_source"] == QUOTE_IBKR_LEGS
    assert [c[0] for c in calls] == ["bag", "leg", "leg"]
    assert "mda_facts" not in [c[0] for c in calls]
