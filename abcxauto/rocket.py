"""Rocket loop helpers — snapshot, Grok JSON cycle, tweak merge, live ledger protocol."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from xai_sdk.chat import system, user

from abcxauto.executor import safe_execute
from abcxauto.kahneman import (
    KAHNEMAN_HEART,
    extract_kahneman,
    format_kahneman_trace,
    gate_incomplete_system2,
    expected_json_shape_hint,
)
from abcxauto.brutal_suite import format_brutal_summary, run_brutal_suite
from abcxauto.llm import GrokClient
from abcxauto.monitor import build_protection_report
from abcxauto.order_lab import auto_reconfig_from_lab
from abcxauto.reality_pulse import build_reality_pulse
from abcxauto.simplify import run_two_simplification_passes
from abcxauto.tools import run_readonly_tool

VALID_ACTIONS = (
    "hold|bracket|market_bracket|market_order|oca|"
    "modify_stop|modify_target|cancel_order|close_option"
)
ALLOWED_ACTIONS = frozenset(VALID_ACTIONS.split("|"))

# Embedded verbatim every cycle (system prompt + user prompt).
ORDER_PROTOCOL = """
=== POSITIONS LEDGER & ORDER ENTRY / EXIT PROTOCOL (MANDATORY) ===

LIVE POSITION LEDGER (fresh from ib.positions() + ib.portfolio() every turn):
For every open position you MUST reason with:
- conId (single source of truth — never rely on symbol alone)
- symbol, secType (STK / OPT / FUT / ...), exchange, currency, multiplier
- For options: lastTradeDateOrContractMonth / expiry, strike, right (C/P)
- signed quantity, avgCost, marketPrice, marketValue, unrealizedPNL, realizedPNL, account
Example: "conId=12345678 | SPY STK SMART USD | pos=+1 | avgCost=741.39 | mkt=742.46 | uPnL=-1.07"

ORDER ENTRY (defined-risk first):
- Preferred: bracket / market_bracket (OCA stop + target).
- Acceptable: market_order / limit_order / stop only as EXITS with closing_position + exact target_conId.
- Complex multi-leg: combo/bag or explicit OCA. Never mix underlyings without naming each conId.
- Every entry references exact Contract (prefer conId). Bare market without exit plan is REJECTED.

ORDER EXIT / CLOSE / FLATTEN:
- Golden rule: NEVER close by symbol. ALWAYS close by exact conId match against the live ledger.
- Before any close tool call, state in rationale:
  1. Refresh implied by LIVE POSITION LEDGER above
  2. "Closing target = conId=XXXX (SPY stock long 1) — NOT the option leg conId=YYYY"
  3. Opposite-side order using the exact same instrument identity
  4. Quantity = abs(current position) for full close
  5. Validation gate: "After this order the target conId position goes to exactly zero. No other positions are touched."
- PANIC FLATTEN: independent close per conId. Do NOT net across unrelated instruments.
- Ambiguity on conId or instrument type → action=hold, explain error in rationale.

Bad: "Close the bottom SPY position" (picks wrong contract).
Good: "Target conId=270639 (SPY STK) currently +1. SELL 1. Separate conId=XXXXX (SPY OPT) remains untouched."
"""

AWARENESS_HEART = """
=== SITUATIONAL AWARENESS (THE HEART — MANDATORY EVERY CYCLE) ===
You receive a REALITY PULSE JSON first. Before ANY action you MUST:
1. Open rationale with the pulse "narrative" line (or rewrite "Current reality: …").
2. Walk the awareness_checklist:
   - Is this instrument tradable right now given session?
   - Is data fresh enough (MDA / IBKR ages)?
   - Does the action respect session liquidity?
   - If closing: exact conId match against position_ledger (never symbol alone)?
   - After close: target conId → zero, no other conIds touched?
