"""Moved promote contract stays importable from the public modules."""

from __future__ import annotations

from abcxauto.brain import AGENT_TOOLS, BrainTurn, _run_tool, grok_turn
from abcxauto.lab_playbook import (
    FILL_ASSUMPTION_PAPER_MID,
    _has_numeric_kill,
    card_verdict,
    clamp_update,
    load_lab,
    maybe_promote,
    save_lab,
)
from abcxauto.playbook import live_cards as live_cards_mod
from abcxauto.playbook import promote as promote_mod
from abcxauto.playbook import schema as schema_mod


def test_promote_contract_lives_in_playbook_promote():
    assert card_verdict is promote_mod.card_verdict
    assert maybe_promote is promote_mod.maybe_promote
    assert _has_numeric_kill is promote_mod._has_numeric_kill
    assert clamp_update is schema_mod.clamp_update
    assert load_lab.__module__ == "abcxauto.playbook.persist"
    assert save_lab.__module__ == "abcxauto.playbook.persist"


def test_numeric_kill_is_max_losses_or_max_loss_usd():
    assert _has_numeric_kill({"sample": 4}) is False
    assert _has_numeric_kill({"max_hold_hours": 8}) is False
    assert _has_numeric_kill({"max_losses": 2}) is True
    assert _has_numeric_kill({"max_loss_usd": 50}) is True


def test_paper_mid_cannot_graduate_without_conservative_mark():
    card = {
        "name": "flush bounce",
        "thesis": "gap retrace",
        "retire_if": {"sample": 3, "max_losses": 2},
        "fill_assumption": FILL_ASSUMPTION_PAPER_MID,
        "type": "market_bracket",
    }
    verdict = card_verdict(
        {"resolved": 3, "resolved_pnl": 90.0, "conservative_pnl": 12.0},
        card,
    )
    assert verdict["graduated"] is False
    assert verdict["cannot_graduate_reason"] == "paper_mid cannot graduate"


def test_live_card_notes_are_not_a_hunt_module():
    import importlib

    import abcxauto.playbook.live_cards as live_cards
    from abcxauto.lab_playbook import apply_hunt_send_sketch, live_card_session_error

    assert live_cards is live_cards_mod
    assert live_card_session_error.__module__ == "abcxauto.playbook.live_cards"
    assert apply_hunt_send_sketch.__module__ == "abcxauto.playbook.live_cards"
    try:
        importlib.import_module("abcxauto.playbook.hunt")
    except ModuleNotFoundError:
        return
    raise AssertionError("abcxauto.playbook.hunt must not exist")


def test_brain_tools_reexport_from_brain():
    import abcxauto.brain_tools as tools

    assert _run_tool is tools._run_tool
    assert AGENT_TOOLS is tools.AGENT_TOOLS
    assert grok_turn.__module__ == "abcxauto.brain"
    assert BrainTurn.__module__ == "abcxauto.brain"
