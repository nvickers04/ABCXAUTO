"""One playbook tree: durable order types, disposable cards branching under them.

    TYPE market_bracket          <- learned execution, durable
      |- card: mega-cap earnings-flush bounce
      |- card: opening-range continuation

A card's position *is* its ticket, so identity is ``(type, name)`` and a winning
card sits inside the type entry it is supposed to improve.

The clerk's half is attribution: a named ``params.card`` tags the fill so the
card is scored on the trades that actually resolved. A missing card is not a
refuse — the lab playbook is a notebook, not a send gate. An operator flatten
or a halt exit is real P&L but not evidence, so it never advances a card toward
live money. Live new risk still needs a promoted snapshot.
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
    card_waiting,
    lab_facts,
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
                when_on="mega/large >=6% earnings-miss gap",
                scan="most_active + top_losers",
                shape="LONG STK market_bracket",
                invalidation="stop through opening low",
                status="testing",
                evidence={"news": "no fresh mega miss", "reads": "BABA -4%"},
            )
        ]
    )
    # A look later: same card, fresh note, gate and declaration not restated.
    _save(
        market_bracket=[
            {"name": "flush bounce", "note": "10:12 ET gate off, BABA only -4%"}
        ]
    )
    card = type_cards(load_lab()["types"], "market_bracket")[0]
    assert not (card.get("note") or "").startswith("10:12 ET")
    assert card["retire_if"] == {
        "sample": 8,
        "condition": "three closes on the lows",
    }
    assert card["thesis"].startswith("mega-cap sales-miss")
    assert card["expect_hit_rate"] == 45.0
    assert card["when_on"].startswith("mega/large")
    assert card["scan"].startswith("most_active")
    assert card["shape"].startswith("LONG STK")
    assert card["invalidation"].startswith("stop through")
    assert card["status"] == "testing"
    assert card["evidence"]["news"].startswith("no fresh")
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


def test_a_named_write_keeps_siblings_left_out_of_the_list():
    """The replace-list was the wipe. One-card write must not drop siblings."""
    _save(market_bracket=[_card("flush bounce"), _card("opening drive")])
    _save(market_bracket=[_card("flush bounce", thesis="flush into support still bounces")])
    names = [c["name"] for c in type_cards(load_lab()["types"], "market_bracket")]
    assert names == ["flush bounce", "opening drive"]
    by_name = {c["name"]: c for c in type_cards(load_lab()["types"], "market_bracket")}
    assert by_name["flush bounce"]["thesis"] == "flush into support still bounces"
    assert by_name["opening drive"]["thesis"] == "flush into support bounces"


def test_status_retired_drops_a_card_from_the_hunt_not_by_omission():
    _save(market_bracket=[_card("flush bounce"), _card("opening drive")])
    _save(market_bracket=[_card("opening drive", status="retired")])
    lab = load_lab()
    by_name = {c["name"]: c for c in type_cards(lab["types"], "market_bracket")}
    assert by_name["flush bounce"]["status"] == "testing"
    assert by_name["opening drive"]["status"] == "retired"
    from abcxauto.lab_playbook import playbook_run_sheets

    assert [row["card"] for row in playbook_run_sheets(lab, flat=True)] == [
        "flush bounce"
    ]


def test_lab_facts_reports_what_is_untried_not_only_what_scored():
    """strategy_scores says what ran; this says what never has."""
    from abcxauto.lab_playbook import lab_facts, open_playbook_types, playbook_payload

    _save(
        market_bracket={"cards": [_card("flush bounce"), _card("opening drive")]},
        buy_option={"cards": [_card("premium spray", status="retired")]},
        vertical_spread={"gotchas": "BAG close is one ticket"},
    )
    facts = lab_facts(load_lab())
    assert facts["cards"]["testing"] >= 2
    assert facts["cards"]["retired"] == 1
    assert facts["resolved_trades"] == 0
    awaiting = {r["card"]: r for r in facts["cards_awaiting_first_trade"]}
    assert "flush bounce [market_bracket]" in awaiting
    assert "opening drive [market_bracket]" in awaiting
    for row in awaiting.values():
        assert row["sends"] == 0
        assert row["days"] is not None
    assert "market_bracket" in facts["trunks_with_cards"]
    assert "buy_option" in facts["trunks_with_cards"]
    untried = facts["entry_trunks_untried"]
    # OPEN types get a locked starter — they are no longer empty slots.
    for name in open_playbook_types():
        assert name not in untried, name
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
    from abcxauto.lab_playbook import lab_wake_bit

    bit = lab_wake_bit(load_lab())
    assert "lab flush bounce" in bit
    assert "0sends" in bit
    # A live card under test is the wake — untried trunks stay on playbook().
    assert "untried=" not in bit


def test_playbook_clip_keeps_lab_ahead_of_the_essay():
    """A 16k notebook used to push lab past the 24k tool clip."""
    from abcxauto.brain import _clip
    from abcxauto.lab_playbook import playbook_payload

    _save(market_bracket={"note": "x" * 20_000, "cards": [_card("flush bounce")]})
    raw = playbook_payload()
    assert list(raw).index("lab") < list(raw).index("tree")
    clipped = _clip(raw)
    assert "entry_trunks_untried" in clipped
    assert "cards_awaiting_first_trade" in clipped
    assert "flush bounce" in clipped
    from abcxauto.brain import PLAYBOOK_CLIP_CHARS

    wide = _clip(raw, max_chars=PLAYBOOK_CLIP_CHARS)
    parsed = json.loads(wide)
    assert parsed["cards"][0]["name"] == "flush bounce"
    assert "TYPE market_bracket" in parsed["tree"]
    tight = json.loads(_clip(raw, max_chars=24_000))
    names = {c["name"] for c in (tight.get("cards") or [])}
    assert "flush bounce" in names
    from abcxauto.lab_playbook import OPEN_TYPE_STARTERS

    assert OPEN_TYPE_STARTERS["iron_condor"]["name"] in names


def test_type_coverage_lists_every_trunk_without_seeding_schema():
    """Operator sees all sendable types. OPEN trunks get a hypothesis card, not schema."""
    from abcxauto.lab_playbook import (
        open_playbook_types,
        playbook_type_keys,
        type_coverage,
        type_schema_echo_keys,
    )

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

    vs = rows["vertical_spread"]
    assert vs["touched"] is True
    assert vs["cards"] >= 1
    assert "tool_order" in vs["learned"]

    for name in open_playbook_types():
        assert rows[name]["cards"] >= 1, name
        assert rows[name]["touched"] is True

    # Schema echoes stay out. Hypothesis cards are not ORDER EXAMPLES copies.
    assert type_schema_echo_keys(lab["types"]) == []
    assert "open_shape" not in json.dumps(lab)
    assert "close_tp_sl" not in json.dumps(lab)


def test_clerk_never_writes_order_examples_into_the_type_layer():
    """The old failure mode: 20 stanzas restating ORDER EXAMPLES verbatim."""
    from abcxauto.lab_playbook import empty_type_catalog
    from abcxauto.order_examples import ORDER_EXAMPLES

    assert empty_type_catalog() == {}
    save_lab(clamp_update({"types": {"bracket": {"gotchas": "limit entry can hang"}}}))
    types = load_lab()["types"]
    assert types["bracket"]["gotchas"] == "limit entry can hang"
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
    # An explicit empty list drops Grok's cards; the locked OPEN starter returns.
    save_lab(clamp_update(_book(market_bracket={"cards": []})))
    names = [c["name"] for c in type_cards(load_lab()["types"], "market_bracket")]
    assert "flush bounce" not in names
    assert names == ["generic STK market bracket"]


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
    vs_names = [c["name"] for c in type_cards(lab["types"], "vertical_spread")]
    assert vs_names[0] == "post-earnings IV crush"
    assert "defined-risk debit/credit vertical" in vs_names
    pairs = [t for t, _c in walk_cards(lab)]
    assert pairs.count("market_bracket") == 2
    assert "vertical_spread" in pairs


def test_the_parent_type_is_the_ticket():
    """Nothing stores a ticket; the read-side projection stamps the parent on."""
    _save(vertical_spread=[_card("post-earnings IV crush")])
    stored = type_cards(load_lab()["types"], "vertical_spread")[0]
    assert stored["name"] == "post-earnings IV crush"
    assert "ticket" not in stored
    flat = [
        c for c in load_lab()["cards"]
        if c.get("name") == "post-earnings IV crush"
    ]
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
    assert ("market_bracket", "earnings flush") in pairs
    assert ("vertical_spread", "earnings flush") in pairs


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


# --- the new-risk card gate is not a send gate --------------------------------


def test_paper_new_risk_without_a_card_is_not_a_notebook_block():
    """Paper 7497: Grok owns the ticket. Missing params.card is not a refuse."""
    _save(market_bracket=[_card("flush bounce"), _card("gap fade")])
    strat, forced = gate_ticket(_entry(), _world())
    assert forced is None, forced
    assert strat == "market_bracket"
    assert new_risk_card_error("") == ""
    assert new_risk_card_error("", type="market_bracket") == ""


def test_paper_new_risk_does_not_care_which_trunk_holds_the_cards():
    _save(vertical_spread=[_card("post-earnings IV crush")])
    strat, forced = gate_ticket(_entry(), _world())
    assert forced is None, forced
    assert strat == "market_bracket"
    assert new_risk_card_error("post-earnings IV crush", type="market_bracket") == ""


def test_new_risk_card_error_is_a_noop_even_when_the_book_is_empty():
    assert new_risk_card_error("") == ""
    assert new_risk_card_error("", type="market_bracket") == ""
    assert new_risk_card_error("moon shot", type="market_bracket") == ""
    assert new_risk_card_error("homeless") == ""


def test_new_risk_with_a_named_card_still_passes():
    _save(market_bracket=[_card("flush bounce")])
    strat, forced = gate_ticket(_entry("flush bounce"), _world())
    assert forced is None, forced
    assert strat == "market_bracket"
    assert new_risk_card_error("  FLUSH Bounce ", type="market_bracket") == ""


def test_unknown_card_name_is_not_a_send_block():
    _save(market_bracket=[_card("flush bounce")])
    assert new_risk_card_error("moon shot", type="market_bracket") == ""
    strat, forced = gate_ticket(_entry("moon shot"), _world())
    assert forced is None, forced
    assert strat == "market_bracket"


def test_a_card_under_another_type_is_not_a_send_block():
    """Notebook nesting is attribution, not a clerk refuse."""
    _save(
        market_bracket=[_card("flush bounce")],
        vertical_spread=[_card("post-earnings IV crush")],
    )
    strat, forced = gate_ticket(_entry("post-earnings IV crush"), _world())
    assert forced is None, forced
    assert strat == "market_bracket"
    assert new_risk_card_error("post-earnings IV crush", type="vertical_spread") == ""


def test_an_ambiguous_bare_name_is_not_a_send_block():
    _save(
        market_bracket=[_card("earnings flush")],
        vertical_spread=[_card("earnings flush")],
    )
    assert new_risk_card_error("earnings flush") == ""
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


def test_retired_card_is_not_a_send_block_and_its_lot_can_still_be_exited():
    _save(market_bracket=[_card("flush bounce", status="retired")])
    strat, forced = gate_ticket(_entry("flush bounce"), _world())
    assert forced is None, forced
    assert strat == "market_bracket"
    assert new_risk_card_error("flush bounce", type="market_bracket") == ""
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


def test_tripped_card_is_not_a_send_block_and_can_still_be_exited(monkeypatch):
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
    assert forced is None, forced
    assert strat == "market_bracket"
    assert new_risk_card_error("flush bounce", type="market_bracket") == ""
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
    facts = [r for r in card_facts() if r.get("card") == "earnings flush"]
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


def test_looks_without_a_send_are_counted_and_never_trip(monkeypatch):
    """A trigger that never prints is a fact. Grok retires it, not the clerk."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)
    written = (now - timedelta(days=4)).isoformat()

    class _J:
        def model_usage_since(self, _since):
            return {"calls": 17}

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: _J())
    _save(
        market_bracket=[
            _card(
                "flush bounce",
                retire_if={
                    "sample": 8,
                    "condition": "no bounce",
                    "max_looks_without_trigger": 12,
                },
            )
        ]
    )
    stored = type_cards(load_lab()["types"], "market_bracket")[0]
    stored["written_at"] = written
    wait = card_waiting({"sends": 0, "trades": 0}, stored, now=now)
    assert wait["looks_without_trigger"] == 17
    assert wait["days_without_trigger"] == 4.0
    assert wait["max_looks_without_trigger"] == 12

    verdict = card_verdict({"sends": 0, "trades": 0, "resolved": 0}, stored)
    assert verdict["tripped"] is False
    assert verdict["graduated"] is False
    assert verdict["looks_without_trigger"] == 17
    assert verdict["trip_reason"] == ""

    facts = lab_facts(load_lab())
    # Disk stamp is this write, not the 4-day fixture — the count still lands.
    row = facts["cards_awaiting_first_trade"][0]
    assert row["card"] == "flush bounce [market_bracket]"
    assert row["sends"] == 0
    assert row["max_looks_without_trigger"] == 12
    assert facts["cards_without_trigger"][0]["card"] == row["card"]


