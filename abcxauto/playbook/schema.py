"""Card and type schema. Notebook shape, not doctrine.

A card stores ``retire_if`` (sample + optional numeric kill fields) and a
``fill_assumption``. Promotion/graduation is ``playbook.promote`` — this
module only normalizes what Grok wrote. Notebook prose is not a send gate.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from abcxauto.playbook.hub import hub as _hub

logger = logging.getLogger(__name__)

_MAX_INSTRUCTIONS = 16000
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
# Room for Grok's hunting cards plus one locked starter per OPEN type.
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
# Paper IBKR option fills are fantasy mids. Stored as a fact; cannot graduate.
FILL_ASSUMPTION_PAPER_MID = "paper_mid"
FILL_ASSUMPTION_FULL_SPREAD = "full_spread"
FILL_ASSUMPTION_CONSERVATIVE = "conservative"
CONSERVATIVE_FILL_ASSUMPTIONS = frozenset({
    FILL_ASSUMPTION_FULL_SPREAD,
    FILL_ASSUMPTION_CONSERVATIVE,
})
FILL_ASSUMPTIONS = (
    FILL_ASSUMPTION_PAPER_MID,
    FILL_ASSUMPTION_FULL_SPREAD,
    FILL_ASSUMPTION_CONSERVATIVE,
)
# Numbers we will not invent. Keys stay stable so a missing series is a gap.
HONESTY_GAP_REASONS: dict[str, str] = {
    "fill_vs_ibkr_last": "journal fills store price, not IBKR last at fill",
    "holdout": "no holdout split on card trades",
    "beat_spy_after_model_cost": "no SPY return series; account_returns is IBKR NAV only",
    "cost_allocated_pnl": "no model cost or no sends to allocate",
    "turnover_per_day": "no card clock",
}
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
# Retired skip-class cards. Presence demotes levered/micro names from scan
# ``deepest`` ranking. Not a send gate; card prose is not a refuse.
SKIP_CARD_NAMES = frozenset(
    {
        "levered-crypto and micro gap chase",
        "naked / short-dated option spray",
    }
)
_STK_HUNT = ("scan", "news", "quote", "candles", "send")
_OPT_HUNT = ("scan", "news", "option_chain", "option_facts", "option_quote", "send")
_OVERLAY_HUNT = ("book", "quote", "option_chain", "option_facts", "send")
# One short locked starter per OPEN type. Structure, not a ticker.
# Seeded on lab load/save when the trunk has no hunting card (and, for
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
    state = book if isinstance(book, dict) else _hub().load_lab()
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


def skip_cards_on_book(book: dict[str, Any] | None = None) -> bool:
    """True when a skip-class card is on the book, including retired.

    Ranking only: levered/micro names are not pinned as scan ``deepest``.
    Not a send refuse and not a parse of card when_on / retrace prose.
    """
    state = book if isinstance(book, dict) else _hub().load_lab()
    want = {n.lower() for n in SKIP_CARD_NAMES}
    for _type_name, card in walk_cards(state):
        name = str((card or {}).get("name") or "").strip().lower()
        if name in want:
            return True
    return False


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
    for key, cap in (
        ("max_loss_usd", 1e9),
        ("max_losses", 200),
        ("max_hold_sessions", 200),
        ("max_hold_hours", 1e6),
    ):
        val = raw.get(key)
        if val in (None, ""):
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            continue
        if num <= 0:
            continue
        out[key] = (
            int(min(num, cap))
            if key in ("max_losses", "max_hold_sessions")
            else round(min(num, cap), 2)
        )
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


def _norm_fill_assumption(raw: Any) -> str:
    """How the card's fills are assumed. Missing is paper_mid — not conservative."""
    text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    compact = text.replace("_", "")
    if text in (
        FILL_ASSUMPTION_PAPER_MID,
        "mid",
        "paper",
        "fantasy_mid",
        "paper_mids",
    ) or compact in {"papermid", "papermids", "mid", "paper", "fantasymid"}:
        return FILL_ASSUMPTION_PAPER_MID
    if text in (
        FILL_ASSUMPTION_FULL_SPREAD,
        "pay_spread",
        "pay_the_spread",
    ) or compact in {"fullspread", "payspread", "paythespread"}:
        return FILL_ASSUMPTION_FULL_SPREAD
    if text in (
        FILL_ASSUMPTION_CONSERVATIVE,
        "conservative_fill",
    ) or compact in {"conservative", "conservativefill"}:
        return FILL_ASSUMPTION_CONSERVATIVE
    return FILL_ASSUMPTION_PAPER_MID


def fill_assumption_of(card: Any) -> str:
    row = card if isinstance(card, dict) else {}
    return _norm_fill_assumption(row.get("fill_assumption"))


