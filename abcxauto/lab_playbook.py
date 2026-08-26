"""Paper lab playbook â€” Grok's notebook; live only follows a promote.

One tree, two layers, both written by Grok::

    TYPE market_bracket            <- durable: tool_order, gotchas, review
      |- card: mega-cap earnings-flush bounce   <- thesis, evidence, retire_if
      |- card: opening-range continuation
    TYPE vertical_spread
      |- card: post-earnings IV crush

* the **trunk** is ``types``: one entry per sendable ORDER_EXAMPLES key holding
  what Grok learned about *executing that structure* â€” the tool sequence that
  works, the execution gotchas, how it reviews the result. Durable, changes
  slowly. The clerk never writes schema here: ``ORDER EXAMPLES`` is already in
  the prompt, and restating it was how ~40% of the old notebook became
  boilerplate. See ``type_schema_echo_keys``.
* the **branches** are that type's ``cards``: disposable hypotheses, each
  carrying its thesis, the evidence that produced it, and the falsification it
  declares for itself (``retire_if``). Numerous, tested, retired.
* **locked OPEN starters** fill a trunk that has no live hypothesis so the book
  is not three flush cards plus empty slots. Seeded on lab load/save only.
  Live snapshots are never seeded. ``locked`` is clerk seed identity — not a
  hunt floor, not a send stamp, not a freeze. Grok rewrites the same name;
  a named write drops ``locked`` so the upgrade can hunt. Virgin starters stay
  in the ``playbook()`` catalog and off the run sheet.

A card's position in the tree *is* its ticket, so a winning card sits inside
the type entry it is supposed to improve â€” promoting what it learned is a move
within one stanza, not a join across two lists. Card identity is therefore
``(type, name)``, not a bare name.

The clerk's job is attribution, not authorship: a named ``params.card`` tags
the fill so the card is scored on its own resolved trades. New risk must name
an existing card (scorecard label). Notebook prose is not a send gate — hold,
gap, tape, session, and book sentences cannot invent a refuse. Operator
flattens, panic, and halt exits are tracked but kept out of the graduation
math — an interrupted trade is neither proof nor falsification. A card
graduates when it meets *its own* declared sample with positive resolved
edge; only graduated cards, inside their pruned type stanzas, reach
``playbook_live.json``. Live new risk still needs that promoted snapshot.

A flat top-level ``cards`` list is still accepted on a write and is still
*projected* on a read for the cockpit and older callers, but the tree is the
only thing stored. See ``_migrate_book`` and ``_flat_card_projection``.

Notebook is not executable, not a standing order. Optional ``next_look_s`` on
a card is a clerk cadence hint. Clerk validates writes against gates
(floors / live / sleeve) like self_tune.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LAB = _REPO_ROOT / "playbook_lab.json"
_DEFAULT_LIVE = _REPO_ROOT / "playbook_live.json"
_MAX_INSTRUCTIONS = 16000
_LEDGER_CAP = 12
_PATCH_KEYS = (
    "instructions",
    "cards",
    "types",
    "catalog",
    "mode",
    "ready_to_promote",
)
# A type entry holds only what Grok learned about running that structure.
# open_shape / close_tp_sl / defined_risk were clerk-derived restatements of
# ORDER EXAMPLES — they are stripped on read and on write, not stored.
TYPE_LEARNED_FIELDS = ("tool_order", "gotchas", "review", "note")
_TYPE_SCHEMA_ECHO = (
    "open_shape",
    "close_tp_sl",
    "defined_risk",
    "ticket_shape",
    "strategies",
)
# Cards are the book. A card lives *under* its type, so the parent key is the
# ticket and identity is (type, name). Shape lives in ``_norm_card``.
# Room for Grok's live cards plus one locked starter per OPEN type.
_MAX_CARDS = 48
_MAX_CARDS_PER_TYPE = 12
# Legacy cards that named no type at all. Never dropped, never sendable (they
# have no ticket to send), surfaced as owing a parent.
UNFILED_KEY = "unfiled_cards"
# Derived on read for the cockpit and older callers; never written to disk.
_PROJECTED_KEYS = ("cards",)
# What a card actually used. Not a checklist — populate what applies.
EVIDENCE_FIELDS = ("scan", "news", "reads", "odds")
CARD_STATUSES = ("testing", "working", "retired")
# Resolved trades before a hit rate is worth printing. Matches path_math's
# refusal to compute expectancy on a thinner sample than this.
_CALIBRATION_MIN_N = 4
# How a card's trade ended. Only ``protective`` and ``decision`` are evidence.
EXIT_PROTECTIVE = "protective"
EXIT_DECISION = "decision"
EXIT_OPERATOR = "operator"
EXIT_OPEN = "open"
CARD_EXIT_KINDS = (EXIT_PROTECTIVE, EXIT_DECISION, EXIT_OPERATOR, EXIT_OPEN)
RESOLVED_EXITS = (EXIT_PROTECTIVE, EXIT_DECISION)
# Not catalog trunks: knobs, plus defined_risk_only rejects.
_SKIP_PLAYBOOK_TYPES = frozenset({
    "set_risk",
    "self_tune",
    "ratio_spread",
    "jade_lizard",
})
# Trunk = these sendable ORDER_EXAMPLES keys. Never invert.
PLAYBOOK_TYPE_KEYS = (
    "market_bracket",
    "bracket",
    "trailing_stop",
    "modify_stop",
    "modify_target",
    "cancel_order",
    "close_option",
    "buy_option",
    "vertical_spread",
    "calendar_spread",
    "diagonal_spread",
    "butterfly",
    "iron_butterfly",
    "iron_condor",
    "straddle",
    "strangle",
    "protective_put",
    "collar",
    "covered_call",
    "cash_secured_put",
)
# Entry trunks that should hold a hypothesis. Exits, knobs, and
# defined_risk_only rejects are not slots. buy_option is skip-only.
OPEN_PLAYBOOK_TYPES = (
    "market_bracket",
    "bracket",
    "vertical_spread",
    "iron_condor",
    "iron_butterfly",
    "butterfly",
    "calendar_spread",
    "diagonal_spread",
    "straddle",
    "strangle",
    "covered_call",
    "cash_secured_put",
    "collar",
    "protective_put",
)
# Grok's live flush book and retired skip cards. Seed never replaces these.
PROTECTED_CARD_NAMES = frozenset(
    {
        "mega-cap earnings-flush bounce",
        "large-cap 3pct gap hold",
        "news-miss large-cap flush",
        "levered-crypto and micro gap chase",
        "naked / short-dated option spray",
        "defined-risk flush debit",
    }
)
_STK_HUNT = ("scan", "news", "quote", "candles", "send")
_OPT_HUNT = ("scan", "news", "option_chain", "option_facts", "option_quote", "send")
_OVERLAY_HUNT = ("book", "quote", "option_chain", "option_facts", "send")
# One short locked starter per OPEN type. Structure, not a ticker.
# Seeded on lab load/save when the trunk has no live hypothesis (and, for
# vertical_spread, when the generic debit/credit card is missing).
OPEN_TYPE_STARTERS: dict[str, dict[str, Any]] = {
    "market_bracket": {
        "name": "generic STK market bracket",
        "thesis": (
            "Defined-risk stock structure: marketable parent with resting "
            "stop and target children. Risk is stop distance times quantity, "
            "not a narrative."
        ),
        "when_on": (
            "Liquid large/mega name on the live IBKR book where a stop can "
            "rest on the wrong side of last. No thin or levered chase."
        ),
        "scan": (
            "most_active plus top losers or gainers; large/mega only; skip "
            "levered products and illiquid names"
        ),
        "shape": (
            "market_bracket: symbol, direction, quantity, stop_price, "
            "target_price. LONG or SHORT (cash-only rejects SHORT stock). "
            "Children rest at IBKR. Close: child stop/target fill, "
            "modify_stop / modify_target, or an exit ticket. Defined risk = "
            "|entry-stop| * qty."
        ),
        "invalidation": (
            "Stop through the written level; wide live spread; unprotected "
            "STK; do not move the stop away from risk."
        ),
        "tool_order": list(_STK_HUNT),
    },
    "bracket": {
        "name": "limit-entry STK bracket",
        "thesis": (
            "Same defined-risk stock structure as a market bracket, but the "
            "parent rests as a limit. Risk is still the stop, not the limit."
        ),
        "when_on": (
            "Wanted price is away from last so a limit can rest. Liquid "
            "large/mega. After fill, stop must still rest on the wrong side "
            "of last."
        ),
        "scan": (
            "most_active plus high or low open gap; large/mega; skip levered "
            "and illiquid names"
        ),
        "shape": (
            "bracket: symbol, direction, quantity, entry_price, stop_price, "
            "target_price. Parent limit plus child stop/target. Cancel the "
            "parent if it never fills. Close path same as market_bracket "
            "children. cash-only rejects SHORT stock."
        ),
        "invalidation": (
            "Limit hanging through the move; stop cannot rest; spread too "
            "wide to define risk."
        ),
        "tool_order": list(_STK_HUNT),
    },
    "vertical_spread": {
        "name": "defined-risk debit/credit vertical",
        "thesis": (
            "One expiry, same right, two strikes. Debit buys direction with "
            "max loss = debit paid. Credit sells defined width. Not a name "
            "list."
        ),
        "when_on": (
            "Liquid optionable name, listed strikes, a width you can price. "
            "Use debit for expansion or credit for crush — not both on one "
            "ticket."
        ),
        "scan": (
            "most_active optionable large/mega; option_chain then "
            "option_facts on the two strikes, not a name dump"
        ),
        "shape": (
            "vertical_spread: long_strike, short_strike, right, expiration, "
            "quantity. Defined risk = width*100 minus credit, or debit paid. "
            "Close: same strategy + closing_position + live limit_price "
            "(one BAG). Never close_option a wing."
        ),
        "invalidation": (
            "Unpriceable width, one-sided market, or a debit with the short "
            "strike on the wrong side of the long."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "iron_condor": {
        "name": "defined-risk iron condor",
        "thesis": (
            "Short a call spread and a put spread, same expiry, long wings "
            "cap the loss. Collects if the name stays inside the shorts."
        ),
        "when_on": (
            "Range-bound tape, no binary event into the body, both wings "
            "listed and tight enough to define risk."
        ),
        "scan": (
            "most_active optionable large/mega with two-sided listed wings; "
            "option_facts on the four strikes, not a name dump"
        ),
        "shape": (
            "iron_condor: put_long_strike < put_short_strike < "
            "call_short_strike < call_long_strike, same expiration, "
            "quantity. Defined risk = wing width minus credit. Close: same "
            "strategy + closing_position + live limit_price (one BAG)."
        ),
        "invalidation": (
            "A missing wing, a binary event into the body, or credit too "
            "small versus width."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "iron_butterfly": {
        "name": "defined-risk iron butterfly",
        "thesis": (
            "Short the ATM body both rights, long equidistant wings. Defined "
            "at the wings; wants a pin, not a trend."
        ),
        "when_on": (
            "Expected pin at a listed center, wings exist at that width, no "
            "event that should blow through the body."
        ),
        "scan": (
            "optionable large/mega; option_chain around ATM; option_facts on "
            "center and wings"
        ),
        "shape": (
            "iron_butterfly: center_strike, wing_width, expiration, "
            "quantity. Defined risk = wing_width minus credit. Close: same "
            "strategy + closing_position + live limit_price (one BAG)."
        ),
        "invalidation": (
            "No listed wings at that width, center not the pin, or event "
            "risk through the body."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "butterfly": {
        "name": "defined-risk butterfly",
        "thesis": (
            "Long the wings, short twice the body, one right, one expiry. "
            "Debit paid is max loss. Wants a pin at the body."
        ),
        "when_on": (
            "A listed equal-width three-strike ladder on one right, a debit "
            "you can define, pin thesis not a trend thesis."
        ),
        "scan": (
            "optionable large/mega; option_chain for equal-width strikes; "
            "option_facts on the three legs"
        ),
        "shape": (
            "butterfly: lower_strike, middle_strike, upper_strike (equal "
            "width), right, expiration, quantity. Defined risk = debit paid. "
            "Close: same strategy + closing_position + live limit_price "
            "(one BAG)."
        ),
        "invalidation": (
            "Uneven width, missing body, or a debit that does not cap loss."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "calendar_spread": {
        "name": "defined-risk calendar",
        "thesis": (
            "Same strike, two expiries, one right. Long the far, short the "
            "near. Debit paid is defined risk. Harvests near-term decay "
            "against the back month."
        ),
        "when_on": (
            "Near expiry has decay to sell, far month listed, no blow-through "
            "event that wrecks the term structure you just bought."
        ),
        "scan": (
            "optionable large/mega; option_chain across two expiries at one "
            "strike; option_facts on both legs"
        ),
        "shape": (
            "calendar_spread: strike, near_expiration, far_expiration, "
            "right, quantity. Defined risk = debit. Close: same strategy + "
            "closing_position + live limit_price (one BAG)."
        ),
        "invalidation": (
            "Far month missing, inverted debit you cannot define, or an "
            "event that reprices the back month against you."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "diagonal_spread": {
        "name": "defined-risk diagonal",
        "thesis": (
            "Different strike and expiry, one right. Long the far, short the "
            "near. The debit (long far) keeps the structure closed-risk."
        ),
        "when_on": (
            "Term structure plus a strike roll you can price; both legs "
            "listed."
        ),
        "scan": (
            "optionable large/mega; two-expiry chain; option_facts on near "
            "and far strikes"
        ),
        "shape": (
            "diagonal_spread: near_strike, far_strike, near_expiration, "
            "far_expiration, right, quantity. Defined risk = debit paid "
            "(do not naked-short the near). Close: same strategy + "
            "closing_position + live limit_price (one BAG)."
        ),
        "invalidation": (
            "Near short without a long far, unpriceable diagonal, or a "
            "missing expiry."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "straddle": {
        "name": "long defined-risk straddle",
        "thesis": (
            "Long both rights at one strike, one expiry. Debit paid is max "
            "loss. Short straddles are a defined_risk_only reject."
        ),
        "when_on": (
            "A move large enough to outrun the debit is plausible; listed "
            "ATM; action=BUY only."
        ),
        "scan": (
            "optionable large/mega around an event or a realized-vol vacuum; "
            "option_facts on the ATM pair, not a name dump"
        ),
        "shape": (
            "straddle: action=BUY, strike, expiration, quantity. Defined "
            "risk = debit. Close: same strategy + closing_position + live "
            "limit_price (one BAG). Never SELL to open."
        ),
        "invalidation": (
            "action would be SELL; debit does not cap loss; one right missing."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "strangle": {
        "name": "long defined-risk strangle",
        "thesis": (
            "Long an OTM put and an OTM call, same expiry. Debit paid is "
            "max loss. Short strangles are a defined_risk_only reject."
        ),
        "when_on": (
            "A wider move than the straddle debit, both OTMs listed, "
            "action=BUY only."
        ),
        "scan": (
            "optionable large/mega; option_chain for the OTM pair; "
            "option_facts on both strikes"
        ),
        "shape": (
            "strangle: action=BUY, put_strike, call_strike, expiration, "
            "quantity. Defined risk = debit. Close: same strategy + "
            "closing_position + live limit_price (one BAG). Never SELL to open."
        ),
        "invalidation": (
            "action would be SELL; a missing wing; debit does not cap loss."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "covered_call": {
        "name": "covered call on long shares",
        "thesis": (
            "Short a call against long shares already in the book. Upside "
            "is capped at the strike; the share lot is the cover. Clerk "
            "does not invent shares."
        ),
        "when_on": (
            "Long STK/ETF already on the book, listed call, willing to cap "
            "the lot. Not an uncovered short call."
        ),
        "scan": (
            "book first for long share lots; then option_chain on those "
            "names. Do not scan a fresh name to naked-short."
        ),
        "shape": (
            "covered_call: expiration, strike, shares (shares required). "
            "Cover must exist. Close: buy back the call or roll_option; "
            "stock exit is a separate stock ticket."
        ),
        "invalidation": (
            "No long shares, shares omitted, or a call that does not cover "
            "the lot."
        ),
        "tool_order": list(_OVERLAY_HUNT),
    },
    "cash_secured_put": {
        "name": "cash-secured put",
        "thesis": (
            "Short put with cash to buy the shares at strike. Defined risk "
            "is assignment at the strike, not a naked short put."
        ),
        "when_on": (
            "Cash to secure the strike, listed put, willing to own the name "
            "at that strike."
        ),
        "scan": (
            "most_active optionable large/mega you would own at the strike; "
            "option_facts on the put, not a name dump"
        ),
        "shape": (
            "cash_secured_put: expiration, strike, contracts. Defined risk "
            "is strike * 100 * contracts. Close: buy back the put, or take "
            "assignment and then a stock exit or protect."
        ),
        "invalidation": (
            "Cash cannot secure the strike; put not listed; treating it as "
            "naked premium."
        ),
        "tool_order": list(_OPT_HUNT),
    },
    "collar": {
        "name": "collar on long shares",
        "thesis": (
            "Long shares already in the book, long a put and short a call. "
            "Defined floor and cap. Clerk does not invent shares."
        ),
        "when_on": (
            "Long STK/ETF on the book that needs a floor, listed put/call "
            "pair, willing to cap the lot."
        ),
        "scan": (
            "book first for long share lots; option_chain on those names only"
        ),
        "shape": (
            "collar: expiration, put_strike, call_strike, shares (shares "
            "required). Close the option pair (or roll); stock is a "
            "separate ticket."
        ),
        "invalidation": (
            "No long shares, shares omitted, or a missing put (that is just "
            "a covered call)."
        ),
        "tool_order": list(_OVERLAY_HUNT),
    },
    "protective_put": {
        "name": "protective put on long shares",
        "thesis": (
            "Long shares already in the book plus a long put. Floor is the "
            "put strike; debit is the cost of the floor. Clerk does not "
            "invent shares."
        ),
        "when_on": (
            "Long STK/ETF on the book that needs an options floor; listed put."
        ),
        "scan": (
            "book first for long share lots; option_chain on those names only"
        ),
        "shape": (
            "protective_put: expiration, strike, shares (shares required). "
            "Defined floor = put. Close the put; stock is a separate ticket."
        ),
        "invalidation": (
            "No long shares, shares omitted, or a put that does not cover "
            "the lot."
        ),
        "tool_order": list(_OVERLAY_HUNT),
    },
}
_TYPE_META_KEYS = frozenset(
    {
        "mode",
        "ready_to_promote",
        "instructions",
        "types",
        "catalog",
        "default_tool_recipe",
    }
    | set(TYPE_LEARNED_FIELDS)
    | set(_TYPE_SCHEMA_ECHO)
)
# Only an unsendable ticket kills a write now. Notes, regime reads, and
# per-name observations are the point of the book.
_HARD_SHAPE = frozenset({"unknown_type"})
# Ceremony leftovers. Must not linger via save_lab merging **prev.
_DEAD_LAB_KEYS = (
    "do_more",
    "stop_doing",
    "basis",
    "evidence",
    "research_tools",
    "diary",
    "nap",
    "naps",
    "wake_at",
    "wake_in_s",
    "wake_if",
    "ticker_list",
    "tickers",
)
# Notebook is not self_tune. Same class of knobs self_tune rejects or clamps â€”
# write_lab_playbook must not loosen floors, switch live, or set a dollar sleeve.
_GATE_FORBIDDEN: dict[str, str] = {
    "trading_mode": "live remains gated â€” notebook cannot switch mode",
    "live_confirm": "live remains gated â€” notebook cannot switch mode",
    "sizing_floors": "operator-only â€” notebook cannot flip sizing floors",
    "trading_budget_usd": "size and risk are % of NetLiq â€” no dollar sleeve",
    "risk_posture": "risk_posture is locked â€” notebook cannot retune",
    "daily_loss_limit_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_position_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_risk_per_trade_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_peak_drawdown_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_option_premium_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_open_positions": "knobs are self_tune â€” notebook cannot retune risk",
    "risk_gates_enabled": "immutable floor â€” notebook cannot disable",
    "auto_panic_on_breach": "immutable floor â€” notebook cannot disable",
    "defined_risk_only": "immutable floor â€” notebook cannot disable",
    "cash_only": "immutable floor â€” notebook cannot disable",
}
# GATES: N% / floor N% NL is clerk law. Notebook may restate it only when
# sizing_floors is ON and N is the live max_risk_per_trade_pct knob.
_GATES_HDR = re.compile(r"\bGATES\b[^:\n]{0,48}:", re.IGNORECASE)
_FLOOR_NL = re.compile(r"\bfloor\s+(\d+(?:\.\d+)?)\s*%\s*NL\b", re.IGNORECASE)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TYPE_HDR = re.compile(r"^TYPE\s+(\S+)", re.IGNORECASE)
_FIELD_LINE = re.compile(
    r"^(tool_order|default_tool_recipe|gotchas|review|note)\s*[:=]\s*(.*)$",
    re.IGNORECASE,
)
_STALE_H_DEFAULT = 1.0
_CARD_WINDOWS = ("15m", "1h", "4h")


def stale_hours() -> float:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_STALE_H") or "").strip()
    if not raw:
        return _STALE_H_DEFAULT
    try:
        return max(0.25, float(raw))
    except ValueError:
        return _STALE_H_DEFAULT


def _lab_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_LAB_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_LAB


def _live_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_LIVE_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_LIVE


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.debug("playbook read failed path=%s", path, exc_info=True)
        return {}


def _write(path: Path, state: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("playbook write failed path=%s", path)


def _drop_dead_lab_keys(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    for key in _DEAD_LAB_KEYS:
        out.pop(key, None)
    for key in _GATE_FORBIDDEN:
        out.pop(key, None)
    out.pop("risk", None)
    out.pop("universe", None)
    out.pop("rejected", None)
    return out


def _named_card_in(cards: Any, name: str) -> dict[str, Any] | None:
    want = str(name or "").strip().lower()
    if not want:
        return None
    for card in cards or []:
        if isinstance(card, dict) and str(card.get("name") or "").strip().lower() == want:
            return card
    return None


def _upsert_named_card(
    branch: list[Any],
    row: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replace a same-name card in place; append if the name is new."""
    key = str(row.get("name") or "").strip().lower()
    out: list[dict[str, Any]] = []
    found = False
    for card in branch:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        if str(card.get("name") or "").strip().lower() == key:
            if not found:
                out.append(row)
                found = True
            continue
        out.append(card)
    if not found:
        out.append(row)
    return out