def test_a_send_restarts_the_waiting_count_from_last_send(monkeypatch):
    from datetime import datetime, timezone

    class _J:
        def model_usage_since(self, since):
            assert "13:32" in str(since)
            return {"calls": 9}

    monkeypatch.setattr("abcxauto.memory.get_journal", lambda: _J())
    card = {
        **_card("flush bounce"),
        "written_at": "2026-08-20T12:00:00+00:00",
    }
    wait = card_waiting(
        {
            "sends": 1,
            "trades": 1,
            "last_send": "2026-08-25T13:32:00+00:00",
        },
        card,
        now=datetime(2026, 8, 25, 15, 32, tzinfo=timezone.utc),
    )
    assert wait["looks_without_trigger"] == 9
    assert wait["days_without_trigger"] == 0.1
    assert wait["last_send"] == "2026-08-25T13:32:00+00:00"
    verdict = card_verdict(
        {
            "sends": 1,
            "trades": 1,
            "resolved": 1,
            "last_send": "2026-08-25T13:32:00+00:00",
        },
        card,
    )
    assert verdict["looks_without_trigger"] == 9
    assert verdict["tripped"] is False


def test_card_written_at_survives_an_evidence_rewrite():
    first = _save(market_bracket=[_card("flush bounce")])
    clock = type_cards(first["types"], "market_bracket")[0]["written_at"]
    assert clock
    held = _save(
        market_bracket=[
            {"name": "flush bounce", "note": "gate still off"}
        ]
    )
    assert held.get("revision_held") is True
    again = type_cards(load_lab()["types"], "market_bracket")[0]
    assert again["written_at"] == clock
    assert not (again.get("note") or "").startswith("gate still off")


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


