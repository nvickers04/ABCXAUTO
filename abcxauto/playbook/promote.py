"""Promote contract — the file a human can hold.

Graduation (``card_verdict``) needs all of:

* conservative fill assumption (not ``paper_mid``)
* computed ``conservative_pnl`` (debit at ask / credit at bid, commissions in)
* ``retire_if.sample`` reached
* one numeric kill: ``max_losses`` or ``max_loss_usd``
* a thesis
* positive conservative edge

``maybe_promote`` snapshots only those graduated cards when
``ready_to_promote`` is set. Paper TWS realized / mids cannot graduate.
Playbook is Grok's notebook, not doctrine. Explore/exploit is a mode bit
(``playbook_mode``); sizing is ``mode_size``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from abcxauto.playbook.hub import hub as _hub
from abcxauto.playbook.persist import _lab_path, _live_path, _score_snap, _write, load_live
from abcxauto.playbook.schema import (
    CARD_EXIT_KINDS,
    CARD_STATUSES,
    CONSERVATIVE_FILL_ASSUMPTIONS,
    EXIT_OPEN,
    FILL_ASSUMPTION_PAPER_MID,
    HONESTY_GAP_REASONS,
    _CALIBRATION_MIN_N,
    _clean_types,
    _flat_card_projection,
    _has_book,
    _norm_expect_hit_rate,
    _strip_projection,
    card_key,
    fill_assumption_of,
    notebook_text,
    walk_cards,
)

logger = logging.getLogger(__name__)

def _has_numeric_kill(retire: Any) -> bool:
    """Promotion needs sample plus one of these. Clerk does not invent them."""
    row = retire if isinstance(retire, dict) else {}
    max_loss = row.get("max_loss_usd")
    if isinstance(max_loss, (int, float)) and float(max_loss) > 0:
        return True
    max_losses = row.get("max_losses")
    if isinstance(max_losses, int) and max_losses > 0:
        return True
    return False


def _looks_since(since_iso: str) -> int | None:
    """Grok looks (model calls) since a stamp. None when the journal is dark."""
    if not str(since_iso or "").strip():
        return None
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        fn = getattr(journal, "model_usage_since", None)
        if not callable(fn):
            return None
        usage = fn(since_iso) or {}
        return int(usage.get("calls") or 0)
    except Exception:
        return None


def _hours_since(raw: str, *, now: datetime | None = None) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        written = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return max(0.0, (clock - written).total_seconds() / 3600.0)


def hold_sessions_open(opened_at: Any, *, now: datetime | None = None) -> int:
    """Inclusive weekday ET dates a ticket has been open.

    Weekend days are skipped. Monday open still open Tuesday is two sessions.
    """
    text = str(opened_at or "").strip()
    if not text:
        return 0
    try:
        start = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    day = start.astimezone(et).date()
    last = clock.astimezone(et).date()
    if last < day:
        return 0
    n = 0
    cur = day
    while cur <= last:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def age_out_reason(
    card: dict[str, Any] | None,
    opened_at: Any,
    *,
    now: datetime | None = None,
) -> str:
    """Empty unless this open ticket is past the card's max_hold_*."""
    row = card if isinstance(card, dict) else {}
    retire = row.get("retire_if") if isinstance(row.get("retire_if"), dict) else {}
    sessions = retire.get("max_hold_sessions")
    hours_cap = retire.get("max_hold_hours")
    held_s = hold_sessions_open(opened_at, now=now)
    held_h = _hours_since(str(opened_at or ""), now=now)
    if isinstance(sessions, int) and sessions > 0 and held_s > sessions:
        return (
            f"open {held_s} sessions at declared max_hold_sessions {sessions}"
        )
    if (
        isinstance(hours_cap, (int, float))
        and float(hours_cap) > 0
        and held_h is not None
        and held_h > float(hours_cap)
    ):
        return (
            f"open {held_h:.1f}h at declared max_hold_hours {hours_cap}"
        )
    return ""


def _card_clock(card: dict[str, Any] | None, book: dict[str, Any] | None = None) -> str:
    """When the hypothesis first landed. Book wipe is the fallback for unstamped cards."""
    row = card if isinstance(card, dict) else {}
    written = str(row.get("written_at") or "").strip()
    if written:
        return written
    blob = book if isinstance(book, dict) else {}
    return str(blob.get("cleared_at") or blob.get("written_at") or "").strip()


