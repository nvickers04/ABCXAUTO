"""Rocket loop helpers — snapshot, Grok JSON cycle, tweak merge, live ledger."""

import asyncio, json, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from xai_sdk.chat import system, user

from abcxauto.executor import safe_execute
from abcxauto.llm import GrokClient
from abcxauto.monitor import build_protection_report
from abcxauto.tools import run_readonly_tool

VALID_ACTIONS = "hold|bracket|market_bracket|oca|modify_stop|modify_target|cancel_order|close_option"
ALLOWED_ACTIONS = frozenset(VALID_ACTIONS.split("|"))
RULES = (
    "ABCXAUTO v0.1. Output ONLY valid JSON. Cash-only until 5 winning paper cycles. "
    "Max 1% risk/trade. Entries MUST be bracket/market_bracket with stop+target. "
    f"action AND strategy MUST be exactly one of: {', '.join(sorted(ALLOWED_ACTIONS))}. "
    "NEVER invent names (no cash_only_mode, hold_existing, protect_existing). Default hold."
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
        return "hold", {"status": "hold", "note": f"invalid strategy {strat!r} coerced to hold"}
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
        qty = p.get("quantity", p.get("position", 0))
        avg = p.get("avgCost", p.get("avg_cost", p.get("averageCost", 0)))
        mkt = p.get("marketPrice", p.get("market_price", 0))
        upnl = p.get("unrealizedPNL", p.get("unrealized_pnl", 0))
        base = f"conId={con} | {sym} {sec} | pos={qty} | avgCost={avg} | mkt={mkt} | uPnL={upnl}"
        if str(sec).upper().startswith("OPT"):
            exp = p.get("expiration") or p.get("lastTradeDateOrContractMonth") or ""
            base += f" | expiry={exp} strike={p.get('strike', '')} right={p.get('right', '')}"
        lines.append(base)
    return "\n".join(lines) + "\n"


SAFETY_CHECKLIST = """
ORDER EXIT PROTOCOL: Never close by symbol alone. Always name Closing target = conId=XXXX.
Prefer target_conId matching LIVE POSITION LEDGER. STK vs OPT are distinct even for same symbol.
"""


def validate_action_against_inventory(action: dict, positions: list) -> tuple:
    """Reject closes that lack exact target_conId or mismatch instrument type."""
    if not action:
        return True, "validated"
    strat = (action.get("strategy") or action.get("action") or "").lower()
    params = action.get("params") or {}
    target = str(action.get("target_conId") or params.get("conId") or params.get("con_id") or "").strip()
    is_close = any(k in strat for k in ("close", "flatten", "market_order", "sell"))
    if not is_close:
        return True, "validated"
    if not target:
        return False, "must specify exact target_conId (never close by symbol alone)"
    matching = [p for p in positions if str(p.get("conId") or p.get("con_id") or "") == target]
    if not matching:
        return False, f"target_conId={target} not found in live ledger"
    m_sec = str(matching[0].get("sec_type") or matching[0].get("secType") or "STK").upper()
    is_opt = "close_option" in strat
    is_stk = not is_opt and ("market" in strat or "sell" in strat or "flatten" in strat or "close" in strat)
    if is_stk and not m_sec.startswith("STK"):
        return False, f"target_conId={target} is {m_sec}, not STK; use close_option"
    if is_opt and not m_sec.startswith("OPT"):
        return False, f"target_conId={target} is {m_sec}, not OPT"
    return True, "validated (exact conId match)"


async def _tool(c: Any, n: str, a: dict | None = None) -> Any:
    return json.loads(await run_readonly_tool(n, a or {}, c))


async def snap(c: Any) -> dict:
    acct, pos, orders, hours, spy = await asyncio.gather(
        _tool(c, "account_summary"), _tool(c, "positions"), _tool(c, "open_orders"),
        _tool(c, "market_hours"), _tool(c, "quote", {"symbol": "SPY"}),
    )
    pl, ol = (pos if isinstance(pos, list) else []), (orders if isinstance(orders, list) else [])
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(), "account": acct, "positions": pl,
        "open_orders": ol, "market_hours": hours, "spy_quote": spy,
        "protection": build_protection_report(pl, ol),
    }


async def grok(g: GrokClient, p: str) -> str:
    chat = g.client.chat.create(
        model=g.model, messages=[system(RULES + json.dumps(TWEAKS)), user(p)],
        temperature=g.temperature, max_tokens=min(2048, g.max_tokens),
    )
    o = ""
    async for _, ch in chat.stream():
        if ch.content:
            o += ch.content
    return o


async def run_cycle(n: int, c: Any, g: GrokClient, h: List[dict], prev: float) -> dict:
    s = await snap(c)
    acct = s.get("account") or {}
    pnl, eq = pnl_of(acct), equity_of(acct)
    positions = s.get("positions") or []
    for p in positions:
        if "conId" not in p and "con_id" in p:
            p["conId"] = p["con_id"]
    inventory = format_position_inventory(positions)
    prompt = (
        f"Cycle {n}.\n{inventory}{SAFETY_CHECKLIST}\n"
        f"Snapshot:\n{json.dumps(s, default=str)[:8000]}\n"
        'JSON: {"action":"...","strategy":"...","params":{},"rationale":"...","target_conId":"..."}'
    )
    act = parse_json(await grok(g, prompt))
    strat, forced = normalize_action(act)
    validation = "n/a"
    reasoning_chain = (act or {}).get("rationale") or (act or {}).get("reasoning_chain") or ""
    if any(k in (act.get("strategy") or act.get("action") or "").lower() for k in ("close", "flatten", "market_order", "sell")):
        if not act.get("target_conId"):
            m = re.search(r"conId\s*=\s*(\S+)", reasoning_chain + " " + str(act.get("params", {})))
            if m:
                act["target_conId"] = m.group(1)
        t = act.get("target_conId")
        if t:
            act.setdefault("params", {})
            act["params"].setdefault("conId", t)
    try:
        ok, vmsg = validate_action_against_inventory(act, positions)
        validation = f"{'ok' if ok else 'rejected'}: {vmsg}"
        if not ok and strat != "hold":
            forced = {"status": "validated_hold", "reason": vmsg}
    except Exception:
        pass
    act["_live_positions"] = positions
    res = forced if forced else ({"status": "hold"} if strat == "hold" else await safe_execute(act, c))
    rec = {
        "cycle": n, "pnl": pnl, "snapshot": s, "action": act, "result": res,
        "inventory": inventory, "validation": validation, "reasoning_chain": reasoning_chain,
    }
    h.append(rec)
    Path("rocket.log").open("a", encoding="utf-8").write(json.dumps(rec, default=str) + "\n")
    twk, tw = "none", {}
    tweak_before = dict(TWEAKS)
    if len(h) >= 2:
        tw = parse_json(await grok(
            g, f"Analyze last 2 cycles:\n{json.dumps(h[-2:], default=str)[:6000]}\n"
            'ONE tweak JSON: {{"type":"config|none","config":{{}},"summary":"one concrete tweak"}}',
        ))
        twk = apply_tweak(tw)
        Path("improvements.log").open("a", encoding="utf-8").write(
            json.dumps({"cycle": n, "tweak": tw}, default=str) + "\n")
    orders = s.get("open_orders") or []
    protection = s.get("protection") or {}
    return {
        "cycle": n, "pnl": pnl, "pnl_chg": pnl - prev, "equity": eq, "strat": strat,
        "result": res, "tweak": twk, "tweak_obj": tw, "risk": risk_label(s),
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
    }
