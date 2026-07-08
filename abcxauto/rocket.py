"""Rocket loop helpers — snapshot, Grok JSON cycle, tweak merge."""

import asyncio, json, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from xai_sdk.chat import system, user

from abcxauto.executor import safe_execute
from abcxauto.llm import GrokClient
from abcxauto.monitor import build_protection_report
from abcxauto.proposals import STRATEGIES
from abcxauto.tools import run_readonly_tool

VALID_ACTIONS = "hold|bracket|market_bracket|oca|modify_stop|modify_target|cancel_order|close_option"
RULES = (
    "ABCXAUTO v0.1. Output ONLY valid JSON. Cash-only until 5 winning paper cycles. "
    "Max 1% risk/trade. Entries MUST be bracket/market_bracket with stop+target. "
    f"action/strategy MUST be one of: {VALID_ACTIONS}. Use hold if unsure."
)
TWEAKS: Dict[str, Any] = {}


def parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group()) if m else {"action": "hold"}


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
    if strat not in STRATEGIES:
        return "hold", {"status": "hold", "note": f"invalid strategy {strat!r} coerced to hold"}
    return strat, None


def risk_label(snap: dict) -> str:
    prot = snap.get("protection") or {}
    bad = prot.get("unprotected_symbols") or []
    return "COMPLIANT" if not bad else f"UNPROTECTED: {', '.join(bad)}"


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
    act = parse_json(await grok(
        g, f"Cycle {n}. Snapshot:\n{json.dumps(s, default=str)[:10000]}\n"
        f'JSON: {{"action":"{VALID_ACTIONS}","strategy":"...","params":{{}},"rationale":"..."}}',
    ))
    strat, forced = normalize_action(act)
    res = forced if forced else ({"status": "hold"} if strat == "hold" else await safe_execute(act, c))
    rec = {"cycle": n, "pnl": pnl, "snapshot": s, "action": act, "result": res}
    h.append(rec)
    Path("rocket.log").open("a", encoding="utf-8").write(json.dumps(rec, default=str) + "\n")
    twk, tw = "none", {}
    if len(h) >= 2:
        tw = parse_json(await grok(
            g, f"Analyze last 2 cycles:\n{json.dumps(h[-2:], default=str)[:6000]}\n"
            'ONE tweak JSON: {{"type":"config|none","config":{{}},"summary":"one concrete tweak"}}',
        ))
        twk = apply_tweak(tw)
        Path("improvements.log").open("a", encoding="utf-8").write(
            json.dumps({"cycle": n, "tweak": tw}, default=str) + "\n")
    pos_n = len(s.get("positions") or [])
    return {
        "cycle": n, "pnl": pnl, "pnl_chg": pnl - prev, "equity": eq, "strat": strat,
        "result": res, "tweak": twk, "tweak_obj": tw, "risk": risk_label(s),
        "portfolio": f"{pos_n} positions | {len(s.get('open_orders') or [])} orders",
    }