3. Prefer hold when session is closed/thin and data is stale unless risk demands protection.
4. PnL is the final truth signal — self-tweaks must relate context to PnL outcomes.
"""

RULES = (
    "ABCXAUTO Pro v0.4 PAPER ONLY. Output ONLY valid JSON. Cash-only until 5 winning paper cycles. "
    "Max 1% risk/trade (or TWEAKS max_risk_pct). Entries MUST be bracket/market_bracket with stop+target. "
    f"action AND strategy MUST be exactly one of: {', '.join(sorted(ALLOWED_ACTIONS))}. "
    "NEVER invent names (no cash_only_mode, hold_existing, protect_existing). Default hold. "
    "market_order is EXIT-ONLY and requires target_conId + closing of that exact conId. "
    "close_option is for OPT legs only with matching target_conId. "
    "Every cycle runs BRUTAL order suite (place/validate/cancel or dry-run for ALL types) — never idle. "
    "Loop: Reality Pulse → brutal suite → fix/simplify → re-test → execute → auto-reconfig. "
    "No force_tweak. PnL + suite pass-rate drive reconfig. "
    + AWARENESS_HEART
    + KAHNEMAN_HEART
    + ORDER_PROTOCOL
)

TWEAKS: Dict[str, Any] = {}


def parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return {"action": "hold"}
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return {"action": "hold"}


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
    """Coerce unknown Grok strategies to hold (never hit safe_execute with invalid names)."""
    strat = (act.get("strategy") or act.get("action") or "hold").strip()
    if strat in ("hold", "none"):
        return "hold", None
    if strat not in ALLOWED_ACTIONS:
        return "hold", {
            "status": "hold",
            "note": f"invalid strategy {strat!r} coerced to hold",
        }
    return strat, None


def risk_label(snap: dict) -> str:
    prot = snap.get("protection") or {}
    bad = prot.get("unprotected_symbols") or []
    return "COMPLIANT" if not bad else f"UNPROTECTED: {', '.join(bad)}"


def position_key(pos: dict) -> str:
    """conId is the single source of truth when present."""
    con = pos.get("conId") or pos.get("con_id") or pos.get("contractId")
    if con is not None and str(con) not in ("", "?"):
        return f"conId={con}"
    sym = pos.get("symbol", "?")
    sec = str(pos.get("secType") or pos.get("sec_type") or "STK").upper()
    if sec.startswith("OPT"):
        exp = pos.get("expiration") or pos.get("lastTradeDateOrContractMonth") or ""
        return f"OPT:{sym}:{exp}:{pos.get('strike', '')}:{pos.get('right', '')}"
    return f"STK:{sym}"


def format_position_inventory(positions: list) -> str:
    """LIVE POSITION LEDGER block for every cycle prompt."""
    if not positions:
        return "LIVE POSITION LEDGER: (none)\n"
    lines = ["LIVE POSITION LEDGER (fresh from ib.positions()/portfolio()):"]
    for p in positions:
        con = p.get("conId") or p.get("con_id") or "?"
        sym = p.get("symbol", "?")
        sec = p.get("secType") or p.get("sec_type") or "STK"
        exch = p.get("exchange") or p.get("primaryExchange") or "SMART"
        ccy = p.get("currency") or "USD"
        mult = p.get("multiplier") or ("100" if str(sec).upper().startswith("OPT") else "1")
        qty = p.get("quantity", p.get("position", 0))
        try:
            qty_s = f"{float(qty):+g}" if qty is not None else "?"
        except (TypeError, ValueError):
            qty_s = str(qty)
        avg = p.get("avgCost", p.get("avg_cost", p.get("averageCost", 0)))
        mkt = p.get("marketPrice", p.get("market_price", 0))
        mval = p.get("marketValue", p.get("market_value", 0))
        upnl = p.get("unrealizedPNL", p.get("unrealized_pnl", 0))
        rpnl = p.get("realizedPNL", p.get("realized_pnl", 0))
        acct = p.get("account") or p.get("account_id") or ""
        base = (
            f"conId={con} | {sym} {sec} {exch} {ccy} mult={mult} | pos={qty_s} | "
            f"avgCost={avg} | mkt={mkt} | mktVal={mval} | uPnL={upnl} | rPnL={rpnl}"
        )
        if acct:
            base += f" | acct={acct}"
        if str(sec).upper().startswith("OPT"):
            exp = p.get("expiration") or p.get("lastTradeDateOrContractMonth") or ""
            base += (
                f" | expiry={exp} strike={p.get('strike', '')} right={p.get('right', '')}"
            )
        lines.append(base)
    return "\n".join(lines) + "\n"


# Keep short alias used by older tests / call sites
SAFETY_CHECKLIST = ORDER_PROTOCOL


def validate_action_against_inventory(action: dict, positions: list) -> tuple:
    """Reject closes that lack exact target_conId or mismatch instrument type."""
    if not action:
        return True, "validated"
    strat = (action.get("strategy") or action.get("action") or "").lower()
    params = action.get("params") or {}
    target = str(
        action.get("target_conId")
        or params.get("conId")
        or params.get("con_id")
        or ""
    ).strip()
    is_close = any(
        k in strat for k in ("close", "flatten", "market_order", "sell", "limit_order")
    )
    # Entries: brackets don't need target_conId
    if not is_close:
        return True, "validated"
    if not target:
        return False, "must specify exact target_conId (never close by symbol alone)"
    matching = [
        p
        for p in positions
        if str(p.get("conId") or p.get("con_id") or "") == target
    ]
    if not matching:
        return False, f"target_conId={target} not found in live ledger"
    m_sec = str(
        matching[0].get("sec_type") or matching[0].get("secType") or "STK"
    ).upper()
    is_opt = "close_option" in strat
    is_stk = not is_opt and any(
        k in strat for k in ("market", "sell", "flatten", "close", "limit")
    )
    if is_stk and not m_sec.startswith("STK"):
        return False, f"target_conId={target} is {m_sec}, not STK; use close_option"
    if is_opt and not m_sec.startswith("OPT"):
        return False, f"target_conId={target} is {m_sec}, not OPT"
    return True, "validated (exact conId match)"


def simulate_close_impact(action: dict, positions: list) -> dict:
    """Dry-run: which conIds would go to zero; which are untouched."""
    ok, msg = validate_action_against_inventory(action, positions)
    target = str(
        (action or {}).get("target_conId")
        or ((action or {}).get("params") or {}).get("conId")
        or ""
    ).strip()
    touched = [p for p in positions if str(p.get("conId") or p.get("con_id") or "") == target]
    untouched = [
        p for p in positions if str(p.get("conId") or p.get("con_id") or "") != target
    ]
    return {
        "ok": ok,
        "message": msg,
        "target_conId": target,
        "would_zero": [
            {
                "conId": p.get("conId") or p.get("con_id"),
                "symbol": p.get("symbol"),
                "sec_type": p.get("sec_type") or p.get("secType"),
                "from_qty": p.get("quantity"),
            }
            for p in touched
        ],
        "untouched_conIds": [
            p.get("conId") or p.get("con_id") for p in untouched
        ],
        "gate": (
            f"After this order the target conId={target or '?'} position goes to "
            "exactly zero. No other positions are touched."
            if ok
            else f"REJECTED: {msg}"
        ),
    }


async def _tool(c: Any, n: str, a: dict | None = None) -> Any:
    return json.loads(await run_readonly_tool(n, a or {}, c))


async def snap(c: Any) -> dict:
    # VIX is best-effort; never block the pulse if the quote tool fails.
    acct, pos, orders, hours, spy = await asyncio.gather(
        _tool(c, "account_summary"),
        _tool(c, "positions"),
        _tool(c, "open_orders"),
        _tool(c, "market_hours"),
        _tool(c, "quote", {"symbol": "SPY"}),
    )
    vix: Any = {}
    try:
        vix = await _tool(c, "quote", {"symbol": "VIX"})
    except Exception:
        vix = {}
    pl, ol = (pos if isinstance(pos, list) else []), (
        orders if isinstance(orders, list) else []
    )
    taken = datetime.now(timezone.utc).isoformat()
    protection = build_protection_report(pl, ol)
    connected = bool(getattr(c, "connected", True))
    pulse = build_reality_pulse(
        account=acct if isinstance(acct, dict) else {},
        positions=pl,
        open_orders=ol,
        market_hours=hours if isinstance(hours, dict) else {},
        spy_quote=spy if isinstance(spy, dict) else {},
        vix_quote=vix if isinstance(vix, dict) else {},
        protection=protection,
        ibkr_connected=connected,
        taken_at=taken,
    )
    return {
        "taken_at": taken,
        "account": acct,
        "positions": pl,
        "open_orders": ol,
        "market_hours": hours,
        "spy_quote": spy,
        "vix_quote": vix,
        "protection": protection,
        "reality_pulse": pulse,
    }


async def grok(g: GrokClient, p: str) -> str:
    chat = g.client.chat.create(
        model=g.model,
        messages=[system(RULES + "\nTWEAKS=" + json.dumps(TWEAKS)), user(p)],
        temperature=g.temperature,
        max_tokens=min(2048, g.max_tokens),
    )
    o = ""
    async for _, ch in chat.stream():
        if ch.content:
            o += ch.content
    return o


def _prepare_close_params(act: dict, positions: list) -> None:
    """Stamp closing_position + conId + qty from live ledger when closing."""
    strat = (act.get("strategy") or act.get("action") or "").lower()
    if not any(k in strat for k in ("close", "market_order", "limit_order", "sell")):
        return
    target = str(
        act.get("target_conId")
        or (act.get("params") or {}).get("conId")
        or (act.get("params") or {}).get("con_id")
        or ""
    ).strip()
    if not target:
        m = re.search(
            r"conId\s*=\s*(\S+)",
            str(act.get("rationale") or "")
            + " "
            + str(act.get("reasoning_chain") or "")
            + " "
            + str(act.get("params") or {}),
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
        (
            p
            for p in positions
            if str(p.get("conId") or p.get("con_id") or "") == target
        ),
        None,
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
    if not act["params"].get("symbol"):
        act["params"]["symbol"] = match.get("symbol")
    if "market_order" in strat or strat in ("sell", "flatten"):
        try:
            signed = float(qty)
        except (TypeError, ValueError):
            signed = 0.0
        act["params"].setdefault("action", "SELL" if signed > 0 else "BUY")


async def run_cycle(n: int, c: Any, g: GrokClient, h: List[dict], prev: float) -> dict:
    s = await snap(c)
    acct = s.get("account") or {}
    pnl, eq = pnl_of(acct), equity_of(acct)
    positions = s.get("positions") or []
    for p in positions:
        if "conId" not in p and "con_id" in p:
            p["conId"] = p["con_id"]
    inventory = format_position_inventory(positions)
    pulse = s.get("reality_pulse") or {}
    # Heart: Reality Pulse → Kahneman System 2 → order protocol → proposal.
    prompt = (
        f"Cycle {n}.\n"
        f"REALITY PULSE (situational awareness heart):\n"
        f"{json.dumps(pulse, default=str)[:6000]}\n\n"
        f"{KAHNEMAN_HEART}\n"
        f"{inventory}\n{ORDER_PROTOCOL}\n"
        f"Raw snapshot (truncated):\n{json.dumps(s, default=str)[:3500]}\n"
        f"{expected_json_shape_hint()}\n"
        "Fill kahneman completely before any non-hold action. "
        "Open rationale with pulse narrative + System 2 summary."
    )
    act = parse_json(await grok(g, prompt))
    strat, forced = normalize_action(act)
    kahneman = extract_kahneman(act)
    act["kahneman"] = kahneman
    kahneman_trace = format_kahneman_trace(kahneman)
    validation = "n/a"
    reasoning_chain = (
        (act or {}).get("rationale")
        or (act or {}).get("reasoning_chain")
        or ""
    )
    # Soft System 2 gate: incomplete deliberative scaffold → hold (no execute).
    s2_ok, s2_msg = gate_incomplete_system2(strat, kahneman)
    if not s2_ok and strat != "hold":
        forced = {
            "status": "hold",
            "note": s2_msg,
            "kahneman_incomplete": True,
        }
        strat = "hold"
        validation = f"system2_gate: {s2_msg}"
    _prepare_close_params(act, positions)
    try:
        ok, vmsg = validate_action_against_inventory(act, positions)
        # Never clobber an earlier system2_gate / rejection message.
        if validation == "n/a":
            validation = f"{'ok' if ok else 'rejected'}: {vmsg}"
        elif not ok and strat != "hold":
            validation = f"{validation}; rejected: {vmsg}"
            forced = {"status": "validated_hold", "reason": vmsg}
            strat = "hold"
        elif not ok and strat == "hold" and not validation.startswith("system2"):
            validation = f"rejected: {vmsg}"
    except Exception:
        pass
    impact = simulate_close_impact(act, positions)
    act["_live_positions"] = positions
    act["_impact"] = impact
    act["_kahneman_trace"] = kahneman_trace

    # v0.4: Brutal suite (never idle) → Fix → Re-test → Execute → Reconfigure
    brutal = await run_brutal_suite(
        connector=c,
        pulse=pulse,
        positions=positions,
        history=h,
        source="cycle",
    )
    lab = {
        "pass_rate": brutal.get("pass_rate"),
        "passed": brutal.get("passed"),
        "failed": brutal.get("failed"),
        "strategies_tested": brutal.get("strategies_tested"),
        "results": brutal.get("results") or [],
        "summary": brutal.get("summary"),
    }
    lab_summary = format_brutal_summary(brutal)
    # Fix round (audited lean passes)
    simplify = run_two_simplification_passes(lab)
    # Immediate re-test after fix (brutal again)
    brutal_retest = await run_brutal_suite(
        connector=c,
        pulse=pulse,
        positions=positions,
        history=h,
        source="cycle_retest",
    )
    lab_retest = {
        "pass_rate": brutal_retest.get("pass_rate"),
        "passed": brutal_retest.get("passed"),
        "failed": brutal_retest.get("failed"),
        "strategies_tested": brutal_retest.get("strategies_tested"),
        "results": brutal_retest.get("results") or [],
        "summary": brutal_retest.get("summary"),
    }
    retest = {
        "after_fix": True,
        "pre_pass_rate": lab.get("pass_rate"),
        "post_pass_rate": lab_retest.get("pass_rate"),
        "pre_failed": lab.get("failed"),
        "post_failed": lab_retest.get("failed"),
        "improved": float(lab_retest.get("pass_rate") or 0)
        >= float(lab.get("pass_rate") or 0),
        "summary": (
            f"re-test after fix: {lab.get('pass_rate')} → {lab_retest.get('pass_rate')} "
            f"(failed {lab.get('failed')} → {lab_retest.get('failed')})"
        ),
        "lab": lab_retest,
        "brutal": brutal_retest,
    }
    lab_summary = (
        f"{lab_summary}\n{retest['summary']}\n"
        f"simplify: {simplify.get('summary')}"
    )
    # Gates use post-fix lab
    rate = float(lab_retest.get("pass_rate") or 1)
    if TWEAKS.get("prefer_bracket_only") and strat not in (
        "hold",
        "bracket",
        "market_bracket",
        "close_option",
        "market_order",
        "modify_stop",
        "modify_target",
        "cancel_order",
        "oca",
    ):
        forced = {
            "status": "hold",
            "note": "lab reconfig prefer_bracket_only — non-preferred strategy held",
        }
        strat = "hold"
        validation = f"{validation}; lab_gate: prefer_bracket_only"

    if rate < float(TWEAKS.get("lab_min_pass_rate", 0.5)) and strat in (
        "bracket",
        "market_bracket",
    ):
        forced = {
            "status": "hold",
            "note": f"re-test lab pass_rate {rate} below min — new entries paused",
        }
        strat = "hold"
        validation = f"{validation}; lab_gate: low_pass_rate_after_retest"

    res = (
        forced
        if forced
        else (
            {"status": "hold"}
            if strat == "hold"
            else await safe_execute(act, c)
        )
    )
    # Reconfigure from post-fix lab + PnL (no manual force-tweak)
    reconfig = auto_reconfig_from_lab(lab_retest, h)
    twk = reconfig.get("summary") or "none"
    tw = {
        **reconfig,
        "simplify": simplify.get("summary"),
        "retest": retest.get("summary"),
    }
    tweak_before = reconfig.get("config_before") or {}

    rec = {
        "cycle": n,
        "pnl": pnl,
        "pnl_chg": pnl - prev,
        "snapshot": s,
        "reality_pulse": pulse,
        "kahneman": kahneman,
        "kahneman_trace": kahneman_trace,
        "order_lab": lab,
        "brutal_suite": brutal,
        "lab_retest": lab_retest,
        "retest": retest,
        "lab_summary": lab_summary,
        "reconfig": reconfig,
        "simplify": simplify,
        "action": act,
        "result": res,
        "inventory": inventory,
        "validation": validation,
        "reasoning_chain": reasoning_chain,
        "impact": impact,
    }
    h.append(rec)
    Path("rocket.log").open("a", encoding="utf-8").write(
        json.dumps(rec, default=str) + "\n"
    )
    Path("improvements.log").open("a", encoding="utf-8").write(
        json.dumps(
            {
                "cycle": n,
                "reconfig": reconfig,
                "retest": retest.get("summary"),
                "lab_pass_rate": lab_retest.get("pass_rate"),
            },
            default=str,
        )
        + "\n"
    )
    if len(h) >= 2 and (lab_retest.get("failed", 0) > 0 or (pnl - prev) < 0):
        try:
            tw_g = parse_json(
                await grok(
                    g,
                    f"Lab+retest+PnL reflection (auto):\n"
                    f"{lab_summary}\n"
                    f"{json.dumps(h[-2:], default=str)[:5000]}\n"
                    'ONE tweak JSON: {{"type":"config|none","config":{{}},'
                    '"summary":"pnl/lab driven tweak"}}',
                )
            )
            if tw_g.get("type") == "config" and tw_g.get("config"):
                apply_tweak(tw_g)
                tw = {**tw, "grok_reflection": tw_g}
                twk = f"{twk}; grok:{tw_g.get('summary', '')}"
        except Exception:
            pass
    orders = s.get("open_orders") or []
    protection = s.get("protection") or {}
    return {
        "cycle": n,
        "pnl": pnl,
        "pnl_chg": pnl - prev,
        "equity": eq,
        "strat": strat,
        "result": res,
        "tweak": twk,
        "tweak_obj": tw,
        "risk": risk_label(s),
        "portfolio": f"{len(positions)} positions | {len(orders)} orders",
        "positions": positions,
        "open_orders": orders,
        "protection": protection,
        "unprotected": list(protection.get("unprotected_symbols") or []),
        "action_obj": act,
        "rationale": act.get("rationale") or "",
        "taken_at": s.get("taken_at") or "",
        "inventory": inventory,
        "validation": validation,
        "reasoning_chain": reasoning_chain,
        "tweak_before": tweak_before,
        "impact": impact,
        "reality_pulse": pulse,
        "kahneman": kahneman,
        "kahneman_trace": kahneman_trace,
        "order_lab": lab_retest,
        "order_lab_pre": lab,
        "brutal_suite": brutal_retest,
        "brutal_suite_pre": brutal,
        "lab_summary": lab_summary,
        "retest": retest,
        "reconfig": reconfig,
        "simplify": simplify,
    }
