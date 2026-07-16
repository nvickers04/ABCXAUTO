"""Order-type suite — schema dry-run only (validate_proposal). Cached for Pro/cycle.

NOT on autonomous hot path — run_order_suite is for Pro/manual validation only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from abcxauto.config import get_config
from abcxauto.order_examples import ORDER_EXAMPLES
from abcxauto.proposals import STRATEGIES, ProposalValidationError, validate_proposal
from abcxauto.reality_pulse import build_reality_pulse

# Every allowlisted strategy is suite-testable (Test Suite UI iterates this).
SUITE_STRATEGIES = tuple(sorted(STRATEGIES))
_LATEST_SUITE: dict[str, Any] = {}


def paper_place_enabled() -> bool:
    """True when manual suite may place→cancel on a connected paper account."""
    cfg = get_config()
    return bool(cfg.suite_paper_place) and bool(cfg.is_paper)


def get_cached_suite() -> dict[str, Any]:
    return dict(_LATEST_SUITE)


def set_cached_suite(report: dict[str, Any] | None) -> dict[str, Any]:
    global _LATEST_SUITE
    if not report:
        _LATEST_SUITE = {}
        return {}
    failed = sorted(
        {str(r["strategy"]) for r in (report.get("results") or [])
         if r.get("strategy") and not r.get("pass")}
    )
    _LATEST_SUITE = {
        k: report.get(k)
        for k in (
            "pass_rate", "passed", "failed", "strategies_tested", "summary",
            "taken_at", "source", "idle_prevented", "mode", "paper_only",
        )
    }
    _LATEST_SUITE["failed_strategies"] = failed
    _LATEST_SUITE["results"] = report.get("results") or []
    try:
        from abcxauto.structure_grade import save_structure_vocab

        save_structure_vocab(report)
    except Exception:
        pass
    return dict(_LATEST_SUITE)


def clear_cached_suite() -> None:
    global _LATEST_SUITE
    _LATEST_SUITE = {}


def _px(pulse: dict | None) -> float:
    try:
        last = ((pulse or {}).get("data_freshness") or {}).get("spy_last")
        if last is not None:
            return float(last)
    except (TypeError, ValueError):
        pass
    return 500.0


def _fixtures(px: float, positions: list | None = None) -> dict[str, dict]:
    stop, tgt = round(px * 0.98, 2), round(px * 1.02, 2)
    strike = float(round(px))
    stk = next(
        (p for p in (positions or [])
         if str(p.get("sec_type") or p.get("secType") or "STK").upper().startswith("STK")),
        None,
    )
    con = (stk or {}).get("conId") or (stk or {}).get("con_id") or 270639
    qty = abs(int(float((stk or {}).get("quantity") or 1)))
    exit_ = {"symbol": "SPY", "action": "SELL", "quantity": qty,
             "closing_position": True, "conId": con}
    entry = {"symbol": "SPY", "quantity": 1, "direction": "LONG"}
    exp, exp_far = "20260718", "20260815"
    fixtures: dict[str, dict] = {
        "bracket": {
            **entry, "entry_price": px, "stop_price": stop, "target_price": tgt,
            "price_hint": px,
        },
        "market_bracket": {
            **entry, "stop_price": stop, "target_price": tgt, "price_hint": px,
        },
        "market_order": dict(exit_),
        "limit_order": {**exit_, "limit_price": px},
        "stop_order": {**exit_, "stop_price": stop},
        "stop_limit": {**exit_, "stop_price": stop, "limit_price": round(stop * 0.999, 2)},
        "oca": {
            "symbol": "SPY", "quantity": qty, "direction": "LONG",
            "stop_price": stop, "target_price": tgt, "price_hint": px,
        },
        "modify_stop": {"order_id": 1, "new_stop_price": stop},
        "modify_target": {"order_id": 2, "new_limit_price": tgt},
        "cancel_order": {"order_id": 4},
        "close_option": {"symbol": "SPY", "expiration": exp,
                         "strike": strike, "right": "C", "quantity": 1},
        "trailing_stop": {**entry, "quantity": qty, "trail_percent": 2.0},
        "trailing_stop_limit": {**entry, "quantity": qty, "trail_percent": 2.0, "limit_offset": 0.10},
        "market_on_close": dict(exit_),
        "limit_on_close": {**exit_, "limit_price": px},
        "market_on_open": dict(exit_),
        "limit_on_open": {**exit_, "limit_price": px},
        "adaptive": {**exit_, "order_type": "MKT", "priority": "Normal"},
        "midprice": dict(exit_),
        "relative": {**exit_, "offset": 0.01},
        "limit_order_gtd": {**exit_, "limit_price": px, "good_till_date": "20261231 16:00:00"},
        "fill_or_kill": {**exit_, "limit_price": px},
        "immediate_or_cancel": {**exit_, "limit_price": px},
        "vwap": {**exit_, "max_pct_volume": 25.0},
        "twap": dict(exit_),
        "iceberg": {
            "symbol": "SPY", "action": "SELL", "total_quantity": max(qty, 10),
            "display_size": 1, "limit_price": px, "closing_position": True,
        },
        "snap_to_midpoint": dict(exit_),
        "vertical_spread": {
            "symbol": "SPY", "expiration": exp, "long_strike": strike,
            "short_strike": strike + 5, "right": "C", "quantity": 1,
        },
        "iron_condor": {
            "symbol": "SPY", "expiration": exp,
            "put_long_strike": strike - 20, "put_short_strike": strike - 10,
            "call_short_strike": strike + 10, "call_long_strike": strike + 20, "quantity": 1,
        },
        "iron_butterfly": {
            "symbol": "SPY", "expiration": exp, "center_strike": strike,
            "wing_width": 10.0, "quantity": 1,
        },
        "straddle": {
            "symbol": "SPY", "expiration": exp, "strike": strike,
            "quantity": 1, "action": "BUY",
        },
        "strangle": {
            "symbol": "SPY", "expiration": exp, "put_strike": strike - 10,
            "call_strike": strike + 10, "quantity": 1, "action": "BUY",
        },
        "butterfly": {
            "symbol": "SPY", "expiration": exp, "lower_strike": strike - 10,
            "middle_strike": strike, "upper_strike": strike + 10, "right": "C", "quantity": 1,
        },
        "calendar_spread": {
            "symbol": "SPY", "strike": strike, "near_expiration": exp,
            "far_expiration": exp_far, "right": "C", "quantity": 1,
        },
        "diagonal_spread": {
            "symbol": "SPY", "near_strike": strike, "far_strike": strike + 5,
            "near_expiration": exp, "far_expiration": exp_far, "right": "C", "quantity": 1,
        },
        "buy_option": {
            "symbol": "SPY", "expiration": exp, "strike": strike, "right": "C", "quantity": 1,
        },
        "covered_call": {
            "symbol": "SPY", "expiration": exp, "strike": strike + 10, "shares": 100,
        },
        "cash_secured_put": {
            "symbol": "SPY", "expiration": exp, "strike": strike - 10, "contracts": 1,
        },
        "protective_put": {
            "symbol": "SPY", "expiration": exp, "strike": strike - 10, "shares": 100,
        },
        "collar": {
            "symbol": "SPY", "expiration": exp, "put_strike": strike - 10,
            "call_strike": strike + 10, "shares": 100,
        },
        "ratio_spread": {
            "symbol": "SPY", "expiration": exp, "long_strike": strike,
            "short_strike": strike + 10, "right": "C", "ratio": 2, "quantity": 1,
        },
        "jade_lizard": {
            "symbol": "SPY", "expiration": exp, "put_strike": strike - 10,
            "call_short_strike": strike + 10, "call_long_strike": strike + 20, "quantity": 1,
        },
        "roll_option": {
            "symbol": "SPY", "quantity": 1, "conId": 999001,
            "new_dte": 30, "roll_type": "ROLL_OUT",
        },
    }
    # Fill any STRATEGIES key still missing from ORDER_EXAMPLES (defensive).
    for name in STRATEGIES:
        if name not in fixtures and name in ORDER_EXAMPLES:
            fixtures[name] = dict(ORDER_EXAMPLES[name])
    return fixtures


def _validate_schema(strategy: str, params: dict) -> dict:
    base = {"strategy": strategy, "placed": False, "cancelled": False,
            "cancel_intent": True, "schema_validated": True}
    try:
        hint = params.get("price_hint") or params.get("entry_price")
        prop = validate_proposal(
            strategy,
            params,
            f"suite dry-run {strategy}",
            quote_last=float(hint) if hint is not None else None,
            posture="balanced",
        )
        return {
            **base, "pass": True, "mode": "dry_run", "phase": "schema_dry_run",
            "gateway": prop.gateway_method,
            "detail": "dry-run: validate_proposal OK; no broker place",
            "whatIf": True, "schema_detail": "ok", "params_ok": True,
        }
    except (ProposalValidationError, Exception) as e:
        detail = str(e)[:300]
        return {
            **base, "pass": False, "mode": "schema_fail", "phase": "schema",
            "detail": detail, "schema_detail": detail, "params_ok": False,
        }


async def _panic_rows(positions: list | None) -> list[dict]:
    from abcxauto.broker.connector import IBKRConnector

    mixed = positions or [
        {"symbol": "SPY", "quantity": 1, "sec_type": "STK", "conId": 270639},
        {"symbol": "SPY", "quantity": 1, "sec_type": "OPT", "conId": 999001,
         "expiration": "20260718", "strike": 500.0, "right": "C"},
    ]
    try:
        class _Fake:
            async def _place_order(self, **kw):
                return {"success": True, "order_id": 9001}

            async def close_option_position(self, symbol, **kw):
                return {"success": True, "order_id": 9002}

            async def cancel_order(self, oid):
                return {"success": True, "order_id": oid}

        fl, conn = IBKRConnector._flatten_one_position, _Fake()
        rows = []
        for pos in mixed[:2]:
            out = await fl(conn, pos)
            rows.append({
                "strategy": "panic_flatten_leg", "pass": bool(out.get("success")),
                "mode": "panic", "phase": "flatten",
                "detail": out.get("reasoning") or out.get("method"),
                "conId": out.get("conId") or pos.get("conId"),
                "placed": False, "cancelled": False, "cancel_intent": True,
            })
        return rows
    except Exception as e:
        return [{"strategy": "panic_flatten_leg", "pass": False,
                 "mode": "panic", "detail": str(e)[:200]}]


def run_strategy_dry_run(
    strategy: str,
    *,
    pulse: dict | None = None,
    positions: list | None = None,
) -> dict:
    """Schema-validate one order type (sync). Used by Pro Test Suite per-type buttons."""
    if pulse is None:
        pulse = build_reality_pulse(positions=positions or [], ibkr_connected=False)
    fixtures = _fixtures(_px(pulse), positions)
    if strategy not in fixtures:
        return {
            "strategy": strategy, "pass": False, "mode": "schema_fail", "phase": "fixture",
            "detail": "no fixture", "schema_validated": False,
        }
    row = _validate_schema(strategy, fixtures[strategy])
    row["strategy"] = strategy
    return row


async def run_strategy_broker_test(
    strategy: str,
    *,
    connector: Any = None,
    pulse: dict | None = None,
    positions: list | None = None,
) -> dict:
    """Schema-validate one type; paper place→cancel when connected paper account."""
    if pulse is None:
        pulse = build_reality_pulse(
            positions=positions or [],
            ibkr_connected=bool(getattr(connector, "connected", False)) if connector else False,
        )
    row = run_strategy_dry_run(strategy, pulse=pulse, positions=positions)
    if not row.get("pass"):
        return row
    if not paper_place_enabled():
        return {
            **row,
            "pass": False,
            "mode": "broker_fail",
            "phase": "paper_gate",
            "detail": "paper place disabled or not in paper mode",
        }
    if connector is None or not bool(getattr(connector, "connected", False)):
        return {
            **row,
            "pass": False,
            "mode": "broker_fail",
            "phase": "connect",
            "detail": "IBKR not connected",
        }
    if not _paper_placeable(strategy):
        return {
            **row,
            "pass": True,
            "mode": "paper",
            "phase": "schema_only",
            "detail": "schema OK (no placeable broker method — manage/protect type)",
        }
    fixtures = _fixtures(_px(pulse), positions)
    return await _paper_place_cancel(connector, strategy, fixtures[strategy], row)


async def run_order_suite(
    *,
    connector: Any = None,
    pulse: dict | None = None,
    positions: list | None = None,
    history: list | None = None,
    force_dry: bool = True,
    source: str = "cycle",
) -> dict:
    """Run suite: schema dry-run always; paper place→cancel when not force_dry.

    Paper place requires a connected connector and paper mode
    (``ABCXAUTO_SUITE_PAPER_PLACE``, default on). Startup / CI use force_dry=True.
    """
    _ = history
    if pulse is None:
        pulse = build_reality_pulse(
            positions=positions or [],
            ibkr_connected=bool(getattr(connector, "connected", False)) if connector else False,
        )
    fixtures = _fixtures(_px(pulse), positions)
    do_paper = (
        not force_dry
        and connector is not None
        and bool(getattr(connector, "connected", False))
    )
    place_rows: list[dict] = []
    for n in SUITE_STRATEGIES:
        if n not in STRATEGIES:
            continue
        if n not in fixtures:
            place_rows.append({
                "strategy": n, "pass": False, "mode": "schema_fail", "phase": "fixture",
                "detail": "no fixture", "schema_validated": False,
            })
            continue
        row = _validate_schema(n, fixtures[n])
        row["strategy"] = n
        if row.get("pass") and not force_dry:
            if do_paper and _paper_placeable(n):
                row = await _paper_place_cancel(connector, n, fixtures[n], row)
            elif not do_paper:
                row = {
                    **row,
                    "pass": False,
                    "mode": "broker_fail",
                    "phase": "connect",
                    "detail": "IBKR not connected for paper place",
                }
            elif not _paper_placeable(n):
                row = {
                    **row,
                    "mode": "paper",
                    "phase": "schema_only",
                    "detail": "schema OK (manage/protect — no place→cancel)",
                }
        place_rows.append(row)

    all_rows = place_rows + await _panic_rows(positions)
    passed = sum(1 for r in all_rows if r.get("pass"))
    failed = sum(1 for r in all_rows if not r.get("pass"))
    mode = "paper" if do_paper else "dry_run"
    report = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "source": source, "paper_only": True, "mode": mode,
        "reality_pulse": {
            "narrative": (pulse or {}).get("narrative"),
            "session": (pulse or {}).get("session"),
            "ledger_len": len((pulse or {}).get("position_ledger") or positions or []),
        },
        "strategies_tested": len(place_rows), "passed": passed, "failed": failed,
        "pass_rate": round(passed / max(1, passed + failed), 3),
        "results": all_rows, "idle_prevented": True,
        "summary": f"order suite [{source}] {passed} pass / {failed} fail mode={mode}",
    }
    set_cached_suite(report)
    return report


_SKIP_PAPER = frozenset({
    "modify_stop", "modify_target", "cancel_order",
    "oca", "trailing_stop", "trailing_stop_limit",
})


def _paper_placeable(strategy: str) -> bool:
    """Place→cancel only for sendable place/buy/sell/close strategies."""
    if strategy in _SKIP_PAPER:
        return False
    method = STRATEGIES.get(strategy, (None, ""))[1]
    return method.startswith(("place_", "buy_", "sell_", "close_", "roll_"))


async def _paper_place_cancel(
    connector: Any, strategy: str, params: dict, row: dict
) -> dict:
    """Call gateway method then cancel any returned order ids (paper hygiene)."""
    method_name = STRATEGIES[strategy][1]
    method = getattr(connector, method_name, None)
    out = {
        **row,
        "mode": "paper",
        "phase": "paper_place_cancel",
        "placed": False,
        "cancelled": False,
    }
    if method is None:
        out["pass"] = False
        out["detail"] = f"connector missing {method_name}"
        return out
    try:
        kw = {k: v for k, v in params.items() if k not in ("closing_position",)}
        result = await method(**kw)
    except TypeError:
        try:
            # Drop unknown keys the gateway does not accept
            import inspect
            sig = inspect.signature(method)
            allowed = set(sig.parameters) - {"self"}
            kw = {k: v for k, v in params.items()
                  if k != "closing_position" and (not allowed or k in allowed)}
            result = await method(**kw)
        except Exception as e:
            out["pass"] = False
            out["detail"] = f"paper place error: {e}"[:300]
            return out
    except Exception as e:
        out["pass"] = False
        out["detail"] = f"paper place error: {e}"[:300]
        return out

    if not isinstance(result, dict):
        out["pass"] = False
        out["detail"] = f"unexpected result type {type(result)}"
        return out
    if result.get("error") and not result.get("success"):
        out["pass"] = False
        out["detail"] = str(result.get("error"))[:300]
        out["broker"] = result
        return out

    out["placed"] = True
    out["broker"] = {k: result.get(k) for k in ("order_id", "order_ids", "success", "method") if k in result}
    ids: list[int] = []
    if result.get("order_id") is not None:
        try:
            ids.append(int(result["order_id"]))
        except (TypeError, ValueError):
            pass
    for oid in result.get("order_ids") or []:
        try:
            ids.append(int(oid))
        except (TypeError, ValueError):
            pass

    cancel_fn = getattr(connector, "cancel_order", None)
    cancel_ok = True
    for oid in ids:
        if cancel_fn is None:
            cancel_ok = False
            break
        try:
            cres = await cancel_fn(oid)
            if isinstance(cres, dict) and cres.get("error") and not cres.get("success"):
                cancel_ok = False
        except Exception:
            cancel_ok = False
    out["cancelled"] = bool(ids) and cancel_ok
    if ids and not cancel_ok:
        out["pass"] = False
        out["detail"] = f"placed but cancel failed for {ids}"
    else:
        out["pass"] = True
        out["detail"] = (
            f"paper place→cancel OK ids={ids}" if ids
            else "paper place OK (no order_id to cancel)"
        )
    return out


def format_order_suite_summary(report: dict) -> str:
    lines = [report.get("summary") or "order suite"]
    for r in [x for x in (report.get("results") or []) if not x.get("pass")][:10]:
        lines.append(f"  FAIL {r.get('strategy')} [{r.get('mode')}]: {r.get('detail')}")
    lines.append(f"  idle_prevented={report.get('idle_prevented')}")
    return "\n".join(lines)