def fill_assumption_is_conservative(card: Any) -> bool:
    return fill_assumption_of(card) in CONSERVATIVE_FILL_ASSUMPTIONS


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
    if "fill_assumption" in raw:
        out["fill_assumption"] = _norm_fill_assumption(raw.get("fill_assumption"))
    elif carried.get("fill_assumption"):
        out["fill_assumption"] = _norm_fill_assumption(carried.get("fill_assumption"))
    else:
        # Honest default: paper IBKR fills are mids until Grok says otherwise.
        out["fill_assumption"] = FILL_ASSUMPTION_PAPER_MID
    written = str(raw.get("written_at") or carried.get("written_at") or "").strip()
    if written:
        out["written_at"] = written[:48]
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
    if row.get("max_hold_sessions"):
        bits.append(f"max_hold_sessions={row['max_hold_sessions']}")
    if row.get("max_hold_hours"):
        bits.append(f"max_hold_hours={row['max_hold_hours']}")
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
        evidence = card.get("evidence") if isinstance(card.get("evidence"), dict) else {}
        for key in EVIDENCE_FIELDS:
            if key == "scan":
                continue
            val = str(evidence.get(key) or "").strip()
            if val:
                lines.append(f"{indent}  evidence.{key}: {val}")
        fill = fill_assumption_of(card)
        lines.append(f"{indent}  fill_assumption: {fill}")
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
    not a standing order: ``format_block`` never paints notes as tickets.
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
        filed, _unfiled = _hub()._file_cards_into_tree(bare, [])
        return filed
    filed, _unfiled = _hub()._file_cards_into_tree(tree, incoming)
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
    prev = _hub().load_lab()

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


__all__ = [
    '_MAX_INSTRUCTIONS',
    '_PATCH_KEYS',
    'TYPE_LEARNED_FIELDS',
    '_TYPE_SCHEMA_ECHO',
    '_MAX_CARDS',
    '_MAX_CARDS_PER_TYPE',
    'UNFILED_KEY',
    '_PROJECTED_KEYS',
    'EVIDENCE_FIELDS',
    'CARD_STATUSES',
    'FILL_ASSUMPTION_PAPER_MID',
    'FILL_ASSUMPTION_FULL_SPREAD',
    'FILL_ASSUMPTION_CONSERVATIVE',
    'CONSERVATIVE_FILL_ASSUMPTIONS',
    'FILL_ASSUMPTIONS',
    'HONESTY_GAP_REASONS',
    '_CALIBRATION_MIN_N',
    'EXIT_PROTECTIVE',
    'EXIT_DECISION',
    'EXIT_OPERATOR',
    'EXIT_OPEN',
    'CARD_EXIT_KINDS',
    'RESOLVED_EXITS',
    '_SKIP_PLAYBOOK_TYPES',
    'PLAYBOOK_TYPE_KEYS',
    'OPEN_PLAYBOOK_TYPES',
    'PROTECTED_CARD_NAMES',
    'SKIP_CARD_NAMES',
    '_STK_HUNT',
    '_OPT_HUNT',
    '_OVERLAY_HUNT',
    'OPEN_TYPE_STARTERS',
    '_TYPE_META_KEYS',
    '_HARD_SHAPE',
    '_DEAD_LAB_KEYS',
    '_GATE_FORBIDDEN',
    '_GATES_HDR',
    '_FLOOR_NL',
    '_PCT',
    '_TYPE_HDR',
    '_FIELD_LINE',
    '_STALE_H_DEFAULT',
    '_CARD_WINDOWS',
    '_field',
    'playbook_type_keys',
    'open_playbook_types',
    'type_coverage',
    'empty_type_catalog',
    '_norm_recipe',
    '_floors_and_knob',
    '_gate_pcts_on_line',
    '_invented_pct_gate_line',
    '_has_invented_pct_gate',
    '_strip_invented_pct_gate_lines',
    '_walk_text',
    '_norm_type_row',
    '_clean_types',
    'type_cards',
    'walk_cards',
    'skip_cards_on_book',
    'card_key',
    '_flat_card_projection',
    '_strip_projection',
    'type_schema_echo_keys',
    '_norm_evidence',
    '_norm_expect_hit_rate',
    '_norm_retire_if',
    '_norm_fill_assumption',
    'fill_assumption_of',
    'fill_assumption_is_conservative',
    'card_ticket_of',
    '_incoming_card_name',
    '_norm_card',
    '_card_name_key',
    '_non_retired_cards',
    '_starter_row',
    '_seed_open_type_starters',
    'unknown_card_tickets',
    'conflicting_card_tickets',
    'untyped_card_names',
    '_norm_cards',
    '_retire_if_line',
    'render_cards',
    'render_playbook_tree',
    'notebook_text',
    '_has_book',
    '_unknown_type_keys',
    '_parse_structured_text',
    '_coerce_types_blob',
    '_looks_like_type_map',
    '_extract_types',
    'book_shape_rejects',
    '_merge_type_catalog',
    '_cards_update',
    'gate_rejects',
    'clamp_update',
    'grounding_error',
]
