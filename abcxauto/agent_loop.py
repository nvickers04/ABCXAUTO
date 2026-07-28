"""Perceive → Judge → Act autonomous loop.

``abcxauto.cycle`` re-exports this API for test/UI compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from xai_sdk.chat import system, user

from abcxauto.book import build_book_from_snap
from abcxauto.config import get_config, resolve_effective_posture, risk_envelope_snapshot
from abcxauto.connections import connection_status
from abcxauto.llm import GrokClient
from abcxauto.memory import get_journal
from abcxauto.monitor import build_protection_report
from abcxauto.opportunity_scan import (
    QUOTE_SOURCES_BLOCK,
    dismiss_cites_tape,
    fetch_scan_metrics,
    merge_tape,
    normalize_tickers,
    scan_opportunities,
    tape_symbols,
)
from abcxauto.order_examples import format_order_examples
from abcxauto.reality_pulse import build_reality_pulse
from abcxauto.send import send_action
from abcxauto.session_cadence import maybe_auto_review_from_cycle
from abcxauto.tools import run_readonly_tool
from abcxauto.trade_plan import (
    close_trade_plan,
    plan_from_hunt_action,
    save_trade_plan,
    sync_open_risk,
)
from abcxauto.world_state import (
    STANCES,
    WorldState,
    build_world_state,
    idle_streak_threshold,
    load_idle_streak,
    update_idle_streak_after_judgment,
)

logger = logging.getLogger(__name__)

# Option multi-leg / lifecycle (executor already knows these; Act must allow).
_OPTION_ENTRY_ACTIONS = (
    "vertical_spread|iron_condor|iron_butterfly|butterfly|straddle|strangle|"
    "calendar_spread|diagonal_spread|buy_option|cash_secured_put|"
    "ratio_spread|jade_lizard"
)
VALID_ACTIONS = (
    "hold|set_risk|bracket|market_bracket|market_order|limit_order|"
    "stop_order|stop_limit|oca|modify_stop|modify_target|cancel_order|"
    "close_option|roll_option|trailing_stop|trailing_stop_limit|"
    "market_on_close|limit_on_close|market_on_open|limit_on_open|"
    "covered_call|protective_put|collar|"
    + _OPTION_ENTRY_ACTIONS
)
ALLOWED_ACTIONS = frozenset(VALID_ACTIONS.split("|"))
BLOCKED_STRAT = "blocked"
_HUNT_OPTION_ENTRIES = frozenset(_OPTION_ENTRY_ACTIONS.split("|"))
AWARENESS_HEART = (
    "\n=== AWARENESS ===\n"
    "Hold when protected or flat. Hold FORBIDDEN when unprotected STK.\n"
    "Close by conId only. Risk gates are hard. PnL is truth.\n"
    "Size each entry so stop risk fits max_risk_per_trade_pct. "
    "set_risk retunes knobs inside the operator posture envelope only.\n"
    "defined_risk_only (operator gate) rejects unlimited-risk option shapes.\n"
)
PROTECT_HOLD_RULES = (
    "PROTECT/HOLD: hold allowed when book is protected or flat. "
    "HOLD FORBIDDEN when unprotected STK - protect (oca/modify_stop) or exit "
    "by exact conId first. Stock entries need bracket/market_bracket with stop+target. "
    "Option entries use allowlisted multi-leg / CSP types (see ORDER EXAMPLES). "
    "market_order is EXIT-ONLY (target_conId). Use direction LONG|SHORT for stock. "
    "set_risk adjusts capital knobs (not risk_posture)."
)
TWEAKS: Dict[str, Any] = {"max_risk_pct": 0.5}
_HIST_KEYS = (
    "cycle", "pnl", "pnl_chg", "reality_pulse", "kahneman", "kahneman_trace",
    "order_lab", "order_suite", "retest", "lab_summary", "result", "inventory",
    "validation", "reasoning_chain", "impact",
)

STANCE_ACTIONS: dict[str, frozenset[str]] = {
    "protect": frozenset({
        "oca", "modify_stop", "modify_target", "cancel_order",
        "market_order", "limit_order", "stop_order", "stop_limit",
        "close_option", "roll_option", "trailing_stop", "trailing_stop_limit",
        "protective_put",
    }),
    "manage": frozenset({
        "modify_stop", "modify_target", "cancel_order", "hold",
        "oca", "market_order", "limit_order", "stop_order", "stop_limit",
        "close_option", "roll_option", "trailing_stop", "trailing_stop_limit",
        "market_on_close", "limit_on_close",
        "covered_call", "protective_put", "collar",
    }) | _HUNT_OPTION_ENTRIES,
    "hunt": frozenset({"bracket", "market_bracket", "set_risk"}) | _HUNT_OPTION_ENTRIES,
    "idle": frozenset({"hold"}),
}


def _build_rules() -> str:
    mandate = (get_config().trading_mandate or "")[:800]
    return (
        "ABCXAUTO Pro PAPER. Output ONLY valid JSON.\n"
        f"MANDATE:\n{mandate}\n\n{PROTECT_HOLD_RULES}\n"
        f"action AND strategy MUST be one of: {', '.join(sorted(ALLOWED_ACTIONS))}. "
        "noop aliases to hold. NEVER invent names.\n" + AWARENESS_HEART
    )


RULES = _build_rules()


def extract_kahneman(_act: dict | None = None) -> dict[str, Any]:
    """Stub — Kahneman soft-gate removed from the hot path."""
    return {
        "system1_scan": "", "system2_base_rate": "", "debias": {},
        "pre_mortem": "", "alternatives": [], "bias_audit": [],
        "complete": False,
        "missing": ["system2_base_rate", "pre_mortem", "bias_audit"],
    }


def format_kahneman_trace(_k: dict | None = None) -> str:
    return "KAHNEMAN: (disabled)"


def expected_json_shape_hint() -> str:
    return (
        'ACT JSON: {"action":"...","strategy":"...","params":{},'
        '"rationale":"why this action fulfills Judgment intent",'
        '"target_conId":"..."} — must satisfy Judgment.intent; '
        "hold when stance=idle; hold FORBIDDEN when unprotected STK. "
        "bracket/market_bracket/oca use direction LONG|SHORT."
    )


def parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return {}


def pnl_of(acct: dict) -> float:
    for k in ("unrealizedpnl", "dailypnl", "netliquidation"):
        try:
            if acct.get(k) is not None:
                return float(acct[k])
        except (TypeError, ValueError):
            pass
    return 0.0


def equity_of(acct: dict) -> float:
    for k in ("netliquidation", "NetLiquidation"):
        try:
            if acct.get(k) is not None:
                return float(acct[k])
        except (TypeError, ValueError):
            pass
    return 0.0


def apply_tweak(tw: dict) -> str:
    if tw.get("type") == "config" and tw.get("config"):
        TWEAKS.update(tw["config"])
    return tw.get("summary", str(tw))


def normalize_action(act: dict) -> tuple[str, dict | None]:
    strat = (act.get("strategy") or act.get("action") or "").strip().lower()
    if strat in ("noop", "none", ""):
        strat = "hold"
    if strat not in ALLOWED_ACTIONS:
        return BLOCKED_STRAT, {
            "status": "blocked",
            "note": f"invalid strategy {strat!r} - not in allowlist",
        }
    return strat, None


def risk_label(snap: dict) -> str:
    bad = (snap.get("protection") or {}).get("unprotected_symbols") or []
    return "COMPLIANT" if not bad else f"UNPROTECTED: {', '.join(bad)}"


def format_position_inventory(positions: list) -> str:
    if not positions:
        return "LIVE POSITION LEDGER: (none)\n"
    lines = ["LIVE POSITION LEDGER:"]
    for p in positions:
        con = p.get("conId") or p.get("con_id") or "?"
        sym = p.get("symbol", "?")
        sec = p.get("secType") or p.get("sec_type") or "STK"
        qty = p.get("quantity", p.get("position", 0))
        try:
            qty_s = f"{float(qty):+g}" if qty is not None else "?"
        except (TypeError, ValueError):
            qty_s = str(qty)
        line = f"conId={con} | {sym} {sec} | pos={qty_s}"
        if str(sec).upper().startswith("OPT"):
            exp = p.get("expiration") or p.get("lastTradeDateOrContractMonth") or ""
            line += f" | expiry={exp} strike={p.get('strike', '')} right={p.get('right', '')}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def validate_action_against_inventory(action: dict, positions: list) -> tuple:
    if not action:
        return True, "validated"
    strat = (action.get("strategy") or action.get("action") or "").lower()
    params = action.get("params") or {}
    target = str(
        action.get("target_conId") or params.get("conId") or params.get("con_id") or ""
    ).strip()
    is_close = any(k in strat for k in ("close", "flatten", "market_order", "sell", "limit_order"))
    if not is_close:
        return True, "validated"
    if not target:
        return False, "must specify exact target_conId (never close by symbol alone)"
    matching = [p for p in positions if str(p.get("conId") or p.get("con_id") or "") == target]
    if not matching:
        return False, f"target_conId={target} not found in live ledger"
    m_sec = str(matching[0].get("sec_type") or matching[0].get("secType") or "STK").upper()
    is_opt = "close_option" in strat
    is_stk = not is_opt and any(k in strat for k in ("market", "sell", "flatten", "close", "limit"))
    if is_stk and not m_sec.startswith("STK"):
        return False, f"target_conId={target} is {m_sec}, not STK; use close_option"
    if is_opt and not m_sec.startswith("OPT"):
        return False, f"target_conId={target} is {m_sec}, not OPT"
    return True, "validated (exact conId match)"


def simulate_close_impact(action: dict, positions: list) -> dict:
    ok, msg = validate_action_against_inventory(action, positions)
    target = str(
        (action or {}).get("target_conId")
        or ((action or {}).get("params") or {}).get("conId") or ""
    ).strip()
    touched = [p for p in positions if str(p.get("conId") or p.get("con_id") or "") == target]
    untouched = [p for p in positions if str(p.get("conId") or p.get("con_id") or "") != target]
    return {
        "ok": ok, "message": msg, "target_conId": target,
        "would_zero": [
            {"conId": p.get("conId") or p.get("con_id"), "symbol": p.get("symbol"),
             "sec_type": p.get("sec_type") or p.get("secType"), "from_qty": p.get("quantity")}
            for p in touched
        ],
        "untouched_conIds": [p.get("conId") or p.get("con_id") for p in untouched],
        "gate": (
            f"After this order the target conId={target or '?'} position goes to "
            "exactly zero. No other positions are touched."
            if ok else f"REJECTED: {msg}"
        ),
    }


async def _tool(c: Any, n: str, a: dict | None = None) -> Any:
    return json.loads(await run_readonly_tool(n, a or {}, c))


async def snap(c: Any) -> dict:
    acct, pos, orders, hours, spy = await asyncio.gather(
        _tool(c, "account_summary"),
        _tool(c, "positions"),
        _tool(c, "open_orders"),
        _tool(c, "market_hours"),
        _tool(c, "quote", {"symbol": "SPY"}),
    )
    try:
        vix = await _tool(c, "quote", {"symbol": "VIX"})
    except Exception:
        vix = {}
    pl = pos if isinstance(pos, list) else []
    ol = orders if isinstance(orders, list) else []
    taken = datetime.now(timezone.utc).isoformat()
    protection = build_protection_report(pl, ol)
    base = {
        "taken_at": taken,
        "account": acct if isinstance(acct, dict) else {},
        "positions": pl, "open_orders": ol,
        "market_hours": hours if isinstance(hours, dict) else {},
        "spy_quote": spy if isinstance(spy, dict) else {},
        "vix_quote": vix if isinstance(vix, dict) else {},
        "protection": protection,
    }
    base["reality_pulse"] = build_reality_pulse(
        account=base["account"], positions=pl, open_orders=ol,
        market_hours=base["market_hours"], spy_quote=base["spy_quote"],
        vix_quote=base["vix_quote"], protection=protection,
        ibkr_connected=bool(getattr(c, "connected", True)), taken_at=taken,
    )
    base["portfolio_state"] = build_book_from_snap(base)
    return base


def _judge_system() -> str:
    mandate = (get_config().trading_mandate or "")[:600]
    return (
        "ABCXAUTO portfolio owner — Judge stage. "
        "You own the book under hard risk code. Output ONLY valid JSON. No orders.\n"
        f"MANDATE:\n{mandate}\n"
        f"{QUOTE_SOURCES_BLOCK}\n"
        "WORLDSTATE + SCAN TAPE are code facts. Tape is unranked MDA (delayed). "
        "You pick hunt symbols — shell does not recommend a top idea.\n"
        "stances: protect | manage | hunt | idle "
        "(what you intend with the book this cycle — not a cost budget).\n"
        "HARD GATES (code): unprotected STK → protect before new risk; "
        "halt blocks entries; capacity/sizing; flat unconfirmed → no new risk. "
        "Open book does NOT forbid hunt when capacity slots remain — multitask.\n"
        "Prefer open-book work when safety Facts broken (unprotected / stop qty). "
        "Optional secondary_intent when multitasking "
        "(e.g. stance=manage + secondary_intent hunt under capacity).\n"
        "To fetch more MDA metrics: set scan_request.symbols (max cap; ticker regex). "
        "Or finalize stance/intent from the tape already present.\n"
        "idle while flat + tape + balanced/aggressive REQUIRES dismissed citing "
        "any tape symbol and why rejected.\n"
        "hunt intent.symbol MUST be on the SCAN TAPE (seed ∪ fetched).\n"
        "Affirm, revise, or close working_thesis.\n"
        "Model API cost and long-run ROI goal are NOT control signals — own the book.\n"
        'JSON: {"stance":"...","thesis":"1-3 sentences","focus":"what mattered",'
        '"dismissed":"why tape symbols rejected (required for idle when tape present)",'
        '"scan_request":{"symbols":[],"reason":""},'
        '"intent":{"kind":"protect|manage|hunt|idle","symbol":null,'
        '"direction":null,"urgency":"low|med|high"},'
        '"secondary_intent":null,'
        '"risk_budget_pct":0.5,"regime_fit":true,"setup_grade":"A|B|C"}'
    )


def _act_system() -> str:
    return (
        _build_rules()
        + "\nYou are ACT. Fulfill Judgment with ONE allowlisted action. "
        "Always decide — hold is valid when the book is protected and nothing "
        "meets the bar; hold is forbidden only while unprotected STK exists "
        "(code enforces). Do not thrift-skip thinking for model cost.\n"
        f"{QUOTE_SOURCES_BLOCK}\n"
        "Hunt structure: use IBKR live last (price_hint / ibkr_live_last) for stock "
        "brackets. Do not size stops from MDA tape last.\n"
        "Do not contradict stance. idle → hold only. "
        "hunt → bracket/market_bracket, set_risk, or allowlisted option entries "
        "(vertical/iron/CSP/…). "
        "protect → oca/modify_*/protective_put/roll_option/close_option/exit by conId. "
        "manage → hold/trail/modify/exit/roll/close_option, overlays "
        "covered_call|collar|protective_put when long shares allow, or option "
        "structures (see TRADE PLAYBOOK + ORDER EXAMPLES).\n"
        + expected_json_shape_hint()
    )


async def grok(g: GrokClient, p: str, *, stage: str = "act") -> str:
    sys_msg = _judge_system() if stage == "judge" else _act_system()
    chat = g.client.chat.create(
        model=g.model,
        messages=[system(sys_msg + "\nTWEAKS=" + json.dumps(TWEAKS)), user(p)],
        temperature=g.temperature,
        max_tokens=min(2048, g.max_tokens),
    )
    o = ""
    async for _, ch in chat.stream():
        if ch.content:
            o += ch.content
    return o


def _prepare_close_params(act: dict, positions: list) -> None:
    """Fill close identity; default quantity to full held; clamp oversize trims."""
    strat = (act.get("strategy") or act.get("action") or "").lower()
    if not any(k in strat for k in ("close", "market_order", "limit_order", "sell")):
        return
    target = str(
        act.get("target_conId")
        or (act.get("params") or {}).get("conId")
        or (act.get("params") or {}).get("con_id") or ""
    ).strip()
    if not target:
        m = re.search(
            r"conId\s*=\s*(\S+)",
            f"{act.get('rationale') or ''} {act.get('reasoning_chain') or ''} {act.get('params') or ''}",
        )
        if m:
            target = m.group(1).rstrip(").,;")
            act["target_conId"] = target
    if not target:
        return
    act.setdefault("params", {})
    act["params"]["conId"] = target
    act["params"]["closing_position"] = True
    match = next(
        (p for p in positions if str(p.get("conId") or p.get("con_id") or "") == target), None
    )
    if not match:
        return
    qty = match.get("quantity", match.get("position", 0)) or 0
    try:
        held = abs(int(float(qty)))
    except (TypeError, ValueError):
        held = 0
    # Partial trim: Grok may pass quantity < held; omit → full close.
    if held and not act["params"].get("quantity"):
        act["params"]["quantity"] = held
    else:
        try:
            want = int(float(act["params"].get("quantity") or 0))
        except (TypeError, ValueError):
            want = 0
        if held and want > held:
            act["params"]["quantity"] = held
    act["params"].setdefault("symbol", match.get("symbol"))
    if "market_order" in strat or strat in ("sell", "flatten"):
        try:
            signed = float(qty)
        except (TypeError, ValueError):
            signed = 0.0
        act["params"].setdefault("action", "SELL" if signed > 0 else "BUY")


def _extract_scan_request(judgment: dict) -> list[str]:
    raw = (judgment or {}).get("scan_request")
    if isinstance(raw, dict):
        return normalize_tickers(raw.get("symbols"))
    if isinstance(raw, list):
        return normalize_tickers(raw)
    return []


def validate_judgment(judgment: dict, world: WorldState) -> tuple[bool, str, dict]:
    """Fail closed. Returns (ok, reason, maybe_patched_judgment)."""
    j = dict(judgment or {})
    stance = str(j.get("stance") or "").strip().lower()
    if stance not in STANCES:
        return False, f"invalid or missing stance {stance!r}", j
    thesis = str(j.get("thesis") or "").strip()
    focus = str(j.get("focus") or "").strip()
    if not thesis or not focus:
        return False, "judgment requires thesis and focus", j
    intent = j.get("intent")
    if not isinstance(intent, dict):
        return False, "judgment.intent must be an object", j
    kind = str(intent.get("kind") or "").strip().lower()
    if not kind:
        return False, "judgment.intent.kind required", j
    j["stance"] = stance
    j["thesis"] = thesis
    j["focus"] = focus
    j["dismissed"] = str(j.get("dismissed") or "").strip()
    j["setup_grade"] = str(j.get("setup_grade") or "B").upper()[:1] or "B"
    if j["setup_grade"] not in ("A", "B", "C"):
        j["setup_grade"] = "B"
    try:
        j["risk_budget_pct"] = float(j.get("risk_budget_pct") or 0)
    except (TypeError, ValueError):
        j["risk_budget_pct"] = 0.0

    if world.needs_protection:
        if stance in ("idle", "hunt"):
            return False, "unprotected STK — stance must be protect (idle/hunt forbidden)", j
        j["stance"] = "protect"
        stance = "protect"

    from abcxauto.mega_worker import capacity_allows_new_risk
    from abcxauto.trade_plan import load_flat_streak

    # Four hard gates only for new risk — open book is NOT a hunt ban.
    if stance == "hunt":
        if load_flat_streak() > 0:
            return False, "book flat unconfirmed — wait before new risk", j
        if not capacity_allows_new_risk(world):
            return False, "capacity full — no new risk (max_open_positions)", j

    # Optional secondary_intent (fluid dual-intent under capacity)
    sec = j.get("secondary_intent")
    if sec is not None and sec != {} and not isinstance(sec, dict):
        return False, "secondary_intent must be object or null", j
    if isinstance(sec, dict) and sec:
        sec_kind = str(sec.get("kind") or "").strip().lower()
        if sec_kind and sec_kind not in STANCES:
            return False, f"invalid secondary_intent.kind {sec_kind!r}", j
        if sec_kind == "hunt":
            if world.needs_protection:
                return False, "unprotected — secondary hunt forbidden", j
            if load_flat_streak() > 0:
                return False, "flat unconfirmed — secondary hunt forbidden", j
            if not capacity_allows_new_risk(world):
                return False, "capacity full — secondary hunt forbidden", j
            from abcxauto.universe import is_legal_symbol

            sec_sym_early = str(sec.get("symbol") or "").upper()
            if sec_sym_early and not is_legal_symbol(sec_sym_early):
                return False, f"secondary hunt {sec_sym_early} outside Universe sandbox", j
        j["secondary_intent"] = sec
    else:
        j["secondary_intent"] = None

    posture = (world.effective_posture or world.risk_posture or "").lower()
    ideas = world.opportunities
    symbols = tape_symbols(ideas)
    # Validate secondary hunt symbol against tape when present
    if isinstance(j.get("secondary_intent"), dict):
        sec = j["secondary_intent"]
        if str(sec.get("kind") or "").lower() == "hunt":
            sec_sym = str(sec.get("symbol") or "").upper()
            if sec_sym and ideas and sec_sym not in symbols:
                return (
                    False,
                    f"secondary hunt symbol {sec_sym} not on SCAN TAPE",
                    j,
                )

    if (
        stance == "idle"
        and world.flat
        and world.session_status == "regular"
        and ideas
        and posture in ("balanced", "aggressive")
    ):
        dismissed = j.get("dismissed") or ""
        if not dismissed:
            return False, "idle requires dismissed citing a SCAN TAPE symbol", j
        if not dismiss_cites_tape(dismissed, ideas):
            return False, "idle dismissed must cite a SCAN TAPE symbol", j

    thresh = idle_streak_threshold(posture)
    idle_anchor = str(world.idle_top_symbol or "").upper()
    if (
        stance == "idle"
        and world.flat
        and ideas
        and int(world.idle_streak or 0) >= thresh
        and idle_anchor
        and idle_anchor in symbols
    ):
        prev = str(load_idle_streak().get("last_dismiss") or "").strip()
        cur = str(j.get("dismissed") or "").strip()
        if prev and cur and cur == prev:
            return False, "idle streak escalate — new dismiss reason or hunt", j

    if stance == "hunt":
        grade = j["setup_grade"]
        if posture == "defensive" and grade != "A":
            return False, "defensive posture requires setup_grade A to hunt", j
        if posture == "balanced" and grade == "C":
            return False, "balanced posture blocks setup_grade C hunts", j
        rf = j.get("regime_fit")
        if posture == "defensive" and rf in (False, "no", "false", "counter", 0, "0"):
            return False, "counter-regime hunt blocked under defensive", j
        sym = str(intent.get("symbol") or "").upper()
        if not sym:
            return False, "hunt requires intent.symbol on SCAN TAPE", j
        if ideas and sym not in symbols:
            return False, f"hunt symbol {sym} not on SCAN TAPE (MDA-validated)", j
        from abcxauto.universe import is_legal_symbol

        if not is_legal_symbol(sym):
            return False, f"hunt symbol {sym} outside Universe sandbox", j
        struct_cool = getattr(world, "structure_cooldown", None) or {}
        if sym in struct_cool:
            return (
                False,
                f"structure cooldown on {sym} ({struct_cool[sym]}) — "
                "hunt a different tape symbol or idle with dismissed citing a tape symbol",
                j,
            )

    open_thesis = (world.working_thesis or "").strip()
    if stance == "idle" and open_thesis and ideas and len(open_thesis) > 20:
        blob = f"{thesis} {focus} {j.get('dismissed') or ''}".upper()
        if (
            "REVISE" not in blob
            and "CLOSE" not in blob
            and "AFFIRM" not in blob
            and not dismiss_cites_tape(blob, ideas)
            and "THESIS" not in blob
        ):
            return False, "idle must address open thesis or a SCAN TAPE symbol", j

    return True, "ok", j


def check_intent_coherence(
    judgment: dict, strat: str, act: dict
) -> tuple[bool, str]:
    stance = str(judgment.get("stance") or "").lower()
    intent = judgment.get("intent") if isinstance(judgment.get("intent"), dict) else {}
    secondary = (
        judgment.get("secondary_intent")
        if isinstance(judgment.get("secondary_intent"), dict)
        else {}
    )
    stream = str((act or {}).get("_stream") or "").lower()
    # Stream / secondary_intent may fulfill hunt while primary stance is manage.
    effective = stance
    use_intent = intent
    if stream in ("new_risk", "escapade") or (
        strat in (frozenset({"bracket", "market_bracket"}) | _HUNT_OPTION_ENTRIES)
        and stance != "hunt"
        and str(secondary.get("kind") or "").lower() == "hunt"
    ):
        effective = "hunt"
        if secondary.get("kind") == "hunt":
            use_intent = secondary
    elif stream == "open_risk" and stance == "hunt":
        effective = "manage"
    allowed = STANCE_ACTIONS.get(effective, frozenset())
    if strat not in allowed:
        # Also accept if allowed under primary stance (dual-intent)
        primary_ok = strat in STANCE_ACTIONS.get(stance, frozenset())
        sec_stance = str(secondary.get("kind") or "").lower()
        sec_ok = sec_stance and strat in STANCE_ACTIONS.get(sec_stance, frozenset())
        if not (primary_ok or sec_ok):
            return False, f"act {strat!r} contradicts stance {stance!r}"
        if sec_ok and not primary_ok:
            effective = sec_stance
            use_intent = secondary
    from abcxauto.structure_complexity import strategy_allowed

    if strat and strat not in ("blocked", "hold") and not strategy_allowed(strat):
        return False, f"structure complexity dial blocks strategy {strat!r}"
    hunt_sym_strats = frozenset({"bracket", "market_bracket"}) | _HUNT_OPTION_ENTRIES
    if effective == "hunt" and strat in hunt_sym_strats:
        want_sym = str(use_intent.get("symbol") or intent.get("symbol") or "").upper()
        got_sym = str(((act.get("params") or {}).get("symbol") or "")).upper()
        if want_sym and got_sym and want_sym != got_sym:
            return False, f"intent symbol {want_sym} != act {got_sym}"
        if strat in ("bracket", "market_bracket"):
            want_dir = str(
                use_intent.get("direction") or intent.get("direction") or ""
            ).upper()
            got_dir = str(((act.get("params") or {}).get("direction") or "")).upper()
            if want_dir and got_dir and want_dir != got_dir:
                return False, f"intent direction {want_dir} != act {got_dir}"
    return True, "ok"


def check_risk_budget(
    judgment: dict, act: dict, net_liq: float, gates: dict | None = None
) -> tuple[bool, str]:
    strat = str(act.get("strategy") or act.get("action") or "").lower()
    if strat not in ("bracket", "market_bracket"):
        return True, "n/a"
    params = act.get("params") or {}
    try:
        qty = float(params.get("quantity") or 0)
        entry = float(params.get("entry_price") or 0)
        stop = float(params.get("stop_price") or 0)
    except (TypeError, ValueError):
        return True, "n/a"
    if qty <= 0 or entry <= 0 or stop <= 0 or net_liq <= 0:
        return True, "n/a"
    risk_dollars = abs(entry - stop) * qty
    risk_pct = 100.0 * risk_dollars / float(net_liq)
    try:
        budget = float(judgment.get("risk_budget_pct") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    gate_max = None
    if gates:
        try:
            gate_max = float(gates.get("max_risk_per_trade_pct") or 0)
        except (TypeError, ValueError):
            gate_max = None
    cap = budget if budget > 0 else (gate_max or 0)
    if cap > 0 and risk_pct > cap * 1.05:
        return False, f"size risk {risk_pct:.2f}% > budget {cap:.2f}%"
    return True, "ok"


def _build_judge_prompt(world: WorldState, *, finalize: bool = False) -> str:
    pressure = ""
    posture = (world.effective_posture or "").lower()
    syms = ", ".join(tape_symbols(world.opportunities)[:12]) or "(empty)"
    from abcxauto.mega_worker import capacity_allows_new_risk, safety_facts_broken

    cap = getattr(world, "capacity", None) or {}
    cap_note = cap.get("note") or ""
    if world.needs_protection:
        pressure = "GATE: unprotected STK — stance MUST be protect (code)."
    elif safety_facts_broken(world):
        pressure = (
            "GATE: safety Facts broken (stop qty / protection) — "
            "prefer open-risk manage/protect Act; new-risk only after safety."
        )
    elif not capacity_allows_new_risk(world):
        pressure = (
            f"GATE: capacity full ({cap_note}) — no new-risk hunt (code). "
            "Open-risk manage/protect only."
        )
    elif world.trade_plan or getattr(world, "trade_plans", None):
        pressure = (
            f"FACT: open book + capacity ({cap_note}). "
            "Multitask OK — manage open-risk and/or hunt new-risk under capacity. "
            "Optional secondary_intent. scan_request allowed when capacity remains."
        )
    elif (
        world.flat
        and world.session_status == "regular"
        and world.opportunities
        and posture in ("balanced", "aggressive")
    ):
        pressure = (
            f"PROCESS: flat + SCAN TAPE present (posture={posture}). "
            "You operate the scanner. idle REQUIRES dismissed citing any tape "
            f"symbol among [{syms}]. Or stance=hunt with intent.symbol on tape "
            "and setup_grade. Optional scan_request for more MDA symbols "
            "(skipped if finalize pass)."
        )
    if finalize:
        pressure += (
            " PROCESS: finalize pass — scan_request ignored; set stance/intent now."
        )
    thresh = idle_streak_threshold(posture)
    if world.idle_streak >= thresh and world.opportunities:
        pressure += (
            f" PROCESS: IDLE STREAK={world.idle_streak} — cannot re-idle with "
            "same dismiss; new reason or hunt."
        )
    cool = getattr(world, "structure_cooldown", None) or {}
    if cool:
        pressure += (
            f" GATE: STRUCTURE COOLDOWN (do not re-hunt): {cool}. "
            "Different tape symbol or idle with dismissed citing a tape symbol."
        )
    fetched = getattr(world, "scan_fetched", None) or []
    if fetched:
        pressure += f" PROCESS: MDA fetch this cycle: {fetched}."
    from abcxauto.world_state import hunt_cooldown_remaining

    soft = []
    for idea in (world.opportunities or [])[:8]:
        sym = str(idea.get("symbol") or "").upper()
        if not sym:
            continue
        n = hunt_cooldown_remaining(world.recent_decisions, sym)
        if n > 0:
            soft.append(f"{sym}({n})")
    if soft:
        pressure += (
            f" PROCESS: soft recent-entry on {', '.join(soft)} — not a hard block."
        )
    from abcxauto.config import (
        format_controls_block,
        format_operator_card_block,
        get_config,
    )
    from abcxauto.trade_playbook import format_trade_playbook, world_hints_from_world

    playbook = format_trade_playbook(
        "",
        world_hints_from_world(world),
        for_judge=True,
    )
    cfg = get_config()
    controls = format_controls_block(cfg)
    card = format_operator_card_block(getattr(cfg, "operator_card", None))
    card_bit = f"\n\n{card}" if card else ""
    return (
        f"=== JUDGE STAGE ===\nCycle {world.cycle}.\n{pressure}\n\n"
        f"{world.prompt_block()}\n\n"
        f"{controls}\n\n"
        f"{playbook}{card_bit}\n\n"
        f"Open working_thesis: {(world.working_thesis or '-')[:300]}\n"
        "Affirm, revise, or close it in thesis. "
        "For manage overlays (covered_call/collar/put), say so in focus.\n"
        "Output judgment JSON only."
    )


def _build_act_prompt(
    world: WorldState,
    judgment: dict,
    *,
    stream: str = "",
) -> str:
    from abcxauto.structure_grade import format_structure_lessons_for_prompt
    from abcxauto.trade_playbook import format_trade_playbook, world_hints_from_world
    from abcxauto.mega_worker import stream_act_prompt_suffix

    lessons = format_structure_lessons_for_prompt(
        getattr(world, "structure_lessons", None)
    )
    vocab = getattr(world, "structure_vocab", None) or {}
    vocab_bit = ""
    if vocab:
        vocab_bit = (
            f"SUITE TRAINER: pass_rate={vocab.get('pass_rate')} "
            f"failed={vocab.get('failed')}\n"
        )
    stance = str((judgment or {}).get("stance") or "").lower()
    # Stream may override effective stance for playbook allowlist
    stream_stance = stance
    if stream == "open_risk" and stance == "hunt":
        stream_stance = "manage"
    elif stream in ("new_risk", "escapade"):
        stream_stance = "hunt"
    playbook = format_trade_playbook(stream_stance, world_hints_from_world(world))
    from abcxauto.config import (
        format_controls_block,
        format_operator_card_block,
        get_config,
    )

    cfg = get_config()
    controls = format_controls_block(cfg)
    card = format_operator_card_block(getattr(cfg, "operator_card", None))
    card_bit = f"{card}\n\n" if card else ""
    ibkr_sym = str(getattr(world, "ibkr_live_symbol", "") or "")
    ibkr_last = getattr(world, "ibkr_live_last", None)
    ibkr_bit = ""
    if ibkr_last is not None and ibkr_sym:
        ibkr_bit = (
            f"IBKR LIVE (source=ibkr freshness=live): {ibkr_sym} last={ibkr_last}\n"
            "Use this for price_hint / stop / target — not MDA tape last.\n"
        )
    stream_bit = ""
    if stream:
        stream_bit = stream_act_prompt_suffix(stream, world=world) + "\n"
    return (
        f"=== ACT STAGE ===\nCycle {world.cycle}.\n"
        f"{stream_bit}"
        f"{QUOTE_SOURCES_BLOCK}\n"
        f"{ibkr_bit}"
        f"JUDGMENT:\n{json.dumps(judgment, default=str)[:2000]}\n\n"
        f"{world.prompt_block(limit=2800)}\n\n"
        f"{controls}\n\n"
        f"{lessons}\n{vocab_bit}"
        "You OWN structure: pick order type + stop/target/qty from IBKR LIVE quote "
        "(price_hint = ibkr last). Never reuse a prior stop if last moved. "
        "Do not use MDA delayed tape last for geometry. "
        "LONG: stop < live < target. Shell rejects wrong-side geometry.\n"
        f"{format_order_examples()}\n\n"
        f"{playbook}\n\n"
        f"{card_bit}"
        f"{format_position_inventory(world.positions)}\n"
        f"{expected_json_shape_hint()}\n"
        "Emit ONE action that fulfills intent. Include price_hint when hunting "
        "(live last). No contradictions."
    )


async def _run_act_streams(
    g: GrokClient,
    world: WorldState,
    judgment: dict,
    *,
    needs_prot: bool = False,
) -> dict:
    """One Act per cycle. Stream label is prompt focus only — not a branch tree."""
    from abcxauto.mega_worker import primary_stream

    stream = primary_stream(judgment, world, needs_prot=needs_prot)
    j_use = dict(judgment or {})
    # Keep Judgment as Grok wrote it; only tag focus for the Act prompt.
    raw = await grok(
        g, _build_act_prompt(world, j_use, stream=stream), stage="act"
    )
    act = parse_json(raw) or {
        "action": "hold",
        "strategy": "hold",
        "rationale": f"empty_act:{stream}",
    }
    act["_stream"] = stream
    return act


def _result_dict(
    *, n: int, s: dict, act: dict, strat: str, result: dict,
    pnl: float, eq: float, prev: float, inventory: str, validation: str,
    kahneman: dict, impact: dict | None = None,
    judgment: dict | None = None,
    world: dict | None = None,
    stage_error: str = "",
) -> dict:
    positions = s.get("positions") or []
    cfg = get_config()
    ideas = list(s.get("opportunities") or [])
    news_items = list(s.get("news_items") or [])
    j = judgment or {}
    market_read = str(
        j.get("focus")
        or (act or {}).get("market_read")
        or (act or {}).get("read")
        or ""
    ).strip()
    rationale = str(
        (act or {}).get("rationale") or j.get("thesis") or ""
    ).strip()
    return {
        "cycle": n, "pnl": pnl, "pnl_chg": pnl - prev, "equity": eq,
        "strat": strat, "result": result, "tweak": "none", "tweak_obj": {},
        "risk": risk_label(s),
        "portfolio": f"{len(positions)} positions | {len(s.get('open_orders') or [])} orders",
        "portfolio_state": s.get("portfolio_state") or {},
        "positions": positions, "open_orders": s.get("open_orders") or [],
        "protection": s.get("protection") or {},
        "unprotected": list((s.get("protection") or {}).get("unprotected_symbols") or []),
        "action_obj": act, "rationale": rationale,
        "market_read": market_read,
        "taken_at": s.get("taken_at") or "", "inventory": inventory,
        "validation": validation,
        "reasoning_chain": market_read or rationale or (act or {}).get("reasoning_chain") or "",
        "tweak_before": {}, "impact": impact or {},
        "reality_pulse": s.get("reality_pulse") or {},
        "kahneman": kahneman, "kahneman_trace": format_kahneman_trace(kahneman),
        "order_lab": {}, "order_suite": {}, "lab_summary": "",
        "retest": {}, "reconfig": {},
        "opportunities": ideas[:5],
        "news_items": [
            {
                "symbol": it.get("symbol"),
                "headline": str(it.get("headline") or "")[:160],
            }
            for it in news_items[:12]
            if it.get("headline")
        ],
        "risk_posture": getattr(cfg, "risk_posture", "") or "",
        "params": (act.get("params") or {}) if isinstance(act, dict) else {},
        "judgment": j,
        "world_state": world or {},
        "stance": j.get("stance") or "",
        "thesis": j.get("thesis") or "",
        "dismissed": j.get("dismissed") or "",
        "intent": j.get("intent") or {},
        "stage_error": stage_error,
        "trade_plan": (world or {}).get("trade_plan"),
        "regime": (world or {}).get("regime") or {},
        "portfolio_risk": (world or {}).get("portfolio_risk") or {},
        "structure_grade": (act or {}).get("_structure_grade") or "",
        "structure_lessons": (world or {}).get("structure_lessons") or [],
        "ibkr_live_last": (world or {}).get("ibkr_live_last"),
        "ibkr_live_symbol": (world or {}).get("ibkr_live_symbol") or "",
        "scan_fetched": list((world or {}).get("scan_fetched") or []),
    }


def _journal_stages(
    out: dict, act: dict, s: dict, judgment: dict | None
) -> None:
    try:
        journal = get_journal()
        strat = out.get("strat")
        result = out.get("result") or {}
        j = judgment or {}
        if j:
            journal.record_judgment(
                cycle=out.get("cycle"),
                stance=str(j.get("stance") or ""),
                thesis=str(j.get("thesis") or ""),
                focus=str(j.get("focus") or ""),
                dismissed=str(j.get("dismissed") or ""),
                intent=j.get("intent") or {},
                judgment=j,
            )
            stance = str(j.get("stance") or "").lower()
            if stance and stance != "idle":
                thesis = str(j.get("thesis") or "").strip()
                if thesis:
                    journal.set_working_thesis(thesis)
        journal.record_decision(
            cycle=out.get("cycle"),
            action=act.get("action") or strat,
            strategy=strat,
            rationale=out.get("rationale") or act.get("rationale") or "",
            portfolio_snapshot=s.get("portfolio_state"),
            outcome={
                **(result if isinstance(result, dict) else {"raw": result}),
                "stance": j.get("stance"),
                "judgment": j,
            },
        )
    except Exception as exc:
        logger.warning("agent_loop: journal stages failed: %s", exc)


def _should_skip_act(
    judgment: dict, world: WorldState, needs_prot: bool
) -> bool:
    """Retired thrift path. Always run Act — ROI/model-cost is not a control signal.

    Kept as a named function so tests/call sites stay stable; always False.
    """
    return False


def _maybe_eod_review(world: WorldState, judgment: dict | None, strat: str) -> None:
    try:
        phase = str((world.regime or {}).get("session_phase") or "")
        if phase != "close":
            return
        maybe_auto_review_from_cycle(
            {
                "end_of_day": True,
                "thesis": (judgment or {}).get("thesis") or world.working_thesis,
                "what_worked": (judgment or {}).get("focus") or "",
                "mistake": (judgment or {}).get("dismissed") or "",
                "next_change": f"last_act={strat}",
            }
        )
    except Exception:
        logger.exception("eod review failed")


async def run_cycle(
    n: int,
    c: Any,
    g: GrokClient,
    h: List[dict],
    prev: float,
) -> dict:
    """Perceive → Judge → Act → hard gates/send → journal.

    Straight ownership loop — not a skip/merge decision tree.
    Shell does not invent stance. Act always runs after a valid Judge.
    Model cost / long-run ROI is a scorecard goal, never a cycle control.
    """
    s = await snap(c)
    positions = s.get("positions") or []
    for p in positions:
        if "conId" not in p and "con_id" in p:
            p["conId"] = p["con_id"]
    pulse = s.get("reality_pulse") or {}
    acct = s.get("account") or {}
    pnl, eq = pnl_of(acct), equity_of(acct)
    inventory = format_position_inventory(positions)
    session = str((pulse.get("session") or {}).get("status") or "").lower()
    needs_prot = bool((s.get("protection") or {}).get("unprotected_symbols"))

    if session != "regular" and not needs_prot:
        act = {
            "action": "skipped", "strategy": "skipped",
            "rationale": "skipped_grok: session_closed",
        }
        out = _result_dict(
            n=n, s=s, act=act, strat="skipped",
            result={"status": "skipped", "note": "skipped_grok: session_closed"},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation="skipped_grok: session_closed", kahneman=extract_kahneman(act),
        )
        _journal_stages(out, act, s, None)
        h.append({"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return out

    # Open-risk continuity from broker book (Fact)
    try:
        thesis_hint = ""
        try:
            thesis_hint = get_journal().get_working_thesis() or ""
        except Exception:
            thesis_hint = ""
        sync_open_risk(
            positions,
            s.get("open_orders") or [],
            thesis=thesis_hint,
            bump=bool(positions),
        )
    except Exception:
        logger.exception("open risk sync failed")

    try:
        from abcxauto.news_feed import fetch_agent_news, format_news_for_prompt
        news_items = await fetch_agent_news(s.get("positions") or [])
        s["news_prompt"] = format_news_for_prompt(news_items)
        s["news_items"] = news_items
    except Exception:
        s["news_prompt"] = ""
        s["news_items"] = []

    try:
        from abcxauto.option_facts import fetch_option_facts

        s["option_facts"] = await fetch_option_facts(positions)
    except Exception:
        logger.debug("option_facts fetch failed", exc_info=True)
        s["option_facts"] = []

    try:
        ideas = await scan_opportunities(positions)
    except Exception:
        logger.exception("opportunity scan failed")
        ideas = []
    s["opportunities"] = ideas

    world = build_world_state(
        cycle=n, snap=s, opportunities=ideas, news_items=s.get("news_items") or [],
    )
    world_dict = world.to_dict()

    # --- JUDGE (optional propose → MDA fetch → finalize) ---
    try:
        raw_j = await grok(g, _build_judge_prompt(world), stage="judge")
        judgment = parse_json(raw_j) or {}
    except Exception as exc:
        logger.exception("judge failed")
        judgment = {}
        stage_err = f"judge_error: {exc}"
        act = {"action": BLOCKED_STRAT, "strategy": BLOCKED_STRAT, "rationale": stage_err}
        out = _result_dict(
            n=n, s=s, act=act, strat=BLOCKED_STRAT,
            result={"status": "blocked", "note": stage_err},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation=stage_err, kahneman=extract_kahneman(act),
            judgment={}, world=world_dict, stage_error=stage_err,
        )
        _journal_stages(out, act, s, None)
        h.append({"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return out

    scan_syms = _extract_scan_request(judgment)
    from abcxauto.mega_worker import capacity_allows_new_risk
    from abcxauto.universe import filter_to_legal

    scan_syms = filter_to_legal(scan_syms)
    allow_scan = (
        bool(scan_syms)
        and not needs_prot
        and capacity_allows_new_risk(world)
        and session == "regular"
    )
    if allow_scan:
        try:
            extra = await fetch_scan_metrics(scan_syms)
            if extra:
                ideas = merge_tape(ideas, extra)
                s["opportunities"] = ideas
                world.opportunities = ideas
                world.scan_fetched = tape_symbols(extra)
                world_dict = world.to_dict()
                world_dict["scan_fetched"] = list(world.scan_fetched)
            raw_j2 = await grok(
                g, _build_judge_prompt(world, finalize=True), stage="judge"
            )
            judgment = parse_json(raw_j2) or judgment
            if isinstance(judgment, dict):
                judgment.pop("scan_request", None)
        except Exception:
            logger.exception("judge scan_request fetch/finalize failed")

    ok_j, jreason, judgment = validate_judgment(judgment, world)
    if not ok_j:
        act = {
            "action": BLOCKED_STRAT, "strategy": BLOCKED_STRAT,
            "rationale": f"judgment_rejected: {jreason}",
        }
        out = _result_dict(
            n=n, s=s, act=act, strat=BLOCKED_STRAT,
            result={"status": "blocked", "note": f"judgment_rejected: {jreason}"},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation=f"judgment_rejected: {jreason}",
            kahneman=extract_kahneman(act),
            judgment=judgment, world=world_dict, stage_error=jreason,
        )
        _journal_stages(out, act, s, judgment)
        h.append({"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return out

    update_idle_streak_after_judgment(judgment, world)

    # IBKR live quote for hunt symbol (geometry truth — not MDA tape)
    hunt_sym = ""
    if str(judgment.get("stance") or "").lower() == "hunt":
        intent = judgment.get("intent") if isinstance(judgment.get("intent"), dict) else {}
        hunt_sym = str(intent.get("symbol") or "").upper()
    if hunt_sym and c is not None:
        try:
            q = await _tool(c, "quote", {"symbol": hunt_sym})
            live = _extract_last(q if isinstance(q, dict) else None)
            if live is not None:
                world.ibkr_live_last = live
                world.ibkr_live_symbol = hunt_sym
                s["ibkr_live_last"] = live
                s["ibkr_live_symbol"] = hunt_sym
                world_dict = world.to_dict()
        except Exception:
            logger.debug("IBKR live quote for %s failed", hunt_sym, exc_info=True)

    # --- ACT (always — one focus stream; no thrift skip / multi-merge tree) ---
    try:
        act = await _run_act_streams(g, world, judgment, needs_prot=needs_prot)
        if not act:
            act = {"action": "hold", "strategy": "hold", "rationale": "empty_act"}
    except Exception as exc:
        logger.exception("act failed")
        act = {
            "action": BLOCKED_STRAT, "strategy": BLOCKED_STRAT,
            "rationale": f"act_error: {exc}",
        }
        out = _result_dict(
            n=n, s=s, act=act, strat=BLOCKED_STRAT,
            result={"status": "blocked", "note": f"act_error: {exc}"},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation=f"act_error: {exc}", kahneman=extract_kahneman(act),
            judgment=judgment, world=world_dict, stage_error=str(exc),
        )
        _journal_stages(out, act, s, judgment)
        h.append({"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return out

    strat, forced = normalize_action(act)
    if strat == "hold" and needs_prot:
        strat = BLOCKED_STRAT
        forced = {
            "status": "blocked",
            "note": "hold_forbidden - unprotected STK needs protection",
        }
        act["strategy"] = act["action"] = BLOCKED_STRAT

    # Intent coherence
    if forced is None and strat != BLOCKED_STRAT:
        ok_i, ireason = check_intent_coherence(judgment, strat, act)
        if not ok_i:
            strat = BLOCKED_STRAT
            forced = {"status": "blocked", "note": f"intent_mismatch: {ireason}"}
            act["strategy"] = act["action"] = BLOCKED_STRAT
            act["rationale"] = f"intent_mismatch: {ireason}"

    # Risk budget vs size
    if forced is None and strat != BLOCKED_STRAT:
        ok_b, breason = check_risk_budget(
            judgment, act, world.net_liquidation, world.gates
        )
        if not ok_b:
            strat = BLOCKED_STRAT
            forced = {"status": "blocked", "note": f"risk_budget: {breason}"}
            act["strategy"] = act["action"] = BLOCKED_STRAT

    # Overlay share-lot guard (covered_call / collar / protective_put)
    if forced is None and strat != BLOCKED_STRAT:
        from abcxauto.structure_grade import append_structure_event
        from abcxauto.trade_playbook import check_overlay_shares

        ok_sh, sh_code, sh_msg = check_overlay_shares(
            strat, act.get("params") or {}, positions
        )
        if not ok_sh:
            overlay_name = strat
            forced = {
                "status": "blocked",
                "note": sh_msg,
                "reason_code": sh_code,
            }
            act["_structure_grade"] = sh_code
            try:
                append_structure_event(
                    {
                        "source": "cycle",
                        "strategy": overlay_name,
                        "symbol": str((act.get("params") or {}).get("symbol") or ""),
                        "direction": "LONG",
                        "params": {
                            k: (act.get("params") or {}).get(k)
                            for k in ("symbol", "shares", "strike", "expiration")
                        },
                        "outcome": sh_code,
                        "reason_code": sh_code,
                        "message": sh_msg,
                    }
                )
            except Exception:
                pass
            strat = BLOCKED_STRAT
            act["strategy"] = act["action"] = BLOCKED_STRAT

    # Hold-streak: block serial hold after escalate threshold with same top opp
    posture = (world.effective_posture or "").lower()
    thresh = idle_streak_threshold(posture)
    if (
        forced is None
        and strat == "hold"
        and judgment.get("stance") == "idle"
        and world.flat
        and ideas
        and int(world.idle_streak or 0) >= thresh
    ):
        # streak already includes prior idles; this cycle's judgment was accepted
        # with new dismiss — allow hold. If streak high and we somehow got here
        # without new dismiss, validate_judgment already blocked.
        pass

    validation = "n/a"
    _prepare_close_params(act, positions)
    try:
        ok, vmsg = validate_action_against_inventory(act, positions)
        validation = f"{'ok' if ok else 'rejected'}: {vmsg}"
        if not ok and strat not in (BLOCKED_STRAT, "skipped", "set_risk"):
            forced, strat = {"status": "validated_block", "reason": vmsg}, BLOCKED_STRAT
    except Exception:
        pass

    impact = simulate_close_impact(act, positions)
    act["_live_positions"], act["_impact"] = positions, impact

    quote_last = await _quote_for_action(act, s, c)
    if quote_last is not None:
        act["_quote_last"] = quote_last
        params = act.setdefault("params", {})
        if isinstance(params, dict) and params.get("price_hint") is None:
            params["price_hint"] = quote_last
    act["_posture"] = world.effective_posture or world.risk_posture
    chosen_strat = strat

    if forced is None and strat in ("market_bracket", "oca", "bracket"):
        from abcxauto.structure_grade import (
            GEOMETRY_REJECTED,
            append_structure_event,
            check_live_geometry,
        )

        ok_g, code, gmsg = check_live_geometry(
            strat,
            act.get("params") or {},
            quote_last=quote_last,
            posture=str(act["_posture"] or "balanced"),
        )
        act["_structure_grade"] = code
        if not ok_g:
            forced = {
                "status": "rejected",
                "error": f"{code}: {gmsg}",
                "reason_code": code,
                "learn": gmsg,
            }
            strat = BLOCKED_STRAT
            act["strategy"] = act["action"] = BLOCKED_STRAT
            append_structure_event(
                {
                    "source": "cycle",
                    "strategy": chosen_strat,
                    "symbol": str((act.get("params") or {}).get("symbol") or "").upper(),
                    "direction": str((act.get("params") or {}).get("direction") or ""),
                    "quote": quote_last,
                    "params": {
                        k: (act.get("params") or {}).get(k)
                        for k in (
                            "stop_price", "target_price", "entry_price", "quantity",
                        )
                    },
                    "outcome": GEOMETRY_REJECTED,
                    "reason_code": code,
                    "message": gmsg[:300],
                }
            )

    if forced is not None:
        result = forced
    elif strat == BLOCKED_STRAT:
        result = {"status": "blocked"}
    elif strat == "hold":
        result = {"status": "hold", "strategy": "hold"}
        act["_structure_grade"] = "hold"
    elif strat == "set_risk":
        result = await send_action(act, c)
        act["_structure_grade"] = "set_risk"
    elif strat in ALLOWED_ACTIONS:
        result = await send_action(act, c)
        rc = str((result or {}).get("reason_code") or "")
        st = str((result or {}).get("status") or "").lower()
        if rc:
            act["_structure_grade"] = rc
        elif st in ("rejected", "blocked", "error", "failed"):
            act["_structure_grade"] = st
        else:
            act["_structure_grade"] = act.get("_structure_grade") or "ok"
    else:
        result = {"status": "blocked"}

    await _post_act_structure_and_plan(
        act=act,
        strat=chosen_strat if strat == BLOCKED_STRAT else strat,
        result=result or {},
        judgment=judgment,
        snap=s,
        quote_last=quote_last,
        connector=c,
    )

    _maybe_eod_review(world, judgment, strat)

    out = _result_dict(
        n=n, s=s, act=act, strat=strat, result=result,
        pnl=pnl, eq=eq, prev=prev, inventory=inventory,
        validation=validation, kahneman=extract_kahneman(act), impact=impact,
        judgment=judgment, world=world_dict,
    )
    _journal_stages(out, act, s, judgment)
    h.append({"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
    return out


def _extract_last(q: dict | None) -> float | None:
    if not isinstance(q, dict):
        return None
    for k in ("last", "price", "close", "c", "mark"):
        if q.get(k) is not None:
            try:
                v = float(q[k])
                if v > 0:
                    return v
            except (TypeError, ValueError):
                continue
    return None


async def _quote_for_action(act: dict, snap: dict, connector: Any = None) -> float | None:
    """IBKR live last for geometry — never use MDA SCAN TAPE last as live.

    Order: connector quote → snap ibkr_live_* → snap spy (if SPY) →
    Grok price_hint / entry only when no IBKR live (non-hunt manage paths).
    Hunt brackets fail closed without IBKR live (returns None → geometry block).
    """
    params = act.get("params") or {}
    sym = str(params.get("symbol") or "").upper()
    strat = str(act.get("strategy") or act.get("action") or "").lower()
    hunt_like = strat in ("bracket", "market_bracket")
    live: float | None = None
    if sym and connector is not None:
        try:
            q = await _tool(connector, "quote", {"symbol": sym})
            live = _extract_last(q if isinstance(q, dict) else None)
        except Exception:
            logger.debug("quote fetch for %s failed", sym, exc_info=True)
    if live is None and snap.get("ibkr_live_symbol") == sym:
        try:
            v = float(snap.get("ibkr_live_last"))
            if v > 0:
                live = v
        except (TypeError, ValueError):
            pass
    if live is None and sym in ("", "SPY"):
        live = _extract_last(snap.get("spy_quote") or {})
    if live is not None:
        try:
            hint = params.get("price_hint")
            if hint is not None and abs(float(hint) - live) / live > 0.01:
                params["price_hint"] = live
                act["params"] = params
        except (TypeError, ValueError):
            pass
        return live
    # Hunt entries: do not fall back to MDA tape / hints as "live"
    if hunt_like:
        return None
    try:
        hint = float(params["price_hint"]) if params.get("price_hint") is not None else None
        if hint and hint > 0:
            return hint
    except (TypeError, ValueError):
        pass
    try:
        ep = float(params["entry_price"]) if params.get("entry_price") is not None else None
        if ep and ep > 0:
            return ep
    except (TypeError, ValueError):
        pass
    return None


async def _post_act_structure_and_plan(
    *,
    act: dict,
    strat: str,
    result: dict,
    judgment: dict,
    snap: dict,
    quote_last: float | None,
    connector: Any,
) -> None:
    """Record structure lessons, open/close trade plan, detect scrapes."""
    from abcxauto.structure_grade import (
        SCRAPE_SUSPECT,
        STRUCTURE_OK,
        append_structure_event,
        detect_scrape_from_fills,
    )

    status = str((result or {}).get("status") or "").lower()
    params = act.get("params") or {}
    symbol = str(params.get("symbol") or result.get("symbol") or "").upper()
    direction = str(params.get("direction") or result.get("direction") or "").upper()
    # Broker results often use success/filled without status=
    ok_dispatch = (
        strat not in (BLOCKED_STRAT, "hold", "skipped", "set_risk")
        and status not in ("blocked", "rejected", "error", "failed", "held", "hold")
        and (
            result.get("success") is True
            or result.get("filled") is True
            or status in ("executed", "submitted", "ok", "filled", "success")
        )
    )

    try:
        if ok_dispatch and strat in ("bracket", "market_bracket"):
            fill_px = None
            try:
                raw_fill = result.get("entry_price") or result.get("avg_fill_price")
                if raw_fill is not None:
                    fill_px = float(raw_fill)
            except (TypeError, ValueError):
                fill_px = None

            # Immediate scrape if stop is wrong-side of actual fill (stale hint bug)
            scrape_now = False
            try:
                stop = float(params["stop_price"]) if params.get("stop_price") is not None else None
            except (TypeError, ValueError):
                stop = None
            if fill_px and stop is not None:
                if direction == "LONG" and stop >= fill_px:
                    scrape_now = True
                if direction == "SHORT" and stop <= fill_px:
                    scrape_now = True

            if scrape_now:
                append_structure_event(
                    {
                        "source": "cycle",
                        "strategy": strat,
                        "symbol": symbol,
                        "direction": direction,
                        "quote": fill_px,
                        "params": {
                            k: params.get(k)
                            for k in (
                                "stop_price", "target_price", "entry_price", "quantity",
                            )
                        },
                        "outcome": SCRAPE_SUSPECT,
                        "reason_code": SCRAPE_SUSPECT,
                        "message": (
                            f"stop {stop} wrong-side of fill {fill_px} — "
                            "rebuild stop from live quote next hunt"
                        ),
                    }
                )
                close_trade_plan("scrape_wrong_side_stop")
            else:
                plan = plan_from_hunt_action(act, str(judgment.get("thesis") or ""))
                if plan:
                    if fill_px is not None:
                        plan.entry_price = fill_px
                    save_trade_plan(plan)
                append_structure_event(
                    {
                        "source": "cycle",
                        "strategy": strat,
                        "symbol": symbol,
                        "direction": direction,
                        "quote": quote_last or fill_px,
                        "params": {
                            k: params.get(k)
                            for k in (
                                "stop_price", "target_price", "entry_price", "quantity",
                            )
                        },
                        "outcome": STRUCTURE_OK,
                        "reason_code": STRUCTURE_OK,
                        "message": "dispatched",
                    }
                )
        elif strat in (
            "market_order", "close_option", "limit_order", "stop_order",
        ) and ok_dispatch:
            from abcxauto.trade_plan import (
                load_trade_plan,
                save_trade_plan,
                stk_qty_for_symbol,
            )

            plan = load_trade_plan()
            positions = list(snap.get("positions") or [])
            orders = list(snap.get("open_orders") or [])
            # Prefer live book after exit when connector available
            if connector is not None:
                try:
                    live_pos = await connector.get_positions()
                    if isinstance(live_pos, list):
                        positions = live_pos
                except Exception:
                    pass
                try:
                    live_ord = await connector.get_open_orders()
                    if isinstance(live_ord, list):
                        orders = live_ord
                except Exception:
                    pass
            if plan and strat in ("market_order", "limit_order", "stop_order"):
                held = abs(stk_qty_for_symbol(positions, plan.symbol))
                try:
                    exit_qty = abs(float(params.get("quantity") or 0))
                except (TypeError, ValueError):
                    exit_qty = 0.0
                plan_q = abs(float(plan.quantity or held or 0))
                # Ledger lag: snap still at pre-trim size → apply exit locally
                if (
                    held > 1e-9
                    and exit_qty > 0.51
                    and exit_qty + 0.51 < held
                    and abs(held - plan_q) < 0.51
                ):
                    est = max(0.0, held - exit_qty)
                    sign = 1.0 if str(plan.direction or "LONG").upper() != "SHORT" else -1.0
                    for p in positions:
                        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
                        if sec not in ("STK", "ETF", ""):
                            continue
                        if str(p.get("symbol") or "").upper() != plan.symbol:
                            continue
                        p["quantity"] = est * sign
                        break
                    plan.quantity = est
                    save_trade_plan(plan)
            if plan:
                # Only clear plan when the plan symbol's STK qty is actually gone.
                # Partial exits / option-only closes keep the plan.
                if abs(stk_qty_for_symbol(positions, plan.symbol)) < 1e-9:
                    close_trade_plan("exit_act")
                else:
                    try:
                        sync_open_risk(
                            positions,
                            orders,
                            thesis=str(judgment.get("thesis") or ""),
                            bump=False,
                            allow_flat_close=False,
                        )
                    except Exception:
                        pass

        # Secondary scrape detection from fills (BOT+SLD within seconds)
        if symbol and ok_dispatch and strat in ("bracket", "market_bracket"):
            fills: list = []
            try:
                if connector is not None and hasattr(connector, "get_recent_executions"):
                    fills = await connector.get_recent_executions() or []
            except Exception:
                fills = []
            if not fills:
                try:
                    import sqlite3
                    from abcxauto.memory.journal import get_journal

                    jpath = get_journal().path
                    conn = sqlite3.connect(jpath)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT ts,symbol,side,quantity,price FROM fills "
                        "ORDER BY id DESC LIMIT 20"
                    ).fetchall()
                    conn.close()
                    fills = [dict(r) for r in rows]
                except Exception:
                    fills = []
            if detect_scrape_from_fills(fills, symbol=symbol):
                append_structure_event(
                    {
                        "source": "cycle",
                        "strategy": strat,
                        "symbol": symbol,
                        "direction": direction,
                        "quote": quote_last,
                        "outcome": SCRAPE_SUSPECT,
                        "reason_code": SCRAPE_SUSPECT,
                        "message": "round-trip fill within scrape window",
                    }
                )
                close_trade_plan("scrape_suspect")
    except Exception:
        logger.exception("post_act structure/plan failed")


def run_session_review_on_stop(summary: Optional[dict] = None) -> dict | None:
    """Operator stop / pause → write session review for next Judge."""
    try:
        return maybe_auto_review_from_cycle(
            {
                "force": True,
                **(summary or {}),
                "notes": "operator stop",
            }
        )
    except Exception:
        logger.exception("session review on stop failed")
        return None
