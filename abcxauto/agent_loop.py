"""Wake loop: snap facts, Grok tools, clerk gates on send.

``abcxauto.cycle`` re-exports this API for test/UI compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, List

from abcxauto.book import build_book_from_snap
from abcxauto.config import get_config
from abcxauto.connections import connection_status  # noqa: F401 — tests patch this
from abcxauto.llm import GrokClient
from abcxauto.memory import get_journal
from abcxauto.monitor import build_protection_report
from abcxauto.order_examples import SENDABLE_TYPES
from abcxauto.reality_pulse import build_reality_pulse
from abcxauto.send import send_action
from abcxauto.tools import run_readonly_tool
from abcxauto.trade_plan import (
    close_trade_plan,
    plan_from_bracket_action,
    save_trade_plan,
    sync_open_risk,
)
from abcxauto.world_state import (
    WorldState,
    build_world_state,
    capacity_allows_new_risk,
    day_facts,
    format_live_poke,
    format_wake,
)
from abcxauto.brain import grok, grok_turn

logger = logging.getLogger(__name__)

# Option multi-leg / lifecycle (executor already knows these).
_OPTION_ENTRY_ACTIONS = (
    "vertical_spread|iron_condor|iron_butterfly|butterfly|straddle|strangle|"
    "calendar_spread|diagonal_spread|buy_option|cash_secured_put|"
    "ratio_spread|jade_lizard"
)
ALLOWED_ACTIONS = frozenset(SENDABLE_TYPES)
VALID_ACTIONS = "|".join(sorted(ALLOWED_ACTIONS))
BLOCKED_STRAT = "blocked"
AWARENESS_HEART = (
    "\n=== SHELL ===\n"
    "Orders: ORDER EXAMPLES only. Close STK by conId. "
    "Hold forbidden while unprotected STK has no last-stop (code). "
    "Risk gates and defined_risk_only cannot be bypassed. "
    "self_tune cannot weaken the immutable floor.\n"
)
RULES = AWARENESS_HEART
SNAP_S = 25.0
_HIST_KEYS = (
    "cycle", "pnl", "pnl_chg", "reality_pulse",
    "result", "inventory",
    "validation", "reasoning_chain", "impact",
)
_HIST_CAP = 24
_NEW_RISK = frozenset(_OPTION_ENTRY_ACTIONS.split("|")) | frozenset(
    {"bracket", "market_bracket"}
)


def pnl_of(acct: dict) -> float:
    """Desktop / cycle Day PnL = IBKR DailyPnL. Not unrealized vs avg cost, not NL."""
    from abcxauto.world_state import daily_pnl_of

    v = daily_pnl_of(acct)
    return float(v) if v is not None else 0.0


def equity_of(acct: dict) -> float:
    for k in ("netliquidation", "NetLiquidation"):
        try:
            if acct.get(k) is not None:
                return float(acct[k])
        except (TypeError, ValueError):
            pass
    return 0.0


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


def _seed_live_quotes(*quotes: Any) -> dict[str, float]:
    qmap: dict[str, float] = {}
    for q in quotes:
        if not isinstance(q, dict):
            continue
        if q.get("error") and q.get("last") is None and q.get("mid") is None:
            continue
        last = q.get("last") if q.get("last") is not None else q.get("mid")
        sym = str(q.get("symbol") or "").upper()
        try:
            px = float(last)
        except (TypeError, ValueError):
            continue
        if sym and px > 0:
            qmap[sym] = px
    return qmap


async def _tool(c: Any, n: str, a: dict | None = None) -> Any:
    raw = await run_readonly_tool(n, a or {}, c)
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError, ValueError):
        logger.warning("snap tool %s returned non-json", n)
        return {"error": f"{n}: bad json"}


async def snap(c: Any) -> dict:
    try:
        acct, pos, orders, hours, spy, vix = await asyncio.wait_for(
            asyncio.gather(
                _tool(c, "account_summary"),
                _tool(c, "positions"),
                _tool(c, "open_orders"),
                _tool(c, "market_hours"),
                _tool(c, "quote", {"symbol": "SPY"}),
                _tool(c, "quote", {"symbol": "VIX"}),
                return_exceptions=True,
            ),
            timeout=SNAP_S,
        )
    except asyncio.TimeoutError:
        logger.warning("snap timed out after %.0fs", SNAP_S)
        acct = pos = orders = hours = spy = vix = TimeoutError("snap")
    pos_ok = isinstance(pos, list)
    ord_ok = isinstance(orders, list)
    if isinstance(acct, Exception):
        acct = {}
    if isinstance(pos, Exception) or isinstance(pos, dict):
        pos = []
    if isinstance(orders, Exception) or isinstance(orders, dict):
        orders = []
    if isinstance(hours, Exception):
        hours = {}
    if isinstance(spy, Exception):
        spy = {}
    if isinstance(vix, Exception) or not isinstance(vix, dict) or vix.get("error"):
        vix = {}
    pl = pos if isinstance(pos, list) else []
    ol = orders if isinstance(orders, list) else []
    acct_d = acct if isinstance(acct, dict) else {}
    try:
        acct_nl = float(
            acct_d.get("netliquidation") or acct_d.get("NetLiquidation") or 0
        )
    except (TypeError, ValueError):
        acct_nl = 0.0
    acct_ok = acct_nl > 0
    taken = datetime.now(timezone.utc).isoformat()
    protection = build_protection_report(pl, ol)
    base = {
        "taken_at": taken,
        "account": acct_d,
        "positions": pl, "open_orders": ol,
        "market_hours": hours if isinstance(hours, dict) else {},
        "spy_quote": spy if isinstance(spy, dict) else {},
        "vix_quote": vix if isinstance(vix, dict) else {},
        "protection": protection,
        "book_unreliable": not (pos_ok and ord_ok and acct_ok),
        "ibkr_live_quotes": _seed_live_quotes(spy, vix),
        "candle_source": "none",
    }
    base["reality_pulse"] = build_reality_pulse(
        account=base["account"], positions=pl, open_orders=ol,
        market_hours=base["market_hours"], spy_quote=base["spy_quote"],
        vix_quote=base["vix_quote"], protection=protection,
        ibkr_connected=bool(getattr(c, "connected", False)), taken_at=taken,
    )
    fills: list = []
    get_fills = getattr(c, "get_fills", None)
    if callable(get_fills):
        try:
            raw_fills = await get_fills()
            if isinstance(raw_fills, list):
                fills = raw_fills[:20]
        except Exception:
            logger.debug("snap fills failed", exc_info=True)
    base["fills"] = fills
    if any(
        str(p.get("secType") or p.get("sec_type") or "").upper() in ("OPT", "FOP")
        for p in pl
        if isinstance(p, dict)
    ):
        try:
            from abcxauto.option_facts import fetch_option_facts

            facts = await asyncio.wait_for(
                fetch_option_facts(pl, connector=c), timeout=8.0
            )
            if isinstance(facts, list):
                base["option_facts"] = facts
        except Exception:
            logger.debug("snap option_facts failed", exc_info=True)
    book_syms: list[str] = []
    seen_q = {"SPY", "VIX"}
    for p in pl:
        if not isinstance(p, dict):
            continue
        sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
        if sec in ("OPT", "FOP", "BAG"):
            continue
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or sym in seen_q:
            continue
        seen_q.add(sym)
        book_syms.append(sym)
        if len(book_syms) >= 6:
            break
    extra_quotes: list[Any] = []
    if book_syms:
        try:
            extra_quotes = list(
                await asyncio.wait_for(
                    asyncio.gather(
                        *[_tool(c, "quote", {"symbol": s}) for s in book_syms],
                        return_exceptions=True,
                    ),
                    timeout=8.0,
                )
            )
        except Exception:
            extra_quotes = []
    qmap = _seed_live_quotes(
        spy,
        vix,
        *[q for q in extra_quotes if isinstance(q, dict)],
    )
    base["ibkr_live_quotes"] = qmap
    keep = getattr(c, "ensure_book_ticks", None)
    if callable(keep) and pl:
        try:
            await asyncio.wait_for(keep(pl), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("ensure_book_ticks timed out")
        except Exception:
            logger.debug("ensure_book_ticks failed", exc_info=True)
    candle_source = "none"
    peek = getattr(c, "realtime_bar_buffer", None)
    if callable(peek):
        for p in pl:
            if not isinstance(p, dict):
                continue
            sec = str(p.get("secType") or p.get("sec_type") or "STK").upper()
            if sec in ("OPT", "FOP", "BAG"):
                continue
            sym = str(p.get("symbol") or "").strip().upper()
            if not sym:
                continue
            try:
                if peek(sym):
                    candle_source = "ibkr_rt_5s"
                    break
            except Exception:
                continue
    base["candle_source"] = candle_source
    base["portfolio_state"] = build_book_from_snap(base)
    return base


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


def ban_hold_active(cfg: Any = None) -> bool:
    """True when hold is not a ticket. Paper default ON; live default OFF; both two-way."""
    from abcxauto.config import get_config

    c = cfg if cfg is not None else get_config()
    if not hasattr(c, "ban_hold"):
        mode = str(getattr(c, "trading_mode", "paper") or "paper").strip().lower()
        return mode != "live"
    return bool(getattr(c, "ban_hold"))


def _new_risk_halted(world: WorldState) -> bool:
    gates = world.gates if isinstance(world.gates, dict) else {}
    if gates.get("halted") or gates.get("is_halted"):
        return True
    book = world.book if isinstance(world.book, dict) else {}
    if book.get("halted"):
        return True
    try:
        from abcxauto.risk_gates import get_risk_gate

        return bool(get_risk_gate().is_halted)
    except Exception:
        return False


def _wake_grok_for_session(
    session: str,
    *,
    needs_prot: bool,
    countdown_s: float | None = None,
    countdown_to: str = "",
) -> bool:
    """Unprotected always. Regular and premarket if Grok is due. Not overnight closed."""
    if needs_prot:
        return True
    sess = str(session or "").lower()
    return sess in ("regular", "premarket")


def _book_unreliable(world: WorldState | None = None, snap: dict | None = None) -> bool:
    if isinstance(snap, dict) and snap.get("book_unreliable"):
        return True
    gates = getattr(world, "gates", None) if world is not None else None
    return bool(isinstance(gates, dict) and gates.get("book_unreliable"))


def is_new_risk(strat: str, params: dict | None = None) -> bool:
    """True for a new structure. A live combo close is not new risk."""
    st = str(strat or "").lower()
    if st not in _NEW_RISK:
        return False
    p = params if isinstance(params, dict) else {}
    if p.get("closing_position") is True:
        return False
    return True


_WORK_TOOLS = frozenset({
    "book",
    "scan",
    "option_facts",
    "quote",
    "option_quote",
    "option_chain",
    "fills",
    "news",
    "candles",
    "odds",
    "status",
    "self_tune",
})


def turn_did_work(turn: Any) -> bool:
    """True if this turn tuned or fetched live facts. Notes-only is not work."""
    for name in getattr(turn, "tool_trace", None) or []:
        if str(name) in _WORK_TOOLS:
            return True
    for item in getattr(turn, "sends", None) or []:
        strat = str((item or {}).get("strat") or "").lower()
        if strat in ("self_tune", "set_risk"):
            return True
    return False


def gate_ticket(act: dict, world: WorldState) -> tuple[str, dict | None]:
    """Clerk gates on send. Returns (strat, forced_result_or_None)."""
    from abcxauto.protect import promote_naked_entry

    promote_naked_entry(act, list(getattr(world, "positions", None) or []))
    strat, forced = normalize_action(act)
    if forced is not None:
        return BLOCKED_STRAT, forced
    needs_prot = bool(getattr(world, "needs_protection", False) or getattr(world, "unprotected", None))
    if strat == "hold" and needs_prot:
        # Unprotected STK last-stop is Risk/protect — not the ban_hold chip.
        return BLOCKED_STRAT, {
            "status": "blocked",
            "note": "hold_forbidden - unprotected STK needs a last-stop",
        }
    if strat == "hold" and ban_hold_active():
        return BLOCKED_STRAT, {
            "status": "blocked",
            "note": "hold is not a ticket",
        }
    params = act.get("params") if isinstance(act.get("params"), dict) else {}
    from abcxauto.world_state import single_leg_vertical_block

    vert_note = single_leg_vertical_block(
        strat, params, getattr(world, "positions", None)
    )
    if vert_note:
        return BLOCKED_STRAT, {"status": "blocked", "note": vert_note}
    if is_new_risk(strat, params):
        if _book_unreliable(world):
            return BLOCKED_STRAT, {
                "status": "blocked",
                "note": "book unreliable — no new risk",
            }
        if needs_prot:
            return BLOCKED_STRAT, {
                "status": "blocked",
                "note": "unprotected lots — protect first (no new risk)",
            }
        try:
            from abcxauto.trade_plan import load_flat_streak

            if load_flat_streak() > 0:
                return BLOCKED_STRAT, {
                    "status": "blocked",
                    "note": "book flat unconfirmed — wait before new risk",
                }
        except Exception:
            pass
        if not capacity_allows_new_risk(world):
            return BLOCKED_STRAT, {
                "status": "blocked",
                "note": "capacity full — no new risk (max_open_positions)",
            }
        sym = str(((act.get("params") or {}).get("symbol") or "")).upper()
        if not sym:
            return BLOCKED_STRAT, {
                "status": "blocked",
                "note": "new risk requires params.symbol",
            }
        cool = getattr(world, "structure_cooldown", None) or {}
        if isinstance(cool, dict) and sym in cool:
            why = cool.get(sym) or "scrape/geometry"
            return BLOCKED_STRAT, {
                "status": "blocked",
                "note": f"structure cooldown {sym}: {why}",
            }
        from abcxauto.lab_playbook import live_new_risk_allowed

        if not live_new_risk_allowed():
            return BLOCKED_STRAT, {
                "status": "blocked",
                "note": "live follower — no promoted paper playbook (no new risk)",
            }
    return strat, None


async def execute_ticket(
    act: dict,
    connector: Any,
    world: WorldState,
    snap: dict,
) -> dict:
    """Normalize, gate, geometry, then send_action. Never bypass the clerk."""
    positions = list(snap.get("positions") or world.positions or [])
    strat, forced = gate_ticket(act, world)
    if forced is not None:
        act["strategy"] = act["action"] = BLOCKED_STRAT
        act["rationale"] = str(forced.get("note") or act.get("rationale") or "")
        return forced

    if strat != BLOCKED_STRAT:
        from abcxauto.structure_grade import append_structure_event
        from abcxauto.trade_playbook import check_overlay_shares

        ok_sh, sh_code, sh_msg = check_overlay_shares(
            strat, act.get("params") or {}, positions
        )
        if not ok_sh:
            try:
                append_structure_event(
                    {
                        "source": "cycle",
                        "strategy": strat,
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
            act["strategy"] = act["action"] = BLOCKED_STRAT
            act["_structure_grade"] = sh_code
            return {"status": "blocked", "note": sh_msg, "reason_code": sh_code}

    _prepare_close_params(act, positions)
    try:
        ok, vmsg = validate_action_against_inventory(act, positions)
        if not ok and strat not in (BLOCKED_STRAT, "skipped", "set_risk", "self_tune", "hold"):
            act["strategy"] = act["action"] = BLOCKED_STRAT
            return {"status": "validated_block", "reason": vmsg}
    except Exception:
        pass

    impact = simulate_close_impact(act, positions)
    act["_live_positions"], act["_impact"] = positions, impact

    quote_last = await _quote_for_action(act, snap, connector)
    if quote_last is not None:
        act["_quote_last"] = quote_last
        params = act.setdefault("params", {})
        if isinstance(params, dict) and params.get("price_hint") is None:
            params["price_hint"] = quote_last
    act["_posture"] = world.effective_posture or world.risk_posture
    from abcxauto.protect import fill_missing_protection

    try:
        cfg = get_config()
    except Exception:
        cfg = None
    fill_missing_protection(
        act,
        quote_last=quote_last,
        equity=equity_of(snap.get("account") or {}) or float(getattr(world, "net_liquidation", 0) or 0),
        posture=str(act["_posture"] or "balanced"),
        cfg=cfg,
        positions=positions,
    )
    strat = str(act.get("strategy") or act.get("action") or strat).strip().lower()
    chosen_strat = strat

    if strat in ("market_bracket", "oca", "bracket"):
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
                        for k in ("stop_price", "target_price", "entry_price", "quantity")
                    },
                    "outcome": GEOMETRY_REJECTED,
                    "reason_code": code,
                    "message": gmsg[:300],
                }
            )
            result = {
                "status": "rejected",
                "error": f"{code}: {gmsg}",
                "reason_code": code,
                "learn": gmsg,
            }
            await _post_act_structure_and_plan(
                act=act, strat=chosen_strat, result=result, judgment={},
                snap=snap, quote_last=quote_last, connector=connector,
            )
            return result

    if strat == "hold":
        result = {"status": "hold", "strategy": "hold"}
        act["_structure_grade"] = "hold"
    elif strat in ALLOWED_ACTIONS:
        result = await send_action(act, connector)
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
        strat=chosen_strat,
        result=result or {},
        judgment={},
        snap=snap,
        quote_last=quote_last,
        connector=connector,
    )
    act["strategy"] = strat
    act["action"] = act.get("action") or strat
    return result or {"status": "blocked"}


def stance_from_book(strat: str, s: dict) -> str:
    """Pacing/UI label from the book — not a Judge form."""
    if (s.get("protection") or {}).get("unprotected_symbols"):
        return "protect"
    st = str(strat or "").lower()
    if st in _NEW_RISK:
        return "new_entry"
    if s.get("positions"):
        return "manage"
    if st in ("hold", "skipped", "blocked", BLOCKED_STRAT):
        return "idle"
    return "idle"


def _result_dict(
    *, n: int, s: dict, act: dict, strat: str, result: dict,
    pnl: float, eq: float, prev: float, inventory: str, validation: str,
    kahneman: dict | None = None, impact: dict | None = None,
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
        "strat": strat, "result": result,
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
        "impact": impact or {},
        "reality_pulse": s.get("reality_pulse") or {},
        "kahneman": kahneman or {}, "kahneman_trace": "",
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
        "stance": j.get("stance") or stance_from_book(strat, s),
        "thesis": j.get("thesis") or rationale,
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
        "book_unreliable": bool(s.get("book_unreliable")),
    }


def _journal_stages(
    out: dict, act: dict, s: dict, judgment: dict | None
) -> None:
    try:
        journal = get_journal()
        strat = str(out.get("strat") or "")
        result = out.get("result") or {}
        j = judgment or {}
        if j:
            journal.record_judgment(
                cycle=out.get("cycle"),
                stance=str(j.get("stance") or out.get("stance") or ""),
                thesis=str(j.get("thesis") or out.get("thesis") or ""),
                focus=str(j.get("focus") or ""),
                dismissed=str(j.get("dismissed") or ""),
                intent=j.get("intent") or {},
                judgment=j,
            )
        thesis = str(j.get("thesis") or out.get("thesis") or act.get("rationale") or "").strip()
        if thesis and strat not in ("hold", "skipped", "blocked", BLOCKED_STRAT):
            journal.set_working_thesis(thesis[:400])
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


def _order_id_of(order: dict) -> int | None:
    raw = order.get("order_id") if order.get("order_id") is not None else order.get("orderId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _collapse_stacked_exits_after_snap(c: Any, s: dict) -> None:
    """Keep one covering STP/TRAIL per STK lot; cancel extras. Never flatten."""
    try:
        from abcxauto.executor import collapse_stacked_protective_exits

        cancelled = await collapse_stacked_protective_exits(
            c, s.get("positions") or [], s.get("open_orders") or []
        )
        if not cancelled:
            return
        drop = set(cancelled)
        s["open_orders"] = [
            o for o in (s.get("open_orders") or [])
            if _order_id_of(o) not in drop
        ]
        pl = s.get("positions") or []
        ol = s.get("open_orders") or []
        s["protection"] = build_protection_report(pl, ol)
        try:
            s["reality_pulse"] = build_reality_pulse(
                account=s.get("account") or {},
                positions=pl,
                open_orders=ol,
                market_hours=s.get("market_hours") or {},
                spy_quote=s.get("spy_quote") or {},
                vix_quote=s.get("vix_quote") or {},
                protection=s["protection"],
                ibkr_connected=bool(getattr(c, "connected", False)),
                taken_at=str(s.get("taken_at") or ""),
            )
        except Exception:
            logger.debug("reality_pulse rebuild after collapse failed", exc_info=True)
    except Exception:
        logger.exception("stacked protective-exit collapse failed")


def _persist_cycle(out: dict) -> dict:
    try:
        from abcxauto.think_stream import write_last_turn

        write_last_turn(out)
    except Exception:
        logger.debug("last_turn persist failed", exc_info=True)
    return out


def _append_hist(h: List[dict], rec: dict) -> None:
    h.append(rec)
    if len(h) <= _HIST_CAP:
        return
    del h[:-_HIST_CAP]
    for old in h[:-3]:
        old.pop("snapshot", None)


async def run_cycle(
    n: int,
    c: Any,
    g: GrokClient,
    h: List[dict],
    prev: float,
) -> dict:
    """Snap facts, Grok tools, clerk on send, journal."""
    s = await snap(c)
    await _collapse_stacked_exits_after_snap(c, s)
    positions = s.get("positions") or []
    for p in positions:
        if "conId" not in p and "con_id" in p:
            p["conId"] = p["con_id"]
    pulse = s.get("reality_pulse") or {}
    acct = s.get("account") or {}
    pnl, eq = pnl_of(acct), equity_of(acct)
    inventory = format_position_inventory(positions)
    sess_block = pulse.get("session") if isinstance(pulse.get("session"), dict) else {}
    session = str(sess_block.get("status") or "").lower()
    try:
        countdown_s = float(sess_block["countdown_s"]) if sess_block.get("countdown_s") is not None else None
    except (TypeError, ValueError):
        countdown_s = None
    countdown_to = str(sess_block.get("countdown_to") or "")
    needs_prot = bool((s.get("protection") or {}).get("unprotected_symbols"))
    ibkr_up = bool(getattr(c, "connected", False))

    if not ibkr_up:
        act = {
            "action": "skipped", "strategy": "skipped",
            "rationale": "skipped_grok: ibkr_down",
        }
        out = _result_dict(
            n=n, s=s, act=act, strat="skipped",
            result={"status": "skipped", "note": "skipped_grok: ibkr_down"},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation="skipped_grok: ibkr_down",
        )
        _journal_stages(out, act, s, None)
        _append_hist(h, {"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return _persist_cycle(out)

    if s.get("book_unreliable"):
        act = {
            "action": "skipped", "strategy": "skipped",
            "rationale": "skipped_grok: book_unreliable",
        }
        out = _result_dict(
            n=n, s=s, act=act, strat="skipped",
            result={"status": "skipped", "note": "skipped_grok: book_unreliable"},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation="skipped_grok: book_unreliable",
        )
        _journal_stages(out, act, s, None)
        _append_hist(h, {"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return _persist_cycle(out)

    if not _wake_grok_for_session(
        session,
        needs_prot=needs_prot,
        countdown_s=countdown_s,
        countdown_to=countdown_to,
    ):
        if session in ("", "closed"):
            why = "session_closed"
        else:
            why = "session_extended"
        act = {
            "action": "skipped", "strategy": "skipped",
            "rationale": f"skipped_grok: {why}",
        }
        out = _result_dict(
            n=n, s=s, act=act, strat="skipped",
            result={"status": "skipped", "note": f"skipped_grok: {why}"},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation=f"skipped_grok: {why}",
        )
        _journal_stages(out, act, s, None)
        _append_hist(h, {"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return _persist_cycle(out)

    from abcxauto.think_stream import emit as think_emit

    think_emit("say", "Book snap done — Grok has the tools.\n")

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

    s.setdefault("news_items", [])
    s.setdefault("option_facts", [])
    s.setdefault("opportunities", [])
    world = build_world_state(
        cycle=n, snap=s, opportunities=[], news_items=[],
    )
    world_dict = world.to_dict()
    day = None
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard(equity=getattr(world, "net_liquidation", None))
        day = day_facts(world, sc)
    except Exception:
        day = None
    wake = format_wake(
        cycle=n,
        session=world.session_status,
        flat=world.flat,
        unprotected=world.unprotected,
        ibkr_up=ibkr_up,
        day=day,
    )
    # Live episode continue: thin poke, not a second wake dump / system prompt.
    try:
        from abcxauto.brain import EPISODE_KINDS
        from abcxauto.wake_bus import last_wake

        ev = last_wake()
        kind = str(ev.kind or "") if ev is not None else ""
        if getattr(g, "chat", None) is not None and kind in EPISODE_KINDS:
            wake = format_live_poke(
                kind=kind,
                detail=str(ev.detail or "") if ev is not None else "",
                session=world.session_status,
                flat=world.flat,
                unprotected=world.unprotected,
                day=day,
            )
    except Exception:
        pass
    think_emit("say", "Wake Grok.\n")
    try:
        from abcxauto.think_stream import write_last_turn

        write_last_turn({
            "cycle": n,
            "strat": "in_progress",
            "rationale": "grok_turn",
            "validation": "",
            "book_unreliable": bool(s.get("book_unreliable")),
            "sends": 0,
            "positions": list(world.positions or []),
            "reality_pulse": s.get("reality_pulse") or {},
            "world_state": world_dict,
        })
    except Exception:
        logger.debug("in-progress last_turn write failed", exc_info=True)

    try:
        turn = await grok_turn(g, connector=c, world=world, snap=s, wake=wake)
    except Exception as exc:
        logger.exception("grok_turn failed")
        act = {
            "action": BLOCKED_STRAT, "strategy": BLOCKED_STRAT,
            "rationale": f"grok_error: {exc}",
        }
        out = _result_dict(
            n=n, s=s, act=act, strat=BLOCKED_STRAT,
            result={"status": "blocked", "note": f"grok_error: {exc}"},
            pnl=pnl, eq=eq, prev=prev, inventory=inventory,
            validation=f"grok_error: {exc}",
            world=world_dict, stage_error=str(exc),
        )
        _journal_stages(out, act, s, None)
        _append_hist(h, {"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS}})
        return _persist_cycle(out)

    act = dict(turn.last_act or {})
    result = dict(turn.last_result or {})
    strat = str(turn.last_strat or act.get("strategy") or "hold")
    if not turn.sends and strat not in (BLOCKED_STRAT, "blocked"):
        # No send this look. Brain may decorate last_* as hold; that is not a ticket.
        if ban_hold_active():
            # Rest is rest — do not invent hold for gate_ticket / journal.
            act = {}
            strat = ""
            result = {}
        else:
            # Live / ban_hold off: no-send remains a hold no-op (gate still applies).
            act = act if act else {
                "action": "hold", "strategy": "hold", "rationale": "no send",
            }
            if not str(act.get("strategy") or act.get("action") or "").strip():
                act = {
                    **act,
                    "action": "hold",
                    "strategy": "hold",
                    "rationale": act.get("rationale") or "no send",
                }
            strat, forced = gate_ticket(act, world)
            if forced is not None:
                result = forced
                act["strategy"] = act["action"] = BLOCKED_STRAT
                act["rationale"] = str(forced.get("note") or "")
            else:
                strat = "hold"
                act.setdefault("action", "hold")
                act.setdefault("strategy", "hold")
                result = {"status": "hold", "strategy": "hold"}

    s["opportunities"] = list(world.opportunities or [])
    s["news_items"] = list(world.news_items or [])
    s["option_facts"] = list(world.option_facts or [])
    if turn.text and not act.get("market_read"):
        act["market_read"] = turn.text[:400]
    tool_note = ""
    if getattr(turn, "tool_budget_hit", False):
        tool_note = "tool_rounds_exhausted"
    impact = act.get("_impact") or simulate_close_impact(act, positions)
    out = _result_dict(
        n=n, s=s, act=act, strat=strat, result=result,
        pnl=pnl, eq=eq, prev=prev, inventory=inventory,
        validation=str(result.get("note") or result.get("status") or "ok"),
        impact=impact, world=world.to_dict(),
        judgment={"lab_playbook": turn.lab_playbook} if turn.lab_playbook else {},
        stage_error=tool_note,
    )
    out["tool_trace"] = list(turn.tool_trace or [])
    out["sends"] = len(turn.sends or [])
    if turn.sends:
        for item in turn.sends:
            _journal_stages(
                {**out, "strat": item.get("strat") or out.get("strat"),
                 "result": item.get("result") or {},
                 "rationale": (item.get("act") or {}).get("rationale") or ""},
                item.get("act") or act, s, None,
            )
    else:
        _journal_stages(out, act, s, None)
    _append_hist(h, {"snapshot": s, "action": act, **{k: out[k] for k in _HIST_KEYS if k in out}})
    return _persist_cycle(out)


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
    Grok price_hint / entry only when no IBKR live (manage / protect paths).
    New-entry brackets fail closed without IBKR live (returns None → geometry block).
    """
    params = act.get("params") or {}
    sym = str(params.get("symbol") or "").upper()
    strat = str(act.get("strategy") or act.get("action") or "").lower()
    needs_live_geometry = strat in ("bracket", "market_bracket")
    live: float | None = None
    if sym and connector is not None:
        try:
            q = await _tool(connector, "quote", {"symbol": sym, "fresh": True})
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
    if live is None and sym:
        qmap = snap.get("ibkr_live_quotes") or {}
        if isinstance(qmap, dict) and qmap.get(sym) is not None:
            try:
                v = float(qmap[sym])
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
    # New-entry brackets: do not fall back to MDA tape / hints as "live"
    if needs_live_geometry:
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
        strat not in (BLOCKED_STRAT, "hold", "skipped", "set_risk", "self_tune")
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
                            "rebuild stop from live quote next look"
                        ),
                    }
                )
                close_trade_plan("scrape_wrong_side_stop")
            else:
                plan = plan_from_bracket_action(act, str(judgment.get("thesis") or ""))
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
            from abcxauto.trade_plan import load_trade_plan, stk_qty_for_symbol

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
