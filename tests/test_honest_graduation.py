"""Honest graduation: paper mid cannot unlock live. No new cathedral."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from abcxauto.lab_playbook import (
    CONSERVATIVE_FILL_ASSUMPTIONS,
    FILL_ASSUMPTION_CONSERVATIVE,
    FILL_ASSUMPTION_FULL_SPREAD,
    FILL_ASSUMPTION_PAPER_MID,
    HONESTY_GAP_REASONS,
    LIVE_HYPOTHESIS_CAP,
    apply_from_judgment,
    attach_card_honesty,
    card_facts,
    card_verdict,
    clamp_update,
    fill_assumption_of,
    hypothesis_cap_reject,
    live_hypothesis_count,
    load_lab,
    maybe_promote,
    save_lab,
    type_cards,
    walk_cards,
)
from abcxauto.llm import SYSTEM_PROMPT

SYSTEM_PROMPT_LOCK = (
    "You own an Interactive Brokers {mode} book. Strategy is yours.\n"
    "Live only follows a promoted playbook. Risk is code.\n"
    "send tickets that match ORDER EXAMPLES.\n"
    "Size vs max_risk_per_trade_pct of NetLiq.\n"
)


def _card(name: str, **over) -> dict:
    row = {
        "name": name,
        "thesis": "flush into support bounces",
        "retire_if": {
            "sample": 3,
            "condition": "no bounce",
            "max_losses": 2,
        },
        "fill_assumption": FILL_ASSUMPTION_FULL_SPREAD,
    }
    row.update(over)
    return row


def _book(*names: str, **over) -> dict:
    cards = [_card(n, **over) for n in names]
    return {"types": {"market_bracket": {"cards": cards}}}


def test_system_prompt_is_unchanged():
    assert SYSTEM_PROMPT == SYSTEM_PROMPT_LOCK


def test_fill_assumption_is_stored_and_defaults_to_paper_mid(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "types": {
                    "market_bracket": {
                        "cards": [
                            {
                                "name": "flush bounce",
                                "thesis": "gap retrace",
                                "retire_if": {"sample": 4, "condition": "x"},
                            }
                        ]
                    }
                }
            }
        )
    )
    card = type_cards(load_lab()["types"], "market_bracket")[0]
    assert card["fill_assumption"] == FILL_ASSUMPTION_PAPER_MID
    assert fill_assumption_of(card) == FILL_ASSUMPTION_PAPER_MID


def test_fill_assumption_aliases_normalize():
    assert fill_assumption_of({"fill_assumption": "paper mid"}) == FILL_ASSUMPTION_PAPER_MID
    assert fill_assumption_of({"fill_assumption": "full spread"}) == FILL_ASSUMPTION_FULL_SPREAD
    assert fill_assumption_of({"fill_assumption": "conservative"}) == FILL_ASSUMPTION_CONSERVATIVE
    assert FILL_ASSUMPTION_PAPER_MID not in CONSERVATIVE_FILL_ASSUMPTIONS


def test_paper_mid_cannot_set_graduated():
    card = {
        **_card(
            "flush bounce",
            fill_assumption="paper_mid",
            retire_if={"sample": 3, "condition": "x", "max_losses": 2},
        ),
        "type": "market_bracket",
    }
    verdict = card_verdict({"resolved": 3, "resolved_pnl": 900.0}, card)
    assert verdict["graduated"] is False
    assert verdict["fill_assumption"] == FILL_ASSUMPTION_PAPER_MID
    assert verdict["cannot_graduate_reason"] == "paper_mid cannot graduate"
    assert verdict["live_evidence"] is False
    assert verdict["paper_resolved_pnl"] == 900.0
    assert verdict["live_resolved_pnl"] is None


def test_conservative_fill_can_graduate_when_sample_and_kill_met():
    card = {
        **_card("flush bounce", fill_assumption="conservative"),
        "type": "market_bracket",
    }
    verdict = card_verdict({"resolved": 3, "resolved_pnl": 120.0}, card)
    assert verdict["graduated"] is True
    assert verdict["fill_assumption"] == FILL_ASSUMPTION_CONSERVATIVE
    assert verdict["cannot_graduate_reason"] == ""
    assert verdict["live_evidence"] is False
    assert verdict["live_resolved_pnl"] is None


def test_full_spread_is_a_conservative_fill_assumption():
    card = {
        **_card("flush bounce", fill_assumption="full_spread"),
        "type": "market_bracket",
    }
    verdict = card_verdict({"resolved": 3, "resolved_pnl": 40.0}, card)
    assert verdict["graduated"] is True
    assert verdict["fill_assumption"] == FILL_ASSUMPTION_FULL_SPREAD


def test_graduation_requires_numeric_kill():
    card = {
        **_card(
            "flush bounce",
            fill_assumption="full_spread",
            retire_if={"sample": 3, "condition": "no bounce"},
        ),
        "type": "market_bracket",
    }
    verdict = card_verdict({"resolved": 3, "resolved_pnl": 900.0}, card)
    assert verdict["graduated"] is False
    assert verdict["needs_numeric_kill"] is True
    assert "numeric kill" in verdict["cannot_graduate_reason"]


def test_promote_needs_sample_and_kill_and_conservative_fill(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(
        clamp_update(
            {
                "ready_to_promote": True,
                "types": {
                    "market_bracket": {
                        "cards": [
                            _card(
                                "flush bounce",
                                fill_assumption="paper_mid",
                            )
                        ]
                    }
                },
            }
        )
    )

    def _facts(*_a, **_k):
        card = type_cards(load_lab()["types"], "market_bracket")[0]
        return [
            {
                **card_verdict(
                    {"resolved": 3, "resolved_pnl": 80.0, "card": "flush bounce"},
                    {**card, "type": "market_bracket"},
                ),
                "card": "flush bounce",
                "type": "market_bracket",
            }
        ]

    monkeypatch.setattr("abcxauto.lab_playbook.card_facts", _facts)
    assert maybe_promote() is None

    save_lab(
        clamp_update(
            {
                "ready_to_promote": True,
                "types": {
                    "market_bracket": {
                        "cards": [_card("flush bounce", fill_assumption="full_spread")]
                    }
                },
            }
        )
    )
    live = maybe_promote()
    assert live is not None
    assert live["graduated"] == ["flush bounce"]


def test_paper_win_rate_is_not_live_evidence():
    card = {
        **_card("flush bounce", fill_assumption="paper_mid", expect_hit_rate=70),
        "type": "market_bracket",
    }
    verdict = card_verdict(
        {"resolved": 10, "resolved_wins": 8, "resolved_pnl": 400.0},
        card,
    )
    assert verdict["graduated"] is False
    assert verdict["live_evidence"] is False
    assert verdict["paper_win_rate"] == 80.0
    assert verdict["live_win_rate"] is None
    assert verdict["calibration"]["live_evidence"] is False
    assert verdict["calibration"]["paper_hit_rate"] == 80.0
    assert verdict["calibration"]["live_hit_rate"] is None
    assert verdict["paper_resolved_pnl"] == 400.0
    assert verdict["live_resolved_pnl"] is None


def test_live_book_labels_resolved_pnl_as_live(monkeypatch):
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: False)
    card = {
        **_card("flush bounce", fill_assumption="conservative"),
        "type": "market_bracket",
    }
    verdict = card_verdict({"resolved": 3, "resolved_pnl": 55.0}, card)
    assert verdict["live_evidence"] is True
    assert verdict["live_resolved_pnl"] == 55.0
    assert verdict["paper_resolved_pnl"] is None
    assert verdict["graduated"] is True


def test_fourth_live_card_write_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LIVE_PATH", str(tmp_path / "live.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    first = apply_from_judgment(
        {"lab_playbook": _book("alpha", "bravo", "charlie")}
    )
    assert first is not None
    assert first.get("status") != "rejected"
    assert first["live_hypotheses"] == LIVE_HYPOTHESIS_CAP

    fourth = apply_from_judgment({"lab_playbook": _book("delta")})
    assert fourth is not None
    assert fourth.get("status") == "rejected"
    assert "hypothesis_cap" in (fourth.get("rejected") or {})
    names = [c["name"] for c in type_cards(load_lab()["types"], "market_bracket")]
    assert "delta" not in names
    assert names[:3] == ["alpha", "bravo", "charlie"]


def test_four_cards_in_one_write_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    out = apply_from_judgment(
        {"lab_playbook": _book("a", "b", "c", "d")}
    )
    assert out is not None
    assert out.get("status") == "rejected"
    assert "hypothesis_cap" in (out.get("rejected") or {})
    assert load_lab() == {} or live_hypothesis_count(load_lab()) == 0


def test_retire_then_write_stays_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    apply_from_judgment({"lab_playbook": _book("alpha", "bravo", "charlie")})
    out = apply_from_judgment(
        {
            "lab_playbook": {
                "types": {
                    "market_bracket": {
                        "cards": [
                            {"name": "charlie", "status": "retired"},
                            _card("delta"),
                        ]
                    }
                }
            }
        }
    )
    assert out is not None
    assert out.get("status") != "rejected"
    assert out["live_hypotheses"] == LIVE_HYPOTHESIS_CAP
    names = {
        c["name"]: c["status"]
        for c in type_cards(load_lab()["types"], "market_bracket")
    }
    assert names["charlie"] == "retired"
    assert names["delta"] == "testing"


def test_already_wide_book_is_flagged_not_wiped(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(clamp_update(_book("a", "b", "c", "d", "e")))
    assert live_hypothesis_count(load_lab()) == 5
    staged = clamp_update({"types": {"market_bracket": {"gotchas": "stop side"}}})
    assert hypothesis_cap_reject(staged, load_lab()) == {}
    out = apply_from_judgment(
        {"lab_playbook": {"types": {"market_bracket": {"gotchas": "stop side"}}}}
    )
    assert out is not None
    assert out.get("status") != "rejected"
    assert out["live_hypotheses"] == 5
    assert "over cap" in (out.get("hypothesis_cap_flag") or "")
    assert live_hypothesis_count(load_lab()) == 5


def test_locked_starters_do_not_count_toward_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    apply_from_judgment({"lab_playbook": _book("alpha")})
    lab = load_lab()
    locked = [c for _t, c in walk_cards(lab) if c.get("locked") is True]
    assert locked
    assert live_hypothesis_count(lab) == 1


def test_honesty_allocates_model_cost_by_sends_and_leaves_named_gaps():
    written = (datetime(2026, 8, 20, tzinfo=timezone.utc)).isoformat()
    rows = attach_card_honesty(
        [
            {
                "card": "alpha",
                "sends": 1,
                "resolved": 1,
                "resolved_pnl": 40.0,
                "written_at": written,
                "fill_assumption": "full_spread",
                "live_evidence": False,
            },
            {
                "card": "bravo",
                "sends": 3,
                "resolved": 2,
                "resolved_pnl": 80.0,
                "written_at": written,
                "fill_assumption": "paper_mid",
                "live_evidence": False,
            },
        ],
        model_cost=8.0,
        now=datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    a, b = rows
    assert a["honesty"]["allocated_model_cost"] == 2.0
    assert a["honesty"]["cost_allocated_pnl"] == 38.0
    assert b["honesty"]["allocated_model_cost"] == 6.0
    assert b["honesty"]["cost_allocated_pnl"] == 74.0
    assert a["honesty"]["turnover_per_day"] == 0.25
    assert a["honesty"]["fill_vs_ibkr_last"] is None
    assert a["honesty"]["holdout"] is None
    assert a["honesty"]["beat_spy_after_model_cost"] is None
    for key in ("fill_vs_ibkr_last", "holdout", "beat_spy_after_model_cost"):
        assert key in a["honesty"]["gaps"]
        assert key in HONESTY_GAP_REASONS
    assert "SPY" in HONESTY_GAP_REASONS["beat_spy_after_model_cost"]
    assert "cost_allocated_pnl" not in a["honesty"]["gaps"]


def test_honesty_does_not_invent_cost_or_spy_when_data_is_missing():
    rows = attach_card_honesty(
        [{"card": "alpha", "sends": 0, "resolved": 0, "resolved_pnl": 0.0}],
        model_cost=None,
    )
    hon = rows[0]["honesty"]
    assert hon["allocated_model_cost"] is None
    assert hon["cost_allocated_pnl"] is None
    assert hon["beat_spy_after_model_cost"] is None
    assert "cost_allocated_pnl" in hon["gaps"]
    assert "beat_spy_after_model_cost" in hon["gaps"]
    assert hon["turnover_per_day"] is None
    assert "turnover_per_day" in hon["gaps"]


def test_card_facts_carry_honesty_and_do_not_graduate_paper_mid(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setenv("ABCXAUTO_CARD_LOG_PATH", str(tmp_path / "cards.jsonl"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(clamp_update(_book("flush bounce", fill_assumption="paper_mid")))
    row = card_facts()[0]
    assert row["fill_assumption"] == FILL_ASSUMPTION_PAPER_MID
    assert row["graduated"] is False
    assert row["honesty"]["live_evidence"] is False
    assert "beat_spy_after_model_cost" in row["honesty"]["gaps"]
    assert row["honesty"]["beat_spy_after_model_cost"] is None


def test_fill_assumption_survives_a_partial_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("ABCXAUTO_PLAYBOOK_LAB_PATH", str(tmp_path / "lab.json"))
    monkeypatch.setattr("abcxauto.lab_playbook.is_paper", lambda: True)
    save_lab(clamp_update(_book("flush bounce", fill_assumption="conservative")))
    save_lab(
        clamp_update(
            {
                "types": {
                    "market_bracket": {
                        "cards": [{"name": "flush bounce", "note": "still hunting"}]
                    }
                }
            }
        )
    )
    card = type_cards(load_lab()["types"], "market_bracket")[0]
    assert card["fill_assumption"] == FILL_ASSUMPTION_CONSERVATIVE
    assert card["retire_if"]["max_losses"] == 2