def test_live_new_risk_needs_a_promoted_book_not_a_cited_card(monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    from abcxauto.lab_playbook import live_new_risk_allowed

    assert live_new_risk_allowed() is False
    strat, forced = gate_ticket(_entry(), _world())
    assert strat == "blocked"
    assert "promoted paper playbook" in str((forced or {}).get("note") or "")

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
    # Citing a card is optional even on live once a promoted book exists.
    assert new_risk_card_error("flush bounce", type="market_bracket") == ""
    assert new_risk_card_error("condor grind", type="iron_condor") == ""
    assert new_risk_card_error("") == ""
    named, named_forced = gate_ticket(_entry(), _world())
    assert named_forced is None, named_forced
    assert named == "market_bracket"


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
    # The undeclared card can still open risk on its own ticket; naming it
    # is attribution, not a clerk refuse.
    assert new_risk_card_error(
        "mega-cap earnings-flush bounce", type="market_bracket"
    ) == ""
    assert new_risk_card_error(
        "naked / short-dated option spray", type="buy_option"
    ) == ""


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
    # It is visible in the render and in the scores. Unfiled is not a send gate.
    assert "UNFILED" in notebook_text(lab)
    assert {"filed", "homeless"} <= {r["card"] for r in card_facts(lab)}
    assert new_risk_card_error("homeless") == ""


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
    assert types["market_bracket"]["gotchas"] == "the one line worth keeping"
    assert type_schema_echo_keys(types) == []
    assert "open_shape" not in types["market_bracket"]
    assert "close_tp_sl" not in types["market_bracket"]
    assert "defined_risk" not in types["market_bracket"]
    # Empty trunk gets the locked STK starter; schema echoes stay gone.
    assert [c["name"] for c in type_cards(types, "market_bracket")] == [
        "generic STK market bracket"
    ]


def test_a_write_after_migration_keeps_the_migrated_cards():
    """The first nested write must not wipe what revision 1 left behind."""
    from abcxauto.lab_playbook import _lab_path, _write

    _write(_lab_path(), {"revision": 1, "cards": REV_1_CARDS, "types": {}})
    save_lab(clamp_update({"types": {"market_bracket": {"gotchas": "stop side of last"}}}))
    lab = load_lab()
    names = {(t, c["name"]) for t, c in walk_cards(lab)}
    assert ("market_bracket", "mega-cap earnings-flush bounce") in names
    assert ("market_bracket", "levered-crypto and micro gap chase") in names
    assert ("buy_option", "naked / short-dated option spray") in names
    assert len(walk_cards(lab)) >= 3
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
    assert "types" not in pb
    assert pb["cards"][0]["ticket"] == "market_bracket"
    assert "TYPE market_bracket" in pb["notes"]
    assert "stop side of last" in pb["notes"]
    assert pb["lab"]["resolved_trades"] == 3
    assert "flush bounce" not in " ".join(
        str(r.get("card") or r) for r in (pb["lab"].get("cards_awaiting_first_trade") or [])
    )


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


def test_run_sheet_follows_parent_tool_order_instead_of_rescan():
    from abcxauto.lab_playbook import playbook_run_sheets

    _save(
        market_bracket={
            "tool_order": ["scan", "news", "quote", "candles", "send"],
            "cards": [
                _card(
                    "flush bounce",
                    when_on="mega/large >=6% earnings-miss gap",
                    scan="most_active + top_losers",
                )
            ],
        },
        buy_option={
            "cards": [_card("premium spray", status="retired")],
        },
    )
    lab = load_lab()
    first = playbook_run_sheets(lab, flat=True)
    by_name = {row["card"]: row for row in first}
    assert "flush bounce" in by_name
    assert first[0]["card"] == "flush bounce"
    assert first[0]["next"] == "scan"
    assert first[0]["sends"] == 0
    assert first[0]["resolved"] == 0
    assert first[0]["sample_left"] == 3
    assert first[0]["tool_order"] == ["scan", "news", "quote", "candles", "send"]
    assert ">=6%" in first[0]["when_on"]
    assert "most_active" in first[0]["scan"]

    after_news = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan", "scan", "news"],
        flat=True,
    )
    assert after_news[0]["next"] == "quote"

    carry = playbook_run_sheets(
        lab,
        tool_trace=["book"],
        last_look=[
            "book",
            "playbook",
            "status",
            "scan",
            "news",
            "write_lab_playbook",
        ],
        flat=True,
    )
    assert carry[0]["next"] == "quote"

    already_live = playbook_run_sheets(
        lab,
        tool_trace=["book"],
        last_look=["book", "scan", "news", "write_lab_playbook"],
        flat=True,
        quoted={"quoted": 12, "rows": [{"symbol": "SNDK", "last": 1485.0}]},
    )
    assert already_live[0]["next"] == "candles"

    scan_carried_news = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan"],
        news=[{"symbol": "SNDK", "headline": "sales miss"}],
        flat=True,
    )
    assert scan_carried_news[0]["next"] == "quote"

    scan_quoted_and_news = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan"],
        news=[{"symbol": "SNDK", "headline": "sales miss"}],
        quoted={"quoted": 1, "rows": [{"symbol": "SNDK", "last": 91.5}]},
        flat=True,
    )
    assert scan_quoted_and_news[0]["next"] == "candles"

    prior_day = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_today=False,
    )
    assert prior_day[0]["next"] == "candles"
    assert prior_day[0]["session_today"] is False

    from abcxauto.lab_playbook import live_card_send_facts

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    shape=(
                        "LONG STK market_bracket. Stop under opening low. "
                        "Qty so dollar risk ≤1% NL and notional ≤25% NL."
                    ),
                )
            ]
        }
    )
    facts = live_card_send_facts()
    assert facts["type"] == "market_bracket"
    assert facts["card"] == "flush bounce"
    assert facts["direction"] == "LONG"
    assert facts["risk_pct"] == 1.0
    assert facts["notional_pct"] == 25.0
    from abcxauto.lab_playbook import live_card_needs_session, live_card_session_error

    assert live_card_needs_session() is True
    assert live_card_session_error({"card": "flush bounce"}) == ""
    assert live_card_session_error(
        {"stop_price": 88.0, "target_price": 93.0},
        {"low": 88.0, "today": True},
    ) == ""

    from abcxauto.lab_playbook import lab_wake_bit, live_card_scan_arenas, live_card_scan_screens

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    scan="most_active + top_losers; mega/large only",
                )
            ]
        }
    )
    assert live_card_scan_arenas() == ["most_active", "top_losers"]
    screens = live_card_scan_screens()
    assert {"arena": "mega_cap", "scan_code": "MOST_ACTIVE"} in screens
    assert {"arena": "mega_cap", "scan_code": "TOP_PERC_LOSE"} in screens
    assert {"arena": "large_cap", "scan_code": "MOST_ACTIVE"} in screens
    assert {"arena": "large_cap", "scan_code": "TOP_PERC_LOSE"} in screens
    assert live_card_scan_screens(scan="most_active + top_losers") == [
        {"arena": "most_active", "scan_code": "MOST_ACTIVE"},
        {"arena": "top_losers", "scan_code": "TOP_PERC_LOSE"},
    ]
    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    scan="most_active + top_losers; mega/large only",
                    when_on="mega/large ≥6% earnings-miss gap",
                )
            ]
        }
    )
    gap_screens = live_card_scan_screens()
    assert {"arena": "mega_cap", "scan_code": "TOP_OPEN_PERC_LOSE"} in gap_screens
    assert {"arena": "large_cap", "scan_code": "TOP_OPEN_PERC_LOSE"} in gap_screens
    assert gap_screens[0] == {"arena": "mega_cap", "scan_code": "TOP_OPEN_PERC_LOSE"}
    bit = lab_wake_bit(load_lab(), flat=True)
    assert "next=scan" in bit
    assert "mega_cap:TOP_OPEN_PERC_LOSE" in bit

    rescanned = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan"],
        last_look=["book", "scan", "news"],
        flat=True,
    )
    assert rescanned[0]["next"] == "news"

    after_send = playbook_run_sheets(
        lab,
        tool_trace=["book"],
        last_look=["book", "scan", "news", "quote", "candles", "send"],
        flat=True,
    )
    assert after_send[0]["next"] == "scan"


