"""Lean autonomous agent loop - snap -> Grok -> normalize -> send_action -> journal.

``abcxauto.cycle`` re-exports this API for test/UI compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from xai_sdk.chat import system, user

from abcxauto.book import build_book_from_snap
from abcxauto.config import get_config, resolve_effective_posture, risk_envelope_snapshot
from abcxauto.connections import connection_status
from abcxauto.llm import GrokClient
from abcxauto.memory import get_journal
from abcxauto.monitor import build_protection_report
from abcxauto.opportunity_scan import format_opportunities, scan_opportunities
from abcxauto.order_examples import format_order_examples
from abcxauto.reality_pulse import build_reality_pulse
from abcxauto.send import send_action
from abcxauto.tools import run_readonly_tool

logger = logging.getLogger(__name__)

VALID_ACTIONS = (
    "hold|set_risk|bracket|market_bracket|market_order|oca|"
    "modify_stop|modify_target|cancel_order|close_option"
)
ALLOWED_ACTIONS = frozenset(VALID_ACTIONS.split("|"))
BLOCKED_STRAT = "blocked"
AWARENESS_HEART = (
    "\n=== AWARENESS ===\n"
    "Hold when protected or flat. Hold FORBIDDEN when unprotected STK.\n"
    "Close by conId only. Risk gates are hard. PnL is truth.\n"
    "Size each entry so stop risk fits max_risk_per_trade_pct. "
    "set_risk retunes knobs inside the operator posture envelope only.\n"
)
PROTECT_HOLD_RULES = (
    "PROTECT/HOLD: hold allowed when book is protected or flat. "
    "HOLD FORBIDDEN when unprotected STK - protect (oca/modify_stop) or exit "
    "by exact conId first. Entries must be bracket/market_bracket with stop+target. "
    "market_order is EXIT-ONLY (target_conId). Use direction LONG|SHORT. "
    "set_risk adjusts capital knobs (not risk_posture)."
)
TWEAKS: Dict[str, Any] = {"max_risk_pct": 0.5}
# Soft hold-streak nudge state (process-local).
_HOLD_STREAK = {"count": 0, "flat": True}
_HIST_KEYS = (
    "cycle", "pnl", "pnl_chg", "reality_pulse", "kahneman", "kahneman_trace",
    "order_lab", "order_suite", "retest", "lab_summary", "result", "inventory",
    "validation", "reasoning_chain", "impact",
)


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
        'JSON: {"action":"...","strategy":"...","params":{},"rationale":"...",'
        '"target_conId":"..."} - hold when protected/flat; hold FORBIDDEN when '
        "unprotected STK. bracket/market_bracket/oca use direction LONG|SHORT. "
        "set_risk params are capital knobs inside the posture envelope."
    )


def parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {"action": "market_bracket", "strategy": "market_bracket", "params": {}}
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return {"action": "market_bracket", "strategy": "market_bracket", "params": {}}


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


def _journal_snippet(s: dict | None = None) -> str:
    try:
        journal = get_journal()
        thesis = journal.get_working_thesis() or ""
        decisions = journal.recent_decisions(limit=3)
    except Exception as exc:
        logger.warning("agent_loop: journal unavailable: %s", exc)
        return "JOURNAL MEMORY: unavailable."
    positions = (s or {}).get("positions") or []
    orders = (s or {}).get("open_orders") or []
    acct = (s or {}).get("account") or {}
    unprotected = list(((s or {}).get("protection") or {}).get("unprotected_symbols") or [])
    live = {
        "net_liquidation": acct.get("netliquidation") or acct.get("NetLiquidation"),
        "n_positions": len(positions), "n_open_orders": len(orders),
        "unprotected_symbols": unprotected,
        "positions": [
            {"conId": p.get("conId") or p.get("con_id"), "symbol": p.get("symbol"),
             "sec": p.get("secType") or p.get("sec_type"),
             "qty": p.get("quantity") or p.get("position")}
            for p in positions[:8]
        ],
    }
    slim = [
        {"action": d.get("action"), "strategy": d.get("strategy"),
         "rationale": (d.get("rationale") or "")[:80]}
        for d in decisions[:3]
    ]
    reality = "REALITY CHECK: ok."
    if unprotected:
        reality = (
            "REALITY CHECK: UNPROTECTED - protect or exit by conId. Hold FORBIDDEN. "
            + ", ".join(str(x) for x in unprotected)
        )
    elif positions and not orders and any(
        str(p.get("secType") or p.get("sec_type") or "STK").upper().startswith("STK")
        and abs(float(p.get("quantity") or p.get("position") or 0)) > 0
        for p in positions
    ):
        reality = "REALITY CHECK: naked STK (zero orders) - protect immediately."
    return (
        f"LIVE BOOK:\n{json.dumps(live, default=str)[:1400]}\n\n{reality}\n\n"
        f"JOURNAL MEMORY:\nworking_thesis={(thesis or '-')[:200]}\n"
        f"recent_decisions={json.dumps(slim, default=str)[:500]}"
    )


async def grok(g: GrokClient, p: str) -> str:
    chat = g.client.chat.create(
        model=g.model,
        messages=[system(_build_rules() + "\nTWEAKS=" + json.dumps(TWEAKS)), user(p)],
        temperature=g.temperature,
        max_tokens=min(2048, g.max_tokens),
    )
    o = ""
    async for _, ch in chat.stream():
        if ch.content:
            o += ch.content
    return o


def _prepare_close_params(act: dict, positions: list) -> None:
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
        q = abs(int(float(qty)))
    except (TypeError, ValueError):
        q = 0
    if q and not act["params"].get("quantity"):
        act["params"]["quantity"] = q
    act["params"].setdefault("symbol", match.get("symbol"))
    if "market_order" in strat or strat in ("sell", "flatten"):
        try:
            signed = float(qty)
        except (TypeError, ValueError):
            signed = 0.0
        act["params"].setdefault("action", "SELL" if signed > 0 else "BUY")


def _risk_prompt_block() -> str:
    try:
        snap = risk_envelope_snapshot()
    except Exception:
        return (
            "RISK POSTURE: (unavailable)\n\n"
        )
    if not snap.get("effective_risk_posture"):
        return (
            "RISK POSTURE: (none — operator has not set risk_posture; "
            "set_risk blocked until then)\n\n"
        )
    cur = snap.get("current") or {}
    env = snap.get("envelope") or {}
    lines = [
        f"RISK POSTURE: {snap.get('risk_posture')} "
        f"(effective={snap.get('effective_risk_posture')})",
        f"BIAS: {snap.get('prompt_bias') or ''}",
        "CURRENT GATES: "
        + ", ".join(f"{k}={cur.get(k)}" for k in sorted(cur)),
        "ENVELOPE (you may set_risk within): "
        + ", ".join(
            f"{k}=[{(env.get(k) or {}).get('floor')}-{(env.get(k) or {}).get('ceil')}]"
            for k in sorted(env)
        ),
        "Size each trade inside max_risk_per_trade_pct. "
        "You may not change risk_posture.",
        "",
    ]
    return "\n".join(lines) + "\n"


def _build_prompt(n: int, s: dict, needs_prot: bool, c: Any = None) -> str:
    book = s.get("portfolio_state") or build_book_from_snap(s)
    s["portfolio_state"] = book
    try:
        conn = connection_status(c)
    except Exception:
        conn = {
            "ibkr_connected": bool(getattr(c, "connected", False)),
            "mda_configured": False, "trading_mode": "paper",
        }
    cfg = get_config()
    mandate = (getattr(cfg, "trading_mandate", None) or "")[:400]
    ideas = s.get("opportunities") or []
    flat = not (s.get("positions") or [])
    posture = resolve_effective_posture(
        getattr(cfg, "risk_posture", "") or "",
        getattr(cfg, "trading_mode", "paper") or "paper",
    )
    if needs_prot:
        focus = (
            "PROTECTION ONLY: unprotected STK - oca / modify_stop / market_order by conId. "
            "HOLD FORBIDDEN. No new entries."
        )
    elif posture in ("balanced", "aggressive") and flat and ideas:
        focus = (
            f"ACTIVE TRADING ({posture}). Top ideas attached — "
            "hold requires a one-line reason vs #1, or take a small bracket / set_risk. "
            "Size per trade inside max_risk_per_trade_pct."
        )
    else:
        focus = (
            "ACTIVE TRADING - hold allowed when protected/flat. One allowlisted action. "
            "Size entries inside max_risk_per_trade_pct; set_risk within envelope."
        )
    if s.get("hold_streak_nudge"):
        focus += (
            " HOLD STREAK: Justify hold vs opportunity #1 or take a small bracket / set_risk."
        )
    pulse = s.get("reality_pulse") or {}
    pulse_bit = ""
    if pulse.get("narrative"):
        pulse_bit = f"REALITY PULSE:\n{str(pulse.get('narrative'))[:500]}\n\n"
    news_bit = str(s.get("news_prompt") or "").strip()
    if news_bit:
        news_bit = news_bit + "\n\n"
    opp_bit = format_opportunities(ideas) + "\n\n"
    return (
        f"Cycle {n}.\nMANDATE:\n{mandate}\n\n"
        f"CONNECTION: {json.dumps(conn, default=str)}\n\n"
        f"PORTFOLIO STATE:\n{json.dumps(book, default=str)[:1800]}\n\n"
        f"{_risk_prompt_block()}"
        f"{pulse_bit}{news_bit}{opp_bit}"
        f"{format_order_examples()}\n\n"
        f"{_journal_snippet(s)}\n\n"
        f"{format_position_inventory(s.get('positions') or [])}\n"
        f"{expected_json_shape_hint()}\n{focus}"
    )


def _result_dict(
    *, n: int, s: dict, act: dict, strat: str, result: dict,
    pnl: float, eq: float, prev: float, inventory: str, validation: str,
    kahneman: dict, impact: dict | None = None,
) -> dict:
    positions = s.get("positions") or []
    return {
        "cycle": n, "pnl": pnl, "pnl_chg": pnl - prev, "equity": eq,
        "strat": strat, "result": result, "tweak": "none", "tweak_obj": {},
        "risk": risk_label(s),
        "portfolio": f"{len(positions)} positions | {len(s.get('open_orders') or [])} orders",
        "portfolio_state": s.get("portfolio_state") or {},
        "positions": positions, "open_orders": s.get("open_orders") or [],
        "protection": s.get("protection") or {},
        "unprotected": list((s.get("protection") or {}).get("unprotected_symbols") or []),
        "action_obj": act, "rationale": act.get("rationale") or "",
        "taken_at": s.get("taken_at") or "", "inventory": inventory,
        "validation": validation,
        "reasoning_chain": act.get("rationale") or act.get("reasoning_chain") or "",
        "tweak_before": {}, "impact": impact or {},
        "reality_pulse": s.get("reality_pulse") or {},
        "kahneman": kahneman, "kahneman_trace": format_kahneman_trace(kahneman),
        "order_lab": {}, "order_suite": {}, "lab_summary": "",
        "retest": {}, "reconfig": {},
    }


def _journal_decision(out: dict, act: dict, s: dict) -> None:
    try:
        journal = get_journal()
        strat = out.get("strat")
        result = out.get("result") or {}
        journal.record_decision(
            cycle=out.get("cycle"), action=act.get("action") or strat, strategy=strat,
            rationale=out.get("rationale") or act.get("rationale") or "",
            portfolio_snapshot=s.get("portfolio_state"), outcome=result,
        )
        if (
            strat not in (BLOCKED_STRAT, "skipped", "hold", "set_risk")
            and isinstance(result, dict)
            and str(result.get("status") or "").lower()
            not in ("blocked", "rejected", "error", "failed", "held")
        ):
            thesis = (
                act.get("rationale") or out.get("rationale") or act.get("reasoning_chain") or ""
            ).strip()
            if thesis:
                journal.set_working_thesis(thesis)
    except Exception as exc:
        logger.warning("agent_loop: journal decision record failed: %s", exc)


def _update_hold_streak(strat: str, flat: bool, ideas: list) -> bool:
    """Track consecutive holds while flat; return whether to nudge next/this cycle."""
    if flat and strat == "hold":
        _HOLD_STREAK["count"] = int(_HOLD_STREAK.get("count") or 0) + 1
    else:
        _HOLD_STREAK["count"] = 0
    _HOLD_STREAK["flat"] = flat
    return bool(
        flat
        and ideas
        and int(_HOLD_STREAK.get("count") or 0) >= 3
    )


async def run_cycle(n: int, c: Any, g: GrokClient, h: List[dict], prev: float) -> dict:
    """One autonomous cycle: snap -> Grok -> normalize -> send_action -> journal."""
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
    flat = not positions

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
        _journal_decision(out, act, s)
        h.append({"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return out

    try:
        from abcxauto.news_feed import fetch_agent_news, format_news_for_prompt
        news_items = await fetch_agent_news(s.get("positions") or [])
        s["news_prompt"] = format_news_for_prompt(news_items)
        s["news_items"] = news_items
    except Exception:
        s["news_prompt"] = ""
        s["news_items"] = []

    try:
        ideas = await scan_opportunities(positions)
    except Exception:
        logger.exception("opportunity scan failed")
        ideas = []
    s["opportunities"] = ideas
    # Nudge when already on a hold streak entering this cycle.
    s["hold_streak_nudge"] = bool(
        flat and ideas and int(_HOLD_STREAK.get("count") or 0) >= 3
    )

    act = parse_json(await grok(g, _build_prompt(n, s, needs_prot, c)))
    strat, forced = normalize_action(act)
    if strat == "hold" and needs_prot:
        strat = BLOCKED_STRAT
        forced = {
            "status": "blocked",
            "note": "hold_forbidden - unprotected STK needs protection",
        }
        act["strategy"] = act["action"] = BLOCKED_STRAT

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
    if forced is not None:
        result = forced
    elif strat == BLOCKED_STRAT:
        result = {"status": "blocked"}
    elif strat == "hold":
        result = {"status": "hold", "strategy": "hold"}
    elif strat == "set_risk":
        result = await send_action(act, c)
    elif strat in ALLOWED_ACTIONS:
        result = await send_action(act, c)
    else:
        result = {"status": "blocked"}

    _update_hold_streak(strat, flat, ideas)

    out = _result_dict(
        n=n, s=s, act=act, strat=strat, result=result,
        pnl=pnl, eq=eq, prev=prev, inventory=inventory,
        validation=validation, kahneman=extract_kahneman(act), impact=impact,
    )
    _journal_decision(out, act, s)
    h.append({"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
    return out