def card_waiting(
    score: dict[str, Any] | None,
    card: dict[str, Any] | None = None,
    *,
    book: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Looks and days since the last send (or since write if never sent).

    Report only — never a trip. A prior fill must not hide later empty hunts:
    that is how a card keeps the same when_on after a -1R and ten rescans.
    Grok judges whether that means wait, retire, or write a different card.
    """
    sc = score if isinstance(score, dict) else {}
    row = card if isinstance(card, dict) else {}
    retire = row.get("retire_if") if isinstance(row.get("retire_if"), dict) else {}
    try:
        declared = int(retire.get("max_looks_without_trigger") or 0) or None
    except (TypeError, ValueError):
        declared = None
    clock = str(sc.get("last_send") or "").strip() or _card_clock(row, book)
    hours = _hours_since(clock, now=now) if clock else None
    looks = _looks_since(clock) if clock else None
    days = round(hours / 24.0, 1) if hours is not None else None
    return {
        "written_at": _card_clock(row, book) or None,
        "last_send": str(sc.get("last_send") or "").strip() or None,
        "looks_without_trigger": looks,
        "days_without_trigger": days,
        "max_looks_without_trigger": declared,
    }

def card_calibration(
    score: dict[str, Any] | None,
    card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The hit rate a card claimed against the one it actually produced.

    Report only. A card that claimed 70 and returned 40 is not tripped and its
    graduation is untouched — the gap is the fact worth reading, because
    positive resolved edge on a hit rate far under the claim is one fat winner
    rather than a repeatable setup. Thin samples say so instead of printing a
    number that cannot mean anything yet.
    """
    sc = score if isinstance(score, dict) else {}
    row = card if isinstance(card, dict) else {}
    declared = _norm_expect_hit_rate(row.get("expect_hit_rate"))
    resolved = int(sc.get("resolved") or 0)
    wins = int(sc.get("resolved_wins") or 0)
    out: dict[str, Any] = {
        "expect_hit_rate": declared,
        "resolved": resolved,
        "resolved_wins": wins,
        "hit_rate": None,
        "hit_rate_gap": None,
        "note": "",
    }
    if resolved < _CALIBRATION_MIN_N:
        out["note"] = f"thin resolved sample ({resolved} of {_CALIBRATION_MIN_N})"
        return out
    hit = round(100.0 * wins / resolved, 1)
    out["hit_rate"] = hit
    if declared is None:
        out["note"] = "no expect_hit_rate declared"
    else:
        out["hit_rate_gap"] = round(hit - declared, 1)
    return out


def _paper_book() -> bool:
    try:
        return bool(_hub().is_paper())
    except Exception:
        return True


def _finite_pnl(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def _verdict_edge(score: dict[str, Any], *, paper: bool) -> float | None:
    """P&L the verdict reads. Paper TWS realized / mid is never this number."""
    cons = _finite_pnl(score.get("conservative_pnl"))
    if cons is not None:
        return cons
    if not paper:
        return float(score.get("resolved_pnl") or 0.0)
    return None


def card_verdict(
    score: dict[str, Any] | None,
    card: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Graduated / tripped from the card's own declaration — honestly.

    The clerk supplies no thresholds. A card that declared nothing can neither
    graduate nor trip — it is flagged as owing a declaration on the next write.
    ``calibration`` rides along as a fact and never moves either verdict.
    ``paper_mid`` cannot set ``graduated``. The edge is ``conservative_pnl``
    (debit at ask / credit at bid, or fill vs NBBO), net of commissions —
    not paper TWS ``resolved_pnl``. Promotion also needs ``retire_if.sample``
    and one numeric kill.
    """
    sc = score if isinstance(score, dict) else {}
    row = card if isinstance(card, dict) else {}
    retire = row.get("retire_if") if isinstance(row.get("retire_if"), dict) else {}
    status = str(row.get("status") or "").strip().lower()
    thesis = bool(str(row.get("thesis") or "").strip())
    try:
        sample = int(retire.get("sample") or 0)
    except (TypeError, ValueError):
        sample = 0
    resolved = int(sc.get("resolved") or 0)
    resolved_pnl = float(sc.get("resolved_pnl") or 0.0)
    losses = int(sc.get("resolved_losses") or 0)
    anchored = str(row.get("type") or row.get("ticket") or sc.get("type") or "")
    locked = row.get("locked") is True
    fill = fill_assumption_of(row)
    conservative = fill in CONSERVATIVE_FILL_ASSUMPTIONS
    has_kill = _has_numeric_kill(retire)
    paper = _paper_book()
    edge = _verdict_edge(sc, paper=paper)
    cons_mark = _finite_pnl(sc.get("conservative_pnl"))
    edge_label = "conservative edge" if cons_mark is not None else "resolved edge"
    cal = card_calibration(sc, row)
    out: dict[str, Any] = {
        "anchored_type": anchored or None,
        "status": status or None,
        "retire_if": dict(retire) or None,
        "declared_sample": sample or None,
        "fill_assumption": fill,
        "needs_retire_if": bool(row) and not retire and not locked,
        "needs_thesis": bool(row) and not thesis and not locked,
        "needs_numeric_kill": bool(row) and not has_kill and not locked,
        "needs_conservative_fill": bool(row) and not conservative and not locked,
        "sample_left": max(0, sample - resolved) if sample else None,
        "calibration": cal,
        "graduated": False,
        "tripped": False,
        "trip_reason": "",
        "cannot_graduate_reason": "",
        "resolved_pnl": resolved_pnl,
        "conservative_pnl": cons_mark,
        "locked": locked,
    }
    out.update(card_waiting(sc, row, now=now))
    if status == "retired":
        return out
    max_loss = retire.get("max_loss_usd")
    max_losses = retire.get("max_losses")
    if sample and resolved >= sample:
        if edge is not None and edge <= 0:
            out["tripped"] = True
            out["trip_reason"] = (
                f"declared sample {sample} reached with {edge_label} "
                f"{edge:+.2f} — retire or rewrite"
            )
            return out
        if not conservative or fill == FILL_ASSUMPTION_PAPER_MID:
            out["cannot_graduate_reason"] = "paper_mid cannot graduate"
        elif paper and cons_mark is None:
            out["cannot_graduate_reason"] = "no conservative_pnl"
        elif edge is None:
            out["cannot_graduate_reason"] = "no conservative_pnl"
        elif not has_kill:
            out["cannot_graduate_reason"] = (
                "needs numeric kill (max_losses or max_loss_usd)"
            )
        elif not thesis:
            out["cannot_graduate_reason"] = "needs thesis"
        else:
            # Conservative mark, not paper TWS realized.
            out["graduated"] = True
    if (
        isinstance(max_loss, (int, float))
        and max_loss > 0
        and edge is not None
        and edge <= -float(max_loss)
    ):
        out["tripped"] = True
        out["graduated"] = False
        out["trip_reason"] = (
            f"{edge_label} {edge:+.2f} hit declared max_loss_usd {max_loss}"
        )
        return out
    if isinstance(max_losses, int) and max_losses > 0 and losses >= max_losses:
        out["tripped"] = True
        out["graduated"] = False
        out["trip_reason"] = (
            f"{losses} losing resolved trades at declared max_losses {max_losses}"
        )
        return out
    for trade in sc.get("open_trades") or []:
        if not isinstance(trade, dict):
            continue
        why = age_out_reason(row, trade.get("opened_at"), now=now)
        if why:
            out["tripped"] = True
            out["graduated"] = False
            out["trip_reason"] = why
            return out
    return out


def age_out_open_lots(
    *,
    now: datetime | None = None,
    book: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Open trades past the card's max_hold_*. Flatten those lots, not the book."""
    paper = _paper_book()
    state = book if isinstance(book, dict) else (
        _hub().load_lab() if paper else load_live()
    )
    index: dict[tuple[str, str], dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for type_name, card in walk_cards(state):
        name = str(card.get("name") or "").strip()
        if not name:
            continue
        index[card_key(type_name, name)] = card
        by_name.setdefault(name.lower(), []).append(card)
    sends = _hub().resolve_send_types(_hub()._card_sends(), state)
    fills, placed, _all_fills, _send_marks = _hub()._journal_exit_facts()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trade in _hub().classify_card_trades(sends, fills, placed):
        if str(trade.get("exit") or "") != EXIT_OPEN:
            continue
        match = index.get(card_key(trade.get("type"), trade.get("card")))
        if match is None:
            same = by_name.get(str(trade.get("card") or "").lower()) or []
            match = same[0] if len(same) == 1 else None
        why = age_out_reason(match or {}, trade.get("opened_at"), now=now)
        if not why:
            continue
        sym = str(trade.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(
            {
                "symbol": sym,
                "card": str(trade.get("card") or ""),
                "opened_at": trade.get("opened_at"),
                "reason": why,
            }
        )
    return out


def _model_cost_usd() -> float | None:
    """Book model bill when the scorecard has it. None is a named gap."""
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard()
    except Exception:
        return None
    cost = (sc or {}).get("model_cost_usd") if isinstance(sc, dict) else None
    try:
        val = float(cost)
    except (TypeError, ValueError):
        return None
    if val != val or val < 0:
        return None
    return val


def attach_card_honesty(
    rows: list[dict[str, Any]] | None,
    *,
    model_cost: float | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Cost share, turnover, named gaps. Does not invent SPY or fill-vs-last.

    Allocates the book model bill by send count. A card with no sends gets no
    share. Missing series stay in ``gaps`` with reasons in HONESTY_GAP_REASONS.
    """
    out = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    total_sends = 0
    for row in out:
        try:
            total_sends += max(0, int(row.get("sends") or 0))
        except (TypeError, ValueError):
            continue
    have_cost = isinstance(model_cost, (int, float)) and float(model_cost) >= 0
    for row in out:
        if row.get("locked") is True:
            continue
        gaps: list[str] = [
            "fill_vs_ibkr_last",
            "holdout",
            "beat_spy_after_model_cost",
        ]
        try:
            sends = max(0, int(row.get("sends") or 0))
        except (TypeError, ValueError):
            sends = 0
        allocated = None
        cost_pnl = None
        if have_cost and total_sends > 0 and sends > 0:
            allocated = round(float(model_cost) * (sends / float(total_sends)), 4)
            try:
                pnl = float(row.get("resolved_pnl") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            cost_pnl = round(pnl - allocated, 4)
        else:
            gaps.append("cost_allocated_pnl")
        clock = str(row.get("written_at") or "").strip()
        hours = _hours_since(clock, now=now) if clock else None
        turnover = None
        if hours is not None and hours > 0:
            try:
                resolved = int(row.get("resolved") or 0)
            except (TypeError, ValueError):
                resolved = 0
            turnover = round(resolved / (hours / 24.0), 4)
        else:
            gaps.append("turnover_per_day")
        row["honesty"] = {
            "fill_assumption": row.get("fill_assumption") or FILL_ASSUMPTION_PAPER_MID,
            "allocated_model_cost": allocated,
            "cost_allocated_pnl": cost_pnl,
            "fill_vs_ibkr_last": None,
            "turnover_per_day": turnover,
            "holdout": None,
            "beat_spy_after_model_cost": None,
            "gaps": gaps,
        }
    return out


def card_facts(book: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every ``(type, card)`` on the tree with its attribution, sent or not."""
    state = book if isinstance(book, dict) else _hub().load_lab()
    pairs = walk_cards(state)
    flat = _flat_card_projection(state)
    scored = {card_key(r.get("type"), r.get("card")): r for r in _hub().card_scores(flat)}
    unresolved = {
        str(r.get("card") or "").lower(): r
        for r in scored.values()
        if not r.get("type") and int(r.get("ambiguous_sends") or 0) > 0
    }
    out: list[dict[str, Any]] = []
    for type_name, card in pairs:
        row = dict(scored.get(card_key(type_name, card.get("name"))) or {})
        if not row:
            row = _hub()._empty_score(str(card.get("name") or ""), type_name)
            row["on_current_book"] = True
        stamped = dict(card)
        stamped["type"] = type_name
        if not str(stamped.get("written_at") or "").strip():
            stamped["written_at"] = _card_clock({}, state)
        row.update(card_verdict(row, stamped))
        row["locked"] = card.get("locked") is True
        stray = unresolved.get(str(card.get("name") or "").lower())
        if stray is not None:
            # Same name under two types: those sends belong to neither.
            row["ambiguous_sends"] = int(stray.get("ambiguous_sends") or 0)
        out.append(row)
    return attach_card_honesty(out, model_cost=_model_cost_usd())


def graduated_card_names(book: dict[str, Any] | None = None) -> list[str]:
    """Cards that cleared honest graduation, not paper-mid sample pleasing."""
    state = book if isinstance(book, dict) else _hub().load_lab()
    declared = state.get("graduated")
    if isinstance(declared, list) and declared:
        return [str(x) for x in declared if str(x).strip()]
    return [
        str(row.get("card"))
        for row in _hub().card_facts(state)
        if row.get("graduated")
    ]


def _owes_declaration(row: dict[str, Any] | None) -> bool:
    blob = row if isinstance(row, dict) else {}
    return bool(
        blob.get("needs_retire_if")
        or blob.get("needs_thesis")
        or blob.get("needs_numeric_kill")
        or blob.get("needs_conservative_fill")
    )


def _card_label(row: dict[str, Any] | None) -> str:
    """``name [type]`` — the readable form of a card's identity."""
    blob = row if isinstance(row, dict) else {}
    name = str(blob.get("card") or blob.get("name") or "")
    parent = str(blob.get("type") or blob.get("ticket") or "")
    return f"{name} [{parent}]" if parent else name


def _card_names_blob(names: list[str]) -> str:
    return " | ".join(names) if names else "none — write_lab_playbook first"


def _cards_under_blob(pairs: list[tuple[str, dict[str, Any]]], type_name: str) -> str:
    """Live card names under one trunk, else where the cards actually are."""
    here = [
        str(c.get("name"))
        for t, c in pairs
        if t == type_name and c.get("status") != "retired"
    ]
    if here:
        return " | ".join(here)
    elsewhere = [
        f"{c.get('name')} [{t or 'unfiled'}]"
        for t, c in pairs
        if c.get("status") != "retired"
    ]
    if elsewhere:
        return f"none (elsewhere: {' | '.join(elsewhere[:8])})"
    return "none — write_lab_playbook first"


def new_risk_card_error(
    card: Any,
    *,
    type: str = "",
    book: dict[str, Any] | None = None,
) -> str:
    """Label gate on new risk. Empty string means this ticket may go.

    ``params.card`` must name an existing playbook card so scorecard/journal
    can tally the strategy. The name is a label, not law: trunk, retired,
    tripped, unfiled, and card prose are not refuses. Exits, protection,
    modifies and cancels never reach here — ``is_new_risk`` is False for
    them. New risk is new risk on either socket.
    ``type`` is accepted for callers that pass the send strategy; it is not
    a trunk match.
    """
    _ = type
    state = book if isinstance(book, dict) else _hub().load_lab()
    pairs = walk_cards(state)
    names = [
        str(c.get("name") or "").strip()
        for _t, c in pairs
        if str(c.get("name") or "").strip()
    ]
    want = str(card or "").strip()
    if not want:
        return (
            "new risk requires params.card naming a playbook card; "
            f"cards: {_card_names_blob(names)}"
        )
    hits = [
        (t, c)
        for t, c in pairs
        if str(c.get("name") or "").strip().lower() == want.lower()
    ]
    if not hits:
        return (
            f"new risk card {want!r} is not on the playbook; "
            f"cards: {_card_names_blob(names)}"
        )
    try:
        from abcxauto.mode_size import exploit_learning_card_error

        exploit_note = exploit_learning_card_error(want, type=type, book=state)
    except Exception:
        exploit_note = ""
    if exploit_note:
        return exploit_note
    return ""

def maybe_promote(*, scorecard: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Promote graduated cards, not the book.

    Graduation is the honest verdict: conservative fill assumption, a
    computed ``conservative_pnl``, declared sample, one numeric kill,
    thesis, positive conservative edge. Paper mid and paper TWS realized
    cannot unlock live. Only those cards reach the live snapshot, inside
    their own pruned type stanza.
    """
    lab = _hub().load_lab()
    if not lab.get("ready_to_promote"):
        return None
    facts = _hub().card_facts(lab)
    grads = [row for row in facts if row.get("graduated")]
    if not grads:
        return None
    keys = {card_key(row.get("type"), row.get("card")) for row in grads}
    # A verdict that named no type still promotes, but only when one card on the
    # tree answers to that name.
    index = _hub().card_types_by_name(lab)
    for row in grads:
        name = str(row.get("card") or "").strip().lower()
        if not row.get("type") and len(index.get(name) or []) == 1:
            keys.add((index[name][0], name))
    types: dict[str, Any] = {}
    names: list[str] = []
    for type_name, stanza in _clean_types(lab.get("types")).items():
        keep = [
            c
            for c in (stanza.get("cards") or [])
            if card_key(type_name, c.get("name")) in keys
        ]
        if not keep:
            continue
        pruned = dict(stanza)
        pruned["cards"] = keep
        types[type_name] = pruned
        names.extend(str(c.get("name")) for c in keep)
    if not names:
        return None
    now = datetime.now(timezone.utc).isoformat()
    sc = scorecard or lab.get("paper_score") or {}
    live = {
        "promoted": True,
        "promoted_at": now,
        "promoted_revision": lab.get("revision"),
        "source": "paper_lab",
        "mode": lab.get("mode"),
        "ready_to_promote": True,
        "types": types,
        "graduated": names,
        "card_scores": grads,
        "instructions": notebook_text({"types": types}),
        "paper_score": _score_snap(sc),
        "book_beating_at_promote": promote_beating(sc),
        "note": "live follows this snapshot; does not copy paper fills",
    }
    _write(_live_path(), live)
    lab["promoted"] = True
    lab["promoted_at"] = now
    _write(_lab_path(), _strip_projection(lab))
    return live


def promote_window() -> str:
    """Scorecard window the promote gate reads. Inception never recovers."""
    import os

    raw = (os.environ.get("ABCXAUTO_PROMOTE_WINDOW") or "").strip()
    return raw or "1d"


def promote_beating(scorecard: dict[str, Any] | None) -> bool | None:
    """Beating on the promote window, falling back to the full-book flag.

    Lifetime ``beating_model`` folds in every past experiment plus the whole
    cumulative model bill, so once it is behind it stays behind. A window is a
    question the lab can actually answer.
    """
    sc = scorecard if isinstance(scorecard, dict) else {}
    wins = sc.get("windows") if isinstance(sc.get("windows"), dict) else {}
    row = wins.get(promote_window()) if isinstance(wins, dict) else None
    if isinstance(row, dict) and row.get("coverage") == "ok":
        return row.get("beating_model")
    return sc.get("beating_model")


def playbook_mode() -> str:
    """explore | exploit. Size is ``mode_size`` — this is the bit, not a label."""
    mode = str((_hub().load_lab() or {}).get("mode") or "explore").strip().lower()
    return mode if mode in ("explore", "exploit") else "explore"


def playbook_next_look_s() -> float | None:
    """Card next_look_s is not a clerk clock. Overnight park is park_clock."""
    return None


def live_has_promoted() -> bool:
    """A promoted snapshot only counts if a graduated card is actually in it."""
    live = load_live()
    if not live.get("promoted") or not _has_book(live):
        return False
    return bool(_hub().graduated_card_names(live))


def live_new_risk_allowed() -> bool:
    """New risk is new risk. The socket is the live switch, not a second gate.

    Promotion snapshot stays as an operator live-enable. It does not refuse
    sends and is not a mid-look rule.
    """
    return True



__all__ = [
    '_has_numeric_kill',
    '_looks_since',
    '_hours_since',
    'hold_sessions_open',
    'age_out_reason',
    '_card_clock',
    'card_waiting',
    'card_calibration',
    '_paper_book',
    '_finite_pnl',
    '_verdict_edge',
    'card_verdict',
    'age_out_open_lots',
    '_model_cost_usd',
    'attach_card_honesty',
    'card_facts',
    'graduated_card_names',
    '_owes_declaration',
    '_card_label',
    '_card_names_blob',
    '_cards_under_blob',
    'new_risk_card_error',
    'maybe_promote',
    'promote_window',
    'promote_beating',
    'playbook_mode',
    'playbook_next_look_s',
    'live_has_promoted',
    'live_new_risk_allowed',
]