def test_run_sheet_manage_uses_review_tools():
    from abcxauto.lab_playbook import playbook_run_sheets

    _save(
        market_bracket={
            "tool_order": ["scan", "news", "quote", "candles", "send"],
            "review": "fills then book; confirm both children rest",
            "cards": [_card("flush bounce")],
        }
    )
    lab = load_lab()
    sheets = playbook_run_sheets(lab, tool_trace=["book"], flat=False)
    assert sheets[0]["next"] == "fills"
    assert sheets[0]["tool_order"] == ["book", "fills", "quote", "candles"]
    assert "fills then book" in sheets[0]["review"]
    after = playbook_run_sheets(lab, tool_trace=["book", "fills"], flat=False)
    assert after[0]["next"] == "quote"
    default_manage = playbook_run_sheets(
        {"types": {"market_bracket": {"cards": [_card("open lot")]}}},
        tool_trace=["book"],
        flat=False,
    )
    assert default_manage[0]["next"] == "fills"
    assert default_manage[0]["tool_order"][:2] == ["book", "fills"]


def test_lab_wake_and_book_payload_paint_the_next_tool(monkeypatch):
    from abcxauto.brain import _book_payload
    from abcxauto.lab_playbook import lab_wake_bit, playbook_payload

    _save(
        market_bracket={
            "tool_order": ["scan", "news", "quote", "candles", "send"],
            "cards": [_card("flush bounce")],
        }
    )
    bit = lab_wake_bit(
        load_lab(),
        last_look=["book", "scan", "news"],
        flat=True,
    )
    assert "lab flush bounce" in bit
    assert "next=quote" in bit
    monkeypatch.setattr(
        "abcxauto.think_stream.last_look_facts",
        lambda: {
            "tools": ["book", "scan", "news"],
            "send_calls": 0,
            "scan_hits": {
                "quoted": 12,
                "rows": [
                    {"symbol": "SNDK", "last": 1485.095, "open_gap_pct": -6.5},
                    {"symbol": "MU", "last": 911.49, "open_gap_pct": -3.3},
                ]
            },
        },
    )
    pb = _book_payload(_world(flat=True, positions=[]), tool_trace=["book"])["playbook"]
    keys = list(pb)
    assert keys.index("run") < keys.index("cards")
    assert pb["run"][0]["next"] == "candles"
    assert [row["symbol"] for row in pb["run"][0]["hits"]] == ["SNDK", "MU"]
    assert "now_beating" in pb
    payload = playbook_payload()
    assert payload["run"][0]["next"] == "candles"
    assert payload["run"][0]["card"] == "flush bounce"


