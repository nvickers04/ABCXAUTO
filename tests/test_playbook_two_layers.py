"""One playbook tree: durable order types, disposable cards branching under them.

    TYPE market_bracket          <- learned execution, durable
      |- card: mega-cap earnings-flush bounce
      |- card: opening-range continuation

A card's position *is* its ticket, so identity is ``(type, name)`` and a winning
card sits inside the type entry it is supposed to improve.

The clerk's half is attribution: new risk must name a card under the type being
sent, every dispatched ticket is tagged, and a card is scored on the trades that
actually resolved. An operator flatten or a halt exit is real P&L but not
evidence, so it never advances a card toward live money.
"""

from __future__ import annotations

import json

import pytest

from abcxauto.agent_loop import gate_ticket, is_new_risk
from abcxauto.lab_playbook import (
    EXIT_DECISION,
    EXIT_OPEN,
    EXIT_OPERATOR,
    EXIT_PROTECTIVE,
    apply_from_judgment,
    card_calibration,
    card_facts,
    card_scores,
    card_verdict,
    classify_card_trades,
    clamp_update,
    graduated_card_names,
    load_lab,
    load_live,
    maybe_promote,
    new_risk_card_error,
    notebook_text,
    record_card_send,
    save_lab,
    type_cards,
    type_schema_echo_keys,
    walk_cards,
)
from abcxauto.world_state import WorldState