def _file_cards_into_tree(
    types: Any,
    cards: list[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Route loose cards under the type they name, creating the trunk if new.

    A card is never dropped for want of a stanza. One that names nothing
    sendable cannot be placed, so it lands in ``unfiled_cards`` where it is
    still visible and still owed a parent. Same-name writes merge against the
    stored card and keep siblings — a one-card flat list is not a wipe.
    """
    allowed = set(playbook_type_keys())
    tree: dict[str, Any] = {
        k: dict(v) for k, v in (types or {}).items() if isinstance(v, dict)
    } if isinstance(types, dict) else {}
    unfiled: list[dict[str, Any]] = []
    for raw in cards:
        ticket = card_ticket_of(raw)
        if not ticket or ticket not in allowed:
            row = _norm_card(raw)
            if row is None:
                continue
            if ticket:
                # Unsendable, so it has no parent — but keep what it claimed so
                # the cockpit can still say which ticket does not exist.
                row["claimed_ticket"] = ticket
            if not any(c["name"].lower() == row["name"].lower() for c in unfiled):
                unfiled.append(row)
            continue
        stanza = dict(tree.get(ticket) or {})
        prev_card = _named_card_in(stanza.get("cards"), _incoming_card_name(raw))
        row = _norm_card(raw, prev=prev_card)
        if row is None:
            continue
        branch = [
            c for c in (stanza.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        stanza["cards"] = _upsert_named_card(branch, row)
        tree[ticket] = stanza
    return tree, unfiled


def _migrate_book(state: dict[str, Any]) -> dict[str, Any]:
    """Read-time migration to the tree. Type schema echoes drop; cards do not.

    A flat ``cards`` list from the old shape is filed under each card's
    ``ticket`` with every field, note and status intact. Revision 1 cards have
    no ``thesis``, ``evidence`` or ``retire_if``: they are real work product, so
    they migrate with empty declarations and are surfaced as owing one on the
    next write, never retired and never dropped.
    """
    if not isinstance(state, dict) or not state:
        return {}
    out = _drop_dead_lab_keys(state)
    loose = list(out.get("cards") or []) + list(out.get(UNFILED_KEY) or [])
    tree, unfiled = _file_cards_into_tree(out.get("types"), loose)
    types = _clean_types(tree)
    if types or "types" in out:
        out["types"] = types
    if unfiled:
        out[UNFILED_KEY] = unfiled
    else:
        out.pop(UNFILED_KEY, None)
    projected = _flat_card_projection(out)
    if projected or "cards" in out:
        out["cards"] = projected
    return out


def load_lab() -> dict[str, Any]:
    """Lab notebook. Missing OPEN types gain locked starters; live cards stay."""
    return _seed_open_type_starters(_migrate_book(_read(_lab_path())))


def load_live() -> dict[str, Any]:
    """Promoted snapshot only. Never seed untested starters onto live."""
    return _migrate_book(_read(_live_path()))


def is_paper() -> bool:
    try:
        from abcxauto.config import get_config

        return bool(get_config().is_paper)
    except Exception:
        return True


def _field(raw: dict[str, Any], prev: dict[str, Any], key: str, default: str = "") -> str:
    if key in raw:
        return str(raw.get(key) or default)
    return str(prev.get(key) or default)


def playbook_type_keys() -> tuple[str, ...]:
    """Sendable ORDER_EXAMPLES keys the notebook may use as trunks."""
    from abcxauto.order_examples import NOT_TICKETS, ORDER_EXAMPLES

    skip = _SKIP_PLAYBOOK_TYPES | NOT_TICKETS
    return tuple(k for k in PLAYBOOK_TYPE_KEYS if k in ORDER_EXAMPLES and k not in skip)


def open_playbook_types() -> tuple[str, ...]:
    """Entry trunks that take a locked starter when they have no live card."""
    allowed = set(playbook_type_keys())
    return tuple(k for k in OPEN_PLAYBOOK_TYPES if k in allowed)


def type_coverage(book: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Operator view: every sendable trunk and whether Grok has filled it in.

    Derived on read for the cockpit, like ``_flat_card_projection`` — never
    stored and never in the payload, because a stanza per sendable key is the
    boilerplate ``empty_type_catalog`` exists to refuse. An untouched trunk here
    is a gap in the notebook, not a slot waiting to be seeded with schema.
    """
    state = book if isinstance(book, dict) else load_lab()
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    out: list[dict[str, Any]] = []
    for name in playbook_type_keys():
        row = types.get(name) if isinstance(types.get(name), dict) else {}
        learned: list[str] = []
        for field in TYPE_LEARNED_FIELDS:
            val = row.get(field)
            if isinstance(val, (list, tuple)):
                if val:
                    learned.append(field)
            elif str(val or "").strip():
                learned.append(field)
        cards = [
            c for c in (row.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        out.append({
            "type": name,
            "cards": len(cards),
            "learned": learned,
            "touched": bool(learned or cards),
        })
    return out


def empty_type_catalog() -> dict[str, Any]:
    """No schema stanzas. Hypothesis cards seed via ``OPEN_TYPE_STARTERS``.

    The old catalog copied ``open_shape`` / ``close_tp_sl`` out of
    ORDER_EXAMPLES. That stays empty. Locked OPEN-type starters are a
    different path: ``_seed_open_type_starters`` on lab load/save.
    """
    return {}


def _norm_recipe(raw: Any) -> list[str]:
    """Optional tool sequence. Stored, never gated."""
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,;>]+|->", raw) if p.strip()]
        return parts[:16]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()][:16]
    return []


def _floors_and_knob() -> tuple[bool, float]:
    """Live clerk flag + max_risk_per_trade_pct. Fail closed: floors off."""
    try:
        from abcxauto.config import get_config
        from abcxauto.risk_gates import sizing_floors_active

        cfg = get_config()
        knob = float(getattr(cfg, "max_risk_per_trade_pct", 0) or 0)
        return bool(sizing_floors_active(cfg)), knob
    except Exception:
        return False, 0.0


def _gate_pcts_on_line(line: str) -> list[float]:
    """Percents claimed as GATES: N% or floor N% NL on one line."""
    pcts: list[float] = []
    if _GATES_HDR.search(line):
        pcts.extend(float(m.group(1)) for m in _PCT.finditer(line))
    pcts.extend(float(m.group(1)) for m in _FLOOR_NL.finditer(line))
    return pcts


def _invented_pct_gate_line(line: str, floors_on: bool, knob: float) -> bool:
    """True when this line invents a % gate the clerk is not enforcing."""
    pcts = _gate_pcts_on_line(line)
    if not pcts:
        return False
    if floors_on and knob > 0 and all(abs(n - knob) < 1e-6 for n in pcts):
        return False
    return True


def _has_invented_pct_gate(text: str) -> bool:
    floors_on, knob = _floors_and_knob()
    return any(_invented_pct_gate_line(line, floors_on, knob) for line in text.splitlines())


def _strip_invented_pct_gate_lines(text: str) -> str:
    """Drop GATES: N% / floor N% NL lines unless floors ON and N is the live knob."""
    floors_on, knob = _floors_and_knob()
    kept = [
        line
        for line in text.splitlines()
        if not _invented_pct_gate_line(line, floors_on, knob)
    ]
    return "\n".join(kept)


def _walk_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_walk_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return "\n".join(_walk_text(v) for v in obj)
    return ""


def _norm_type_row(row: Any, *, prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """One trunk: learned execution plus its cards.

    Schema echoes are dropped, never merged forward. ``cards`` omitted keeps
    the previous branch list. A non-empty ``cards`` list merges by name —
    one upgraded card does not wipe siblings. ``cards: []`` is the explicit
    clear of this type.
    """
    prev = prev if isinstance(prev, dict) else {}
    src = row if isinstance(row, dict) else {}
    out: dict[str, Any] = {}
    incoming_order = src.get("tool_order", src.get("default_tool_recipe"))
    if "tool_order" in src or "default_tool_recipe" in src:
        rec = _norm_recipe(incoming_order)
    else:
        rec = _norm_recipe(prev.get("tool_order") or prev.get("default_tool_recipe"))
    if rec:
        out["tool_order"] = rec
    for key in ("gotchas", "review", "note"):
        val = src.get(key) if key in src else prev.get(key)
        if key == "note" and val in (None, ""):
            val = src.get("notes") if "notes" in src else None
        text = _strip_invented_pct_gate_lines(str(val or "")).strip()[:1200]
        if text:
            out[key] = text
    if "cards" in src:
        cards = _norm_cards(
            src.get("cards"),
            cap=_MAX_CARDS_PER_TYPE,
            prev=prev.get("cards"),
        )
    else:
        cards = _norm_cards(prev.get("cards"), cap=_MAX_CARDS_PER_TYPE)
    if cards:
        out["cards"] = cards
    return out


def _clean_types(types: Any) -> dict[str, Any]:
    """Keep sendable trunks that hold learnings or cards. Drop schema echoes."""
    if not isinstance(types, dict):
        return {}
    out: dict[str, Any] = {}
    budget = _MAX_CARDS
    for name in playbook_type_keys():
        row = types.get(name)
        if not isinstance(row, dict):
            continue
        stanza = _norm_type_row(row, prev={})
        cards = stanza.get("cards") or []
        if cards:
            # Tree-wide cap, applied trunk by trunk in catalog order.
            keep = cards[:budget]
            budget -= len(keep)
            if keep:
                stanza["cards"] = keep
            else:
                stanza.pop("cards", None)
        if stanza:
            out[name] = stanza
    return out


def type_cards(types: Any, name: str) -> list[dict[str, Any]]:
    """Cards branching under one type."""
    row = (types or {}).get(name) if isinstance(types, dict) else None
    if not isinstance(row, dict):
        return []
    return [c for c in (row.get("cards") or []) if isinstance(c, dict) and c.get("name")]


def walk_cards(state: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(type, card)`` pair in the tree, in catalog order.

    Unfiled legacy cards come last with an empty type: they are surfaced and
    scored, but nothing can send them.
    """
    blob = state if isinstance(state, dict) else {}
    types = blob.get("types") if isinstance(blob.get("types"), dict) else {}
    out: list[tuple[str, dict[str, Any]]] = []
    for name in playbook_type_keys():
        for card in type_cards(types, name):
            out.append((name, card))
    for card in blob.get(UNFILED_KEY) or []:
        if isinstance(card, dict) and card.get("name"):
            out.append(("", card))
    return out


def card_key(type_name: Any, card_name: Any) -> tuple[str, str]:
    """Identity of a card: its type and its name, both case-folded."""
    return (
        str(type_name or "").strip().lower(),
        str(card_name or "").strip().lower(),
    )


def _flat_card_projection(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Read-only flat view of the tree, each card stamped with its parent.

    The cockpit and the scorecard table were written against a flat list. The
    tree is the only stored shape, so this is derived on read and stripped
    before any write â€” see ``_strip_projection``.
    """
    out: list[dict[str, Any]] = []
    for type_name, card in walk_cards(state):
        row = dict(card)
        row["ticket"] = type_name or str(card.get("claimed_ticket") or "")
        row["type"] = type_name
        out.append(row)
    return out


def _strip_projection(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    for key in _PROJECTED_KEYS:
        out.pop(key, None)
    return out


def type_schema_echo_keys(types: Any) -> list[str]:
    """Schema-restatement keys still present on a type map. Should be empty."""
    if not isinstance(types, dict):
        return []
    found: list[str] = []
    for row in types.values():
        if not isinstance(row, dict):
            continue
        for key in _TYPE_SCHEMA_ECHO:
            if key in row and key not in found:
                found.append(key)
    return found


def _norm_evidence(raw: Any, *, scan: str = "") -> dict[str, str]:
    """What the card actually used. Populate what applies, not a checklist."""
    src = raw if isinstance(raw, dict) else {}
    if isinstance(raw, str) and raw.strip():
        src = {"reads": raw}
    out: dict[str, str] = {}
    for key in EVIDENCE_FIELDS:
        val = src.get(key)
        if key == "reads" and val in (None, "", []):
            val = src.get("tool_reads")
        if key == "news" and val in (None, "", []):
            val = src.get("headlines")
        if isinstance(val, (list, tuple)):
            val = ", ".join(str(x).strip() for x in val if str(x).strip())
        text = str(val or "").strip()[:800]
        if text:
            out[key] = text
    if scan and not out.get("scan"):
        out["scan"] = scan
    return out


def _norm_expect_hit_rate(raw: Any) -> float | None:
    """The win rate a card claims, as a percent. Measured, never enforced.

    A fraction (``0.62``) and a percent (``62``) both mean 62 — the model
    writes whichever it thinks in, and ``1`` reads as certainty, not 1%.
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    if val <= 1.0:
        val *= 100.0
    if val > 100.0:
        return None
    return round(val, 1)


def _norm_retire_if(raw: Any) -> dict[str, Any]:
    """The card's own falsification. Clerk enforces it; it never invents one."""
    if isinstance(raw, str):
        text = raw.strip()
        return {"condition": text[:600]} if text else {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    sample = raw.get("sample", raw.get("sample_size", raw.get("n")))
    try:
        n = int(float(sample)) if sample not in (None, "") else 0
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        out["sample"] = min(n, 200)
    cond = str(raw.get("condition") or raw.get("retire_if") or "").strip()
    if cond:
        out["condition"] = _strip_invented_pct_gate_lines(cond).strip()[:600]
    for key, cap in (("max_loss_usd", 1e9), ("max_losses", 200)):
        val = raw.get(key)
        if val in (None, ""):
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        out[key] = int(min(num, cap)) if key == "max_losses" else round(min(num, cap), 2)
    looks = raw.get("max_looks_without_trigger", raw.get("max_looks"))
    try:
        n_looks = int(float(looks)) if looks not in (None, "") else 0
    except (TypeError, ValueError):
        n_looks = 0
    if n_looks > 0:
        # Stored so Grok can see the count against its own claim. The clerk
        # never trips on it — a trigger that never prints is a judgment.
        out["max_looks_without_trigger"] = min(n_looks, 10_000)
    return out


def card_ticket_of(raw: Any) -> str:
    """The type a loose card claims. Only used to file it under its parent."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("ticket") or raw.get("strategy") or "").strip().lower()[:60]


def _incoming_card_name(raw: Any) -> str:
    """Name on an unnormalized card, for matching it to the one on disk."""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        return str(raw.get("name") or raw.get("setup") or "").strip()
    return ""


def _norm_card(raw: Any, *, prev: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One card. Its parent type is the ticket, so no ``ticket`` is stored.

    A card already on the tree keeps its declarations and gate fields when a
    write omits them. Named writes merge, so an evidence-only rewrite must not
    delete the ``retire_if`` / ``when_on`` it did not restate: the clerk then
    reported the card as owing a declaration, and the next look re-hunted a
    gate that was still on disk until the wipe. Observations still replace
    when Grok sends them; omitted fields persist until Grok changes them.
    ``locked`` is clerk seed identity. Re-norm of a stored starter keeps it;
    a named rewrite drops it so Grok's upgrade is not frozen off the hunt.
    """
    carried = prev if isinstance(prev, dict) else {}
    if isinstance(raw, str):
        name = raw.strip()
        raw = {"name": name} if name else {}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("setup") or "").strip()
    if not name:
        return None
    scan = raw.get("scan")
    if isinstance(scan, (list, tuple)):
        scan = ", ".join(str(x).strip() for x in scan if str(x).strip())
    scan_s = str(scan or "").strip()[:400]
    incoming_evidence = raw.get("evidence")
    evidence = _norm_evidence(incoming_evidence, scan=scan_s)
    # ``scan`` stays top-level as well: it is the one evidence field that
    # already exists on disk and on the cockpit card. What the card wrote there
    # wins — the evidence copy only fills the gap.
    scan_s = scan_s or evidence.get("scan") or ""
    if not scan_s:
        scan_s = str(carried.get("scan") or "").strip()[:400]
    status = str(raw.get("status") or "").strip().lower()
    if not status:
        status = str(carried.get("status") or "testing").strip().lower()
    if status not in CARD_STATUSES:
        # Graduation is the clerk's verdict from resolved trades, not a status
        # Grok can assert.
        status = "working" if status == "graduated" else "testing"
    thesis = _strip_invented_pct_gate_lines(
        str(raw.get("thesis") or "").strip()
    ).strip()[:1200]
    if not thesis:
        thesis = str(carried.get("thesis") or "").strip()[:1200]
    when_on = str(raw.get("when_on") or "").strip()[:800]
    if not when_on:
        when_on = str(carried.get("when_on") or "").strip()[:800]
    shape = str(raw.get("shape") or raw.get("ticket_shape") or "").strip()[:800]
    if not shape:
        shape = str(carried.get("shape") or "").strip()[:800]
    invalidation = str(raw.get("invalidation") or "").strip()[:800]
    if not invalidation:
        invalidation = str(carried.get("invalidation") or "").strip()[:800]
    note = str(raw.get("note") or raw.get("notes") or "").strip()[:1200]
    if not note:
        note = str(carried.get("note") or "").strip()[:1200]
    if not evidence and isinstance(carried.get("evidence"), dict):
        evidence = {
            k: str(v).strip()[:800]
            for k, v in carried["evidence"].items()
            if str(v or "").strip()
        }
    out: dict[str, Any] = {
        "name": name[:120],
        "thesis": thesis,
        "when_on": when_on,
        "scan": scan_s,
        "shape": shape,
        "invalidation": invalidation,
        "status": status,
        "note": note,
    }
    if evidence:
        out["evidence"] = evidence
    expect = _norm_expect_hit_rate(raw.get("expect_hit_rate"))
    if expect is None:
        expect = _norm_expect_hit_rate(carried.get("expect_hit_rate"))
    if expect is not None:
        out["expect_hit_rate"] = expect
    retire = _norm_retire_if(raw.get("retire_if"))
    if not retire:
        retire = _norm_retire_if(carried.get("retire_if"))
    if retire:
        out["retire_if"] = retire
    written = str(raw.get("written_at") or carried.get("written_at") or "").strip()
    if written:
        out["written_at"] = written[:48]
    look = raw.get("next_look_s")
    if look is None:
        look = carried.get("next_look_s")
    if look is not None:
        try:
            from abcxauto.park_clock import clamp_next_look_s

            clamped = clamp_next_look_s(look)
        except Exception:
            clamped = None
        if clamped is not None:
            out["next_look_s"] = clamped
    order = _norm_recipe(raw.get("tool_order") or raw.get("default_tool_recipe"))
    if not order:
        order = _norm_recipe(
            carried.get("tool_order") or carried.get("default_tool_recipe")
        )
    if order:
        out["tool_order"] = order
    # Pinning locked from ``carried`` froze upgrades off the hunt. Seed and
    # re-norm of a stored starter pass no prev, so the flag on the row sticks.
    # A named rewrite (prev is the stored card) drops it even if Grok copies
    # locked=true — lock is not a freeze.
    if prev is None and raw.get("locked") is True:
        out["locked"] = True
    return out


def _card_name_key(card: Any) -> str:
    if isinstance(card, dict):
        return str(card.get("name") or "").strip().lower()
    return ""


def _non_retired_cards(cards: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for card in cards or []:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        if str(card.get("status") or "").strip().lower() == "retired":
            continue
        out.append(card)
    return out


def _starter_row(type_name: str) -> dict[str, Any] | None:
    spec = OPEN_TYPE_STARTERS.get(type_name)
    if not isinstance(spec, dict) or not spec.get("name"):
        return None
    row = _norm_card({**spec, "status": "testing", "locked": True})
    return row


def _seed_open_type_starters(state: dict[str, Any] | None) -> dict[str, Any]:
    """Fill missing OPEN types with one locked starter. Never clobber live cards.

    Lab only. A 3-type book gains the missing trunks on load; Grok's flush
    cards and retired skip cards keep their fields. Empty ``{}`` stays empty.
    Live snapshots must not call this.
    """
    if not isinstance(state, dict) or not state:
        return state if isinstance(state, dict) else {}
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    if not types and not state.get("cards") and not state.get(UNFILED_KEY):
        return state
    tree: dict[str, Any] = {k: dict(v) for k, v in types.items() if isinstance(v, dict)}
    changed = False
    for type_name in open_playbook_types():
        starter = _starter_row(type_name)
        if starter is None:
            continue
        stanza = dict(tree.get(type_name) or {})
        cards = [
            c for c in (stanza.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        names = {_card_name_key(c) for c in cards}
        starter_key = _card_name_key(starter)
        if starter_key in names:
            rec = _norm_recipe(starter.get("tool_order"))
            if rec and not stanza.get("tool_order"):
                stanza["tool_order"] = rec
                tree[type_name] = stanza
                changed = True
            continue
        live = _non_retired_cards(cards)
        # market_bracket already has the live flush book — keep it.
        # vertical_spread keeps defined-risk flush debit and still needs the
        # generic debit/credit starter when that name is missing.
        if type_name == "market_bracket" and live:
            continue
        if type_name != "vertical_spread" and live:
            continue
        rec = _norm_recipe(starter.get("tool_order"))
        if rec and not stanza.get("tool_order"):
            stanza["tool_order"] = rec
        cards = cards + [starter]
        stanza["cards"] = cards[:_MAX_CARDS_PER_TYPE]
        tree[type_name] = stanza
        changed = True
    if not changed:
        return state
    out = dict(state)
    out["types"] = _clean_types(tree)
    projected = _flat_card_projection(out)
    if projected or "cards" in out:
        out["cards"] = projected
    return out


def unknown_card_tickets(cards: Any) -> list[str]:
    """Loose-card ``ticket`` values that are not sendable ORDER_EXAMPLES keys.

    Only reachable from the flat write shape. A nested card cannot have an
    unknown ticket â€” its parent key is validated as a type.
    """
    if not isinstance(cards, list):
        return []
    allowed = set(playbook_type_keys())
    bad: list[str] = []
    for raw in cards:
        if _norm_card(raw) is None:
            continue
        ticket = card_ticket_of(raw)
        if ticket and ticket not in allowed and ticket not in bad:
            bad.append(ticket)
    return bad


def conflicting_card_tickets(types: Any) -> list[str]:
    """Nested cards whose ``ticket`` disagrees with the type they sit under.

    Position decides the ticket. A card that says otherwise is a write the
    clerk refuses rather than silently re-filing.
    """
    if not isinstance(types, dict):
        return []
    bad: list[str] = []
    for name, row in types.items():
        if not isinstance(row, dict):
            continue
        for raw in row.get("cards") or []:
            ticket = card_ticket_of(raw)
            if not ticket or ticket == str(name).strip().lower():
                continue
            label = f"{str((raw or {}).get('name') or '?')[:60]}: {ticket} under {name}"
            if label not in bad:
                bad.append(label)
    return bad


def untyped_card_names(cards: Any) -> list[str]:
    """Loose cards with no ticket at all. They have no home in the tree."""
    if not isinstance(cards, list):
        return []
    out: list[str] = []
    for raw in cards:
        row = _norm_card(raw)
        if row is None or card_ticket_of(raw):
            continue
        if row["name"] not in out:
            out.append(row["name"])
    return out


def _norm_cards(
    raw: Any,
    *,
    cap: int = _MAX_CARDS,
    prev: Any = None,
) -> list[dict[str, Any]]:
    """Normalize one card list. Named writes merge; an empty list clears.

    ``prev`` is the branch list already on the tree. A name that reappears is
    merged against its stored self so declarations survive a partial rewrite.
    Names left out stay — the replace-list was the wipe that collapsed the
    book to three flush cards. Drop a card with ``status=retired`` (stays on
    the book, off the hunt) or ``cards: []`` (explicit clear of this type).
    Last write of a name wins within the incoming list. Sibling order is
    previous order, then new names.
    """
    if not isinstance(raw, list):
        return []
    if not raw:
        return []
    carried: dict[str, dict[str, Any]] = {}
    prev_rows: list[dict[str, Any]] = []
    for row in prev or []:
        if isinstance(row, dict) and row.get("name"):
            carried[str(row["name"]).strip().lower()] = row
            prev_rows.append(row)
    incoming_by_key: dict[str, dict[str, Any]] = {}
    incoming_order: list[str] = []
    for item in raw:
        row = _norm_card(item, prev=carried.get(_incoming_card_name(item).lower()))
        if not row:
            continue
        key = row["name"].lower()
        if key not in incoming_by_key:
            incoming_order.append(key)
        incoming_by_key[key] = row
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in prev_rows:
        key = str(row["name"]).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(incoming_by_key[key] if key in incoming_by_key else row)
        if len(out) >= cap:
            return out
    for key in incoming_order:
        if key in seen:
            continue
        seen.add(key)
        out.append(incoming_by_key[key])
        if len(out) >= cap:
            break
    return out


def _retire_if_line(retire: Any) -> str:
    row = retire if isinstance(retire, dict) else {}
    bits: list[str] = []
    if row.get("sample"):
        bits.append(f"sample={row['sample']}")
    if row.get("max_loss_usd"):
        bits.append(f"max_loss_usd={row['max_loss_usd']}")
    if row.get("max_losses"):
        bits.append(f"max_losses={row['max_losses']}")
    if row.get("max_looks_without_trigger"):
        bits.append(f"max_looks_without_trigger={row['max_looks_without_trigger']}")
    if row.get("condition"):
        bits.append(str(row["condition"]))
    return " ".join(bits)


def render_cards(cards: list[dict[str, Any]] | None, *, indent: str = "") -> str:
    """Readable card stanzas. Indented, these are the branches of a trunk."""
    rows = [c for c in (cards or []) if isinstance(c, dict) and c.get("name")]
    if not rows:
        return ""
    lines: list[str] = []
    for card in rows:
        head = f"{indent}CARD {card.get('name')}"
        status = str(card.get("status") or "").strip()
        if status:
            head += f"  [{status}]"
        if card.get("locked") is True:
            head += "  [locked]"
        lines.append(head)
        for key in ("thesis", "when_on", "scan", "shape", "invalidation", "note"):
            val = str(card.get(key) or "").strip()
            if val:
                lines.append(f"{indent}  {key}: {val}")
        order = _norm_recipe(card.get("tool_order") or card.get("default_tool_recipe"))
        if order:
            lines.append(f"{indent}  tool_order: {', '.join(str(x) for x in order)}")
        look = card.get("next_look_s")
        if look is not None:
            lines.append(f"{indent}  next_look_s: {look}")
        evidence = card.get("evidence") if isinstance(card.get("evidence"), dict) else {}
        for key in EVIDENCE_FIELDS:
            if key == "scan":
                continue
            val = str(evidence.get(key) or "").strip()
            if val:
                lines.append(f"{indent}  evidence.{key}: {val}")
        retire = _retire_if_line(card.get("retire_if"))
        lines.append(
            f"{indent}  retire_if: {retire}"
            if retire
            else f"{indent}  retire_if: NOT DECLARED"
        )
    return "\n".join(lines)


def render_playbook_tree(state: dict[str, Any] | None) -> str:
    """The tree: each trunk's learnings, then the cards branching under it.

    Accepts the whole book or just its ``types`` map, so older callers that
    passed the type layer alone still render.
    """
    blob = state if isinstance(state, dict) else {}
    types = blob.get("types") if isinstance(blob.get("types"), dict) else blob
    if not isinstance(types, dict) or not types:
        types = {}
    lines: list[str] = []
    for name in playbook_type_keys():
        row = types.get(name)
        if not isinstance(row, dict):
            continue
        stanza = _norm_type_row(row, prev={})
        if not stanza:
            continue
        lines.append(f"TYPE {name}")
        order = stanza.get("tool_order")
        if order:
            lines.append(f"  tool_order: {', '.join(str(x) for x in order)}")
        for key in ("gotchas", "review", "note"):
            val = str(stanza.get(key) or "").strip()
            if val:
                lines.append(f"  {key}: {val}")
        branch = render_cards(stanza.get("cards"), indent="  ")
        if branch:
            lines.append(branch)
    unfiled = blob.get(UNFILED_KEY) if isinstance(blob, dict) else None
    if isinstance(unfiled, list) and unfiled:
        lines.append("UNFILED (needs a parent type before it can send)")
        lines.append(render_cards(unfiled, indent="  "))
    return "\n".join(lines)


def notebook_text(state: dict[str, Any] | None) -> str:
    """The tree. Prose instructions are the fallback when nothing is written."""
    blob = state if isinstance(state, dict) else {}
    tree = render_playbook_tree(blob)
    if tree:
        return tree
    return str(blob.get("instructions") or "").strip()


def _has_book(state: dict[str, Any] | None) -> bool:
    return bool(notebook_text(state))


def _unknown_type_keys(blob: dict[str, Any]) -> list[str]:
    allowed = set(playbook_type_keys())
    return sorted(
        k for k in blob
        if k not in allowed and k not in _TYPE_META_KEYS
    )


def _parse_structured_text(text: str) -> dict[str, Any] | None:
    """TYPE stanzas written as text. Learned fields only."""
    types: dict[str, Any] = {}
    cur_type: str | None = None
    found_type = False
    for raw_line in text.splitlines():
        s = raw_line.strip()
        if not s:
            continue
        m = _TYPE_HDR.match(s)
        if m:
            cur_type = m.group(1).strip().rstrip(":").strip()
            types.setdefault(cur_type, {})
            found_type = True
            continue
        m = _FIELD_LINE.match(s)
        if not m or not cur_type:
            continue
        field = m.group(1).lower()
        val = m.group(2).strip()
        if field == "default_tool_recipe":
            field = "tool_order"
        if field == "tool_order":
            types[cur_type][field] = _norm_recipe(val)
        else:
            types[cur_type][field] = val
    if not found_type:
        return None
    return types


def _coerce_types_blob(blob: Any) -> dict[str, Any] | None:
    if blob is True:
        return {}
    if isinstance(blob, str):
        text = blob.strip()
        if not text:
            return None
        if text.lower() in ("catalog", "type catalog", "types"):
            return {}
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                inner = parsed.get("types")
                if isinstance(inner, dict):
                    return inner
                if (
                    parsed == {}
                    or any(k in playbook_type_keys() for k in parsed)
                    or _looks_like_type_map(parsed)
                ):
                    return parsed
                return None
        return _parse_structured_text(text)
    if isinstance(blob, dict):
        inner = blob.get("types")
        if isinstance(inner, dict) and (
            inner == {}
            or any(k in playbook_type_keys() for k in inner)
            or _looks_like_type_map(inner)
        ):
            return inner
        if any(k in playbook_type_keys() for k in blob) or blob == {} or _looks_like_type_map(blob):
            return blob
    return None


def _looks_like_type_map(blob: dict[str, Any]) -> bool:
    if not blob:
        return False
    stanza = (
        set(TYPE_LEARNED_FIELDS)
        | set(_TYPE_SCHEMA_ECHO)
        | {"default_tool_recipe", "cards"}
    )
    values = list(blob.values())
    if not all(isinstance(v, dict) for v in values):
        return False
    return any(stanza & set(v) for v in values)


def _extract_types(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Incoming types dict, or None if omitted. Error is unknown_type / unstructured."""
    for key in ("types", "catalog"):
        if key not in raw:
            continue
        blob = raw.get(key)
        if blob is None or blob == "":
            continue
        parsed = _coerce_types_blob(blob)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    inst = raw.get("instructions")
    if isinstance(inst, dict):
        parsed = _coerce_types_blob(inst)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    if isinstance(inst, str) and inst.strip():
        parsed = _coerce_types_blob(inst)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    return None, ""


def book_shape_rejects(raw: Any) -> dict[str, str]:
    """Reject only writes the tree cannot hold. Prose observations always save.

    Three ways a card has no place: it claims a ticket the clerk cannot send, it
    claims none at all, or it sits under one type while claiming another. The
    last one is the reason this is a reject and not a silent re-filing â€” position
    and ticket disagreeing is exactly the ambiguity nesting removes. Everything
    else — notes, regime reads, per-name observations — saves. The notebook is
    not a standing order: ``format_block`` never paints notes as tickets, and
    ``next_look_s`` is a clerk cadence hint, not a send.
    """
    if not isinstance(raw, dict):
        return {}
    if "cards" in raw:
        bad = unknown_card_tickets(raw.get("cards"))
        if bad:
            return {
                "unknown_type": f"card ticket must be a sendable type ({', '.join(bad)})"
            }
        orphans = untyped_card_names(raw.get("cards"))
        if orphans:
            return {
                "unknown_type": (
                    "a card lives under an order type — nest it in "
                    f"types[<type>].cards or give it a ticket ({', '.join(orphans)})"
                )
            }
    incoming, err = _extract_types(raw)
    if isinstance(incoming, dict):
        clash = conflicting_card_tickets(incoming)
        if clash:
            return {
                "unknown_type": (
                    "a card's ticket must match the type it sits under "
                    f"({'; '.join(clash)})"
                )
            }
    if err == "unknown_type":
        blob = raw.get("types") if "types" in raw else raw.get("catalog")
        parsed = blob if isinstance(blob, dict) else _coerce_types_blob(blob)
        bad_types = _unknown_type_keys(parsed) if isinstance(parsed, dict) else []
        label = ", ".join(bad_types) if bad_types else "unknown"
        return {"unknown_type": f"do not add unknown types ({label})"}
    return {}


def _merge_type_catalog(
    prev: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any] | None:
    """Patch the learned layer. The clerk contributes nothing of its own.

    The old version stamped every trunk's ``open_shape`` / ``close_tp_sl`` from
    ORDER_EXAMPLES on every write, so the notebook grew a verbatim copy of the
    ticket schema Grok already has. Now an untouched type keeps exactly what
    Grok last wrote â€” learnings *and* the cards branching under it â€” and a type
    with nothing learned and nothing to test is simply absent.
    """
    allowed_set = set(playbook_type_keys())
    if _unknown_type_keys(incoming):
        return None
    out = _clean_types(prev.get("types"))
    for name, row in (incoming or {}).items():
        if name not in allowed_set:
            continue
        merged = _norm_type_row(row, prev=out.get(name))
        if merged:
            out[name] = merged
        else:
            out.pop(name, None)
    return out


def _cards_update(raw: dict[str, Any], prev: dict[str, Any]) -> dict[str, Any] | None:
    """Incoming loose ``cards`` list filed under the tree, or None if absent.

    A non-empty flat list updates those names and keeps every sibling and
    every other trunk — the old strip-then-file is how a one-card write
    wiped the book. ``cards: []`` is the explicit clear of every branch.
    Nested ``types[*].cards`` then wins for any trunk that declared one.
    """
    if "cards" not in raw:
        return None
    incoming = list(raw.get("cards") or [])
    tree = _clean_types(prev.get("types"))
    if not incoming:
        bare = {
            name: {k: v for k, v in row.items() if k != "cards"}
            for name, row in tree.items()
            if isinstance(row, dict)
        }
        filed, _unfiled = _file_cards_into_tree(bare, [])
        return filed
    filed, _unfiled = _file_cards_into_tree(tree, incoming)
    return filed


def gate_rejects(raw: Any) -> dict[str, str]:
    """Reject floors / live / sleeve knobs on a notebook write. Notes stay notes."""
    if not isinstance(raw, dict):
        return {}
    rejected: dict[str, str] = {}
    for key, reason in _GATE_FORBIDDEN.items():
        if key in raw:
            rejected[key] = reason
    # Nested self_tune-shaped blobs are not notebook fields either.
    for nest in ("risk", "universe"):
        blob = raw.get(nest)
        if nest in raw and isinstance(blob, dict):
            rejected[nest] = "knobs are self_tune â€” notebook cannot retune"
            for key, reason in _GATE_FORBIDDEN.items():
                if key in blob:
                    rejected[key] = reason
    inst = str(raw.get("instructions") or "")
    types_text = _walk_text(raw.get("types")) if isinstance(raw.get("types"), dict) else ""
    catalog_text = _walk_text(raw.get("catalog")) if isinstance(raw.get("catalog"), dict) else ""
    if (
        (inst and _has_invented_pct_gate(inst))
        or (types_text and _has_invented_pct_gate(types_text))
        or (catalog_text and _has_invented_pct_gate(catalog_text))
    ):
        rejected["invented_pct_gate"] = "notebook cannot invent a % gate"
    return rejected


def clamp_update(raw: Any) -> dict[str, Any] | None:
    """Full rewrite or patch. Omitted fields keep the previous lab text.

    Gate knobs (floors / live / sleeve) are never stored â€” see gate_rejects.
    Invented GATES: N% / floor N% NL lines are stripped unless floors are ON
    and N equals the live max_risk_per_trade_pct knob.
    The book is one TYPE tree (sendable keys) with cards branching under it,
    not a diary.
    """
    if not isinstance(raw, dict):
        return None
    if not any(k in raw for k in _PATCH_KEYS):
        return None
    if _HARD_SHAPE.intersection(book_shape_rejects(raw)):
        return None
    prev = load_lab()

    # A loose cards[] list merges by name into the tree; nested types[*].cards
    # then wins for any trunk that named one. cards: [] still clears.
    base = _cards_update(raw, prev)
    cards_given = base is not None
    staged = dict(prev)
    if base is not None:
        staged["types"] = base

    incoming, err = _extract_types(raw)
    types_given = incoming is not None
    types: dict[str, Any] = {}
    if incoming is not None:
        merged = _merge_type_catalog(staged, incoming)
        types = merged if merged is not None else {}
    else:
        types = _clean_types(staged.get("types"))
    _ = err

    if "instructions" in raw:
        instructions = _strip_invented_pct_gate_lines(
            str(raw.get("instructions") or "")
        )
    elif types:
        instructions = _strip_invented_pct_gate_lines(notebook_text({"types": types}))
    else:
        instructions = str(prev.get("instructions") or "")
    instructions = instructions.strip()[:_MAX_INSTRUCTIONS]

    if not instructions and not types:
        return None
    mode = _field(raw, prev, "mode", "explore").strip().lower()
    if mode not in ("explore", "exploit"):
        mode = "explore"
    ready = raw["ready_to_promote"] if "ready_to_promote" in raw else prev.get("ready_to_promote")
    out: dict[str, Any] = {
        "mode": mode,
        "instructions": instructions,
        "ready_to_promote": bool(ready),
    }
    if types or types_given or cards_given:
        # Writing an empty stanza is how Grok drops a type it no longer trusts.
        out["types"] = types
    return out


def grounding_error(
    raw: dict[str, Any] | None,
    *,
    tool_trace: list[str] | None = None,
) -> str:
    """Shape only. The notebook is Grok's; clerk does not demand a constitution."""
    if not isinstance(raw, dict):
        return "write_lab_playbook needs a notebook object"
    return ""


def _card_log_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_CARD_LOG_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _REPO_ROOT / "data" / "state" / "card_sends.jsonl"


def _send_is_new_risk(strategy: str, params: dict[str, Any] | None) -> bool:
    """Reuse the clerk's own predicate so sample counting matches the gate."""
    try:
        from abcxauto.agent_loop import is_new_risk

        return bool(is_new_risk(strategy, params))
    except Exception:
        return False


def card_types_by_name(state: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """``name`` -> the types it branches under. More than one is ambiguous."""
    blob = state if isinstance(state, dict) else load_lab()
    out: dict[str, list[str]] = {}
    for type_name, card in walk_cards(blob):
        key = str(card.get("name") or "").strip().lower()
        if not key:
            continue
        seen = out.setdefault(key, [])
        if type_name and type_name not in seen:
            seen.append(type_name)
    return out


def resolve_card_type(
    card: Any,
    *,
    strategy: str = "",
    state: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Which type a card send belongs to. Returns ``(type, ambiguous)``.

    A new-risk send is unambiguous by construction: the gate already proved the
    card lives under the strategy being sent. Anything else â€” a management
    ticket, or a row written before cards were nested â€” is resolved by name,
    and only when exactly one type claims that name.
    """
    name = str(card or "").strip().lower()
    if not name:
        return "", False
    index = card_types_by_name(state)
    types = index.get(name) or []
    want = str(strategy or "").strip().lower()
    if want and want in types:
        return want, False
    if len(types) == 1:
        return types[0], False
    if len(types) > 1:
        return "", True
    return "", False


def record_card_send(
    *,
    card: str,
    strategy: str,
    symbol: str = "",
    result: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Tie a dispatched ticket to the card that called for it.

    Without this the only feedback a card gets is whole-book drift, which is
    the same number for every card. ``new_risk`` separates the entry that opens
    a trade from the management tickets that follow, so a card's declared sample
    counts trades and not keystrokes. ``type`` pins the row to one branch of the
    tree, because a name alone is no longer an identity.
    """
    name = str(card or "").strip()
    if not name:
        return
    from abcxauto.memory.journal import _order_ids_from_result_json

    try:
        oids = sorted(_order_ids_from_result_json(json.dumps(result or {}, default=str)))
    except Exception:
        oids = []
    try:
        card_type, _ambiguous = resolve_card_type(name, strategy=strategy)
    except Exception:
        card_type = ""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "card": name[:120],
        "type": card_type[:60],
        "strategy": str(strategy or "")[:60],
        "symbol": str(symbol or "").upper()[:12],
        "order_ids": oids[:12],
        "new_risk": _send_is_new_risk(strategy, params),
    }
    path = _card_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        logger.debug("card send log write failed", exc_info=True)


def _card_sends(limit: int = 400) -> list[dict[str, Any]]:
    path = _card_log_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                blob = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(blob, dict) and blob.get("card"):
                rows.append(blob)
    except OSError:
        return []
    return rows


def resolve_send_types(
    sends: list[dict[str, Any]] | None,
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stamp every send row with its ``(type, name)`` identity.

    Rows written before cards were nested carry only a name. One resolves when
    exactly one type claims that name, or when the row's own strategy is a type
    holding it. When two types hold the name the row is marked ``ambiguous`` and
    attributed to neither: crediting a trade to a card that never asked for it
    is worse than leaving it visibly unattributed.
    """
    index = card_types_by_name(state)
    out: list[dict[str, Any]] = []
    for row in sends or []:
        if not isinstance(row, dict) or not row.get("card"):
            continue
        item = dict(row)
        have = str(item.get("type") or "").strip().lower()
        if have:
            item["type"] = have
            item["ambiguous"] = False
            out.append(item)
            continue
        name = str(item.get("card") or "").strip().lower()
        types = index.get(name) or []
        want = str(item.get("strategy") or "").strip().lower()
        if want and want in types:
            item["type"], item["ambiguous"] = want, False
        elif len(types) == 1:
            item["type"], item["ambiguous"] = types[0], False
        else:
            item["type"], item["ambiguous"] = "", len(types) > 1
        out.append(item)
    return out


def _send_oids(row: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for oid in row.get("order_ids") or []:
        try:
            out.append(int(oid))
        except (TypeError, ValueError):
            continue
    return out


def _ts_num(raw: Any) -> float:
    """Epoch seconds from an ISO stamp. Fills say ``Z``, card sends say ``+00:00``."""
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def classify_card_trades(
    sends: list[dict[str, Any]] | None,
    fills: list[dict[str, Any]] | None,
    dispatched: set | None = None,
) -> list[dict[str, Any]]:
    """One row per new-risk card send: how that trade actually ended.

    ``protective`` â€” the card's own stop/target filled.
    ``decision``   â€” a dispatched ticket closed it (Grok's call).
    ``operator``   â€” the exit has no dispatch behind it: a manual TWS flatten,
                     another client session, or the panic/halt path. The card's
                     thesis was interrupted, so it is neither proof nor
                     falsification and never counts toward the declared sample.
    ``open``       â€” nothing has closed it yet.
    """
    rows = [r for r in (sends or []) if isinstance(r, dict) and r.get("card")]
    rows.sort(key=lambda r: _ts_num(r.get("ts")))
    placed = set(dispatched or set())
    # Which send produced each order id, so a fill can be traced to its ticket.
    owner: dict[int, dict[str, Any]] = {}
    for row in rows:
        for oid in _send_oids(row):
            owner.setdefault(oid, row)
    closers = [
        f
        for f in (fills or [])
        if isinstance(f, dict) and f.get("order_id") is not None
    ]
    closers.sort(key=lambda f: _ts_num(f.get("ts")))
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("new_risk") is False:
            continue
        if row.get("new_risk") is None and not _send_is_new_risk(
            str(row.get("strategy") or ""), None
        ):
            continue
        sym = str(row.get("symbol") or "").upper()
        ts = str(row.get("ts") or "")
        opened = _ts_num(ts)
        own = set(_send_oids(row))
        trade: dict[str, Any] = {
            "card": str(row.get("card") or ""),
            "type": str(row.get("type") or ""),
            "ambiguous": bool(row.get("ambiguous")),
            "symbol": sym,
            "strategy": str(row.get("strategy") or ""),
            "opened_at": ts,
            "exit": EXIT_OPEN,
            "realized_pnl": None,
            "exit_order_id": None,
            "exit_at": None,
        }
        for idx, fill in enumerate(closers):
            if idx in used:
                continue
            if sym and str(fill.get("symbol") or "").upper() != sym:
                continue
            if opened and _ts_num(fill.get("ts")) < opened:
                continue
            oid = int(fill["order_id"])
            src = owner.get(oid)
            if oid in own:
                kind = EXIT_PROTECTIVE
            elif src is not None or oid in placed:
                kind = EXIT_DECISION
            else:
                kind = EXIT_OPERATOR
            used.add(idx)
            trade.update(
                exit=kind,
                realized_pnl=round(float(fill.get("realized_pnl") or 0.0), 4),
                exit_order_id=oid,
                exit_at=fill.get("ts"),
            )
            break
        out.append(trade)
    return out


def _journal_exit_facts() -> tuple[list[dict[str, Any]], set]:
    """Closing fills plus the order ids the clerk dispatched. Degrades to empty."""
    fills: list[dict[str, Any]] = []
    placed: set = set()
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        fn = getattr(journal, "closing_fills", None)
        if callable(fn):
            fills = [f for f in (fn() or []) if isinstance(f, dict)]
        fn = getattr(journal, "dispatched_order_ids", None)
        if callable(fn):
            placed = set(fn() or set())
    except Exception:
        logger.debug("journal exit facts unavailable", exc_info=True)
    return fills, placed


def _empty_score(card: str = "", card_type: str = "") -> dict[str, Any]:
    return {
        "card": card,
        "type": card_type,
        "sends": 0,
        "realized_pnl": 0.0,
        "attributed_fills": 0,
        "last_send": None,
        "symbols": [],
        "trades": 0,
        "resolved": 0,
        "interrupted": 0,
        "open": 0,
        "resolved_pnl": 0.0,
        "interrupted_pnl": 0.0,
        "resolved_wins": 0,
        "resolved_losses": 0,
        "ambiguous_sends": 0,
        "exits": {k: 0 for k in CARD_EXIT_KINDS},
    }


def card_scores(cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Per-card attribution: what it sent, what resolved, what interrupted it.

    Buckets are keyed by ``(type, name)``, so the same setup name under two
    order types scores as the two different experiments it is. ``realized_pnl``
    is every dollar the card's own order ids booked â€” the book number has to
    reconcile. ``resolved_pnl`` is the graduation number: only trades whose exit
    was the card's own protection or a dispatched decision.
    """
    raw_sends = _card_sends()
    if not raw_sends:
        return []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for c in cards or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        name = str(c["name"]).strip().lower()
        by_key[card_key(c.get("type") or c.get("ticket"), name)] = c
        by_name.setdefault(name, []).append(c)
    state: dict[str, Any] | None = None
    if cards is not None:
        # Resolve legacy name-only rows against the book we were handed, not
        # whatever happens to be on disk.
        tree: dict[str, Any] = {}
        for c in cards:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            parent = str(c.get("type") or c.get("ticket") or "").strip().lower()
            if parent:
                tree.setdefault(parent, {}).setdefault("cards", []).append(c)
        state = {"types": tree}
    sends = resolve_send_types(raw_sends, state)
    realized: dict[int, float] = {}
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        fn = getattr(journal, "realized_by_order_id", None)
        if callable(fn):
            realized = dict(fn() or {})
    except Exception:
        realized = {}
    fills, placed = _journal_exit_facts()
    trades = classify_card_trades(sends, fills, placed)
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    def _bucket(card_type: Any, name: str) -> dict[str, Any]:
        key = card_key(card_type, name)
        return buckets.setdefault(key, _empty_score(name, key[0]))

    for row in sends:
        name = str(row.get("card") or "")
        bucket = _bucket(row.get("type"), name)
        bucket["sends"] += 1
        if row.get("ambiguous"):
            bucket["ambiguous_sends"] += 1
        bucket["last_send"] = row.get("ts") or bucket["last_send"]
        sym = str(row.get("symbol") or "")
        if sym and sym not in bucket["symbols"]:
            bucket["symbols"].append(sym)
        for oid in _send_oids(row):
            if oid in realized:
                bucket["realized_pnl"] += float(realized[oid])
                bucket["attributed_fills"] += 1
    for trade in trades:
        bucket = _bucket(trade.get("type"), str(trade.get("card") or ""))
        kind = str(trade.get("exit") or EXIT_OPEN)
        bucket["trades"] += 1
        bucket["exits"][kind] = bucket["exits"].get(kind, 0) + 1
        pnl = trade.get("realized_pnl")
        pnl_f = float(pnl) if isinstance(pnl, (int, float)) else 0.0
        if kind in RESOLVED_EXITS:
            bucket["resolved"] += 1
            bucket["resolved_pnl"] += pnl_f
            if pnl_f < 0:
                bucket["resolved_losses"] += 1
            elif pnl_f > 0:
                bucket["resolved_wins"] += 1
        elif kind == EXIT_OPERATOR:
            bucket["interrupted"] += 1
            bucket["interrupted_pnl"] += pnl_f
        else:
            bucket["open"] += 1
    out: list[dict[str, Any]] = []
    for (card_type, name), bucket in buckets.items():
        for key in ("realized_pnl", "resolved_pnl", "interrupted_pnl"):
            bucket[key] = round(float(bucket[key]), 4)
        bucket["symbols"] = bucket["symbols"][:8]
        match = by_key.get((card_type, name))
        if match is None:
            same = by_name.get(name) or []
            match = same[0] if len(same) == 1 else None
        if by_key:
            bucket["on_current_book"] = match is not None
        bucket.update(card_verdict(bucket, match))
        out.append(bucket)
    return sorted(
        out,
        key=lambda b: (-int(b.get("sends") or 0), str(b.get("card")), str(b.get("type"))),
    )


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


def _ensure_card_clocks(
    state: dict[str, Any],
    prev: dict[str, Any],
    now: str,
) -> None:
    """Stamp written_at once. Existing unstamped cards inherit the last wipe."""
    prev_by = {
        card_key(t, c.get("name")): c
        for t, c in walk_cards(prev)
        if isinstance(c, dict) and c.get("name")
    }
    old_fallback = str(
        (prev or {}).get("cleared_at") or (prev or {}).get("written_at") or now
    ).strip()
    for _t, card in walk_cards(state):
        if not isinstance(card, dict) or not card.get("name"):
            continue
        if str(card.get("written_at") or "").strip():
            continue
        old = prev_by.get(card_key(_t, card.get("name")))
        if old and str(old.get("written_at") or "").strip():
            card["written_at"] = str(old["written_at"]).strip()[:48]
        elif old:
            card["written_at"] = old_fallback[:48]
        else:
            card["written_at"] = now[:48]


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


def card_verdict(
    score: dict[str, Any] | None,
    card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Graduated / tripped strictly from the card's own declaration.

    The clerk supplies no thresholds. A card that declared nothing can neither
    graduate nor trip â€” it is flagged as owing a declaration on the next write.
    ``calibration`` rides along as a fact and never moves either verdict: a
    miscalibrated card with positive resolved edge still graduates.
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
    out: dict[str, Any] = {
        "anchored_type": anchored or None,
        "status": status or None,
        "retire_if": dict(retire) or None,
        "declared_sample": sample or None,
        "needs_retire_if": bool(row) and not retire and not locked,
        "needs_thesis": bool(row) and not thesis and not locked,
        "sample_left": max(0, sample - resolved) if sample else None,
        "calibration": card_calibration(sc, row),
        "graduated": False,
        "tripped": False,
        "trip_reason": "",
        "locked": locked,
    }
    out.update(card_waiting(sc, row))
    if status == "retired":
        return out
    max_loss = retire.get("max_loss_usd")
    max_losses = retire.get("max_losses")
    if sample and resolved >= sample:
        if resolved_pnl > 0:
            # A card with no written thesis has nothing for live to follow.
            out["graduated"] = bool(thesis)
        else:
            out["tripped"] = True
            out["trip_reason"] = (
                f"declared sample {sample} reached with resolved edge "
                f"{resolved_pnl:+.2f} — retire or rewrite"
            )
            return out
    if isinstance(max_loss, (int, float)) and max_loss > 0 and resolved_pnl <= -float(max_loss):
        out["tripped"] = True
        out["graduated"] = False
        out["trip_reason"] = (
            f"resolved edge {resolved_pnl:+.2f} hit declared max_loss_usd {max_loss}"
        )
        return out
    if isinstance(max_losses, int) and max_losses > 0 and losses >= max_losses:
        out["tripped"] = True
        out["graduated"] = False
        out["trip_reason"] = (
            f"{losses} losing resolved trades at declared max_losses {max_losses}"
        )
    return out


def card_facts(book: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every ``(type, card)`` on the tree with its attribution, sent or not."""
    state = book if isinstance(book, dict) else load_lab()
    pairs = walk_cards(state)
    flat = _flat_card_projection(state)
    scored = {card_key(r.get("type"), r.get("card")): r for r in card_scores(flat)}
    unresolved = {
        str(r.get("card") or "").lower(): r
        for r in scored.values()
        if not r.get("type") and int(r.get("ambiguous_sends") or 0) > 0
    }
    out: list[dict[str, Any]] = []
    for type_name, card in pairs:
        row = dict(scored.get(card_key(type_name, card.get("name"))) or {})
        if not row:
            row = _empty_score(str(card.get("name") or ""), type_name)
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
    return out


def graduated_card_names(book: dict[str, Any] | None = None) -> list[str]:
    """Cards that met their own declared sample with positive resolved edge."""
    state = book if isinstance(book, dict) else load_lab()
    declared = state.get("graduated")
    if isinstance(declared, list) and declared:
        return [str(x) for x in declared if str(x).strip()]
    return [
        str(row.get("card"))
        for row in card_facts(state)
        if row.get("graduated")
    ]


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
    them. Live new risk still needs ``live_new_risk_allowed`` (promoted book).
    ``type`` is accepted for callers that pass the send strategy; it is not
    a trunk match.
    """
    _ = type
    paper = is_paper()
    state = book if isinstance(book, dict) else (load_lab() if paper else load_live())
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
    return ""


def strategy_scores() -> list[dict[str, Any]]:
    """Realized P&L per sendable strategy, from the journal's fills join."""
    try:
        from abcxauto.memory import get_journal

        rows = get_journal().strategy_performance() or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "strategy": row.get("strategy"),
                "n_fills": row.get("n_fills"),
                "realized_pnl": round(float(row.get("realized_pnl_sum") or 0.0), 4),
                "commissions": round(float(row.get("commissions_sum") or 0.0), 4),
                "last_fill_ts": row.get("last_fill_ts"),
            }
        )
    return sorted(out, key=lambda r: float(r.get("realized_pnl") or 0.0), reverse=True)


def _score_snap(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else {}
    return {
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "model_cost_usd": sc.get("model_cost_usd"),
    }


def _outcome_card(card: dict[str, Any] | None) -> dict[str, Any]:
    """Ledger row is the score of a card, not the notes."""
    out = dict(card) if isinstance(card, dict) else {}
    out.pop("instructions", None)
    out.pop("types", None)
    out.pop("cards", None)
    for key in _DEAD_LAB_KEYS:
        out.pop(key, None)
    return out


def _ledger_card(state: dict[str, Any], scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else (
        state.get("paper_score") if isinstance(state.get("paper_score"), dict) else {}
    )
    lots = [str(x) for x in (state.get("lots_at_write") or [])][:16]
    return {
        "revision": state.get("revision"),
        "written_at": state.get("written_at"),
        "mode": state.get("mode"),
        "ready_to_promote": bool(state.get("ready_to_promote")),
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "lots_at_write": lots,
    }


def _compact_card(card: dict[str, Any] | None) -> dict[str, Any]:
    c = card if isinstance(card, dict) else {}
    return {
        "revision": c.get("revision"),
        "written_at": c.get("written_at"),
        "mode": c.get("mode"),
        "ready_to_promote": c.get("ready_to_promote"),
        "beating_model": c.get("beating_model"),
        "edge_usd": c.get("edge_usd"),
        "book_return_pct": c.get("book_return_pct"),
        "closed_edge": c.get("closed_edge"),
        "closed_beating": c.get("closed_beating"),
        "closed_at": c.get("closed_at"),
    }


def _close_card(
    card: dict[str, Any],
    scorecard: dict[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    sc = _score_snap(scorecard)
    out = _outcome_card(card)
    out["closed_at"] = now
    out["closed_edge"] = sc.get("edge_usd")
    out["closed_beating"] = sc.get("beating_model")
    out["closed_return_pct"] = sc.get("book_return_pct")
    return out


def ensure_ledger(lab: dict[str, Any] | None) -> list[dict[str, Any]]:
    """In-memory ledger. Seed from the current blob if the file is still flat."""
    state = lab if isinstance(lab, dict) else {}
    rows = [_outcome_card(r) for r in (state.get("ledger") or []) if isinstance(r, dict)]
    if rows:
        return rows[-_LEDGER_CAP:]
    if state.get("instructions") or state.get("revision") or state.get("types"):
        return [_ledger_card(state, state.get("paper_score"))]
    return []


def revision_card(revision: int, lab: dict[str, Any] | None = None) -> dict[str, Any] | None:
    state = lab if isinstance(lab, dict) else load_lab()
    want = int(revision)
    for row in reversed(ensure_ledger(state)):
        try:
            if int(row.get("revision") or 0) == want:
                return _outcome_card(row)
        except (TypeError, ValueError):
            continue
    try:
        if int(state.get("revision") or 0) == want:
            return _ledger_card(state, state.get("paper_score"))
    except (TypeError, ValueError):
        return None
    return None


def _norm_book_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _card_book_tuple(card: dict[str, Any]) -> tuple[Any, ...]:
    retire = card.get("retire_if") if isinstance(card.get("retire_if"), dict) else {}
    return (
        _norm_book_text(card.get("name")).lower(),
        _norm_book_text(card.get("thesis")),
        _norm_book_text(card.get("when_on")),
        _norm_book_text(card.get("scan")),
        _norm_book_text(card.get("shape")),
        _norm_book_text(card.get("invalidation")),
        _norm_book_text(card.get("status")).lower(),
        card.get("locked") is True,
        json.dumps(retire, sort_keys=True, default=str),
        _norm_book_text(card.get("expect_hit_rate")),
    )


def book_fingerprint(state: dict[str, Any] | None) -> tuple[Any, ...]:
    """Durable book only. Notes, evidence, and next_look_s are look diary."""
    blob = state if isinstance(state, dict) else {}
    types = _clean_types(blob.get("types"))
    trunks: list[tuple[Any, ...]] = []
    for type_name in sorted(types):
        stanza = types[type_name] if isinstance(types.get(type_name), dict) else {}
        cards = tuple(
            _card_book_tuple(c)
            for c in (stanza.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        )
        order = tuple(
            _norm_book_text(x).lower()
            for x in (stanza.get("tool_order") or [])
            if str(x or "").strip()
        )
        trunks.append(
            (
                type_name,
                order,
                _norm_book_text(stanza.get("gotchas")),
                _norm_book_text(stanza.get("review")),
                cards,
            )
        )
    return (
        str(blob.get("mode") or "explore").strip().lower(),
        bool(blob.get("ready_to_promote")),
        tuple(trunks),
    )


def _cadence_tuple(book: dict[str, Any] | None) -> tuple[tuple[Any, ...], ...]:
    rows: list[tuple[Any, ...]] = []
    for type_name, card in walk_cards(book if isinstance(book, dict) else {}):
        rows.append((type_name, card.get("name"), card.get("next_look_s")))
    return tuple(rows)


def _held_book(
    prev: dict[str, Any],
    staged: dict[str, Any],
    update: dict[str, Any],
) -> dict[str, Any]:
    """Same fingerprint: keep the last real book. Overlay clerk-only fields.

    Cadence may move. Sanitized instructions (invented gates stripped) must
    land. Diary notes and evidence do not — those were minting fake progress.
    """
    out = _strip_projection(copy.deepcopy(prev))
    dest_types = out.get("types") if isinstance(out.get("types"), dict) else {}
    src_types = staged.get("types") if isinstance(staged.get("types"), dict) else {}
    for tname, stanza in src_types.items():
        dst = dest_types.get(tname)
        if not isinstance(dst, dict) or not isinstance(stanza, dict):
            continue
        by_name = {
            c.get("name"): c
            for c in (stanza.get("cards") or [])
            if isinstance(c, dict) and c.get("name")
        }
        for card in dst.get("cards") or []:
            if not isinstance(card, dict):
                continue
            src = by_name.get(card.get("name"))
            if isinstance(src, dict) and "next_look_s" in src:
                card["next_look_s"] = src["next_look_s"]
    return out


def save_lab(
    update: dict[str, Any],
    *,
    scorecard: dict[str, Any] | None = None,
    persist_instructions: bool = False,
) -> dict[str, Any]:
    prev = load_lab()
    now = datetime.now(timezone.utc).isoformat()
    lots_at = update.get("lots_at_write")
    if not lots_at:
        try:
            from abcxauto.think_stream import LAST_TURN_PATH, _read_json

            lots_at = list((_read_json(LAST_TURN_PATH) or {}).get("open_lots") or [])
        except Exception:
            lots_at = list(prev.get("lots_at_write") or [])
    staged = _strip_projection(
        _seed_open_type_starters(
            _migrate_book(
                {
                    **_strip_projection(prev),
                    **update,
                }
            )
        )
    )
    prev_rev = int(prev.get("revision") or 0)
    hold = prev_rev > 0 and book_fingerprint(prev) == book_fingerprint(staged)
    if hold:
        out = _held_book(prev, staged, update)
        if persist_instructions and "instructions" in update:
            out["instructions"] = update.get("instructions") or ""
        dirty = (
            (out.get("instructions") or "") != (prev.get("instructions") or "")
            or _cadence_tuple(out) != _cadence_tuple(prev)
        )
        if dirty:
            disk = dict(out)
            disk.pop("revision_held", None)
            _write(_lab_path(), disk)
        out["revision_held"] = True
        return out
    ledger = ensure_ledger(prev)
    rev = prev_rev + 1
    if ledger and scorecard:
        ledger[-1] = _close_card(ledger[-1], scorecard, now)
    # A caller may still hand us the flat shape, so file ``update``'s cards into
    # the tree. ``prev``'s derived list is dropped first so a projection replay
    # does not double-file. Named writes merge; they do not wipe siblings.
    state = _strip_projection(
        _seed_open_type_starters(
            _migrate_book(
                {
                    **_strip_projection(prev),
                    **update,
                    "revision": rev,
                    "written_at": now,
                    "promoted": False,
                    "lots_at_write": [str(x) for x in (lots_at or [])][:32],
                }
            )
        )
    )
    _ensure_card_clocks(state, prev, now)
    if scorecard:
        state["paper_score"] = _score_snap(scorecard)
    ledger.append(_ledger_card(state, state.get("paper_score")))
    state["ledger"] = ledger[-_LEDGER_CAP:]
    state.pop("revision_held", None)
    _write(_lab_path(), state)
    return state


def maybe_promote(*, scorecard: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Promote graduated cards, not the book.

    A card graduates on its own declared sample with positive resolved edge.
    Only those cards reach the live snapshot, and they travel inside their own
    pruned type stanza â€” one lucky ad-hoc trade no longer unlocks live for
    everything, and live never sees a hypothesis that has not earned it.
    """
    lab = load_lab()
    if not lab.get("ready_to_promote"):
        return None
    facts = card_facts(lab)
    grads = [row for row in facts if row.get("graduated")]
    if not grads:
        return None
    keys = {card_key(row.get("type"), row.get("card")) for row in grads}
    # A verdict that named no type still promotes, but only when one card on the
    # tree answers to that name.
    index = card_types_by_name(lab)
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
    """explore = widen the search, keep size small. exploit = trade the winners."""
    mode = str((load_lab() or {}).get("mode") or "explore").strip().lower()
    return mode if mode in ("explore", "exploit") else "explore"


def playbook_next_look_s() -> float | None:
    """Smallest next_look_s on a live (non-retired) card, if any."""
    try:
        from abcxauto.park_clock import clamp_next_look_s
    except Exception:
        return None
    found: list[float] = []
    try:
        lab = load_lab()
    except Exception:
        return None
    for _typ, card in walk_cards(lab):
        if str(card.get("status") or "").strip().lower() == "retired":
            continue
        clamped = clamp_next_look_s(card.get("next_look_s"))
        if clamped is not None:
            found.append(clamped)
    if not found:
        return None
    return min(found)


def live_has_promoted() -> bool:
    """A promoted snapshot only counts if a graduated card is actually in it."""
    live = load_live()
    if not live.get("promoted") or not _has_book(live):
        return False
    return bool(graduated_card_names(live))


def live_new_risk_allowed() -> bool:
    """Paper may take new risk. Live needs at least one graduated card."""
    if is_paper():
        return True
    return live_has_promoted()


def _reject_note(rejected: dict[str, str]) -> str:
    if "unknown_type" in rejected:
        return rejected["unknown_type"]
    if "invented_pct_gate" in rejected and set(rejected) <= {"invented_pct_gate"}:
        return "notebook cannot invent a % gate"
    if "ticker_list" in rejected:
        return rejected["ticker_list"]
    if "diary" in rejected:
        return rejected["diary"]
    if "shape" in rejected:
        return rejected["shape"]
    if "invented_pct_gate" in rejected:
        return "notebook cannot invent a % gate"
    return "notebook cannot loosen gates"


def apply_from_judgment(judgment: dict[str, Any] | None) -> dict[str, Any] | None:
    """Paper: persist Grok's notebook. Live: ignore writes.

    Gate knobs in the payload are rejected (not applied). Notes still save.
    Diary / ticker-list / unknown-type writes do not save.
    """
    if not judgment or not is_paper():
        return None
    raw = judgment.get("lab_playbook") or judgment.get("playbook")
    rejected = dict(gate_rejects(raw))
    rejected.update(book_shape_rejects(raw))
    if _HARD_SHAPE.intersection(rejected):
        return {
            "status": "rejected",
            "rejected": rejected,
            "note": _reject_note(rejected),
        }
    update = clamp_update(raw)
    if not update:
        if rejected:
            note = "notebook cannot loosen gates"
            if "invented_pct_gate" in rejected:
                note = "notebook cannot invent a % gate"
            return {
                "status": "rejected",
                "rejected": rejected,
                "note": _reject_note(rejected),
            }
        return None
    score = None
    try:
        from abcxauto.scorecard import compute_scorecard

        score = compute_scorecard()
    except Exception:
        score = None
    state = save_lab(
        update,
        scorecard=score,
        persist_instructions="invented_pct_gate" in rejected,
    )
    maybe_promote(scorecard=score)
    out = dict(state)
    facts = card_facts(state)
    if state.get("revision_held"):
        out["revision_held"] = True
        wait_row = next(
            (r for r in facts if r.get("looks_without_trigger") is not None),
            None,
        )
        looks = (wait_row or {}).get("looks_without_trigger")
        note = "book unchanged — revision held"
        if isinstance(looks, int):
            note += f"; {looks} looks since last send or write"
            out["looks_without_trigger"] = looks
        out.setdefault("note", note)
    out["cards"] = _flat_card_projection(state)
    out["graduated_cards"] = [_card_label(r) for r in facts if r.get("graduated")]
    out["tripped_cards"] = [_card_label(r) for r in facts if r.get("tripped")]
    out["needs_declaration"] = [
        _card_label(r)
        for r in facts
        if r.get("needs_retire_if") or r.get("needs_thesis")
    ]
    if rejected:
        out["rejected"] = rejected
        if "invented_pct_gate" in rejected:
            out["note"] = "notes saved; invented % gate lines stripped"
        else:
            out["note"] = "notes saved; gate knobs ignored"
    return out


def playbook_is_stale(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a standing card is older than the lab rewrite cadence."""
    state = lab if isinstance(lab, dict) else load_lab()
    if not _has_book(state):
        return False
    age = playbook_age_hours(state, now=now)
    return age is not None and age >= stale_hours()


def _window_edges(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    wins = (scorecard or {}).get("windows") if isinstance(scorecard, dict) else None
    if not isinstance(wins, dict):
        wins = {}
    out: dict[str, Any] = {}
    for label in _CARD_WINDOWS:
        row = wins.get(label) if isinstance(wins.get(label), dict) else {}
        out[f"win_{label}"] = row.get("edge_usd")
        out[f"win_{label}_beat"] = row.get("beating_model")
    return out


def _since_write_score(
    lab: dict[str, Any],
    scorecard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Book vs model since this card was written. Inception hole stays on the scorecard."""
    written = str(lab.get("written_at") or "")
    now_nl = (scorecard or {}).get("net_liquidation") if isinstance(scorecard, dict) else None
    empty = {"since_write_edge": None, "since_write_pnl": None, "since_write_cost": None}
    if not written:
        return empty
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
    except Exception:
        return empty
    start_nl, _start_ts = None, None
    try:
        if hasattr(journal, "nav_at_or_before"):
            start_nl, _start_ts = journal.nav_at_or_before(written)
    except Exception:
        start_nl = None
    usage = {}
    try:
        if hasattr(journal, "model_usage_since"):
            usage = dict(journal.model_usage_since(written) or {})
    except Exception:
        usage = {}
    cost = float(usage.get("cost_usd") or 0.0)
    try:
        now_f = float(now_nl) if now_nl is not None else None
        start_f = float(start_nl) if start_nl is not None else None
    except (TypeError, ValueError):
        return {**empty, "since_write_cost": cost}
    if now_f is None or start_f is None or start_f <= 0:
        return {**empty, "since_write_cost": cost}
    pnl = now_f - start_f
    return {
        "since_write_edge": pnl - cost,
        "since_write_pnl": pnl,
        "since_write_cost": cost,
    }


def _parse_card_clock(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hypothesis_clock(lab: dict[str, Any] | None) -> str:
    """Newest testing card clock. Lab written_at is a diary stamp after holds.

    First-card-only made a 5-day flush sibling keep ``book_stale`` on after a
    same-day card was written and sent — Grok then wrote the book every look.
    """
    state = lab if isinstance(lab, dict) else {}
    best = ""
    best_dt: datetime | None = None
    for _type_name, card in walk_cards(state):
        if not _is_live_hypothesis(card):
            continue
        clock = str(card.get("written_at") or "").strip()
        dt = _parse_card_clock(clock)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best = clock
    return best or str(state.get("written_at") or "").strip()


def playbook_age_hours(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> float | None:
    raw = _hypothesis_clock(lab)
    if not raw:
        return None
    try:
        written = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return max(0.0, (clock - written).total_seconds() / 3600.0)


_HUNT_PREFIX = frozenset({
    "book",
    "status",
    "playbook",
    "write_lab_playbook",
    "self_tune",
    "set_wake",
})
_DEFAULT_HUNT_ORDER = ("scan", "news", "quote", "candles", "send")
_DEFAULT_MANAGE_ORDER = ("book", "fills", "quote", "candles")


def _tool_names(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= 16:
            break
    return out


def _next_in_order(order: list[str], done: list[str]) -> str:
    seen = {str(name) for name in done}
    for name in order:
        if name not in seen:
            return name
    return order[-1] if order else "book"


def _effective_tool_trace(
    this_look: list[str] | None,
    last_look: list[str] | None,
    *,
    managing: bool,
) -> list[str]:
    """This look's tools, or last look's hunt if this look has not started one.

    A completed send starts a new hunt when the book is flat. An open book
    uses the manage order and does not inherit last look's scan loop.
    """
    this = _tool_names(this_look)
    last = _tool_names(last_look)
    if any(name not in _HUNT_PREFIX for name in this):
        return this
    if managing or "send" in last:
        return this
    return last + this


def _screen_quoted(raw: Any) -> bool:
    """True when a scan already stamped IBKR last/bid/ask on its rows."""
    if raw is True:
        return True
    if isinstance(raw, (int, float)) and raw > 0:
        return True
    if not isinstance(raw, dict):
        return False
    try:
        if int(raw.get("quoted") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    hits = raw.get("scan_hits") if isinstance(raw.get("scan_hits"), dict) else raw
    if not isinstance(hits, dict):
        return False
    try:
        if int(hits.get("quoted") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    for row in hits.get("rows") or []:
        if not isinstance(row, dict):
            continue
        if row.get("last") is not None or row.get("ibkr") or row.get("bid") is not None:
            return True
    return False


def _scan_carries_news(raw: Any) -> bool:
    """True when a scan already nested MDA headlines on the same hits."""
    if raw is True:
        return True
    if isinstance(raw, list):
        return any(
            isinstance(item, dict)
            and (item.get("headline") or item.get("title") or item.get("summary"))
            for item in raw
        )
    if not isinstance(raw, dict):
        return False
    items = raw.get("news")
    if isinstance(items, list) and items:
        return True
    for row in list(raw.get("hits") or []) + list(raw.get("rows") or []):
        if not isinstance(row, dict):
            continue
        if row.get("news"):
            return True
        mda = row.get("mda")
        if isinstance(mda, dict) and mda.get("news"):
            return True
    return False


_CARD_RISK_RE = re.compile(r"(?i)(?:dollar\s+)?risk\s*[≤<=]{1,2}\s*([\d.]+)\s*%")
_CARD_NOTIONAL_RE = re.compile(r"(?i)notional\s*[≤<=]{1,2}\s*([\d.]+)\s*%")
_CARD_MIN_GAP_RE = re.compile(r"(?:>=|≥)\s*([\d.]+)\s*%")
_CARD_MIN_PRICE_RE = re.compile(r"(?i)sub-?\s*\$?\s*([\d.]+)")
_CARD_TIGHT_SPREAD_RE = re.compile(r"(?i)tight.{0,24}spread|spread.{0,24}tight")
_CARD_REENTRY_RE = re.compile(r"(?i)do not re-enter|same session")
_CARD_NO_ADD_RE = re.compile(r"(?i)\bno add\b")
_CARD_ONE_NAME_RE = re.compile(r"(?i)\bone name\b")
_CARD_SKIP_SPY_RE = re.compile(
    r"(?i)\bSPY\b.{0,48}(?:same session|scrape)|(?:skip|no|not|never|do not).{0,24}\bSPY\b"
)
_CARD_SESSION_RE = re.compile(
    r"(?i)opening low|opening high|session low|session high|gap retrace"
)
_CARD_HOLD_OPEN_RE = re.compile(
    r"(?i)hold(?:s|ing)? above(?: the)? open(?!ing)"
)


def _send_facts_from_row(type_name: str, card: dict[str, Any]) -> dict[str, Any]:
    shape = str(card.get("shape") or "")
    upper = shape.upper()
    direction = (
        "LONG" if "LONG" in upper else ("SHORT" if "SHORT" in upper else None)
    )
    risk_m = _CARD_RISK_RE.search(shape)
    notional_m = _CARD_NOTIONAL_RE.search(shape)
    try:
        risk_pct = float(risk_m.group(1)) if risk_m else None
    except (TypeError, ValueError):
        risk_pct = None
    try:
        notional_pct = float(notional_m.group(1)) if notional_m else None
    except (TypeError, ValueError):
        notional_pct = None
    return {
        "type": type_name,
        "card": card.get("name"),
        "direction": direction,
        "risk_pct": risk_pct,
        "notional_pct": notional_pct,
    }


def live_card_send_facts(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> dict[str, Any]:
    """Type, card, direction, and size caps from a named card or the first live one."""
    named = str(card or "").strip().lower()
    for type_name, row in _walk_testing(book):
        if named and str(row.get("name") or "").strip().lower() != named:
            continue
        return _send_facts_from_row(type_name, row)
    return {}


def live_card_gap_floors(
    book: dict[str, Any] | None = None,
    *,
    deepest: float | None = None,
) -> list[dict[str, Any]]:
    """Each testing card's |gap| floor, and whether ``deepest`` clears it."""
    out: list[dict[str, Any]] = []
    for _type_name, row in _walk_testing(book):
        gap = _card_min_gap_pct(row)
        if gap is None:
            continue
        item: dict[str, Any] = {"card": row.get("name"), "min_gap_pct": gap}
        if deepest is not None:
            item["met"] = deepest + 1e-9 >= gap
        out.append(item)
    return out


def _is_live_hypothesis(card: Any) -> bool:
    """Grok's testing card. Virgin locked starters are catalog, not a hunt."""
    if not isinstance(card, dict) or not card.get("name"):
        return False
    if str(card.get("status") or "testing").strip().lower() == "retired":
        return False
    if card.get("locked") is True:
        return False
    return True


def _testing_card(
    book: dict[str, Any] | None,
    card_name: Any = None,
) -> dict[str, Any] | None:
    """Named testing card, or the first live card when no name is given."""
    state = book if isinstance(book, dict) else load_lab()
    want = str(card_name or "").strip().lower()
    first: dict[str, Any] | None = None
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        if first is None:
            first = card
        if want and str(card.get("name") or "").strip().lower() == want:
            return card
    return None if want else first


def _card_min_gap_pct(card: dict[str, Any] | None) -> float | None:
    row = card if isinstance(card, dict) else {}
    match = _CARD_MIN_GAP_RE.search(str(row.get("when_on") or ""))
    if not match:
        return None
    try:
        val = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _walk_testing(
    book: dict[str, Any] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    state = book if isinstance(book, dict) else load_lab()
    out: list[tuple[str, dict[str, Any]]] = []
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        out.append((type_name, card))
    return out


def _tightest_matching_card(
    book: dict[str, Any] | None,
    mag: float | None,
    *,
    card_name: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """Named card if its floor is met, else the tightest testing card the gap clears."""
    want = str(card_name or "").strip().lower()
    hits: list[tuple[float, str, dict[str, Any]]] = []
    for type_name, card in _walk_testing(book):
        if want and str(card.get("name") or "").strip().lower() != want:
            continue
        floor = _card_min_gap_pct(card)
        if floor is not None and (mag is None or mag + 1e-9 < floor):
            continue
        hits.append((floor or 0.0, type_name, card))
    if not hits:
        return None
    hits.sort(key=lambda row: row[0], reverse=True)
    return hits[0][1], hits[0][2]


def live_card_min_gap_pct(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> float | None:
    """|gap| floor on a named card, else the loosest floor among testing cards.

    Scan paint uses the loosest floor so a 3% sibling can fire while a 6%
    card stays on the book. Send uses ``card=`` so the ticket's own floor binds.
    """
    state = book if isinstance(book, dict) else load_lab()
    named = str(card or "").strip()
    if named:
        return _card_min_gap_pct(_testing_card(state, named))
    floors: list[float] = []
    for type_name, row in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(row):
            continue
        gap = _card_min_gap_pct(row)
        if gap is not None:
            floors.append(gap)
    return min(floors) if floors else None


def _session_gap_mag(session: Any) -> float | None:
    if not isinstance(session, dict):
        return None
    raw = session.get("gap_pct", session.get("open_gap_pct"))
    if raw is None:
        return None
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return None


def live_card_needs_session(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    """True when the card's stop is the opening low / gap retrace."""
    row = _testing_card(book, card)
    if not row:
        return False
    text = " ".join(
        str(row.get(key) or "")
        for key in ("shape", "when_on", "invalidation", "thesis")
    )
    return bool(_CARD_SESSION_RE.search(text))


def live_card_needs_hold_above_open(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    """True when the card wants last above the RTH open, not only the low."""
    row = _testing_card(book, card)
    if row is not None:
        text = " ".join(
            str(row.get(key) or "")
            for key in ("when_on", "shape", "thesis")
        )
        return bool(_CARD_HOLD_OPEN_RE.search(text))
    return bool(_CARD_HOLD_OPEN_RE.search(_live_card_prose(book, ("when_on", "shape", "thesis"))))


def live_card_session_error(
    params: dict[str, Any] | None,
    session: Any = None,
    book: dict[str, Any] | None = None,
) -> str:
    """No-op. Card prose is a notebook, not a hold / gap / candles send gate."""
    return ""


def session_card_open_print_error(
    params: dict[str, Any] | None,
    session: Any = None,
    book: dict[str, Any] | None = None,
    *,
    market_session: str = "",
) -> str:
    """Refuse session-card new risk until today's RTH open print exists.

    Tape fact, not card prose. Hold / gap / candles / sitting on the low
    still cannot invent a refuse. ``live_card_session_error`` stays a no-op.
    """
    p = params if isinstance(params, dict) else {}
    try:
        if not live_card_needs_session(book, card=p.get("card")):
            return ""
    except Exception:
        return ""
    try:
        from abcxauto.structure_grade import session_usable
    except Exception:
        def session_usable(s: Any) -> bool:
            return isinstance(s, dict) and s.get("today") is True
    if session_usable(session):
        return ""
    sess = str(market_session or "").lower()
    prior_day = isinstance(session, dict) and session.get("today") is False
    if sess == "regular" and not prior_day:
        return ""
    return "session card needs today's opening print"


def _live_card_prose(
    book: dict[str, Any] | None,
    keys: tuple[str, ...],
    type_keys: tuple[str, ...] = (),
) -> str:
    state = book if isinstance(book, dict) else load_lab()
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        bits = [str(card.get(key) or "") for key in keys]
        stanza = types.get(type_name) if isinstance(types.get(type_name), dict) else {}
        bits.extend(str(stanza.get(key) or "") for key in type_keys)
        return " ".join(bits)
    return ""


def live_card_needs_tight_spread(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    row = _testing_card(book, card)
    if row is not None:
        text = " ".join(
            str(row.get(key) or "")
            for key in ("when_on", "thesis", "shape")
        )
        return bool(_CARD_TIGHT_SPREAD_RE.search(text))
    return bool(
        _CARD_TIGHT_SPREAD_RE.search(
            _live_card_prose(book, ("when_on", "thesis", "shape"))
        )
    )


def live_card_skips_spy(book: dict[str, Any] | None = None) -> bool:
    text = " ".join(
        [
            _live_card_prose(
                book,
                ("when_on", "shape", "invalidation", "thesis"),
                ("gotchas", "review"),
            ),
            notebook_text(book if isinstance(book, dict) else load_lab())[:800],
        ]
    )
    return bool(_CARD_SKIP_SPY_RE.search(text))


def _tape_symbols(quoted: Any) -> set[str]:
    names: set[str] = set()
    blobs: list[dict[str, Any]] = []
    if isinstance(quoted, dict):
        hits = quoted.get("scan_hits")
        if isinstance(hits, dict):
            blobs.append(hits)
        blobs.append(quoted)
    for blob in blobs:
        for row in blob.get("rows") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("symbol") or "").upper().strip()
            if name:
                names.add(name)
        for raw in blob.get("symbols") or []:
            name = str(raw or "").upper().strip()
            if name:
                names.add(name)
    return names


def _has_tape_blob(quoted: Any) -> bool:
    if not isinstance(quoted, dict):
        return False
    if "scan_hits" in quoted or "rows" in quoted or quoted.get("quoted") is not None:
        return True
    return False


def _explicit_empty_tape(quoted: Any) -> bool:
    """True only when a screen ran and produced no names. Missing tape is not that."""
    if not isinstance(quoted, dict):
        return False
    hits = quoted.get("scan_hits") if isinstance(quoted.get("scan_hits"), dict) else None
    blob = hits if hits is not None else quoted
    if "rows" not in blob and blob.get("quoted") is None:
        return False
    return _tape_empty(quoted)


def _tape_empty(quoted: Any) -> bool:
    if quoted is True:
        return False
    if isinstance(quoted, (int, float)) and quoted > 0:
        return False
    if not isinstance(quoted, dict):
        return True
    try:
        if int(quoted.get("quoted") or 0) > 0:
            return False
    except (TypeError, ValueError):
        pass
    hits = quoted.get("scan_hits") if isinstance(quoted.get("scan_hits"), dict) else {}
    try:
        if int(hits.get("quoted") or 0) > 0:
            return False
    except (TypeError, ValueError):
        pass
    return not _tape_symbols(quoted)


def live_card_needs_no_reentry(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> bool:
    row = _testing_card(book, card)
    text = " ".join(
        [
            " ".join(
                str((row or {}).get(key) or "")
                for key in ("invalidation", "shape", "when_on", "review")
            ),
            _live_card_prose(
                book,
                ("review", "invalidation", "shape", "when_on"),
                ("review",),
            ),
        ]
    )
    return bool(_CARD_REENTRY_RE.search(text))


def _et_day_of(ts: str) -> str:
    raw = str(ts or "").strip()
    if not raw:
        return ""
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return raw[:10] if len(raw) >= 10 else ""


def card_sent_symbol_today(card: str, symbol: str) -> bool:
    """True when this card already opened new risk in ``symbol`` today (ET)."""
    name = str(card or "").strip().lower()
    want = str(symbol or "").upper().strip()
    if not want:
        return False
    try:
        from abcxauto.opportunity_scan import _et_calendar_day

        today = _et_calendar_day()
    except Exception:
        today = ""
    if not today:
        return False
    for row in _card_sends():
        if not row.get("new_risk"):
            continue
        if str(row.get("symbol") or "").upper() != want:
            continue
        if name and str(row.get("card") or "").strip().lower() != name:
            continue
        if _et_day_of(str(row.get("ts") or "")) == today:
            return True
    return False


def _scan_hit_row(snap: dict[str, Any] | None, symbol: str) -> dict[str, Any]:
    if not isinstance(snap, dict) or not symbol:
        return {}
    want = str(symbol).upper()
    blobs: list[dict[str, Any]] = []
    hits = snap.get("scan_hits") if isinstance(snap.get("scan_hits"), dict) else None
    if hits:
        blobs.append(hits)
    blobs.append(snap)
    for blob in blobs:
        for row in blob.get("rows") or []:
            if isinstance(row, dict) and str(row.get("symbol") or "").upper() == want:
                return row
    return {}


def _positive_px(raw: Any) -> float | None:
    if isinstance(raw, dict):
        return _positive_px(raw.get("last")) or _positive_px(raw.get("mid"))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _row_ibkr_last(row: dict[str, Any] | None) -> float | None:
    if not isinstance(row, dict):
        return None
    ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else {}
    return _positive_px(ibkr.get("last")) or _positive_px(row.get("last"))


def ibkr_live_last(
    symbol: str,
    *,
    snap: dict[str, Any] | None = None,
    quoted: Any = None,
) -> float | None:
    """IBKR last for a name: quote map first, then this look's scan print."""
    want = str(symbol or "").upper()
    if not want:
        return None
    for blob in (snap, quoted):
        if not isinstance(blob, dict):
            continue
        qmap = blob.get("ibkr_live_quotes")
        if isinstance(qmap, dict):
            px = _positive_px(qmap.get(want))
            if px:
                return px
        px = _row_ibkr_last(_scan_hit_row(blob, want))
        if px:
            return px
    return None


def _open_stk_symbols(positions: Any) -> list[str]:
    out: list[str] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        sec = str(pos.get("sec_type") or pos.get("secType") or "STK").upper()
        if sec not in ("STK", "ETF"):
            continue
        try:
            qty = float(pos.get("quantity") or pos.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue
        name = str(pos.get("symbol") or "").upper().strip()
        if name and name not in out:
            out.append(name)
    return out


def live_card_book_error(
    params: dict[str, Any] | None,
    positions: Any = None,
    book: dict[str, Any] | None = None,
) -> str:
    """No-op. Card prose is a notebook, not a no-add / one-name send gate."""
    return ""


def _spread_width(session: Any, snap: dict[str, Any] | None, symbol: str) -> float | None:
    sources = []
    if isinstance(session, dict):
        sources.append(session)
    row = _scan_hit_row(snap, symbol)
    if row:
        sources.append(row)
        ibkr = row.get("ibkr")
        if isinstance(ibkr, dict):
            sources.append(ibkr)
    for src in sources:
        try:
            bid = float(src.get("bid"))
            ask = float(src.get("ask"))
        except (TypeError, ValueError):
            continue
        if bid > 0 and ask > bid:
            return ask - bid
    return None


def live_card_tape_error(
    params: dict[str, Any] | None,
    session: Any = None,
    snap: dict[str, Any] | None = None,
    book: dict[str, Any] | None = None,
) -> str:
    """No-op. Card prose is a notebook, not a price / spread / reentry send gate."""
    return ""


_CAP_SCAN_WORDS = {
    "mega": "mega_cap",
    "mega_cap": "mega_cap",
    "large": "large_cap",
    "large_cap": "large_cap",
    "mid": "mid_cap",
    "mid_cap": "mid_cap",
}


def _live_card_scan_line(book: dict[str, Any] | None = None) -> str:
    state = book if isinstance(book, dict) else load_lab()
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        return str(card.get("scan") or "")
    return ""


def live_card_scan_arenas(book: dict[str, Any] | None = None) -> list[str]:
    """Known arena names written on the first live card's scan line."""
    try:
        from abcxauto.universe import ARENA_CATALOG

        known = {str(key).lower() for key in ARENA_CATALOG}
    except Exception:
        known = {"most_active", "top_losers", "mega_cap", "large_cap"}
    out: list[str] = []
    for token in re.findall(r"[a-z][a-z0-9_]+", _live_card_scan_line(book).lower()):
        if token in known and token not in out:
            out.append(token)
    return out


def live_card_scan_screens(
    book: dict[str, Any] | None = None,
    *,
    scan: str | None = None,
) -> list[dict[str, str]]:
    """Written scan line as compose screens: universe × sort.

    ``most_active + top_losers; mega/large only`` is mega/large ranked by
    those sorts, not an unfiltered MOST_ACTIVE junk tape.
    """
    try:
        from abcxauto.universe import ARENA_CATALOG
    except Exception:
        ARENA_CATALOG = {}
    if scan is not None:
        text = scan
    else:
        lines: list[str] = []
        state = book if isinstance(book, dict) else load_lab()
        for type_name, card in walk_cards(state):
            if not type_name:
                continue
            if not _is_live_hypothesis(card):
                continue
            line = str(card.get("scan") or "").strip()
            if line and line not in lines:
                lines.append(line)
        text = " ".join(lines) if lines else _live_card_scan_line(book)
    if not text:
        return []
    caps: list[str] = []
    sorts: list[str] = []
    for token in re.findall(r"[a-z][a-z0-9_]+", text.lower()):
        cap = _CAP_SCAN_WORDS.get(token)
        if cap:
            if cap not in caps:
                caps.append(cap)
            continue
        meta = ARENA_CATALOG.get(token) or {}
        if meta.get("group") == "scans" and token not in sorts:
            sorts.append(token)
        elif meta.get("group") == "caps" and token not in caps:
            caps.append(token)
    # A ≥N% gap card is not a MOST_ACTIVE page. IBKR's open-gap loser
    # screen is the sort that surfaces a -20% name the %change ranks miss.
    min_gap = bool(scan is None and live_card_min_gap_pct(book))
    if (
        min_gap
        and "top_open_perc_lose" not in sorts
        and "low_open_gap" not in sorts
    ):
        sorts.append("top_open_perc_lose")
    if min_gap:
        gap_first = (
            "top_open_perc_lose",
            "low_open_gap",
            "top_losers",
            "top_perc_lose",
        )
        sorts = [s for s in gap_first if s in sorts] + [
            s for s in sorts if s not in gap_first
        ]
    screens: list[dict[str, str]] = []
    if caps and sorts:
        pairs = ((cap, sort) for cap in caps for sort in sorts)
    elif sorts:
        pairs = ((sort, sort) for sort in sorts)
    elif caps:
        pairs = ((cap, "") for cap in caps)
    else:
        pairs = ()
    for arena, sort in pairs:
        code = ""
        if sort:
            ibkr = (ARENA_CATALOG.get(sort) or {}).get("ibkr") or {}
            code = str(ibkr.get("scanCode") or "").strip().upper()
        if arena:
            row = {"arena": arena}
            if code:
                row["scan_code"] = code
            screens.append(row)
        if len(screens) >= 8:
            break
    return screens


def live_card_scan_constraints(
    book: dict[str, Any] | None = None,
    *,
    card: Any = None,
) -> dict[str, Any]:
    """Price floor, CORP-only, and cap floor written on a card's scan line."""
    named = str(card or "").strip()
    if named:
        row = _testing_card(book, named)
        line = str((row or {}).get("scan") or "")
    else:
        line = _live_card_scan_line(book)
    if not line:
        return {}
    out: dict[str, Any] = {}
    match = _CARD_MIN_PRICE_RE.search(line)
    if match:
        try:
            px = float(match.group(1))
            if px > 0:
                out["min_price"] = px
        except (TypeError, ValueError):
            pass
    if re.search(r"(?i)levered", line):
        out["skip_levered"] = True
    caps: list[str] = []
    for screen in live_card_scan_screens(book, scan=line):
        arena = str(screen.get("arena") or "")
        if arena in ("mega_cap", "large_cap", "mid_cap") and arena not in caps:
            caps.append(arena)
    if not caps:
        for token in re.findall(r"[a-z][a-z0-9_]+", line.lower()):
            cap = _CAP_SCAN_WORDS.get(token)
            if cap and cap not in caps:
                caps.append(cap)
    if caps:
        out["caps"] = caps
    return out


def apply_card_constraints_to_spec(
    spec: dict[str, Any] | None,
    book: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Overlay the written scan floors onto one IBKR spec. Never lowers a tighter floor."""
    applied: dict[str, Any] = {}
    if not spec:
        return spec, applied
    constraints = live_card_scan_constraints(book)
    if not constraints:
        return spec, applied
    out = dict(spec)
    min_px = constraints.get("min_price")
    if min_px is not None:
        try:
            cur = float(out.get("abovePrice") or 0)
        except (TypeError, ValueError):
            cur = 0.0
        out["abovePrice"] = max(cur, float(min_px))
        applied["card_min_price"] = float(min_px)
        applied["above_price"] = out["abovePrice"]
    if constraints.get("skip_levered"):
        out["stockTypeFilter"] = "CORP"
        applied["card_stock_type"] = "CORP"
    floors: list[float] = []
    try:
        from abcxauto.universe import ARENA_CATALOG
    except Exception:
        ARENA_CATALOG = {}
    for cap in constraints.get("caps") or []:
        ibkr = (ARENA_CATALOG.get(cap) or {}).get("ibkr") or {}
        raw = ibkr.get("marketCapAbove")
        if raw is None:
            continue
        try:
            floors.append(float(raw))
        except (TypeError, ValueError):
            continue
    if floors:
        floor = min(floors)
        try:
            cur = float(out.get("marketCapAbove") or 0)
        except (TypeError, ValueError):
            cur = 0.0
        out["marketCapAbove"] = max(cur, floor)
        applied["card_market_cap_above"] = floor
        applied["market_cap_above"] = out["marketCapAbove"]
    return out, applied


def drop_hits_off_card(
    rows: list[dict[str, Any]] | None,
    book: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop quoted names under the written sub-$ floor. Unknown last stays."""
    constraints = live_card_scan_constraints(book)
    min_px = constraints.get("min_price")
    src = [r for r in (rows or []) if isinstance(r, dict)]
    if not min_px:
        return src, []
    keep: list[dict[str, Any]] = []
    dropped: list[str] = []
    for row in src:
        last = row.get("last")
        if last is None:
            ibkr = row.get("ibkr")
            if isinstance(ibkr, dict):
                last = ibkr.get("last")
            elif ibkr is not None:
                last = ibkr
        if last is None:
            keep.append(row)
            continue
        try:
            if float(last) + 1e-9 < float(min_px):
                name = str(row.get("symbol") or "").upper()
                if name:
                    dropped.append(name)
                continue
        except (TypeError, ValueError):
            keep.append(row)
            continue
        keep.append(row)
    return keep, dropped


def scan_screen_key(arena: str = "", scan_code: str = "") -> str:
    name = str(arena or "").strip()
    code = str(scan_code or "").strip()
    if name and code:
        return f"{name}:{code}"
    return name or code


def _session_rows(session_range: Any) -> list[dict[str, Any]]:
    store = session_range if isinstance(session_range, dict) else {}
    if not store:
        return []
    try:
        from abcxauto.think_stream import _compact_session_range

        compact = _compact_session_range(store)
        if compact:
            store = compact
    except Exception:
        pass
    if store.get("ticket") or store.get("low") is not None or store.get("open") is not None:
        return [store]
    return [rng for rng in store.values() if isinstance(rng, dict)]


def _has_today_session(session_range: Any) -> bool:
    """True when this look already has a today RTH range to pin the stop."""
    return any(rng.get("today") is True for rng in _session_rows(session_range))


def _today_session_on_lows(session_range: Any) -> bool:
    """True when every today session is sitting on or through the opening low."""
    saw_today = False
    for rng in _session_rows(session_range):
        if rng.get("today") is not True:
            continue
        saw_today = True
        if rng.get("above_low") is not False:
            return False
    return saw_today


def _today_session_under_min_gap(session_range: Any, min_gap: float) -> bool:
    """True when every today gap we have is under the card's written floor."""
    saw_gap = False
    for rng in _session_rows(session_range):
        if rng.get("today") is not True:
            continue
        mag = _session_gap_mag(rng)
        if mag is None:
            continue
        saw_gap = True
        if mag + 1e-9 >= min_gap:
            return False
    return saw_gap


def session_target(session: Any, direction: str = "LONG") -> float | None:
    """30% retrace, or 50% if last already traded through 30. None if both are gone."""
    if not isinstance(session, dict):
        return None
    side = str(direction or "LONG").upper()
    last = session.get("last")
    try:
        last_f = float(last) if last not in (None, "") else None
    except (TypeError, ValueError):
        last_f = None
    for key in ("retrace_30", "retrace_50"):
        raw = session.get(key)
        if raw is None:
            continue
        try:
            tgt = float(raw)
        except (TypeError, ValueError):
            continue
        if last_f is None:
            return tgt
        if side == "SHORT":
            if tgt < last_f:
                return tgt
        elif tgt > last_f:
            return tgt
    return None


def hunt_send_sketch(
    session_range: Any,
    tape: Any = None,
    *,
    card: Any = None,
) -> dict[str, Any] | None:
    """Stamped ticket on today's session, if any. Clerk does not invent fields."""
    store = session_range if isinstance(session_range, dict) else {}
    if not store:
        return None
    try:
        from abcxauto.think_stream import _compact_session_range

        compact = _compact_session_range(store)
        if compact:
            store = compact
    except Exception:
        pass
    items: list[tuple[str, dict[str, Any]]] = []
    if store.get("ticket") or store.get("low") is not None or store.get("open") is not None:
        items.append(("", store))
    else:
        for key, rng in store.items():
            if isinstance(rng, dict):
                items.append((str(key), rng))

        def _gap_mag(item: tuple[str, dict[str, Any]]) -> float:
            try:
                raw = item[1].get("gap_pct", item[1].get("open_gap_pct"))
                return abs(float(raw or 0))
            except (TypeError, ValueError):
                return 0.0

        items.sort(key=_gap_mag, reverse=True)
    named = str(card or "").strip()
    if named:
        for type_name, row in _walk_testing():
            if str(row.get("name") or "").strip().lower() == named.lower():
                if type_name not in ("market_bracket", "bracket"):
                    return None
                break
    min_px = live_card_scan_constraints().get("min_price")
    no_reentry = live_card_needs_no_reentry()
    tight = live_card_needs_tight_spread()
    skip_spy = live_card_skips_spy()
    allowed = _tape_symbols(tape) if _has_tape_blob(tape) else set()
    scanned = _has_tape_blob(tape)
    for sym, rng in items:
        if rng.get("today") is not True:
            continue
        prior = rng.get("ticket") if isinstance(rng.get("ticket"), dict) else {}
        want_card = named or str(prior.get("card") or "").strip()
        mag = _session_gap_mag(rng)
        picked = _tightest_matching_card(None, mag, card_name=want_card or None)
        existing = _testing_card(None, want_card) if want_card else None
        if picked is None:
            if want_card and existing is not None:
                continue
            if not want_card and live_card_min_gap_pct() is not None:
                continue
            type_name = str(prior.get("strategy") or "")
            row = {"name": want_card or prior.get("card")}
        else:
            type_name, row = picked
        if type_name and type_name not in ("market_bracket", "bracket"):
            continue
        pick_name = str(row.get("name") or "")
        min_gap = _card_min_gap_pct(existing or row)
        if min_gap and (mag is None or mag + 1e-9 < min_gap):
            continue
        if min_px is not None and rng.get("last") is not None:
            try:
                if float(rng["last"]) + 1e-9 < float(min_px):
                    continue
            except (TypeError, ValueError):
                pass
        name = str(sym or prior.get("symbol") or "").upper()
        if skip_spy and name == "SPY":
            continue
        if scanned and name and name not in allowed:
            continue
        if no_reentry and name and card_sent_symbol_today(
            pick_name or str(prior.get("card") or ""), name
        ):
            continue
        if tight and rng.get("spread") is not None and rng.get("last") is not None:
            stop = prior.get("stop_price") if prior.get("stop_price") not in (None, "") else rng.get("low")
            if stop not in (None, ""):
                try:
                    if float(rng["spread"]) + 1e-9 >= abs(float(rng["last"]) - float(stop)):
                        continue
                except (TypeError, ValueError):
                    pass
        ticket = rng.get("ticket") if isinstance(rng.get("ticket"), dict) else {}
        hold_side = str(ticket.get("direction") or "LONG").upper()
        if rng.get("above_low") is False and hold_side != "SHORT":
            continue
        if (
            live_card_needs_hold_above_open(card=pick_name)
            and rng.get("above_open") is False
            and hold_side != "SHORT"
        ):
            continue
        tgt = session_target(rng, hold_side)
        if tgt is None and (
            rng.get("retrace_30") is not None or rng.get("retrace_50") is not None
        ):
            continue
        sketch = dict(ticket)
        # Session key names the row. Clerk does not invent the rest of the ticket.
        if name:
            sketch["symbol"] = name
        if sketch.get("symbol") and sketch.get("card"):
            return sketch
    return None


def apply_hunt_send_sketch(act: dict[str, Any], snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """No-op. Playbook is a notebook — clerk does not fill omitted send fields."""
    return None


def hunt_recipe_has(name: str, book: dict[str, Any] | None = None) -> bool:
    """Whether a live card's hunt order names this tool."""
    want = str(name or "").strip()
    if not want:
        return False
    for row in playbook_run_sheets(book, flat=True):
        if want in (row.get("tool_order") or []):
            return True
    return False


def playbook_run_sheets(
    book: dict[str, Any] | None = None,
    *,
    tool_trace: list[str] | None = None,
    last_look: list[str] | None = None,
    flat: bool | None = None,
    quoted: Any = None,
    news: Any = None,
    session_today: bool | None = None,
    session_range: Any = None,
    positions: Any = None,
) -> list[dict[str, Any]]:
    """Live cards as a run sheet: parent tool_order and the next unused tool.

    Facts only. Clerk does not invent a thesis. Locked OPEN starters are the
    notebook catalog, not a parallel hunt — they stay off the run sheet.
    After last look already ran scan/news, the next look starts at the first
    unread hunt tool. A screen that already stamped live last/bid/ask counts
    as quote. Headlines already nested on that screen count as news.
    """
    state = book if isinstance(book, dict) else load_lab()
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    managing = flat is False
    done = _effective_tool_trace(tool_trace, last_look, managing=managing)
    if not managing and _scan_carries_news(news):
        if "news" not in done:
            done = list(done) + ["news"]
    if not managing and _screen_quoted(quoted):
        if "quote" not in done:
            done = list(done) + ["quote"]
    scored = {
        card_key(row.get("type"), row.get("card")): row
        for row in card_facts(state)
    }
    out: list[dict[str, Any]] = []
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_live_hypothesis(card):
            continue
        status = str(card.get("status") or "testing").strip().lower()
        stanza = types.get(type_name) if isinstance(types.get(type_name), dict) else {}
        parent_order = _norm_recipe(
            stanza.get("tool_order") or stanza.get("default_tool_recipe")
        )
        card_order = _norm_recipe(
            card.get("tool_order") or card.get("default_tool_recipe")
        )
        review = str(stanza.get("review") or card.get("review") or "").strip()
        if managing:
            # Review is prose, not a recipe. A line that only names book
            # must not hide fills/quote/candles.
            order = list(_DEFAULT_MANAGE_ORDER)
        else:
            order = card_order or parent_order or list(_DEFAULT_HUNT_ORDER)
        score = scored.get(card_key(type_name, card.get("name"))) or {}
        nxt = _next_in_order(order, done)
        if (
            not managing
            and isinstance(quoted, dict)
            and quoted.get("ok") is False
        ):
            done = [name for name in done if name != "scan"]
            nxt = _next_in_order(order, done)
        elif (
            not managing
            and "scan" in done
            and quoted is not None
            and _explicit_empty_tape(quoted)
        ):
            nxt = ""
        if not managing and nxt == "send" and (
            session_today is False or not _has_today_session(session_range)
        ):
            nxt = "candles"
        row: dict[str, Any] = {
            "type": type_name,
            "card": card.get("name"),
            "status": status,
            "tool_order": order,
            "next": nxt,
            "sends": int(score.get("sends") or 0),
            "resolved": int(score.get("resolved") or 0),
        }
        if session_today is False:
            row["session_today"] = False
        min_gap = live_card_min_gap_pct(state, card=card.get("name"))
        if min_gap:
            row["min_gap_pct"] = min_gap
        min_px = live_card_scan_constraints(state, card=card.get("name")).get("min_price")
        if min_px:
            row["min_price"] = min_px
        if not nxt:
            row["next"] = ""
            row["gate"] = "off"
        if nxt == "send":
            sketch = hunt_send_sketch(
                session_range, tape=quoted, card=card.get("name")
            )
            if sketch:
                book_note = live_card_book_error(sketch, positions, state)
                if book_note:
                    row["next"] = ""
                    row["gate"] = "off"
                else:
                    row["send"] = sketch
            elif _today_session_on_lows(session_range):
                nxt = "candles"
                row["next"] = "candles"
                row["above_low"] = False
            elif min_gap and _today_session_under_min_gap(session_range, min_gap):
                row["next"] = ""
                row["gate"] = "off"
            else:
                row["next"] = ""
                row["gate"] = "off"
        if score.get("sample_left") is not None:
            row["sample_left"] = score.get("sample_left")
        if score.get("looks_without_trigger") is not None:
            row["looks_without_trigger"] = score.get("looks_without_trigger")
        if score.get("days_without_trigger") is not None:
            row["days_without_trigger"] = score.get("days_without_trigger")
        if managing:
            if review:
                row["review"] = review[:240]
        else:
            when_on = str(card.get("when_on") or "").strip()
            scan = str(card.get("scan") or "").strip()
            if when_on:
                row["when_on"] = when_on[:200]
            if scan:
                row["scan"] = scan[:160]
            screens = live_card_scan_screens(state)
            if screens:
                row["screens"] = [
                    scan_screen_key(str(s.get("arena") or ""), str(s.get("scan_code") or ""))
                    for s in screens[:8]
                    if s.get("arena") or s.get("scan_code")
                ]
        out.append(row)
        if len(out) >= 6:
            break
    return out


def playbook_glance(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score since the last write. Not the notebook text â€” Grok asks playbook() for that."""
    facts = playbook_facts(scorecard)
    return {
        "revision": facts.get("revision"),
        "age_h": facts.get("age_h"),
        "since_write_edge": facts.get("since_write_edge"),
        "now_edge": facts.get("now_edge"),
        "now_edge_pct_of_nl": facts.get("now_edge_pct_of_nl"),
        "now_beating": facts.get("now_beating"),
        "win_4h": facts.get("win_4h"),
        "lots_at_write": list(facts.get("lots_at_write") or [])[:16],
        "stale": playbook_is_stale(),
    }


def _MANAGEMENT_TRUNKS() -> frozenset[str]:
    """Trunks that adjust or close existing risk rather than open it."""
    from abcxauto.proposals import MANAGEMENT_STRATEGIES

    return MANAGEMENT_STRATEGIES | frozenset({"close_option"})


def lab_facts(
    book: dict[str, Any] | None = None,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What the lab has under test, and which trunks carry no hypothesis.

    The other half of ``strategy_scores``. That key reports what already traded
    and what it earned, so on its own the only breadth signal on this surface is
    the P&L of what ran — which reads as a reason to run it again. This reports
    what has never been tried. Counts and names only: which structure to test is
    the notebook's business, not the clerk's.

    Locked OPEN starters fill empty trunks so the book is not three flush
    cards plus blank slots. They count in ``cards`` but are not the wake —
    Grok's own hypotheses stay on ``cards_awaiting_first_trade``.

    Only entry structures count as untried. ``modify_stop`` and ``cancel_order``
    adjust risk that already exists, so listing them as gaps would invite a
    hypothesis about cancelling an order.
    """
    state = book if isinstance(book, dict) else load_lab()
    card_rows = rows if rows is not None else card_facts(state)
    by_status = {status: 0 for status in CARD_STATUSES}
    awaiting: list[dict[str, Any]] = []
    idle: list[dict[str, Any]] = []
    resolved_total = 0
    for row in card_rows:
        status = str(row.get("status") or "testing").strip().lower()
        if status in by_status:
            by_status[status] += 1
        n = int(row.get("resolved") or 0)
        resolved_total += n
        if status == "retired":
            continue
        if row.get("locked") is True:
            continue
        wait_row = {
            "card": _card_label(row),
            "sends": int(row.get("sends") or 0),
            "resolved": n,
            "looks": row.get("looks_without_trigger"),
            "days": row.get("days_without_trigger"),
            "last_send": row.get("last_send"),
            "max_looks_without_trigger": row.get("max_looks_without_trigger"),
        }
        idle.append(wait_row)
        if n == 0:
            awaiting.append(wait_row)
    coverage = type_coverage(state)
    return {
        "cards": by_status,
        "resolved_trades": resolved_total,
        "cards_awaiting_first_trade": awaiting[:12],
        "cards_without_trigger": idle[:12],
        "trunks_with_cards": [r["type"] for r in coverage if r["cards"]],
        "entry_trunks_untried": [
            r["type"] for r in coverage
            if not r["cards"] and r["type"] not in _MANAGEMENT_TRUNKS()
        ],
    }


def lab_wake_bit(
    book: dict[str, Any] | None = None,
    *,
    tool_trace: list[str] | None = None,
    last_look: list[str] | None = None,
    flat: bool | None = None,
    quoted: Any = None,
    session_range: Any = None,
    positions: Any = None,
) -> str:
    """One wake-line clause: the waiting card and the next unused tool.

    Counts and the parent tool_order only. Which structure to test is Grok's.
    """
    try:
        facts = lab_facts(book)
    except Exception:
        return ""
    bits: list[str] = []
    awaiting = facts.get("cards_awaiting_first_trade") or []
    if awaiting:
        top = awaiting[0] if isinstance(awaiting[0], dict) else {}
        name = str(top.get("card") or "").split(" [")[0][:40]
        wait: list[str] = []
        looks = top.get("looks")
        days = top.get("days")
        if isinstance(looks, int):
            wait.append(f"{looks}looks")
        if isinstance(days, (int, float)) and days:
            wait.append(f"{days:g}d")
        wait.append("0sends")
        if name:
            bits.append(f"lab {name} {'/'.join(wait)}")
    try:
        sheets = playbook_run_sheets(
            book,
            tool_trace=tool_trace,
            last_look=last_look,
            flat=flat,
            quoted=quoted,
            session_range=session_range,
            positions=positions,
        )
    except Exception:
        sheets = []
    if sheets:
        sheet = sheets[0]
        for cand in sheets:
            send_row = cand.get("send")
            if cand.get("next") == "send" and isinstance(send_row, dict) and send_row.get("symbol"):
                sheet = cand
                break
        else:
            for cand in sheets:
                if cand.get("gate") != "off" and cand.get("next"):
                    sheet = cand
                    break
        if not awaiting:
            name = str(sheet.get("card") or "").strip()[:40]
            if name:
                bits.append(f"lab {name}")
        nxt = str(sheet.get("next") or "").strip()
        if sheet.get("gate") == "off":
            bits.append("gate=off")
        if nxt:
            bits.append(f"next={nxt}")
        looks = sheet.get("looks_without_trigger")
        if isinstance(looks, int) and looks and not awaiting:
            bits.append(f"{looks}looks_no_trigger")
        if nxt == "scan":
            screens = sheet.get("screens") or []
            if screens:
                bits.append(str(screens[0])[:40])
        send = sheet.get("send") if nxt == "send" else None
        if isinstance(send, dict) and send.get("symbol"):
            card = str(send.get("card") or "").strip()
            bit = f"send {send['symbol']}"
            if card:
                bit += f" card={card[:40]}"
            bits.append(bit)
    else:
        untried = facts.get("entry_trunks_untried") or []
        if untried:
            bits.append(f"untried={len(untried)}")
    try:
        if playbook_is_stale(book):
            bits.append("book_stale")
    except Exception:
        pass
    return " ".join(bits)


def playbook_facts(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forest vs the last write: age and score at write vs now. No lecture."""
    from abcxauto.world_state import pct_of_nl

    lab = load_lab()
    inst = notebook_text(lab)
    at_write = lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    now_sc = scorecard if isinstance(scorecard, dict) else {}
    age = playbook_age_hours(lab)
    ledger = [_compact_card(r) for r in ensure_ledger(lab)]
    since = _since_write_score(lab, now_sc)
    nl = now_sc.get("net_liquidation")
    at_edge = at_write.get("edge_usd")
    now_edge = now_sc.get("edge_usd")
    since_edge = since.get("since_write_edge")
    facts = {
        "revision": lab.get("revision"),
        "mode": lab.get("mode") or None,
        "has_instructions": bool(inst),
        "ready_to_promote": bool(lab.get("ready_to_promote")) if inst else None,
        "age_h": round(age, 1) if age is not None else None,
        "at_write_edge": at_edge,
        "at_write_edge_pct_of_nl": pct_of_nl(at_edge, nl),
        "at_write_beating": at_write.get("beating_model"),
        "now_edge": now_edge,
        "now_edge_pct_of_nl": pct_of_nl(now_edge, nl),
        "now_beating": now_sc.get("beating_model"),
        "since_write_edge": since_edge,
        "since_write_edge_pct_of_nl": pct_of_nl(since_edge, nl),
        "since_write_pnl": since.get("since_write_pnl"),
        "lots_at_write": [str(x) for x in (lab.get("lots_at_write") or [])][:16],
        "ledger": ledger[-8:],
    }
    facts.update(_window_edges(now_sc))
    halt = _clerk_halt_slice(now_sc)
    facts.update(halt)
    if isinstance(halt, dict):
        facts["halt_trips_at_pct_of_nl"] = pct_of_nl(halt.get("halt_trips_at_usd"), nl)
        facts["ibkr_day_vs_halt_pct_of_nl"] = pct_of_nl(halt.get("ibkr_day_vs_halt"), nl)
    return facts


def _clerk_halt_slice(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    from abcxauto.book import clerk_halt_facts

    nl = None
    day = None
    if isinstance(scorecard, dict):
        nl = scorecard.get("net_liquidation")
        day = scorecard.get("ibkr_daily_pnl")
        if day is None:
            day = scorecard.get("daily_pnl")
    if nl is None or day is None:
        try:
            from abcxauto.memory import get_journal

            perf = get_journal().account_performance() or {}
            if nl is None:
                nl = perf.get("net_liquidation")
            if day is None:
                day = perf.get("daily_pnl")
        except Exception:
            pass
    return clerk_halt_facts(nl, day)


def format_ledger_line(facts: dict[str, Any] | None) -> str:
    rows = (facts or {}).get("ledger") if isinstance(facts, dict) else None
    if not isinstance(rows, list) or not rows:
        return ""
    bits = []
    for row in rows[-4:]:
        if not isinstance(row, dict) or row.get("revision") is None:
            continue
        bit = f"r{row.get('revision')}:{row.get('edge_usd')}"
        if row.get("closed_edge") is not None:
            bit += f">{row.get('closed_edge')}"
        bits.append(bit)
    return " ".join(bits)


def _live_scorecard(lab: dict[str, Any] | None = None) -> dict[str, Any]:
    """Current scorecard. The stamp on disk is at_write, not now."""
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard()
        if isinstance(sc, dict) and sc:
            return sc
    except Exception:
        pass
    state = lab if isinstance(lab, dict) else {}
    stamp = state.get("paper_score")
    return stamp if isinstance(stamp, dict) else {}


def format_block() -> str:
    """Compact forest for book/wake. Full prose is the playbook tool."""
    paper = is_paper()
    lab = load_lab()
    live = load_live()
    if paper:
        inst = notebook_text(lab)
        if not inst:
            return "LAB PLAYBOOK: none. write_lab_playbook to set; playbook tool for full text.\n"
        live_sc = _live_scorecard(lab)
        facts = playbook_facts(live_sc)
        ledger = format_ledger_line(facts)
        lots = facts.get("lots_at_write") or []
        lots_s = ",".join(str(x) for x in lots[:8]) if lots else "none"
        return (
            "LAB PLAYBOOK:\n"
            f"- rev={lab.get('revision')} mode={lab.get('mode') or 'explore'} "
            f"promoted={bool(lab.get('promoted'))}\n"
            f"- since_write={facts.get('since_write_edge')} "
            f"now_edge={facts.get('now_edge')} "
            f"4h={facts.get('win_4h')} age_h={facts.get('age_h')}\n"
            f"- lots_at_write={lots_s}\n"
            f"- ledger: {ledger or 'none'}\n"
            "- notebook: playbook tool; send is the book\n"
        )
    inst = notebook_text(live)
    if not inst:
        return "LIVE: no promoted paper playbook. New risk blocked until promote (code).\n"
    return (
        "LIVE PLAYBOOK (promoted snapshot):\n"
        f"- promoted_revision={live.get('promoted_revision')} "
        f"promoted_at={live.get('promoted_at')}\n"
        "- notebook: playbook tool\n"
    )


def clear_lab(*, reason: str = "") -> dict[str, Any]:
    """Operator wipe. Grok starts a new notebook; no standing essay."""
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "mode": "explore",
        "instructions": "",
        "types": {},
        "ready_to_promote": False,
        "promoted": False,
        "revision": 0,
        "written_at": now,
        "cleared_at": now,
        "cleared_reason": str(reason or "")[:240],
        "ledger": [],
        "paper_score": {},
    }
    _write(_lab_path(), state)
    return state


def playbook_payload(revision: Any = None, *, full: bool = False) -> dict[str, Any]:
    """Notebook plus score since write. full is accepted and ignored â€” the notes are the tool."""
    paper = is_paper()
    lab = load_lab() if paper else load_live()
    live_sc = _live_scorecard(lab) if paper else (
        lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    )
    facts = playbook_facts(live_sc)
    inst = notebook_text(lab)
    types = lab.get("types") if isinstance(lab.get("types"), dict) else {}
    current: dict[str, Any] = {
        "revision": lab.get("revision") or lab.get("promoted_revision"),
        "mode": lab.get("mode"),
        "ready_to_promote": bool(lab.get("ready_to_promote")),
        "promoted": bool(lab.get("promoted")),
        "written_at": lab.get("written_at") or lab.get("promoted_at"),
        "paper_score": lab.get("paper_score") or {},
        "instructions_n": len(inst),
        "stale": playbook_is_stale(lab) if paper else False,
    }
    cards = _flat_card_projection(lab)
    facts_by_card = card_facts(lab)
    last_facts: dict[str, Any] = {}
    try:
        from abcxauto.think_stream import last_look_for_hunt

        last_facts = last_look_for_hunt()
    except Exception:
        last_facts = {}
    last_tools = list(last_facts.get("tools") or [])
    out: dict[str, Any] = {
        "scope": "lab" if paper else "live",
        # Facts and the notebook before the score tables. Copying types into
        # current plus a trailing tree used to blow the 24k tool clip and
        # return broken JSON — Grok then re-hunted a card it had already
        # written.
        "lab": lab_facts(lab, rows=facts_by_card),
        "run": playbook_run_sheets(
            lab,
            last_look=last_tools,
            quoted=last_facts,
            session_range=last_facts.get("session_range"),
        ),
        "score": {
            "revision": facts.get("revision"),
            "age_h": facts.get("age_h"),
            "at_write_edge": facts.get("at_write_edge"),
            "now_edge": facts.get("now_edge"),
            "since_write_edge": facts.get("since_write_edge"),
            "since_write_pnl": facts.get("since_write_pnl"),
            "lots_at_write": list(facts.get("lots_at_write") or []),
            "clerk_halted": facts.get("clerk_halted"),
            "halt_kind": facts.get("halt_kind"),
            "halt_reason": facts.get("halt_reason"),
            "daily_loss_limit_pct": facts.get("daily_loss_limit_pct"),
            "halt_trips_at_usd": facts.get("halt_trips_at_usd"),
            "halt_trips_at_pct_of_nl": facts.get("halt_trips_at_pct_of_nl"),
            "ibkr_day_vs_halt": facts.get("ibkr_day_vs_halt"),
            "ibkr_day_vs_halt_pct_of_nl": facts.get("ibkr_day_vs_halt_pct_of_nl"),
            "now_edge_pct_of_nl": facts.get("now_edge_pct_of_nl"),
            "since_write_edge_pct_of_nl": facts.get("since_write_edge_pct_of_nl"),
        },
        "cards": cards,
        "tree": notebook_text(lab),
        "card_scores": facts_by_card,
        "graduated": [_card_label(r) for r in facts_by_card if r.get("graduated")],
        "tripped": [_card_label(r) for r in facts_by_card if r.get("tripped")],
        "needs_declaration": [
            _card_label(r)
            for r in facts_by_card
            if r.get("needs_retire_if") or r.get("needs_thesis")
        ],
        "strategy_scores": strategy_scores(),
        "unfiled_cards": list(lab.get(UNFILED_KEY) or []),
        "ledger": [_compact_card(r) for r in ensure_ledger(lab)],
        "facts": facts,
        "current": current,
        "types": types,
    }
    if revision in (None, ""):
        return out
    try:
        want = int(revision)
    except (TypeError, ValueError):
        out["error"] = "revision must be an int"
        return out
    card = revision_card(want, lab)
    if card is None:
        out["error"] = f"revision {want} not in ledger"
        return out
    out["revision"] = _outcome_card(card)
    return out