def test_book_run_does_not_paint_stale_overnight_hits(monkeypatch):
    from abcxauto.brain import _book_payload

    _save(
        market_bracket={
            "tool_order": ["scan", "news", "quote", "candles", "send"],
            "cards": [_card("flush bounce")],
        }
    )
    stale = {
        "tools": ["book", "scan", "news"],
        "send_calls": 0,
        "fresh": False,
        "scan_hits": {
            "quoted": 12,
            "rows": [{"symbol": "SNDK", "last": 1485.095, "open_gap_pct": -6.5}],
        },
    }
    monkeypatch.setattr("abcxauto.think_stream.last_look_facts", lambda *a, **k: stale)
    monkeypatch.setattr("abcxauto.think_stream.last_look_for_hunt", lambda *a, **k: {})
    pb = _book_payload(_world(flat=True, positions=[]), tool_trace=["book"])["playbook"]
    assert "hits" not in pb["run"][0]
    assert pb["run"][0]["next"] == "scan"


def test_run_sheet_and_wake_paint_send_sketch_from_session(monkeypatch):
    from abcxauto.brain import _book_payload
    from abcxauto.lab_playbook import lab_wake_bit, playbook_run_sheets

    _save(
        market_bracket={
            "tool_order": ["scan", "news", "quote", "candles", "send"],
            "cards": [
                _card(
                    "flush bounce",
                    shape="LONG STK market_bracket. Qty so dollar risk <=1% NL.",
                )
            ],
        }
    )
    lab = load_lab()
    rng = {
        "SNDK": {
            "today": True,
            "low": 88.0,
            "retrace_30": 93.0,
            "ticket": {
                "strategy": "market_bracket",
                "card": "flush bounce",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
                "quantity": 10,
            },
        }
    }
    sheets = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_range=rng,
    )
    assert sheets[0]["next"] == "send"
    assert sheets[0]["send"]["symbol"] == "SNDK"
    assert sheets[0]["send"]["card"] == "flush bounce"
    assert sheets[0]["send"]["stop_price"] == 88.0
    assert sheets[0]["send"]["quantity"] == 10
    bit = lab_wake_bit(
        lab,
        last_look=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_range=rng,
    )
    assert "next=send" in bit
    assert "send SNDK" in bit
    assert "card=flush bounce" in bit
    hunt = {
        "tools": ["book", "scan", "news", "quote", "candles"],
        "send_calls": 0,
        "fresh": True,
        "scan_hits": {"quoted": 1, "rows": [{"symbol": "SNDK", "last": 91.5}]},
        "session_range": rng,
    }
    monkeypatch.setattr("abcxauto.think_stream.last_look_facts", lambda *a, **k: hunt)
    monkeypatch.setattr("abcxauto.think_stream.last_look_for_hunt", lambda *a, **k: hunt)
    pb = _book_payload(_world(flat=True, positions=[]), tool_trace=["book"])["playbook"]
    assert pb["run"][0]["next"] == "send"
    assert pb["run"][0]["send"]["symbol"] == "SNDK"


def test_apply_hunt_send_sketch_does_not_fill_omitted_ticket_fields():
    from abcxauto.lab_playbook import apply_hunt_send_sketch

    rng = {
        "SNDK": {
            "today": True,
            "low": 88.0,
            "ticket": {
                "strategy": "market_bracket",
                "card": "flush bounce",
                "symbol": "SNDK",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
                "quantity": 10,
            },
        }
    }
    snap = {"session_range": rng}
    act = {
        "strategy": "market_bracket",
        "params": {"card": "flush bounce", "symbol": "SNDK", "stop_price": 87.5},
        "rationale": "my stop",
    }
    assert apply_hunt_send_sketch(act, snap) is None
    assert act["params"]["stop_price"] == 87.5
    assert act["rationale"] == "my stop"
    assert act["params"].get("quantity") in (None, "")
    assert act["params"].get("target_price") in (None, "")
    assert act["params"].get("direction") in (None, "")
    assert "_hunt_sketch" not in act

    thin = {
        "strategy": "market_bracket",
        "params": {"card": "flush bounce"},
        "rationale": "",
    }
    assert apply_hunt_send_sketch(thin, snap) is None
    assert thin["params"] == {"card": "flush bounce"}
    assert thin.get("rationale") == ""
    for key in ("symbol", "direction", "stop_price", "target_price", "quantity"):
        assert thin["params"].get(key) in (None, "")
    assert "_hunt_sketch" not in thin

    other = {
        "strategy": "market_bracket",
        "params": {"card": "other card"},
    }
    assert apply_hunt_send_sketch(other, snap) is None
    assert other["params"].get("symbol") is None

    no_card = {
        "strategy": "market_bracket",
        "params": {"symbol": "SNDK", "quantity": 10, "stop_price": 88.0},
        "rationale": "",
    }
    assert apply_hunt_send_sketch(no_card, snap) is None
    assert no_card["params"].get("card") in (None, "")
    assert no_card["params"]["symbol"] == "SNDK"
    assert no_card.get("rationale") == ""

    no_stop = {
        "strategy": "market_bracket",
        "params": {"symbol": "SNDK", "card": "flush bounce", "quantity": 10},
    }
    assert apply_hunt_send_sketch(no_stop, snap) is None
    assert no_stop["params"].get("stop_price") in (None, "")
    assert no_stop["params"].get("target_price") in (None, "")
    assert no_stop["params"]["quantity"] == 10


