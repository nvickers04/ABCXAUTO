"""Heavy order-type testing lab — constant self-test & reconfigure heart.

Every rocket cycle runs a dry validation suite across all registered strategies
(schema + situational relevance + conId exit rules). Results drive automatic
TWEAKS reconfiguration. Nothing here places live orders (what-if / paper schema).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from abcxauto.proposals import STRATEGIES, ProposalValidationError, validate_proposal

# Core types always exercised; full registry exercised when session allows.
CORE_STOCK_ENTRY = ("bracket", "market_bracket")
CORE_STOCK_EXIT = ("market_order", "limit_order", "stop_order", "stop_limit")
CORE_PROTECT = ("oca", "trailing_stop", "trailing_stop_limit")
CORE_MANAGE = ("modify_stop", "modify_target", "modify_order", "cancel_order")
CORE_OPT = ("close_option", "vertical_spread", "iron_condor", "straddle", "strangle")
COMBO = (
    "iron_butterfly",
    "butterfly",
    "calendar_spread",
    "diagonal_spread",
    "covered_call",
    "protective_put",
    "collar",
    "ratio_spread",
    "jade_lizard",
)

LAB_LOG = Path("order_lab.log")


def _px(pulse: dict | None) -> float:
    try:
        last = ((pulse or {}).get("data_freshness") or {}).get("spy_last")
        if last is not None:
            return float(last)
    except (TypeError, ValueError):
        pass
    return 500.0


def _fixtures(px: float, positions: list | None = None) -> dict[str, dict]:
    """Synthetic but schema-valid params for every registered strategy."""
    long_stop, long_tgt = round(px * 0.98, 2), round(px * 1.02, 2)
    short_stop, short_tgt = round(px * 1.02, 2), round(px * 0.98, 2)
    exp = "20260718"
    exp2 = "20260815"
    strike = round(px)
    pos = positions or []
    stk = next(
        (
            p
            for p in pos
            if str(p.get("sec_type") or p.get("secType") or "STK").upper().startswith("STK")
        ),
        None,
    )
    opt = next(
        (
            p
            for p in pos
            if str(p.get("sec_type") or p.get("secType") or "").upper().startswith("OPT")
        ),
        None,
    )
    con_stk = (stk or {}).get("conId") or (stk or {}).get("con_id") or 270639
    con_opt = (opt or {}).get("conId") or (opt or {}).get("con_id") or 999001
    qty_stk = abs(int(float((stk or {}).get("quantity") or 1)))
    return {
        "bracket": {
            "symbol": "SPY",
            "quantity": 1,
            "direction": "LONG",
            "entry_price": px,
            "stop_price": long_stop,
            "target_price": long_tgt,
        },
        "market_bracket": {
            "symbol": "SPY",
            "quantity": 1,
            "direction": "LONG",
            "stop_price": long_stop,
            "target_price": long_tgt,
        },
        "market_order": {
            "symbol": "SPY",
            "action": "SELL",
            "quantity": qty_stk,
            "closing_position": True,
            "conId": con_stk,
        },
        "limit_order": {
            "symbol": "SPY",
            "action": "SELL",
            "quantity": qty_stk,
            "limit_price": px,
            "closing_position": True,
            "conId": con_stk,
        },
        "stop_order": {
            "symbol": "SPY",
            "action": "SELL",
            "quantity": qty_stk,
            "stop_price": long_stop,
            "closing_position": True,
        },
        "stop_limit": {
            "symbol": "SPY",
            "action": "SELL",
            "quantity": qty_stk,
            "stop_price": long_stop,
            "limit_price": round(long_stop * 0.999, 2),
            "closing_position": True,
        },
        "oca": {
            "symbol": "SPY",
            "quantity": qty_stk,
            "direction": "LONG",
            "stop_price": long_stop,
            "target_price": long_tgt,
        },
        "trailing_stop": {
            "symbol": "SPY",
            "quantity": qty_stk,
            "direction": "LONG",
            "trail_percent": 1.0,
        },
        "trailing_stop_limit": {
            "symbol": "SPY",
            "quantity": qty_stk,
            "direction": "LONG",
            "trail_percent": 1.0,
            "limit_offset": 0.5,
        },
        "modify_stop": {"order_id": 1, "new_stop_price": long_stop},
        "modify_target": {"order_id": 2, "new_limit_price": long_tgt},
        "modify_order": {"order_id": 3, "limit_price": px},
        "cancel_order": {"order_id": 4},
        "close_option": {
            "symbol": "SPY",
            "expiration": exp,
            "strike": float(strike),
            "right": "C",
            "quantity": 1,
        },
        "vertical_spread": {
            "symbol": "SPY",
            "expiration": exp,
            "right": "C",
            "long_strike": float(strike),
            "short_strike": float(strike + 5),
            "quantity": 1,
            "limit_price": 1.0,
        },
        "iron_condor": {
            "symbol": "SPY",
            "expiration": exp,
            "put_long_strike": float(strike - 20),
            "put_short_strike": float(strike - 10),
            "call_short_strike": float(strike + 10),
            "call_long_strike": float(strike + 20),
            "quantity": 1,
            "limit_price": 0.5,
        },
        "iron_butterfly": {
            "symbol": "SPY",
            "expiration": exp,
            "center_strike": float(strike),
            "wing_width": 10.0,
            "quantity": 1,
            "limit_price": 0.5,
        },
        "straddle": {
            "symbol": "SPY",
            "expiration": exp,
            "strike": float(strike),
            "quantity": 1,
            "action": "BUY",
            "limit_price": 2.0,
        },
        "strangle": {
            "symbol": "SPY",
            "expiration": exp,
            "put_strike": float(strike - 10),
            "call_strike": float(strike + 10),
            "quantity": 1,
            "action": "BUY",
            "limit_price": 1.5,
        },
        "butterfly": {
            "symbol": "SPY",
            "expiration": exp,
            "right": "C",
            "lower_strike": float(strike - 5),
            "middle_strike": float(strike),
            "upper_strike": float(strike + 5),
            "quantity": 1,
            "limit_price": 0.3,
        },
        "calendar_spread": {
            "symbol": "SPY",
            "near_expiration": exp,
            "far_expiration": exp2,
            "strike": float(strike),
            "right": "C",
            "quantity": 1,
            "limit_price": 0.4,
        },
        "diagonal_spread": {
            "symbol": "SPY",
            "near_expiration": exp,
            "far_expiration": exp2,
            "near_strike": float(strike),
            "far_strike": float(strike + 5),
            "right": "C",
            "quantity": 1,
            "limit_price": 0.4,
        },
        "covered_call": {
            "symbol": "SPY",
            "expiration": exp,
            "strike": float(strike + 5),
            "shares": 100,
        },
        "protective_put": {
            "symbol": "SPY",
            "expiration": exp,
            "strike": float(strike - 5),
            "shares": 100,
        },
        "collar": {
            "symbol": "SPY",
            "expiration": exp,
            "put_strike": float(strike - 10),
            "call_strike": float(strike + 10),
            "shares": 100,
        },
        "ratio_spread": {
            "symbol": "SPY",
            "expiration": exp,
            "right": "C",
            "long_strike": float(strike),
            "short_strike": float(strike + 5),
            "ratio": (1, 2),
            "quantity": 1,
            "limit_price": 0.1,
        },
        "jade_lizard": {
            "symbol": "SPY",
            "expiration": exp,
            "put_strike": float(strike - 10),
            "call_short_strike": float(strike + 5),
            "call_long_strike": float(strike + 15),
            "quantity": 1,
            "limit_price": 0.5,
        },
    }


def _session_status(pulse: dict | None) -> str:
    return str(((pulse or {}).get("session") or {}).get("status") or "closed").lower()


def strategies_for_session(session: str) -> list[str]:
    """Which strategy families are relevant to test given session liquidity."""
    names = list(CORE_STOCK_ENTRY) + list(CORE_STOCK_EXIT) + list(CORE_PROTECT) + list(CORE_MANAGE)
    if session == "regular":
        names += list(CORE_OPT) + list(COMBO)
    elif session in ("premarket", "postmarket"):
        # Extended: stock exits/entries only; skip complex options combos
        pass
    else:
        # Closed: still schema-test stock protect/exit (paper reconfig), not new risk entries
        names = list(CORE_STOCK_EXIT) + list(CORE_PROTECT) + list(CORE_MANAGE)
    # Always include every registered strategy at least for schema pass when regular
    if session == "regular":
        for k in STRATEGIES:
            if k not in names:
                names.append(k)
    return [n for n in names if n in STRATEGIES]


def _validate_schema(strategy: str, params: dict) -> dict:
    try:
        prop = validate_proposal(strategy, params, f"lab what-if test for {strategy}")
        return {
            "strategy": strategy,
            "pass": True,
            "phase": "schema",
            "gateway": prop.gateway_method,
            "detail": "ok",
            "expected_pnl_note": "schema-valid; live PnL TBD on execute",
        }
    except ProposalValidationError as e:
        return {
            "strategy": strategy,
            "pass": False,
            "phase": "schema",
            "detail": str(e)[:300],
            "expected_pnl_note": "n/a — rejected before send",
        }
    except Exception as e:
        return {
            "strategy": strategy,
            "pass": False,
            "phase": "schema",
            "detail": f"unexpected: {e}"[:300],
            "expected_pnl_note": "n/a",
        }


def run_order_lab(
    *,
    pulse: dict | None,
    positions: list | None = None,
    proposal: dict | None = None,
    history: list | None = None,
) -> dict:
    """Run heavy order testing for this cycle. Pure / dry-run (no broker)."""
    px = _px(pulse)
    session = _session_status(pulse)
    fixtures = _fixtures(px, positions)
    names = strategies_for_session(session)
    results: list[dict] = []

    for name in names:
        params = fixtures.get(name)
        if params is None:
            results.append(
                {
                    "strategy": name,
                    "pass": False,
                    "phase": "fixture",
                    "detail": "no lab fixture — build missing",
                    "expected_pnl_note": "n/a",
                }
            )
            continue
        results.append(_validate_schema(name, params))

    # Proposal-specific gates (inventory + impact) when Grok proposed a trade
    # Lazy imports avoid circular dependency with rocket.py
    from abcxauto.rocket import simulate_close_impact, validate_action_against_inventory

    prop_results: list[dict] = []
    if proposal:
        strat = (proposal.get("strategy") or proposal.get("action") or "hold").strip()
        if strat and strat not in ("hold", "none"):
            ok, msg = validate_action_against_inventory(proposal, positions or [])
            impact = simulate_close_impact(proposal, positions or [])
            prop_results.append(
                {
                    "strategy": strat,
                    "pass": bool(ok),
                    "phase": "proposal_inventory",
                    "detail": msg,
                    "expected_pnl_note": impact.get("gate") or "",
                    "impact": impact,
                }
            )
            # Schema check on actual proposal params
            try:
                validate_proposal(
                    strat,
                    proposal.get("params") or {},
                    proposal.get("rationale") or "proposal lab",
                )
                prop_results.append(
                    {
                        "strategy": strat,
                        "pass": True,
                        "phase": "proposal_schema",
                        "detail": "ok",
                    }
                )
            except Exception as e:
                prop_results.append(
                    {
                        "strategy": strat,
                        "pass": False,
                        "phase": "proposal_schema",
                        "detail": str(e)[:300],
                    }
                )

    # Mini suite on recent history — catch repeated conId / schema failures
    hist_results: list[dict] = []
    for rec in (history or [])[-3:]:
        act = rec.get("action") or {}
        st = (act.get("strategy") or act.get("action") or "").lower()
        val = str(rec.get("validation") or "")
        if st and st not in ("hold", "none"):
            hist_results.append(
                {
                    "cycle": rec.get("cycle"),
                    "strategy": st,
                    "pass": "rejected" not in val.lower()
                    and "system2_gate" not in val.lower(),
                    "phase": "recent_history",
                    "detail": val[:200],
                    "pnl": rec.get("pnl"),
                }
            )

    all_rows = results + prop_results + hist_results
    passed = sum(1 for r in all_rows if r.get("pass"))
    failed = sum(1 for r in all_rows if not r.get("pass"))
    report = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "px": px,
        "strategies_tested": len(results),
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / max(1, passed + failed), 3),
        "results": results,
        "proposal_tests": prop_results,
        "history_tests": hist_results,
        "summary": f"lab {passed} pass / {failed} fail (session={session})",
    }
    try:
        LAB_LOG.open("a", encoding="utf-8").write(json.dumps(report, default=str) + "\n")
    except OSError:
        pass
    return report


def auto_reconfig_from_lab(lab: dict, hist: list | None = None) -> dict:
    """Constant self-reconfigure: map lab/PnL signals → TWEAKS (no manual force)."""
    from abcxauto.rocket import TWEAKS, apply_tweak

    before = dict(TWEAKS)
    actions: list[str] = []
    rate = float(lab.get("pass_rate") or 0)
    failed_names = [
        r.get("strategy")
        for r in (lab.get("results") or [])
        if not r.get("pass")
    ]
    prop_fails = [r for r in (lab.get("proposal_tests") or []) if not r.get("pass")]

    # Tighten risk when lab is unhealthy
    if rate < 0.85 or failed_names:
        cfg = {
            "lab_min_pass_rate": 0.85,
            "max_risk_pct": min(float(TWEAKS.get("max_risk_pct", 1.0)), 0.5),
            "prefer_bracket_only": True,
        }
        if failed_names:
            cfg["lab_failed_strategies"] = sorted(set(str(x) for x in failed_names))[:12]
        msg = apply_tweak(
            {
                "type": "config",
                "config": cfg,
                "summary": f"auto-reconfig: lab rate={rate:.0%} fails={failed_names[:5]}",
            }
        )
        actions.append(msg)

    if prop_fails:
        msg = apply_tweak(
            {
                "type": "config",
                "config": {
                    "require_target_conId": True,
                    "hold_on_inventory_reject": True,
                },
                "summary": "auto-reconfig: proposal inventory/schema failed → stricter conId gates",
            }
        )
        actions.append(msg)

    # PnL attribution nudge from last cycles
    pnl_deltas = []
    for rec in (hist or [])[-5:]:
        try:
            pnl_deltas.append(float(rec.get("pnl_chg") or 0))
        except (TypeError, ValueError):
            pass
    if pnl_deltas and sum(pnl_deltas) < 0 and len(pnl_deltas) >= 3:
        sleep = float(TWEAKS.get("cycle_sleep_s", 8))
        msg = apply_tweak(
            {
                "type": "config",
                "config": {
                    "cycle_sleep_s": min(30.0, max(5.0, sleep * 1.25)),
                    "drawdown_slowdown": True,
                },
                "summary": "auto-reconfig: recent PnL negative → slower cycles",
            }
        )
        actions.append(msg)
    elif rate >= 0.95 and not prop_fails:
        # Healthy lab — allow slightly faster loop
        sleep = float(TWEAKS.get("cycle_sleep_s", 8))
        if sleep > 4:
            msg = apply_tweak(
                {
                    "type": "config",
                    "config": {"cycle_sleep_s": max(3.0, sleep * 0.9)},
                    "summary": "auto-reconfig: lab healthy → slightly faster cycles",
                }
            )
            actions.append(msg)

    if not actions:
        # Still record heartbeat reconfig of "none" for audit trail
        actions.append("auto-reconfig: none (lab stable)")

    return {
        "type": "auto_reconfig",
        "summary": "; ".join(actions)[:400],
        "config_before": before,
        "config_after": dict(TWEAKS),
        "actions": actions,
        "lab_pass_rate": rate,
    }


def format_lab_summary(lab: dict) -> str:
    lines = [
        f"ORDER LAB: {lab.get('summary')}  pass_rate={lab.get('pass_rate')}",
    ]
    fails = [r for r in (lab.get("results") or []) if not r.get("pass")]
    for r in fails[:8]:
        lines.append(f"  FAIL {r.get('strategy')} [{r.get('phase')}]: {r.get('detail')}")
    for r in (lab.get("proposal_tests") or [])[:4]:
        tag = "PASS" if r.get("pass") else "FAIL"
        lines.append(f"  {tag} proposal/{r.get('phase')}: {r.get('detail')}")
    return "\n".join(lines)