@pytest.fixture(autouse=True)
def _lab(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
    monkeypatch.setenv("ABCXAUTO_FLAT_STREAK_PATH", str(tmp_path / "flat.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    return tmp_path


def _world(**kwargs) -> WorldState:
    base = dict(
        cycle=1,
        session_status="regular",
        flat=False,
        needs_protection=False,
        unprotected=[],
        net_liquidation=40_000.0,
        daily_pnl=0.0,
        positions=[{"symbol": "WMT", "sec_type": "STK", "quantity": 70, "conId": 9}],
        open_orders=[],
        opportunities=[],
        news_items=[],
        risk_posture="balanced",
        effective_posture="balanced",
        gates={},
        envelope={},
        regime={},
        portfolio_risk={"n_positions": 1},
        working_thesis="",
        recent_decisions=[],
        trade_plan=None,
        capacity={
            "open_count": 1,
            "max_open_positions": 6,
            "slots_left": 5,
            "allows_new_risk": True,
        },
    )
    base.update(kwargs)
    return WorldState(**base)


def _entry(card: str | None = None, symbol: str = "QQQ", strategy: str = "market_bracket") -> dict:
    params = {
        "symbol": symbol,
        "quantity": 1,
        "direction": "LONG",
        "stop_price": 90.0,
        "target_price": 110.0,
    }
    if card is not None:
        params["card"] = card
    return {
        "action": strategy,
        "strategy": strategy,
        "params": params,
        "rationale": "new entry",
    }


def _card(name: str, **over) -> dict:
    """A card as Grok writes it: no ticket, because its parent type is one."""
    row = {
        "name": name,
        "thesis": "flush into support bounces",
        "retire_if": {"sample": 3, "condition": "no bounce off the opening low"},
    }
    row.update(over)
    return row


def _book(**by_type) -> dict:
    """The nested write shape. A value is a card list or a whole type stanza."""
    types: dict[str, dict] = {}
    for name, val in by_type.items():
        types[name] = dict(val) if isinstance(val, dict) else {"cards": list(val)}
    return {"types": types}


def _save(**by_type) -> dict:
    update = clamp_update(_book(**by_type))
    assert update is not None
    return save_lab(update)


# --- the trunk: types hold learnings, never the ticket schema -----------------


def test_type_layer_holds_learned_execution_only():
    update = clamp_update(
        {
            "types": {
                "market_bracket": {
                    "tool_order": ["scan", "quote", "candles", "send"],
                    "gotchas": "stop must be the wrong side of last or IBKR rejects",
                    "review": "fills then book; confirm both children rest",
                }
            }
        }
    )
    assert update is not None
    save_lab(update)
    types = load_lab()["types"]
    assert types["market_bracket"]["tool_order"][0] == "scan"
    assert "IBKR rejects" in types["market_bracket"]["gotchas"]
    assert type_schema_echo_keys(types) == []
    text = notebook_text(load_lab())
    assert "TYPE market_bracket" in text
    assert "tool_order:" in text


def test_evidence_rewrite_keeps_the_declaration_it_did_not_restate():
    """The loop this kills: every look, a note-only card write deleted retire_if,
    the clerk reported the card as owing one, and the model spent a second write
    restoring it — twice the revisions, and no falsification in between.
    """
    _save(
        market_bracket=[
            _card(
                "flush bounce",
                thesis="mega-cap sales-miss gap retraces 30-50%",
                retire_if={"sample": 8, "condition": "three closes on the lows"},
                expect_hit_rate=45,
            )
        ]
    )
    # A look later: same card, fresh evidence, declaration not restated.
    _save(
        market_bracket=[
            {"name": "flush bounce", "note": "10:12 ET gate off, BABA only -4%"}
        ]
    )
    card = type_cards(load_lab()["types"], "market_bracket")[0]
    assert card["note"].startswith("10:12 ET")
    assert card["retire_if"] == {
        "sample": 8,
        "condition": "three closes on the lows",
    }
    assert card["thesis"].startswith("mega-cap sales-miss")
    assert card["expect_hit_rate"] == 45.0
    # So the clerk stops asking for a declaration that was never really gone.
    facts = {r["card"]: r for r in card_facts(load_lab())}
    assert facts["flush bounce"]["needs_retire_if"] is False
    assert facts["flush bounce"]["needs_thesis"] is False


def test_a_rewritten_declaration_still_replaces_the_old_one():
    """Carrying forward must not pin a card to its first declaration."""
    _save(market_bracket=[_card("flush bounce", retire_if={"sample": 8, "condition": "a"})])
    _save(
        market_bracket=[
            _card("flush bounce", retire_if={"sample": 3, "condition": "b"})
        ]
    )
    card = type_cards(load_lab()["types"], "market_bracket")[0]
    assert card["retire_if"] == {"sample": 3, "condition": "b"}


def test_a_card_left_out_of_the_list_is_still_dropped():
    """Merge is per re-sent card, so retiring by omission still works."""
    _save(market_bracket=[_card("flush bounce"), _card("opening drive")])
    _save(market_bracket=[_card("flush bounce")])
    names = [c["name"] for c in type_cards(load_lab()["types"], "market_bracket")]
    assert names == ["flush bounce"]


def test_lab_facts_reports_what_is_untried_not_only_what_scored():
    """strategy_scores says what ran; this says what never has."""
    from abcxauto.lab_playbook import lab_facts, playbook_payload

    _save(
        market_bracket={"cards": [_card("flush bounce"), _card("opening drive")]},
        buy_option={"cards": [_card("premium spray", status="retired")]},
        vertical_spread={"gotchas": "BAG close is one ticket"},
    )
    facts = lab_facts(load_lab())
    assert facts["cards"] == {"testing": 2, "working": 0, "retired": 1}
    assert facts["resolved_trades"] == 0
    assert sorted(facts["cards_awaiting_first_trade"]) == [
        "flush bounce [market_bracket]",
        "opening drive [market_bracket]",
    ]
    assert facts["trunks_with_cards"] == ["market_bracket", "buy_option"]
    untried = facts["entry_trunks_untried"]
    # A trunk holding only a learning still carries no hypothesis.
    assert "vertical_spread" in untried
    assert "iron_condor" in untried
    assert "market_bracket" not in untried
    # Management trunks are not gaps: they adjust risk that already exists, so
    # "untried" must not invite a hypothesis about cancelling an order.
    for managed in ("modify_stop", "modify_target", "cancel_order", "trailing_stop"):
        assert managed not in untried, managed
    assert "close_option" not in untried
    # It reaches the model on the playbook tool, beside strategy_scores.
    payload = playbook_payload()
    assert payload["lab"]["entry_trunks_untried"] == untried
    assert "strategy_scores" in payload


def test_type_coverage_lists_every_trunk_without_seeding_one():
    """Operator sees all sendable types; the book still stores only what Grok wrote."""
    from abcxauto.lab_playbook import playbook_type_keys, type_coverage

    _save(
        market_bracket={
            "gotchas": "stop must be the wrong side of live last",
            "cards": [_card("flush bounce")],
        },
        vertical_spread={"tool_order": ["quote", "option_chain", "send"]},
    )
    lab = load_lab()
    rows = {r["type"]: r for r in type_coverage(lab)}
    assert list(rows) == list(playbook_type_keys())

    mb = rows["market_bracket"]
    assert mb["touched"] is True
    assert mb["cards"] == 1
    assert "gotchas" in mb["learned"]

    # A learning with no card still earns the trunk; a list field counts.
    vs = rows["vertical_spread"]
    assert vs["touched"] is True
    assert vs["cards"] == 0
    assert vs["learned"] == ["tool_order"]

    untouched = [k for k, r in rows.items() if not r["touched"]]
    assert "iron_condor" in untouched
    assert rows["iron_condor"]["cards"] == 0
    assert rows["iron_condor"]["learned"] == []

    # The view is derived. Nothing seeded an empty stanza onto disk.
    assert set(lab["types"]) == {"market_bracket", "vertical_spread"}
    assert "iron_condor" not in json.dumps(lab)


def test_clerk_never_writes_order_examples_into_the_type_layer():
    """The old failure mode: 20 stanzas restating ORDER EXAMPLES verbatim."""
    from abcxauto.lab_playbook import empty_type_catalog
    from abcxauto.order_examples import ORDER_EXAMPLES

    assert empty_type_catalog() == {}
    save_lab(clamp_update({"types": {"bracket": {"gotchas": "limit entry can hang"}}}))
    types = load_lab()["types"]
    # Only what Grok touched, and no key derived from the ticket schema.
    assert list(types) == ["bracket"]
    assert type_schema_echo_keys(types) == []
    blob = json.dumps(types)
    for key in ORDER_EXAMPLES["bracket"]:
        assert f'"{key}"' not in blob
    assert "open_shape" not in json.dumps(load_lab())
    assert "close_tp_sl" not in json.dumps(load_lab())


def test_untouched_type_keeps_what_grok_wrote():
    save_lab(clamp_update({"types": {"bracket": {"gotchas": "limit entry can hang"}}}))
    save_lab(clamp_update({"types": {"iron_condor": {"review": "check both wings rest"}}}))
    types = load_lab()["types"]
    assert types["bracket"]["gotchas"] == "limit entry can hang"
    assert types["iron_condor"]["review"] == "check both wings rest"


def test_a_note_only_write_does_not_prune_the_cards_under_that_type():
    """Learnings and branches share a stanza; touching one must not cut the other."""
    _save(market_bracket=[_card("flush bounce")])
    save_lab(clamp_update({"types": {"market_bracket": {"gotchas": "stop side of last"}}}))
    lab = load_lab()
    assert lab["types"]["market_bracket"]["gotchas"] == "stop side of last"
    assert [c["name"] for c in type_cards(lab["types"], "market_bracket")] == [
        "flush bounce"
    ]
    # An explicit empty list is still how a type's cards are dropped.
    save_lab(clamp_update(_book(market_bracket={"cards": []})))
    assert type_cards(load_lab()["types"], "market_bracket") == []


# --- the tree: cards branch underneath their order type ----------------------


def test_cards_branch_under_their_order_type():
    _save(
        market_bracket=[_card("flush bounce"), _card("opening-range continuation")],
        vertical_spread=[_card("post-earnings IV crush")],
    )
    lab = load_lab()
    assert [c["name"] for c in type_cards(lab["types"], "market_bracket")] == [
        "flush bounce",
        "opening-range continuation",
    ]
    assert [c["name"] for c in type_cards(lab["types"], "vertical_spread")] == [
        "post-earnings IV crush"
    ]
    assert [t for t, _c in walk_cards(lab)] == [
        "market_bracket",
        "market_bracket",
        "vertical_spread",
    ]


def test_the_parent_type_is_the_ticket():
    """Nothing stores a ticket; the read-side projection stamps the parent on."""
    _save(vertical_spread=[_card("post-earnings IV crush")])
    stored = type_cards(load_lab()["types"], "vertical_spread")[0]
    assert "ticket" not in stored
    flat = load_lab()["cards"]
    assert flat[0]["ticket"] == "vertical_spread"
    assert flat[0]["type"] == "vertical_spread"


def test_a_card_ticket_matching_its_parent_is_accepted():
    _save(market_bracket=[_card("flush bounce", ticket="market_bracket")])
    assert [c["name"] for c in type_cards(load_lab()["types"], "market_bracket")] == [
        "flush bounce"
    ]


def test_a_card_ticket_conflicting_with_its_parent_is_rejected():
    """The two must never disagree silently — position wins or the write fails."""
    out = apply_from_judgment(
        {"lab_playbook": _book(market_bracket=[_card("flush bounce", ticket="buy_option")])}
    )
    assert out is not None
    assert out.get("status") == "rejected"
    note = str(out.get("note") or "")
    assert "must match the type it sits under" in note
    assert "buy_option under market_bracket" in note
    assert load_lab() == {}


def test_render_is_the_tree():
    _save(
        market_bracket={
            "gotchas": "stop side of last",
            "cards": [_card("flush bounce")],
        }
    )
    text = notebook_text(load_lab())
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0] == "TYPE market_bracket"
    assert lines[1] == "  gotchas: stop side of last"
    assert lines[2] == "  CARD flush bounce  [testing]"
    # The card's own fields sit deeper than the card header.
    assert any(ln.startswith("    thesis:") for ln in lines)
    assert "ticket=" not in text


def test_two_types_can_hold_the_same_card_name():
    """Identity is (type, name): the same idea through two structures is two tests."""
    _save(
        market_bracket=[_card("earnings flush")],
        vertical_spread=[_card("earnings flush")],
    )
    pairs = [(t, c["name"]) for t, c in walk_cards(load_lab())]
    assert pairs == [
        ("market_bracket", "earnings flush"),
        ("vertical_spread", "earnings flush"),
    ]


# --- cards carry thesis, evidence, and their own falsification ----------------


def test_card_carries_thesis_evidence_and_declared_death():
    _save(
        market_bracket=[
            _card(
                "mega-cap earnings-flush bounce",
                evidence={
                    "scan": "most_active + top_losers, mega only",
                    "news": "WMT sales miss, weakest US sales in six years",
                    "reads": "quote WMT 103.08; candles 5m holding the open low",
                    "odds": "Polymarket Fed-hold 82%",
                },
                retire_if={
                    "sample": 6,
                    "condition": "three closes back on the lows after entry",
                    "max_loss_usd": 400,
                },
            )
        ]
    )
    card = type_cards(load_lab()["types"], "market_bracket")[0]
    assert card["thesis"].startswith("flush into support")
    assert card["evidence"]["news"].startswith("WMT sales miss")
    assert card["evidence"]["odds"] == "Polymarket Fed-hold 82%"
    assert card["retire_if"] == {
        "sample": 6,
        "condition": "three closes back on the lows after entry",
        "max_loss_usd": 400.0,
    }
    # scan stays top-level too, so the existing cockpit row still paints.
    assert card["scan"].startswith("most_active")


def test_grok_cannot_declare_itself_graduated():
    _save(market_bracket=[_card("index momo", status="graduated")])
    assert type_cards(load_lab()["types"], "market_bracket")[0]["status"] == "working"
    assert graduated_card_names() == []


def test_missing_declaration_is_surfaced_not_rejected():
    """Revision 1 cards have no retire_if. They must survive the migration."""
    out = apply_from_judgment(
        {"lab_playbook": _book(market_bracket=[{"name": "legacy card"}])}
    )
    assert out is not None
    assert out.get("status") != "rejected"
    assert out["needs_declaration"] == ["legacy card [market_bracket]"]
    card = type_cards(load_lab()["types"], "market_bracket")[0]
    assert card["name"] == "legacy card"
    assert card["status"] == "testing"
    assert "retire_if" not in card


# --- the new-risk card gate ---------------------------------------------------


def test_new_risk_without_a_card_is_blocked_and_names_the_cards_under_that_type():
    _save(market_bracket=[_card("flush bounce"), _card("gap fade")])
    strat, forced = gate_ticket(_entry(), _world())
    assert strat == "blocked"
    note = str((forced or {}).get("note") or "")
    assert note.startswith(
        "new risk requires params.card naming a card under TYPE market_bracket; "
        "cards under market_bracket: "
    )
    assert "flush bounce" in note
    assert "gap fade" in note


def test_new_risk_points_at_the_right_trunk_when_the_cards_are_elsewhere():
    _save(vertical_spread=[_card("post-earnings IV crush")])
    strat, forced = gate_ticket(_entry(), _world())
    assert strat == "blocked"
    note = str((forced or {}).get("note") or "")
    assert "cards under market_bracket: none (elsewhere: " in note
    assert "post-earnings IV crush [vertical_spread]" in note


def test_new_risk_names_no_cards_when_the_book_is_empty():
    assert "write_lab_playbook first" in new_risk_card_error("")
    assert "write_lab_playbook first" in new_risk_card_error("", type="market_bracket")


def test_new_risk_with_a_live_card_passes_the_gate():
    _save(market_bracket=[_card("flush bounce")])
    strat, forced = gate_ticket(_entry("flush bounce"), _world())
    assert forced is None, forced
    assert strat == "market_bracket"
    # Case and padding are not the point of the gate.
    assert new_risk_card_error("  FLUSH Bounce ", type="market_bracket") == ""


def test_unknown_card_name_is_blocked_with_the_real_names():
    _save(market_bracket=[_card("flush bounce")])
    note = new_risk_card_error("moon shot", type="market_bracket")
    assert "'moon shot' is not on the playbook" in note
    assert "flush bounce" in note


def test_a_card_under_another_type_cannot_be_sent_as_this_one():
    """The ripple of nesting: the ticket has to be the trunk the card grew on."""
    _save(
        market_bracket=[_card("flush bounce")],
        vertical_spread=[_card("post-earnings IV crush")],
    )
    strat, forced = gate_ticket(_entry("post-earnings IV crush"), _world())
    assert strat == "blocked"
    note = str((forced or {}).get("note") or "")
    assert "lives under TYPE vertical_spread, not market_bracket" in note
    assert "send it as vertical_spread" in note
    # And it goes through fine on its own ticket.
    assert new_risk_card_error("post-earnings IV crush", type="vertical_spread") == ""


def test_an_ambiguous_bare_name_asks_for_the_type():
    _save(
        market_bracket=[_card("earnings flush")],
        vertical_spread=[_card("earnings flush")],
    )
    note = new_risk_card_error("earnings flush")
    assert "exists under TYPE market_bracket, vertical_spread" in note
    # Sending the ticket disambiguates it.
    assert new_risk_card_error("earnings flush", type="market_bracket") == ""


def test_top_level_card_arg_is_hoisted_into_params():
    from abcxauto.tool_args import normalize_tool_call

    _name, args = normalize_tool_call(
        "send", {"strategy": "market_bracket", "symbol": "QQQ", "card": "flush bounce"}
    )
    assert args["params"]["card"] == "flush bounce"


# --- exits are never blocked --------------------------------------------------


@pytest.mark.parametrize(
    "strategy,params",
    [
        ("modify_stop", {"symbol": "WMT", "order_id": 4445, "new_stop_price": 103.15}),
        ("modify_target", {"symbol": "WMT", "order_id": 4446, "new_limit_price": 106.0}),
        ("cancel_order", {"order_id": 4446}),
        ("trailing_stop", {"symbol": "WMT", "quantity": 70, "direction": "LONG",
                           "trail_percent": 2.0}),
        ("market_order", {"symbol": "WMT", "quantity": 70, "action": "SELL",
                          "closing_position": True}),
        ("limit_order", {"symbol": "WMT", "quantity": 70, "limit_price": 104.0,
                         "closing_position": True}),
        ("vertical_spread", {"symbol": "SPY", "closing_position": True, "quantity": 1}),
    ],
)
def test_management_and_exits_need_no_card(strategy, params):
    """Existing invariant: the clerk never strands a lot it let you open."""
    _save(market_bracket=[_card("flush bounce")])
    assert is_new_risk(strategy, params) is False
    strat, forced = gate_ticket(
        {"action": strategy, "strategy": strategy, "params": dict(params)}, _world()
    )
    assert strat == strategy, forced
    assert forced is None, forced


def test_exits_are_not_gated_even_when_the_tree_is_empty():
    """No book at all still must not strand a live lot."""
    assert load_lab() == {}
    strat, forced = gate_ticket(
        {
            "action": "market_order",
            "strategy": "market_order",
            "params": {
                "symbol": "WMT",
                "quantity": 70,
                "action": "SELL",
                "closing_position": True,
            },
        },
        _world(),
    )
    assert strat == "market_order"
    assert forced is None


def test_retired_card_cannot_open_but_its_lot_can_still_be_exited():
    _save(market_bracket=[_card("flush bounce", status="retired")])
    strat, forced = gate_ticket(_entry("flush bounce"), _world())
    assert strat == "blocked"
    note = str((forced or {}).get("note") or "")
    assert "retired" in note
    assert "under TYPE market_bracket" in note
    exit_strat, exit_forced = gate_ticket(
        {
            "action": "market_order",
            "strategy": "market_order",
            "params": {
                "symbol": "WMT",
                "quantity": 70,
                "action": "SELL",
                "closing_position": True,
                "card": "flush bounce",
            },
        },
        _world(),
    )
    assert exit_strat == "market_order"
    assert exit_forced is None


def test_tripped_card_cannot_open_but_can_still_be_exited(monkeypatch):
    _save(market_bracket=[_card("flush bounce")])
    monkeypatch.setattr(
        "abcxauto.lab_playbook.card_facts",
        lambda *_a, **_k: [
            {
                "card": "flush bounce",
                "type": "market_bracket",
                "tripped": True,
                "trip_reason": "declared sample 3 reached with resolved edge -80.00",
            }
        ],
    )
    strat, forced = gate_ticket(_entry("flush bounce"), _world())
    assert strat == "blocked"
    note = str((forced or {}).get("note") or "")
    assert "tripped its declared retire_if" in note
    assert "declared sample 3" in note
    close_strat, close_forced = gate_ticket(
        {
            "action": "close_option",
            "strategy": "close_option",
            "params": {"conId": 7, "quantity": 1, "symbol": "SPY", "card": "flush bounce"},
        },
        _world(positions=[{"symbol": "SPY", "sec_type": "OPT", "quantity": 1, "conId": 7}]),
    )
    assert close_strat == "close_option"
    assert close_forced is None


# --- attribution: how a trade ended ------------------------------------------


def _send(
    card,
    oids,
    *,
    ts,
    symbol="WMT",
    new_risk=True,
    strategy="market_bracket",
    card_type="market_bracket",
):
    row = {
        "ts": ts,
        "card": card,
        "strategy": strategy,
        "symbol": symbol,
        "order_ids": list(oids),
        "new_risk": new_risk,
    }
    if card_type is not None:
        row["type"] = card_type
    return row


def _fill(oid, pnl, *, ts, symbol="WMT"):
    return {"ts": ts, "order_id": oid, "symbol": symbol, "realized_pnl": pnl}


def _write_card_log(rows):
    """Card sends with fixed stamps. record_card_send stamps ``now``."""
    import os
    from pathlib import Path

    path = Path(os.environ["ABCXAUTO_CARD_LOG_PATH"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


def test_stop_or_target_fill_is_the_cards_own_exit():
    trades = classify_card_trades(
        [_send("flush bounce", [4444, 4445, 4446], ts="2026-08-20T14:00:00Z")],
        [_fill(4446, 310.0, ts="2026-08-20T15:00:00Z")],
        {4444, 4445, 4446},
    )
    assert [t["exit"] for t in trades] == [EXIT_PROTECTIVE]
    assert trades[0]["realized_pnl"] == 310.0
    assert trades[0]["type"] == "market_bracket"


def test_a_dispatched_close_is_groks_decision():
    trades = classify_card_trades(
        [
            _send("flush bounce", [4444, 4445, 4446], ts="2026-08-20T14:00:00Z"),
        ],
        [_fill(5001, -40.0, ts="2026-08-20T15:30:00Z")],
        {4444, 4445, 4446, 5001},
    )
    assert [t["exit"] for t in trades] == [EXIT_DECISION]


def test_an_undispatched_exit_is_an_operator_flatten():
    """Manual TWS flatten, another client session, or the panic/halt path."""
    trades = classify_card_trades(
        [_send("flush bounce", [4444, 4445, 4446], ts="2026-08-20T14:00:00Z")],
        [_fill(9999, 78.0, ts="2026-08-20T17:45:00Z")],
        {4444, 4445, 4446},
    )
    assert [t["exit"] for t in trades] == [EXIT_OPERATOR]
    assert trades[0]["realized_pnl"] == 78.0


def test_an_unresolved_trade_is_open_not_resolved():
    trades = classify_card_trades(
        [_send("flush bounce", [4444], ts="2026-08-20T14:00:00Z")], [], {4444}
    )
    assert [t["exit"] for t in trades] == [EXIT_OPEN]


def test_management_sends_do_not_count_as_trades():
    trades = classify_card_trades(
        [
            _send("flush bounce", [4444, 4445], ts="2026-08-20T14:00:00Z"),
            _send(
                "flush bounce",
                [4447],
                ts="2026-08-20T14:30:00Z",
                new_risk=False,
                strategy="modify_stop",
            ),
        ],
        [],
        set(),
    )
    assert len(trades) == 1


def test_one_exit_does_not_resolve_two_entries():
    trades = classify_card_trades(
        [
            _send("flush bounce", [10, 11], ts="2026-08-20T14:00:00Z"),
            _send("flush bounce", [20, 21], ts="2026-08-20T14:05:00Z"),
        ],
        [_fill(11, 50.0, ts="2026-08-20T15:00:00Z")],
        {10, 11, 20, 21},
    )
    kinds = sorted(t["exit"] for t in trades)
    assert kinds == [EXIT_OPEN, EXIT_PROTECTIVE]


def test_card_scores_separate_resolved_from_interrupted(monkeypatch):
    class _J:
        def realized_by_order_id(self, **_k):
            return {4446: 310.0, 9999: 78.0}

        def closing_fills(self, **_k):
            return [
                _fill(4446, 310.0, ts="2026-08-20T15:00:00Z"),
                _fill(9999, 78.0, ts="2026-08-20T18:00:00Z", symbol="COST"),
            ]

        def dispatched_order_ids(self, **_k):
            return {4444, 4445, 4446, 7001, 7002}

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: _J())
    _write_card_log(
        [
            _send("flush bounce", [4444, 4445, 4446], ts="2026-08-20T14:00:00+00:00"),
            _send(
                "flush bounce",
                [7001, 7002],
                ts="2026-08-20T17:00:00+00:00",
                symbol="COST",
            ),
        ]
    )
    row = card_scores([{"name": "flush bounce", "ticket": "market_bracket"}])[0]
    assert row["type"] == "market_bracket"
    assert row["trades"] == 2
    assert row["resolved"] == 1
    assert row["interrupted"] == 1
    assert row["resolved_pnl"] == 310.0
    assert row["interrupted_pnl"] == 78.0
    assert row["exits"][EXIT_PROTECTIVE] == 1
    assert row["exits"][EXIT_OPERATOR] == 1
    # The book number still reconciles: every attributed dollar is present.
    assert row["realized_pnl"] == 310.0
    assert row["attributed_fills"] == 1


def test_the_same_name_under_two_types_scores_as_two_experiments(monkeypatch):
    class _J:
        def realized_by_order_id(self, **_k):
            return {11: 200.0, 21: -50.0}

        def closing_fills(self, **_k):
            return [
                _fill(11, 200.0, ts="2026-08-20T15:00:00Z"),
                _fill(21, -50.0, ts="2026-08-20T16:00:00Z", symbol="SPY"),
            ]

        def dispatched_order_ids(self, **_k):
            return {10, 11, 20, 21}

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: _J())
    _save(
        market_bracket=[_card("earnings flush")],
        vertical_spread=[_card("earnings flush")],
    )
    _write_card_log(
        [
            _send("earnings flush", [10, 11], ts="2026-08-20T14:00:00+00:00"),
            _send(
                "earnings flush",
                [20, 21],
                ts="2026-08-20T14:30:00+00:00",
                symbol="SPY",
                strategy="vertical_spread",
                card_type="vertical_spread",
            ),
        ]
    )
    facts = {(r["type"], r["card"]): r for r in card_facts()}
    assert facts[("market_bracket", "earnings flush")]["resolved_pnl"] == 200.0
    assert facts[("vertical_spread", "earnings flush")]["resolved_pnl"] == -50.0


def test_a_legacy_name_only_row_resolves_when_one_type_claims_it(monkeypatch):
    """card_sends.jsonl predates nesting: those rows carry a name and no type."""
    monkeypatch.setattr(
        "abcxauto.memory.get_journal",
        lambda: type("J", (), {"realized_by_order_id": lambda self, **_k: {41: 120.0}})(),
    )
    _save(market_bracket=[_card("flush bounce")])
    _write_card_log(
        [
            _send(
                "flush bounce",
                [41],
                ts="2026-08-20T14:00:00+00:00",
                card_type=None,
            )
        ]
    )
    row = card_facts()[0]
    assert row["type"] == "market_bracket"
    assert row["realized_pnl"] == 120.0
    assert row["ambiguous_sends"] == 0


def test_a_legacy_name_only_row_is_attributed_to_neither_twin(monkeypatch):
    """Two types hold the name: guessing would credit a card that never asked."""
    monkeypatch.setattr(
        "abcxauto.memory.get_journal",
        lambda: type("J", (), {"realized_by_order_id": lambda self, **_k: {41: 500.0}})(),
    )
    _save(
        market_bracket=[_card("earnings flush")],
        vertical_spread=[_card("earnings flush")],
    )
    _write_card_log(
        [
            _send(
                "earnings flush",
                [41],
                ts="2026-08-20T14:00:00+00:00",
                strategy="bracket",
                card_type=None,
            )
        ]
    )
    facts = card_facts()
    assert [r["realized_pnl"] for r in facts] == [0.0, 0.0]
    # Not silently dropped either — both candidates are told it is unattributed.
    assert [r["ambiguous_sends"] for r in facts] == [1, 1]


def test_record_card_send_stamps_the_type_from_the_tree(tmp_path):
    _save(vertical_spread=[_card("post-earnings IV crush")])
    record_card_send(
        card="post-earnings IV crush",
        strategy="vertical_spread",
        symbol="SPY",
        result={"order_id": 55},
        params={"symbol": "SPY"},
    )
    rows = [
        json.loads(ln)
        for ln in (tmp_path / "cards.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert rows[0]["type"] == "vertical_spread"
    assert rows[0]["new_risk"] is True


def test_record_card_send_fires_and_joins_through_to_card_scores(monkeypatch):
    monkeypatch.setattr(
        "abcxauto.memory.get_journal",
        lambda: type("J", (), {"realized_by_order_id": lambda self, **_k: {41: 120.0}})(),
    )
    _save(market_bracket=[_card("flush bounce")])
    record_card_send(
        card="flush bounce",
        strategy="market_bracket",
        symbol="NVDA",
        result={"order_id": 41},
        params={"symbol": "NVDA"},
    )
    row = card_scores(load_lab()["cards"])[0]
    assert row["sends"] == 1
    assert row["realized_pnl"] == 120.0
    assert row["on_current_book"] is True


def test_send_tool_tags_the_card_on_a_successful_dispatch(monkeypatch, tmp_path):
    """params.card must reach record_card_send through the real send path."""
    import asyncio

    from abcxauto.brain import BrainTurn, _run_tool

    _save(market_bracket=[_card("flush bounce")])
    seen: list[dict] = []
    monkeypatch.setattr(
        "abcxauto.agent_loop.execute_ticket",
        _fake_execute({"status": "submitted", "success": True, "order_ids": [77, 78]}),
    )
    monkeypatch.setattr(
        "abcxauto.lab_playbook.record_card_send",
        lambda **kw: seen.append(kw),
    )
    turn = BrainTurn()
    asyncio.run(
        _run_tool(
            "send",
            {
                "strategy": "market_bracket",
                "symbol": "QQQ",
                "quantity": 1,
                "direction": "LONG",
                "card": "flush bounce",
            },
            connector=None,
            world=_world(),
            snap={},
            turn=turn,
        )
    )
    assert seen and seen[0]["card"] == "flush bounce"
    assert seen[0]["strategy"] == "market_bracket"


def _fake_execute(result):
    async def _run(act, connector, world, snap):
        return dict(result)

    return _run


# --- graduation ---------------------------------------------------------------


def test_card_graduates_only_at_its_declared_sample_with_positive_edge():
    card = {
        **_card("flush bounce", retire_if={"sample": 3, "condition": "no bounce"}),
        "type": "market_bracket",
    }
    short = card_verdict({"resolved": 2, "resolved_pnl": 900.0}, card)
    assert short["graduated"] is False
    assert short["tripped"] is False
    assert short["sample_left"] == 1

    met = card_verdict({"resolved": 3, "resolved_pnl": 900.0}, card)
    assert met["graduated"] is True
    assert met["anchored_type"] == "market_bracket"

    flat = card_verdict({"resolved": 3, "resolved_pnl": 0.0}, card)
    assert flat["graduated"] is False
    assert flat["tripped"] is True
    assert "declared sample 3" in flat["trip_reason"]


def test_card_without_a_declaration_can_neither_graduate_nor_trip():
    card = {"name": "legacy", "type": "market_bracket", "status": "testing"}
    verdict = card_verdict({"resolved": 40, "resolved_pnl": 5000.0}, card)
    assert verdict["graduated"] is False
    assert verdict["tripped"] is False
    assert verdict["needs_retire_if"] is True
    assert verdict["needs_thesis"] is True


# --- calibration: the claimed hit rate against the delivered one --------------


def test_declared_hit_rate_survives_a_write_as_a_percent():
    _save(
        market_bracket=[
            _card("as percent", expect_hit_rate=62),
            _card("as fraction", expect_hit_rate=0.62),
            _card("certain", expect_hit_rate=1),
            _card("nonsense", expect_hit_rate=140),
            _card("undeclared"),
        ]
    )
    cards = {c["name"]: c for _t, c in walk_cards(load_lab())}
    # A model writes 62 or 0.62 and means the same thing; 1 is certainty.
    assert cards["as percent"]["expect_hit_rate"] == 62.0
    assert cards["as fraction"]["expect_hit_rate"] == 62.0
    assert cards["certain"]["expect_hit_rate"] == 100.0
    # Out of range is dropped, not clamped to a number Grok never claimed.
    assert "expect_hit_rate" not in cards["nonsense"]
    assert "expect_hit_rate" not in cards["undeclared"]


def test_thin_resolved_sample_reports_no_hit_rate():
    card = _card("flush bounce", expect_hit_rate=70)
    cal = card_calibration({"resolved": 3, "resolved_wins": 3}, card)
    assert cal["hit_rate"] is None
    assert cal["hit_rate_gap"] is None
    assert "thin resolved sample" in cal["note"]
    assert cal["expect_hit_rate"] == 70.0


def test_hit_rate_gap_is_measured_against_the_claim():
    card = _card("flush bounce", expect_hit_rate=70)
    cal = card_calibration({"resolved": 10, "resolved_wins": 4}, card)
    assert cal["hit_rate"] == 40.0
    assert cal["hit_rate_gap"] == -30.0
    assert cal["note"] == ""

    honest = card_calibration({"resolved": 10, "resolved_wins": 7}, card)
    assert honest["hit_rate"] == 70.0
    assert honest["hit_rate_gap"] == 0.0


def test_hit_rate_is_measured_even_with_nothing_declared():
    cal = card_calibration({"resolved": 8, "resolved_wins": 2}, _card("no claim"))
    assert cal["hit_rate"] == 25.0
    assert cal["hit_rate_gap"] is None
    assert "no expect_hit_rate declared" in cal["note"]


def test_miscalibration_is_reported_but_never_blocks_graduation():
    """One fat winner on a 40% hit rate still graduates — the gap is the tell."""
    card = {
        **_card(
            "flush bounce",
            retire_if={"sample": 10, "condition": "no bounce"},
            expect_hit_rate=70,
        ),
        "type": "market_bracket",
    }
    verdict = card_verdict(
        {"resolved": 10, "resolved_wins": 4, "resolved_pnl": 900.0}, card
    )
    assert verdict["graduated"] is True
    assert verdict["tripped"] is False
    assert verdict["trip_reason"] == ""
    assert verdict["calibration"]["hit_rate_gap"] == -30.0


def test_wins_are_counted_off_resolved_exits_and_reach_the_card(monkeypatch):
    """End to end: declared claim, four protective exits, measured hit rate."""
    pnls = {102: -40.0, 202: 120.0, 302: 80.0, 402: 60.0}

    class _J:
        def realized_by_order_id(self, **_k):
            return dict(pnls)

        def closing_fills(self, **_k):
            return [
                _fill(102, -40.0, ts="2026-08-20T15:00:00Z", symbol="WMT"),
                _fill(202, 120.0, ts="2026-08-20T16:00:00Z", symbol="COST"),
                _fill(302, 80.0, ts="2026-08-20T17:00:00Z", symbol="TGT"),
                _fill(402, 60.0, ts="2026-08-20T18:00:00Z", symbol="XLE"),
            ]

        def dispatched_order_ids(self, **_k):
            return {101, 102, 201, 202, 301, 302, 401, 402}

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: _J())
    _save(
        market_bracket=[
            _card(
                "flush bounce",
                retire_if={"sample": 4, "condition": "x"},
                expect_hit_rate=70,
            )
        ]
    )
    _write_card_log([
        _send("flush bounce", [101, 102], ts="2026-08-20T14:00:00+00:00", symbol="WMT"),
        _send("flush bounce", [201, 202], ts="2026-08-20T14:10:00+00:00", symbol="COST"),
        _send("flush bounce", [301, 302], ts="2026-08-20T14:20:00+00:00", symbol="TGT"),
        _send("flush bounce", [401, 402], ts="2026-08-20T14:30:00+00:00", symbol="XLE"),
    ])
    row = card_facts()[0]
    assert row["resolved"] == 4
    assert row["resolved_wins"] == 3
    assert row["resolved_losses"] == 1
    cal = row["calibration"]
    assert cal["hit_rate"] == 75.0
    assert cal["expect_hit_rate"] == 70.0
    assert cal["hit_rate_gap"] == 5.0


def test_declared_max_loss_and_max_losses_trip_early():
    card = _card(
        "flush bounce",
        retire_if={
            "sample": 20,
            "condition": "no bounce",
            "max_loss_usd": 300,
            "max_losses": 2,
        },
    )
    by_loss = card_verdict({"resolved": 3, "resolved_pnl": -320.0}, card)
    assert by_loss["tripped"] is True
    assert "max_loss_usd" in by_loss["trip_reason"]

    by_count = card_verdict(
        {"resolved": 4, "resolved_pnl": -10.0, "resolved_losses": 2}, card
    )
    assert by_count["tripped"] is True
    assert "max_losses" in by_count["trip_reason"]


def test_operator_exits_do_not_advance_a_card_toward_graduation(monkeypatch):
    """The correctness case: interrupted trades are not a tested sample."""

    class _J:
        def realized_by_order_id(self, **_k):
            return {9001: 120.0, 9002: 140.0, 9003: 160.0}

        def closing_fills(self, **_k):
            return [
                _fill(9001, 120.0, ts="2026-08-20T15:00:00Z", symbol="WMT"),
                _fill(9002, 140.0, ts="2026-08-20T16:00:00Z", symbol="COST"),
                _fill(9003, 160.0, ts="2026-08-20T17:00:00Z", symbol="TGT"),
            ]

        def dispatched_order_ids(self, **_k):
            # Entries were dispatched; not one of the exits was.
            return {1, 2, 3}

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: _J())
    _save(
        market_bracket=[
            _card("flush bounce", retire_if={"sample": 3, "condition": "x"})
        ]
    )
    _write_card_log(
        [
            _send("flush bounce", [1], ts="2026-08-20T14:00:00+00:00", symbol="WMT"),
            _send("flush bounce", [2], ts="2026-08-20T14:10:00+00:00", symbol="COST"),
            _send("flush bounce", [3], ts="2026-08-20T14:20:00+00:00", symbol="TGT"),
        ]
    )
    row = card_facts()[0]
    assert row["trades"] == 3
    assert row["interrupted"] == 3
    assert row["resolved"] == 0
    assert row["interrupted_pnl"] == 420.0
    # Three profitable flattens, and the card has learned nothing.
    assert row["graduated"] is False
    assert row["tripped"] is False
    assert row["sample_left"] == 3
    assert graduated_card_names() == []
    assert maybe_promote() is None


def test_halt_and_panic_exits_read_as_operator_exits():
    """The panic path does not dispatch, so its fills look like a manual flatten."""
    trades = classify_card_trades(
        [_send("flush bounce", [1], ts="2026-08-20T14:00:00Z")],
        [_fill(4242, -900.0, ts="2026-08-20T15:00:00Z")],
        {1},
    )
    assert trades[0]["exit"] == EXIT_OPERATOR


# --- promote -----------------------------------------------------------------


def test_live_snapshot_holds_only_graduated_cards_inside_their_types(monkeypatch):
    save_lab(
        clamp_update(
            {
                "ready_to_promote": True,
                "instructions": "regime prose that live has no business reading",
                "types": {
                    "market_bracket": {
                        "gotchas": "stop side of last",
                        "cards": [_card("flush bounce"), _card("half-gap fade")],
                    },
                    "iron_condor": {
                        "gotchas": "both wings must rest",
                        "cards": [_card("condor grind")],
                    },
                    "bracket": {"gotchas": "limit entry hangs"},
                },
            }
        )
    )
    monkeypatch.setattr(
        "abcxauto.lab_playbook.card_facts",
        lambda *_a, **_k: [
            {"card": "flush bounce", "type": "market_bracket", "graduated": True},
            {"card": "half-gap fade", "type": "market_bracket", "graduated": False},
            {"card": "condor grind", "type": "iron_condor", "graduated": False},
        ],
    )
    live = maybe_promote()
    assert live is not None
    on_disk = load_live()
    # The graduated card travels inside its own pruned stanza.
    assert list(on_disk["types"]) == ["market_bracket"]
    assert on_disk["types"]["market_bracket"]["gotchas"] == "stop side of last"
    assert [c["name"] for c in on_disk["types"]["market_bracket"]["cards"]] == [
        "flush bounce"
    ]
    assert on_disk["graduated"] == ["flush bounce"]
    assert "regime prose" not in json.dumps(on_disk)
    assert "half-gap fade" not in json.dumps(on_disk)
    assert "condor grind" not in json.dumps(on_disk)


def test_live_new_risk_needs_a_graduated_card_and_must_cite_it(monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    from abcxauto.lab_playbook import live_new_risk_allowed

    assert live_new_risk_allowed() is False

    assert load_live() == {}  # nothing promoted yet

    from abcxauto.lab_playbook import _live_path, _write

    _write(
        _live_path(),
        {
            "promoted": True,
            "types": {
                "market_bracket": {"cards": [_card("flush bounce")]},
                "iron_condor": {"cards": [_card("condor grind")]},
            },
            "graduated": ["flush bounce"],
        },
    )
    assert live_new_risk_allowed() is True
    assert new_risk_card_error("flush bounce", type="market_bracket") == ""
    note = new_risk_card_error("condor grind", type="iron_condor")
    assert "must cite a graduated card" in note
    assert "flush bounce" in note


def test_a_promoted_snapshot_with_no_graduated_card_does_not_unlock_live(monkeypatch):
    from abcxauto.lab_playbook import _live_path, _write, live_has_promoted

    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    _write(
        _live_path(),
        {"promoted": True, "instructions": "SETUP anything", "graduated": []},
    )
    assert live_has_promoted() is False


# --- migration of the operator's revision 1 ----------------------------------


REV_1_CARDS = [
    {
        "name": "mega-cap earnings-flush bounce",
        "when_on": "Mega/large-cap with a >=6% earnings gap holding the open low.",
        "scan": "most_active + top_losers; mega/large only",
        "ticket": "market_bracket",
        "shape": "LONG STK. Stop under opening low.",
        "invalidation": "Stop through opening low.",
        "status": "testing",
        "note": "LIVE: WMT 70 long @ 103.08, stop 101.0 oid 4445, target 107.5 oid 4446.",
    },
    {
        "name": "levered-crypto and micro gap chase",
        "when_on": "Never.",
        "scan": "top_gainers, high_open_gap",
        "ticket": "market_bracket",
        "shape": "none",
        "invalidation": "n/a",
        "status": "retired",
        "note": "Not an edge.",
    },
    {
        "name": "naked / short-dated option spray",
        "when_on": "Never.",
        "scan": "n/a",
        "ticket": "buy_option",
        "shape": "none",
        "invalidation": "n/a",
        "status": "retired",
        "note": "Options were the inception drawdown.",
    },
]


def test_revision_1_cards_migrate_into_the_tree_untouched():
    """A replay of the operator's real revision 1, which had no type stanzas."""
    from abcxauto.lab_playbook import _lab_path, _write

    _write(
        _lab_path(),
        {
            "mode": "explore",
            "instructions": "2026-08-20 13:33 ET regime notes.",
            "cards": REV_1_CARDS,
            "types": {},
            "revision": 1,
        },
    )
    lab = load_lab()
    # Each card landed under the type its ticket named; the stanzas were created.
    assert [c["name"] for c in type_cards(lab["types"], "market_bracket")] == [
        "mega-cap earnings-flush bounce",
        "levered-crypto and micro gap chase",
    ]
    assert [c["name"] for c in type_cards(lab["types"], "buy_option")] == [
        "naked / short-dated option spray"
    ]
    assert lab.get("unfiled_cards") is None
    # Every field, note and status survived.
    for src in REV_1_CARDS:
        got = next(c for _t, c in walk_cards(lab) if c["name"] == src["name"])
        for key in ("when_on", "scan", "shape", "invalidation", "status", "note"):
            assert got.get(key, "") == src.get(key, ""), (src["name"], key)
        assert "ticket" not in got
    live = type_cards(lab["types"], "market_bracket")[0]
    assert "WMT 70 long" in live["note"]
    # No declaration yet: owed on the next write, not invalid.
    assert "retire_if" not in live
    facts = {r["card"]: r for r in card_facts(lab)}
    flush = facts["mega-cap earnings-flush bounce"]
    assert flush["type"] == "market_bracket"
    assert flush["needs_retire_if"] is True
    assert flush["needs_thesis"] is True
    assert flush["tripped"] is False
    assert flush["graduated"] is False
    # The undeclared card can still open risk on its own ticket.
    assert new_risk_card_error(
        "mega-cap earnings-flush bounce", type="market_bracket"
    ) == ""
    assert "retired" in new_risk_card_error(
        "naked / short-dated option spray", type="buy_option"
    )


def test_migration_never_drops_a_card_it_cannot_file():
    """A card naming nothing sendable is parked, not deleted."""
    from abcxauto.lab_playbook import _lab_path, _write

    _write(
        _lab_path(),
        {
            "revision": 1,
            "cards": [
                {"name": "homeless", "note": "keep me"},
                {"name": "filed", "ticket": "market_bracket"},
            ],
        },
    )
    lab = load_lab()
    assert [c["name"] for c in type_cards(lab["types"], "market_bracket")] == ["filed"]
    assert [c["name"] for c in lab["unfiled_cards"]] == ["homeless"]
    assert lab["unfiled_cards"][0]["note"] == "keep me"
    # It is visible in the render and in the scores, but it cannot take risk.
    assert "UNFILED" in notebook_text(lab)
    assert {r["card"] for r in card_facts(lab)} == {"filed", "homeless"}
    assert "no parent order type" in new_risk_card_error("homeless")


def test_legacy_type_schema_echo_is_dropped_on_read():
    from abcxauto.lab_playbook import _lab_path, _write

    _write(
        _lab_path(),
        {
            "revision": 1,
            "types": {
                "market_bracket": {
                    "defined_risk": True,
                    "open_shape": "symbol, quantity, direction, stop_price, target_price",
                    "close_tp_sl": "child stop + target; modify_stop / modify_target",
                    "strategies": [{"name": "old branch"}],
                    "gotchas": "the one line worth keeping",
                }
            },
        },
    )
    types = load_lab()["types"]
    assert types["market_bracket"] == {"gotchas": "the one line worth keeping"}
    assert type_schema_echo_keys(types) == []


def test_a_write_after_migration_keeps_the_migrated_cards():
    """The first nested write must not wipe what revision 1 left behind."""
    from abcxauto.lab_playbook import _lab_path, _write

    _write(_lab_path(), {"revision": 1, "cards": REV_1_CARDS, "types": {}})
    save_lab(clamp_update({"types": {"market_bracket": {"gotchas": "stop side of last"}}}))
    lab = load_lab()
    assert len(walk_cards(lab)) == 3
    assert lab["types"]["market_bracket"]["gotchas"] == "stop side of last"


# --- clerk gates that must not regress ---------------------------------------


def test_two_layer_write_still_rejects_knobs_and_invented_gates():
    out = apply_from_judgment(
        {
            "lab_playbook": {
                "instructions": "Prefer flush bounces.\nGATES: 0.5% / floor 0.5% NL",
                "trading_mode": "live",
                "sizing_floors": False,
                "trading_budget_usd": 50_000,
                **_book(market_bracket=[_card("flush bounce")]),
            }
        }
    )
    assert out is not None
    rejected = out.get("rejected") or {}
    assert "trading_mode" in rejected
    assert "sizing_floors" in rejected
    assert "trading_budget_usd" in rejected
    assert "invented_pct_gate" in rejected
    lab = load_lab()
    assert "Prefer flush bounces." in lab["instructions"]
    assert "GATES: 0.5%" not in lab["instructions"]
    assert type_cards(lab["types"], "market_bracket")[0]["name"] == "flush bounce"


def test_prose_instructions_still_save_alongside_the_tree():
    out = apply_from_judgment(
        {
            "lab_playbook": {
                "instructions": "WMT flushed on a sales miss; peers held.",
                "types": {
                    "market_bracket": {
                        "gotchas": "stop side of last",
                        "cards": [_card("flush bounce")],
                    }
                },
            }
        }
    )
    assert out is not None
    lab = load_lab()
    assert lab["instructions"] == "WMT flushed on a sales miss; peers held."
    assert lab["types"]["market_bracket"]["gotchas"] == "stop side of last"
    assert type_cards(lab["types"], "market_bracket")[0]["name"] == "flush bounce"


def test_card_ticket_must_still_be_a_sendable_type():
    out = apply_from_judgment(
        {"lab_playbook": {"cards": [{"name": "moon", "ticket": "yolo_calls"}]}}
    )
    assert out is not None
    assert out.get("status") == "rejected"


def test_an_unknown_type_key_is_still_refused():
    out = apply_from_judgment(
        {"lab_playbook": {"types": {"yolo_calls": {"cards": [_card("moon")]}}}}
    )
    assert out is not None
    assert out.get("status") == "rejected"


def test_book_payload_surfaces_which_cards_earned(monkeypatch):
    from abcxauto.brain import _book_payload

    _save(
        market_bracket={
            "gotchas": "stop side of last",
            "cards": [_card("flush bounce")],
        }
    )
    monkeypatch.setattr(
        "abcxauto.lab_playbook.card_facts",
        lambda *_a, **_k: [
            {
                "card": "flush bounce",
                "type": "market_bracket",
                "resolved": 3,
                "interrupted": 2,
                "resolved_pnl": 240.0,
                "anchored_type": "market_bracket",
                "graduated": True,
                "tripped": False,
                "needs_retire_if": False,
                "needs_thesis": False,
            }
        ],
    )
    pb = _book_payload(_world())["playbook"]
    assert pb["graduated"] == ["flush bounce [market_bracket]"]
    assert pb["tripped"] == []
    assert pb["card_scores"][0]["interrupted"] == 2
    assert pb["card_scores"][0]["anchored_type"] == "market_bracket"
    assert pb["types"]["market_bracket"]["gotchas"] == "stop side of last"
    # The tree stanza carries its own branches, and the flat view still exists.
    assert [c["name"] for c in pb["types"]["market_bracket"]["cards"]] == [
        "flush bounce"
    ]
    assert pb["cards"][0]["ticket"] == "market_bracket"
    assert "TYPE market_bracket" in pb["notes"]


def test_playbook_tool_reports_the_tree_and_the_verdicts():
    from abcxauto.lab_playbook import playbook_payload

    _save(market_bracket={"review": "fills then book", "cards": [_card("flush bounce")]})
    payload = playbook_payload()
    assert payload["types"]["market_bracket"]["review"] == "fills then book"
    assert [c["name"] for c in payload["types"]["market_bracket"]["cards"]] == [
        "flush bounce"
    ]
    assert payload["cards"][0]["name"] == "flush bounce"
    assert payload["cards"][0]["ticket"] == "market_bracket"
    assert "TYPE market_bracket" in payload["tree"]
    assert payload["graduated"] == []
    assert payload["tripped"] == []
    assert payload["needs_declaration"] == []