def test_hunt_send_sketch_does_not_invent_target_from_retrace():
    from abcxauto.lab_playbook import hunt_send_sketch, session_target

    rng = {
        "today": True,
        "low": 88.0,
        "last": 94.0,
        "above_low": True,
        "open_gap_pct": -6.5,
        "retrace_30": 93.0,
        "retrace_50": 95.0,
        "ticket": {
            "card": "flush bounce",
            "strategy": "market_bracket",
            "direction": "LONG",
        },
    }
    assert session_target(rng, "LONG") == 95.0
    sketch = hunt_send_sketch({"SNDK": rng})
    assert sketch is not None
    assert sketch.get("card") == "flush bounce"
    assert sketch.get("target_price") in (None, "")
    assert sketch.get("stop_price") in (None, "")
    assert sketch.get("quantity") in (None, "")
    through = dict(rng, last=96.0)
    assert session_target(through, "LONG") is None
    assert hunt_send_sketch({"SNDK": through}) is None


def test_hunt_send_sketch_prefers_the_wider_gap():
    from abcxauto.lab_playbook import hunt_send_sketch

    sketch = hunt_send_sketch({
        "MU": {
            "today": True,
            "low": 900.0,
            "open_gap_pct": -3.3,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        },
        "SNDK": {
            "today": True,
            "low": 88.0,
            "open_gap_pct": -6.5,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        },
    })
    assert sketch is not None
    assert sketch["symbol"] == "SNDK"
    assert sketch.get("stop_price") in (None, "")
    assert sketch.get("target_price") in (None, "")
    assert sketch.get("quantity") in (None, "")
    assert sketch.get("direction") in (None, "")
    assert hunt_send_sketch({
        "SNDK": {
            "low": 88.0,
            "open_gap_pct": -6.5,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        }
    }) is None


def test_hunt_send_sketch_skips_a_name_sitting_on_the_opening_low():
    from abcxauto.lab_playbook import hunt_send_sketch, live_card_session_error, playbook_run_sheets

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    shape="LONG STK. Stop under opening low.",
                    when_on="price holding above the opening low",
                )
            ]
        }
    )
    store = {
        "SNDK": {
            "today": True,
            "low": 88.0,
            "last": 88.0,
            "above_low": False,
            "open_gap_pct": -6.5,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        },
        "MU": {
            "today": True,
            "low": 900.0,
            "last": 910.0,
            "above_low": True,
            "open_gap_pct": -3.3,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        },
    }
    sketch = hunt_send_sketch(store)
    assert sketch is not None
    assert sketch["symbol"] == "MU"
    assert hunt_send_sketch({"SNDK": store["SNDK"]}) is None
    sheets = playbook_run_sheets(
        load_lab(),
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_today=True,
        session_range={"SNDK": store["SNDK"]},
    )
    assert sheets[0]["next"] == "candles"
    assert sheets[0]["above_low"] is False
    assert live_card_session_error(
        {"card": "flush bounce", "direction": "LONG"},
        store["SNDK"],
    ) == ""
    assert live_card_session_error(
        {"card": "flush bounce", "direction": "LONG"},
        store["MU"],
    ) == ""


def test_hunt_send_sketch_skips_a_name_still_under_the_open():
    from abcxauto.lab_playbook import hunt_send_sketch, live_card_session_error

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    shape="LONG STK. Stop under opening low.",
                    when_on="hold above the open after a 5-min bar",
                )
            ]
        }
    )
    store = {
        "SNDK": {
            "today": True,
            "open": 90.0,
            "low": 88.0,
            "last": 89.0,
            "above_low": True,
            "above_open": False,
            "open_gap_pct": -6.5,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        }
    }
    assert hunt_send_sketch(store) is None
    assert live_card_session_error(
        {"card": "flush bounce", "direction": "LONG"},
        store["SNDK"],
    ) == ""


def test_hunt_send_sketch_skips_a_gap_under_the_written_floor():
    from abcxauto.lab_playbook import (
        hunt_send_sketch,
        lab_wake_bit,
        live_card_min_gap_pct,
        live_card_session_error,
        playbook_run_sheets,
    )

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    shape="LONG STK. Stop under opening low.",
                    when_on="mega/large ≥6% earnings-miss gap, hold above the opening low",
                )
            ]
        }
    )
    assert live_card_min_gap_pct() == 6.0
    store = {
        "MU": {
            "today": True,
            "low": 900.0,
            "last": 910.0,
            "above_low": True,
            "open_gap_pct": -3.3,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        },
        "SNDK": {
            "today": True,
            "low": 88.0,
            "last": 91.5,
            "above_low": True,
            "open_gap_pct": -6.5,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        },
    }
    sketch = hunt_send_sketch(store)
    assert sketch is not None
    assert sketch["symbol"] == "SNDK"
    assert hunt_send_sketch({"MU": store["MU"]}) is None
    sheets = playbook_run_sheets(
        load_lab(),
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_today=True,
        session_range={"MU": store["MU"]},
    )
    assert sheets[0]["next"] == ""
    assert sheets[0]["gate"] == "off"
    assert sheets[0]["min_gap_pct"] == 6.0
    assert "send" not in sheets[0]
    bit = lab_wake_bit(
        load_lab(),
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_range={"MU": store["MU"]},
    )
    assert "gate=off" in bit
    assert "next=send" not in bit
    assert live_card_session_error(
        {"card": "flush bounce", "direction": "LONG"},
        store["MU"],
    ) == ""
    assert live_card_session_error(
        {"card": "flush bounce", "direction": "LONG"},
        store["SNDK"],
    ) == ""


def test_hunt_send_sketch_skips_a_name_without_a_gap_print():
    from abcxauto.lab_playbook import hunt_send_sketch, live_card_min_gap_pct

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    shape="LONG STK. Stop under opening low.",
                    when_on="mega/large ≥6% earnings-miss gap, hold above the opening low",
                )
            ]
        }
    )
    assert live_card_min_gap_pct() == 6.0
    assert hunt_send_sketch({
        "AMD": {
            "today": True,
            "low": 160.0,
            "last": 165.0,
            "above_low": True,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        }
    }) is None


def test_sibling_cards_bind_gap_and_sketch_to_card_name():
    from abcxauto.brain import _stamp_session_ticket
    from abcxauto.lab_playbook import (
        hunt_send_sketch,
        lab_wake_bit,
        live_card_gap_floors,
        live_card_min_gap_pct,
        live_card_session_error,
        playbook_run_sheets,
    )

    _save(
        market_bracket={
            "tool_order": ["scan", "news", "quote", "candles", "send"],
            "cards": [
                _card(
                    "flush bounce",
                    shape="LONG STK. Stop under opening low.",
                    when_on="mega/large ≥6% earnings-miss gap, hold above the opening low",
                    scan="most_active + top_losers + low_open_gap; mega/large only",
                ),
                _card(
                    "3pct gap hold",
                    shape="LONG STK. Stop under opening low. One name.",
                    when_on="mega/large ≥3% open gap, hold above the open",
                    scan="top_losers + low_open_gap + top_open_perc_lose; mega/large only",
                ),
            ],
        },
        vertical_spread={
            "cards": [
                _card(
                    "defined-risk flush debit",
                    when_on="same flush tape, gap >=3%, defined-risk only",
                    shape="debit vertical. option_facts first.",
                )
            ]
        },
    )
    assert live_card_min_gap_pct() == 3.0
    assert live_card_min_gap_pct(card="flush bounce") == 6.0
    assert live_card_min_gap_pct(card="3pct gap hold") == 3.0
    floors = {row["card"]: row for row in live_card_gap_floors(deepest=3.8)}
    assert floors["flush bounce"] == {
        "card": "flush bounce",
        "min_gap_pct": 6.0,
        "met": False,
    }
    assert floors["3pct gap hold"] == {
        "card": "3pct gap hold",
        "min_gap_pct": 3.0,
        "met": True,
    }
    alb = {
        "today": True,
        "low": 134.0,
        "last": 135.2,
        "above_low": True,
        "above_open": True,
        "open": 136.1,
        "open_gap_pct": -3.8,
        "retrace_30": 137.7,
        "retrace_50": 138.8,
    }
    assert live_card_session_error(
        {"card": "flush bounce", "direction": "LONG"},
        alb,
    ) == ""
    assert live_card_session_error(
        {"card": "3pct gap hold", "direction": "LONG"},
        alb,
    ) == ""
    sketch = hunt_send_sketch({"ALB": alb})
    assert sketch is None
    assert hunt_send_sketch({"ALB": alb}, card="flush bounce") is None
    assert hunt_send_sketch({"ALB": alb}, card="3pct gap hold") is None
    assert hunt_send_sketch({"ALB": alb}, card="defined-risk flush debit") is None
    stamped = dict(alb)
    _stamp_session_ticket(stamped)
    assert stamped["ticket"]["card"] == "3pct gap hold"
    sheets = playbook_run_sheets(
        load_lab(),
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_today=True,
        session_range={"ALB": alb},
    )
    by_name = {row["card"]: row for row in sheets}
    assert by_name["flush bounce"]["min_gap_pct"] == 6.0
    assert by_name["flush bounce"].get("gate") == "off"
    assert by_name["3pct gap hold"]["min_gap_pct"] == 3.0
    assert "send" not in by_name["3pct gap hold"]
    bit = lab_wake_bit(
        load_lab(),
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_range={"ALB": alb},
    )
    assert "send ALB" not in bit


def test_live_card_scan_constraints_apply_written_floors():
    from abcxauto.lab_playbook import (
        apply_card_constraints_to_spec,
        drop_hits_off_card,
        hunt_send_sketch,
        live_card_scan_constraints,
        live_card_tape_error,
    )

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    scan=(
                        "most_active + top_losers; mega/large only; "
                        "skip levered ETFs and sub-$15 names"
                    ),
                    when_on="mega/large ≥6% earnings-miss gap",
                    shape="LONG STK. Stop under opening low.",
                )
            ]
        }
    )
    c = live_card_scan_constraints()
    assert c["min_price"] == 15.0
    assert c["skip_levered"] is True
    assert c["caps"] == ["mega_cap", "large_cap"]
    spec, applied = apply_card_constraints_to_spec(
        {
            "scanCode": "MOST_ACTIVE",
            "stockTypeFilter": "CORP,ETF",
            "abovePrice": 5.0,
        }
    )
    assert spec["abovePrice"] == 15.0
    assert spec["stockTypeFilter"] == "CORP"
    assert spec["marketCapAbove"] == 10_000_000_000.0
    assert applied["card_min_price"] == 15.0
    tighter, _ = apply_card_constraints_to_spec(
        {
            "scanCode": "MOST_ACTIVE",
            "abovePrice": 20.0,
            "marketCapAbove": 200_000_000_000.0,
            "stockTypeFilter": "CORP",
        }
    )
    assert tighter["abovePrice"] == 20.0
    assert tighter["marketCapAbove"] == 200_000_000_000.0
    keep, dropped = drop_hits_off_card(
        [
            {"symbol": "AAOI", "last": 12.0},
            {"symbol": "SNDK", "last": 91.5},
            {"symbol": "UNK"},
        ]
    )
    assert [r["symbol"] for r in keep] == ["SNDK", "UNK"]
    assert dropped == ["AAOI"]
    assert live_card_tape_error(
        {"symbol": "AAOI"},
        {"today": True, "last": 12.0},
    ) == ""
    cheap = {
        "AAOI": {
            "today": True,
            "last": 12.0,
            "low": 11.0,
            "above_low": True,
            "open_gap_pct": -8.0,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        }
    }
    assert hunt_send_sketch(cheap) is None


def test_live_card_tape_blocks_wide_spread_and_same_session_reentry():
    from abcxauto.lab_playbook import (
        hunt_send_sketch,
        live_card_needs_no_reentry,
        live_card_needs_tight_spread,
        live_card_tape_error,
        record_card_send,
    )

    _save(
        market_bracket={
            "review": "After exit do not re-enter that name the same session.",
            "cards": [
                _card(
                    "flush bounce",
                    when_on="tight live spread, hold above the opening low",
                    shape="LONG STK. Stop under opening low.",
                )
            ],
        }
    )
    assert live_card_needs_tight_spread() is True
    assert live_card_needs_no_reentry() is True
    wide = {
        "today": True,
        "last": 91.5,
        "low": 88.0,
        "bid": 89.0,
        "ask": 93.0,
        "spread": 4.0,
        "above_low": True,
        "open_gap_pct": -6.5,
    }
    assert live_card_tape_error(
        {"card": "flush bounce", "symbol": "SNDK", "stop_price": 88.0},
        wide,
    ) == ""
    tight = dict(wide)
    tight.update({"bid": 91.45, "ask": 91.55, "spread": 0.10})
    assert live_card_tape_error(
        {"card": "flush bounce", "symbol": "SNDK", "stop_price": 88.0},
        tight,
    ) == ""
    assert hunt_send_sketch({
        "SNDK": {
            **wide,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        }
    }) is None
    record_card_send(
        card="flush bounce",
        strategy="market_bracket",
        symbol="SNDK",
        result={"status": "ok"},
        params={"symbol": "SNDK", "card": "flush bounce"},
    )
    assert live_card_tape_error(
        {"card": "flush bounce", "symbol": "SNDK", "stop_price": 88.0},
        tight,
    ) == ""
    assert hunt_send_sketch({
        "SNDK": {
            **tight,
            "ticket": {"card": "flush bounce", "strategy": "market_bracket"},
        }
    }) is None


def test_live_card_book_error_is_not_a_send_gate():
    from abcxauto.lab_playbook import live_card_book_error, playbook_run_sheets

    _save(
        market_bracket={
            "cards": [
                _card(
                    "flush bounce",
                    shape="LONG STK market_bracket. One name, no add.",
                )
            ]
        }
    )
    lots = [{"symbol": "SNDK", "sec_type": "STK", "quantity": 10}]
    assert live_card_book_error({"symbol": "SNDK"}, []) == ""
    assert live_card_book_error({"symbol": "SNDK"}, lots) == ""
    assert live_card_book_error({"symbol": "MU"}, lots) == ""
    sheets = playbook_run_sheets(
        load_lab(),
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_range={
            "MU": {
                "today": True,
                "low": 900.0,
                "retrace_30": 920.0,
                "above_low": True,
                "ticket": {
                    "strategy": "market_bracket",
                    "card": "flush bounce",
                    "direction": "LONG",
                    "stop_price": 900.0,
                    "target_price": 920.0,
                    "quantity": 1,
                },
            }
        },
        positions=lots,
    )
    # Hunt sketch is a notebook suggestion. Card prose cannot hide it.
    assert sheets[0].get("gate") != "off" or "send" in sheets[0]
    assert live_card_book_error(
        sheets[0].get("send") or {"symbol": "MU"}, lots, load_lab()
    ) == ""


def test_ibkr_live_last_reads_scan_rows_without_quote_map():
    from abcxauto.lab_playbook import ibkr_live_last

    assert ibkr_live_last(
        "SNDK",
        snap={
            "scan_hits": {
                "quoted": 1,
                "rows": [{"symbol": "SNDK", "last": 91.5}],
            }
        },
    ) == 91.5
    assert ibkr_live_last(
        "SNDK",
        quoted={"quoted": 1, "rows": [{"symbol": "SNDK", "last": 91.5}]},
    ) == 91.5
    assert ibkr_live_last("MU", snap={"scan_hits": {"rows": [{"symbol": "SNDK", "last": 91.5}]}}) is None


def test_empty_scan_does_not_walk_the_hunt_to_send():
    from abcxauto.lab_playbook import hunt_send_sketch, playbook_run_sheets

    _save(
        market_bracket={
            "gotchas": "do not re-ticket SPY the same session",
            "tool_order": ["scan", "news", "quote", "candles", "send"],
            "cards": [_card("flush bounce", when_on="mega/large >=6% earnings-miss gap")],
        }
    )
    lab = load_lab()
    empty = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan"],
        flat=True,
        quoted={"quoted": 0, "rows": []},
    )
    assert empty[0]["next"] == ""
    assert empty[0]["gate"] == "off"

    failed = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan"],
        flat=True,
        quoted={"ok": False, "error": "IBKR scanner timeout"},
    )
    assert failed[0]["next"] == "scan"

    no_sketch = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_range={},
    )
    assert no_sketch[0]["next"] == "candles"
    assert no_sketch[0].get("gate") != "off"
    prior = playbook_run_sheets(
        lab,
        tool_trace=["book", "scan", "news", "quote", "candles"],
        flat=True,
        session_range={
            "SNDK": {
                "today": False,
                "low": 88.0,
                "last": 91.5,
                "above_low": True,
                "open_gap_pct": -6.5,
                "retrace_30": 93.0,
            }
        },
    )
    assert prior[0]["next"] == "candles"
    assert prior[0].get("gate") != "off"

    spy = {
        "SPY": {
            "today": True,
            "low": 640.0,
            "last": 650.0,
            "above_low": True,
            "retrace_30": 655.0,
            "ticket": {
                "strategy": "market_bracket",
                "card": "flush bounce",
                "direction": "LONG",
                "stop_price": 640.0,
                "target_price": 655.0,
                "quantity": 1,
            },
        }
    }
    assert hunt_send_sketch(spy) is None
    assert hunt_send_sketch(
        spy,
        tape={"rows": [{"symbol": "SNDK", "last": 91.5}]},
    ) is None
    assert hunt_send_sketch(
        {
            "SNDK": {
                "today": True,
                "low": 88.0,
                "last": 91.5,
                "above_low": True,
                "open_gap_pct": -6.5,
                "retrace_30": 93.0,
                "ticket": {
                    "strategy": "market_bracket",
                    "card": "flush bounce",
                    "direction": "LONG",
                    "stop_price": 88.0,
                    "target_price": 93.0,
                    "quantity": 10,
                },
            }
        },
        tape={"rows": [{"symbol": "SNDK", "last": 91.5}]},
    )["symbol"] == "SNDK"
    sized = hunt_send_sketch({
        "SNDK": {
            "today": True,
            "low": 88.0,
            "last": 91.5,
            "above_low": True,
            "open_gap_pct": -6.5,
            "size": {"risk_usd": 500.0, "card_qty": 10},
            "ticket": {
                "strategy": "market_bracket",
                "card": "flush bounce",
                "direction": "LONG",
                "stop_price": 88.0,
                "target_price": 93.0,
            },
        }
    })
    assert sized is not None
    assert sized.get("quantity") in (None, "")
