"""KEEP-3: hash-bound dry-run preview + single-use place token.

Clerk gates used to fire only after a ticket was formed, with no readonly
preview and no token bound to the ticket hash. This module:

* previews a ticket (pass/refuse, max-loss, would_refuse[]) without writing
  an order
* issues a single-use token bound to (legs, qty, side, card, limit)
* refuses place unless that token matches this ticket

Exits / management never need a token. F9/F10 are not softened — kill-look
refuses appear in would_refuse. Looking and 7496 stay off.

KEEP-4 (``token_ttl``) is the only liveness clock (H-TTL). This module
does not keep a local token store — a hash/used row here must not
authorize place after TTL expiry. Hash bind stays here; expiry refuses
before place.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import uuid
from typing import Any

logger = logging.getLogger(__name__)

REASON_PREVIEW_TOKEN = "preview_token_required"
REASON_PREVIEW_MISMATCH = "preview_token_mismatch"
REASON_PREVIEW_USED = "preview_token_used"
REASON_PREVIEW_INVALID = "preview_token_invalid"

try:
    from abcxauto.token_ttl import REASON_TOKEN_EXPIRED
except ImportError:  # pragma: no cover — KEEP-4 is on master
    REASON_TOKEN_EXPIRED = "token_expired"
REASON_PREVIEW_EXPIRED = REASON_TOKEN_EXPIRED

_TOKEN_KEYS = (
    "preview_token",
    "place_token",
    "dry_run_token",
    "approval_token",
    "_preview_token",
    "_place_token",
)

_LEG_KEYS = (
    "symbol",
    "expiration",
    "expiry",
    "right",
    "long_strike",
    "short_strike",
    "strike",
    "put_strike",
    "call_strike",
    "put_long_strike",
    "put_short_strike",
    "call_short_strike",
    "call_long_strike",
    "center_strike",
    "wing_width",
    "lower_strike",
    "middle_strike",
    "upper_strike",
    "near_expiration",
    "far_expiration",
    "conId",
    "con_id",
    "stop_price",
    "target_price",
    "entry_price",
)


def reset_preview_state() -> None:
    """Drop KEEP-4 in-process tokens. Tests only. No local store."""
    try:
        from abcxauto.token_ttl import reset_place_tokens_for_tests

        reset_place_tokens_for_tests()
    except ImportError:
        pass


def _params_of(act: Any) -> dict[str, Any]:
    if not isinstance(act, dict):
        return {}
    params = act.get("params")
    return dict(params) if isinstance(params, dict) else {}


def _strategy_of(act: Any) -> str:
    if not isinstance(act, dict):
        return ""
    return str(act.get("strategy") or act.get("action") or "").strip().lower()


def _finite(raw: Any) -> float | None:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return n


def _canon(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    num = _finite(value)
    if num is not None and not isinstance(value, str):
        if num == int(num):
            return int(num)
        return round(num, 6)
    if isinstance(value, str):
        return value.strip()
    return value


def _qty_of(params: dict[str, Any]) -> int | None:
    raw = params.get("quantity")
    if raw in (None, ""):
        raw = params.get("qty") or params.get("shares")
    n = _finite(raw)
    if n is None or n < 1:
        return None
    return int(n)


def _side_of(act: dict[str, Any], params: dict[str, Any]) -> str:
    for src in (params, act):
        for key in ("direction", "action", "side"):
            raw = src.get(key)
            if raw in (None, ""):
                continue
            text = str(raw).strip().upper()
            if text in ("LONG", "BUY"):
                return "LONG"
            if text in ("SHORT", "SELL"):
                return "SHORT"
            return text
    return ""


def _card_of(act: dict[str, Any], params: dict[str, Any]) -> str:
    return str(params.get("card") or act.get("card") or "").strip()


def _limit_of(params: dict[str, Any]) -> Any:
    for key in ("limit_price", "entry_price"):
        if params.get(key) not in (None, ""):
            return _canon(params.get(key))
    return None


def _legs_of(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _LEG_KEYS:
        if params.get(key) in (None, ""):
            continue
        out[key] = _canon(params.get(key))
    return out


def ticket_preview_hash(act: Any) -> str:
    """Stable SHA-256 of legs, qty, side, card, limit.

    ``_hash_as_sent_qty`` means quantity was filled from size_pct_nl after
    the model sent the ticket. The hash stays on the absent quantity so a
    passing preview still places.
    """
    params = _params_of(act)
    if isinstance(act, dict) and act.get("_hash_as_sent_qty"):
        qty = None
    else:
        qty = _qty_of(params)
    payload = {
        "card": _card_of(act if isinstance(act, dict) else {}, params),
        "legs": _legs_of(params),
        "limit": _limit_of(params),
        "qty": qty,
        "side": _side_of(act if isinstance(act, dict) else {}, params),
        "strategy": _strategy_of(act),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def ticket_max_loss(act: Any) -> float | None:
    """Conservative defined-risk dollars. None when the ticket has no size."""
    if isinstance(act, dict):
        raw = act.get("max_loss")
        n = _finite(raw)
        if n is not None:
            return abs(n)
    params = _params_of(act)
    qty = _qty_of(params) or 1
    limit = _finite(params.get("limit_price"))
    if limit is not None:
        limit = abs(limit)
    strat = _strategy_of(act)

    def _width(*keys: str) -> float | None:
        vals = [_finite(params.get(k)) for k in keys]
        if any(v is None for v in vals):
            return None
        return abs(float(vals[0]) - float(vals[1]))  # type: ignore[arg-type]

    if strat == "vertical_spread":
        width = _width("long_strike", "short_strike")
        if width is None:
            return None
        if limit is not None and limit < width:
            return (width - limit) * 100.0 * qty
        if limit is not None:
            return limit * 100.0 * qty
        return width * 100.0 * qty
    if strat == "iron_condor":
        put_w = _width("put_short_strike", "put_long_strike")
        call_w = _width("call_long_strike", "call_short_strike")
        if put_w is None or call_w is None:
            return None
        width = min(put_w, call_w)
        if limit is not None:
            return max(0.0, (width - limit) * 100.0 * qty)
        return width * 100.0 * qty
    if strat == "iron_butterfly":
        wing = _finite(params.get("wing_width"))
        if wing is None:
            return None
        if limit is not None:
            return max(0.0, (abs(wing) - limit) * 100.0 * qty)
        return abs(wing) * 100.0 * qty
    if strat in ("bracket", "market_bracket"):
        stop = _finite(params.get("stop_price"))
        entry = _finite(params.get("entry_price")) or _finite(params.get("price_hint"))
        if stop is not None and entry is not None:
            return abs(entry - stop) * qty
        target = _finite(params.get("target_price"))
        if stop is not None and target is not None:
            return abs(target - stop) * qty
        return None
    if strat in ("buy_option", "cash_secured_put", "covered_call"):
        if limit is not None:
            return limit * 100.0 * qty
        return None
    if limit is not None:
        return limit * 100.0 * qty
    return None


def needs_place_token(act: Any) -> bool:
    """True for new risk. Exits / manage / cover skip the token."""
    from abcxauto.agent_loop import is_new_risk

    params = _params_of(act)
    return is_new_risk(_strategy_of(act), params)


def is_preview_request(act: Any) -> bool:
    if not isinstance(act, dict):
        return False
    for key in ("preview", "dry_run"):
        raw = act.get(key)
        if raw in (True, 1, "1", "true", "True", "yes", "on"):
            return True
    params = act.get("params")
    if isinstance(params, dict):
        for key in ("preview", "dry_run"):
            raw = params.get(key)
            if raw in (True, 1, "1", "true", "True", "yes", "on"):
                return True
    return False


def extract_place_token(act: Any) -> str:
    if not isinstance(act, dict):
        return ""
    for key in _TOKEN_KEYS:
        raw = act.get(key)
        if raw not in (None, ""):
            return str(raw).strip()
    params = act.get("params")
    if isinstance(params, dict):
        for key in _TOKEN_KEYS:
            raw = params.get(key)
            if raw not in (None, ""):
                return str(raw).strip()
    return ""


def _dedupe(reasons: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in reasons:
        note = str(raw or "").strip()
        if not note or note in seen:
            continue
        seen.add(note)
        out.append(note)
    return out


def _ticket_proposal(act: dict[str, Any]) -> Any:
    """Lightweight proposal for always-armed preview checks. Not a place path."""
    from types import SimpleNamespace

    params = _params_of(act)
    direction = params.get("direction")
    if direction in (None, ""):
        side = _side_of(act, params)
        if side in ("LONG", "SHORT"):
            direction = side
    return SimpleNamespace(
        strategy=_strategy_of(act),
        params=SimpleNamespace(
            action=params.get("action") or act.get("action"),
            closing_position=params.get("closing_position", False),
            contracts=params.get("contracts"),
            direction=direction,
            entry_price=params.get("entry_price"),
            limit_price=params.get("limit_price"),
            price_hint=params.get("price_hint"),
            quantity=params.get("quantity") or params.get("qty") or params.get("shares"),
            stop_price=params.get("stop_price"),
            strike=params.get("strike"),
            symbol=params.get("symbol"),
            target_price=params.get("target_price"),
        ),
    )


def _preview_account(world: Any, snap_d: dict[str, Any]) -> dict[str, Any]:
    """Account tags from this look. Do not invent cash."""
    acct = snap_d.get("account")
    if isinstance(acct, dict) and acct:
        return acct
    if world is None:
        return {}
    out: dict[str, Any] = {}
    nl = getattr(world, "net_liquidation", None)
    if nl not in (None, ""):
        out["netliquidation"] = nl
    pnl = getattr(world, "daily_pnl", None)
    if pnl not in (None, ""):
        out["dailypnl"] = pnl
    return out


def _quote_map_last(qmap: Any, symbol: str) -> float | None:
    """Positive last from an ibkr_live_quotes map. No invented price."""
    if not isinstance(qmap, dict) or not symbol:
        return None
    raw = qmap.get(symbol)
    if isinstance(raw, dict):
        for key in ("last", "price", "mid", "mark"):
            px = _finite(raw.get(key))
            if px is not None and px > 0:
                return px
        return None
    px = _finite(raw)
    if px is not None and px > 0:
        return px
    return None


def _preview_ibkr_last(
    symbol: str,
    snap_d: dict[str, Any],
    world: Any,
) -> float | None:
    """This-look IBKR last for mode_size when the ticket has no entry/limit.

    Order: snap ibkr_live_quotes[symbol] → snap ibkr_live_last when
    ibkr_live_symbol matches → world.ibkr_live_quotes. Never invent.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    px = _quote_map_last(snap_d.get("ibkr_live_quotes"), sym)
    if px is not None:
        return px
    live_sym = str(snap_d.get("ibkr_live_symbol") or "").strip().upper()
    if live_sym == sym:
        px = _finite(snap_d.get("ibkr_live_last"))
        if px is not None and px > 0:
            return px
    if world is not None:
        px = _quote_map_last(getattr(world, "ibkr_live_quotes", None), sym)
        if px is not None:
            return px
    return None


def _preview_position_rows(snap_d: dict[str, Any], world: Any) -> list[Any]:
    """Positions the preview can see — snap first, else world."""
    rows = snap_d.get("positions") if isinstance(snap_d, dict) else None
    if not rows and world is not None:
        rows = getattr(world, "positions", None)
    return list(rows or [])


def _largest_long_stk_open_frag(
    snap_d: dict[str, Any],
    world: Any,
) -> str | None:
    """Largest long STK lot fact for size_cash refuse. No sell/rotate wording."""
    best: tuple[float, str, int, float] | None = None
    for pos in _preview_position_rows(snap_d, world):
        if not isinstance(pos, dict):
            continue
        sec = str(
            pos.get("secType") or pos.get("sec_type") or pos.get("sec") or "STK"
        ).upper()
        if sec and not sec.startswith("STK"):
            continue
        qty_f = _finite(
            pos.get("quantity")
            if pos.get("quantity") is not None
            else (
                pos.get("position")
                if pos.get("position") is not None
                else pos.get("qty")
            )
        )
        if qty_f is None or qty_f <= 0:
            continue
        sym = str(pos.get("symbol") or "").strip().upper()
        if not sym:
            continue
        px = _preview_ibkr_last(sym, snap_d, world)
        if px is None or px <= 0:
            for key in (
                "market_price",
                "marketPrice",
                "mkt",
                "last",
                "avg_cost",
                "avgCost",
                "averageCost",
                "avg",
            ):
                px = _finite(pos.get(key))
                if px is not None and px > 0:
                    break
                px = None
        if px is None or px <= 0:
            continue
        qty_i = int(qty_f) if abs(qty_f - int(qty_f)) < 1e-9 else int(qty_f)
        if qty_i < 1:
            continue
        mv = float(qty_i) * float(px)
        if best is None or mv > best[0]:
            best = (mv, sym, qty_i, mv)
    if best is None:
        return None
    _mv, sym, qty_i, mv = best
    if abs(mv - round(mv)) < 0.005:
        mv_txt = f"${int(round(mv))}"
    else:
        mv_txt = f"${mv:.2f}"
    return f"open {sym} qty={qty_i} mv={mv_txt}"


def _size_cash_refuse_note(
    *,
    notional: float,
    cash: float,
    book: float,
    work: dict[str, Any],
    snap_d: dict[str, Any],
    world: Any,
) -> str:
    """size_cash refuse with live print + fit qty so the model can resent."""
    from abcxauto.world_state import pct_of_nl

    params = _params_of(work)
    note = (
        f"size_cash {pct_of_nl(notional, book)} > "
        f"{pct_of_nl(cash, book)}"
    )
    live: float | None = None
    for key in ("entry_price", "limit_price", "price_hint"):
        live = _finite(params.get(key))
        if live is not None and live > 0:
            break
        live = None
    if live is None:
        live = _preview_ibkr_last(
            str(params.get("symbol") or params.get("underlying") or ""),
            snap_d,
            world,
        )
    if live is not None and live > 0:
        note = f"{note} print={live}"
    fit: int | None = None
    qty = _qty_of(params)
    if qty is not None and notional > 0 and cash > 0:
        unit = float(notional) / float(qty)
        if unit > 0:
            fit = int(float(cash) / unit)
    if (fit is None or fit < 1) and live is not None and live > 0 and cash > 0:
        try:
            from abcxauto.send import option_size_mark

            mark, mult = option_size_mark(_strategy_of(work), params, live)
        except Exception:
            mark, mult = live, 1.0
        if mark is not None and mark > 0 and mult > 0:
            fit = int(float(cash) / (float(mark) * float(mult)))
    # Keep fit_qty=0 when cash cannot cover one unit — never invent shares.
    if fit is not None and fit >= 0:
        note = f"{note} fit_qty={fit}"
    open_frag = _largest_long_stk_open_frag(snap_d, world)
    if open_frag:
        note = f"{note} {open_frag}"
    return note


def _always_armed_refuses(
    work: dict[str, Any],
    world: Any,
    snap_d: dict[str, Any],
) -> list[str]:
    """Mirror #200 send refusals that stay armed when paper gates are off.

    New-risk card label, daily-loss, defined-risk, cash-only, and the halt
    latch. Preview is sync and must not call ``halt()`` or async
    ``pre_trade_check``.
    """
    from abcxauto.agent_loop import is_new_risk

    params = _params_of(work)
    if not is_new_risk(_strategy_of(work), params):
        return []

    from abcxauto.config import get_config
    from abcxauto.risk_gates import new_risk_card_error

    cfg = get_config()
    reasons: list[str] = []
    # Same label gate as gate_ticket. Never invent a card; exits never reach here.
    card_note = new_risk_card_error(
        params.get("card") or work.get("card"), type=_strategy_of(work)
    )
    if card_note:
        reasons.append(str(card_note))
    proposal = _ticket_proposal(work)

    try:
        from abcxauto.risk_gates import check_defined_risk_only

        try:
            ok_dr, why_dr = check_defined_risk_only(proposal, cfg)
        except TypeError:
            ok_dr, why_dr = check_defined_risk_only(proposal)
        if not ok_dr:
            reasons.append(str(why_dr))
    except Exception:
        logger.debug("preview defined-risk check failed", exc_info=True)

    try:
        from abcxauto.risk_gates import check_alloc_gates

        ok_alloc, why_alloc = check_alloc_gates(proposal, snap_d)
        if not ok_alloc:
            reasons.append(str(why_alloc))
    except Exception:
        logger.debug("preview alloc-size check failed", exc_info=True)

    try:
        from abcxauto.risk_gates import get_risk_gate

        gate = get_risk_gate()
        if gate.is_halted:
            reasons.append(f"Trading halted: {gate.halt_reason}")
    except Exception:
        logger.debug("preview halt-latch check failed", exc_info=True)

    try:
        breaker_on = float(getattr(cfg, "daily_loss_limit_pct", 0) or 0) > 0
    except (TypeError, ValueError):
        breaker_on = False
    cash_on = bool(getattr(cfg, "cash_only", False))
    account = _preview_account(world, snap_d)

    try:
        if breaker_on or cash_on:
            from abcxauto.risk_gates import (
                _account_float,
                _account_number_state,
                estimate_notional,
                risk_base_usd,
            )
            from abcxauto.world_state import pct_of_nl

            nl_state, net_liq = _account_number_state(
                account, "netliquidation", "NetLiquidation"
            )
            book = 0.0
            if breaker_on:
                pnl_state, daily_pnl_raw = _account_number_state(
                    account, "dailypnl", "DailyPnL"
                )
                if nl_state != "ok" or net_liq is None or net_liq <= 0:
                    reasons.append(
                        "Risk gate fail-closed: NetLiquidation unavailable "
                        "or non-positive"
                    )
                elif pnl_state == "unreadable":
                    reasons.append("Risk gate fail-closed: DailyPnL unreadable")
                else:
                    daily_pnl = 0.0 if daily_pnl_raw is None else daily_pnl_raw
                    book = risk_base_usd(net_liq, cfg)
                    limit = -(float(cfg.daily_loss_limit_pct) / 100.0) * book
                    if daily_pnl <= limit:
                        day_pct = pct_of_nl(daily_pnl, book)
                        reasons.append(
                            f"daily_loss {day_pct} <= -{cfg.daily_loss_limit_pct}"
                        )
            elif nl_state == "ok" and net_liq is not None:
                book = risk_base_usd(net_liq, cfg)

            if cash_on:
                from abcxauto.risk_gates import is_exit_or_management

                # Exits / management free cash; never size_cash a reducing SELL.
                if is_exit_or_management(proposal):
                    pass
                else:
                    direction = getattr(proposal.params, "direction", None)
                    if (
                        proposal.strategy in ("bracket", "market_bracket")
                        and str(direction or "").upper() == "SHORT"
                    ):
                        reasons.append(
                            "Cash-only mode: SHORT stock brackets are rejected "
                            "(no short selling). Set ABCXAUTO_CASH_ONLY=false to allow."
                        )
                    # Spend cap is TotalCashValue only — same as risk_gates.
                    # AvailableFunds is margin buying power, never the cash_only cap.
                    cash = _account_float(
                        account,
                        "TotalCashValue",
                        "totalcashvalue",
                        "total_cash",
                        "TotalCash",
                    )
                    if cash is None:
                        reasons.append(
                            "Risk gate fail-closed: cash-only mode requires "
                            "TotalCashValue in account summary"
                        )
                    else:
                        try:
                            notional = estimate_notional(proposal)
                        except Exception:
                            notional = None
                        if notional is None:
                            reasons.append("size_unknown_notional")
                        elif notional > cash:
                            reasons.append(
                                _size_cash_refuse_note(
                                    notional=float(notional),
                                    cash=float(cash),
                                    book=book,
                                    work=work,
                                    snap_d=snap_d,
                                    world=world,
                                )
                            )
    except Exception:
        logger.debug("preview daily-loss/cash-only check failed", exc_info=True)

    return reasons


def collect_would_refuse(
    act: Any,
    world: Any = None,
    snap: dict[str, Any] | None = None,
) -> list[str]:
    """Clerk refuses this ticket would hit. Never writes an order."""
    reasons: list[str] = []
    work = copy.deepcopy(act) if isinstance(act, dict) else {}
    try:
        from abcxauto.tool_args import bind_send_card

        bind_send_card(work)
    except Exception:
        logger.debug("preview bind_send_card failed", exc_info=True)
    snap_d = snap if isinstance(snap, dict) else {}

    try:
        from abcxauto.config import get_config
        from abcxauto.send import _paper_live_port

        port = _paper_live_port(get_config())
        if port is not None:
            reasons.append(
                f"IBKR port {port} is the live family (7496/4001); "
                "TRADING_MODE is paper — not placing"
            )
    except Exception:
        logger.debug("preview live-port check failed", exc_info=True)

    sess = ""
    if world is not None:
        sess = str(getattr(world, "session_status", "") or "")
    if not sess:
        sess = str(work.get("_desk_session") or "")
    sess = sess.strip().lower()
    if sess == "unknown":
        sess = ""
    # Research sessions are not a blanket preview refuse — cash / defined-risk /
    # daily-loss / research_thin still append below.

    try:
        from abcxauto.thin_rth_kill_look import kill_look_send_block

        positions = list(
            snap_d.get("positions")
            or getattr(world, "positions", None)
            or []
        )
        open_lots = list(getattr(world, "open_lots", None) or [])
        blocked = kill_look_send_block(
            work,
            session=sess,
            positions=positions,
            open_lots=open_lots,
            in_flight=bool(
                snap_d.get("kill_entry_in_flight")
                or getattr(world, "kill_entry_in_flight", False)
            ),
        )
        if blocked is not None:
            reasons.append(
                str(blocked.get("note") or blocked.get("reason_code") or "kill_look")
            )
    except Exception:
        logger.debug("preview kill-look check failed", exc_info=True)
        reasons.append("kill-look gate failed closed")

    try:
        reasons.extend(_always_armed_refuses(work, world, snap_d))
    except Exception:
        logger.debug("preview always-armed check failed", exc_info=True)
        reasons.append("always-armed gates failed closed")

    if world is not None:
        try:
            from abcxauto.agent_loop import gate_ticket

            _strat, forced = gate_ticket(work, world)
            if forced is not None:
                reasons.append(
                    str(forced.get("note") or forced.get("reason") or "gate_ticket")
                )
        except Exception:
            logger.debug("preview gate_ticket failed", exc_info=True)

        params = _params_of(work)
        strat = _strategy_of(work)
        positions = list(
            snap_d.get("positions")
            or getattr(world, "positions", None)
            or []
        )
        orders = list(
            snap_d.get("open_orders")
            or getattr(world, "open_orders", None)
            or []
        )
        try:
            from abcxauto.trade_playbook import check_overlay_shares

            ok_sh, _code, sh_msg = check_overlay_shares(
                strat, params, positions, orders
            )
            if not ok_sh:
                reasons.append(str(sh_msg or "overlay_shares"))
        except Exception:
            logger.debug("preview overlay check failed", exc_info=True)
        try:
            from abcxauto.look_snapshot import check_ticket_numbers

            ok_n, _n_code, n_msg = check_ticket_numbers(strat, params, snap_d)
            if not ok_n:
                reasons.append(str(n_msg or "stale_or_invented_number"))
        except Exception:
            logger.debug("preview look-numbers check failed", exc_info=True)
        try:
            from abcxauto.agent_loop import is_new_risk

            needs_research = is_new_risk(strat, params)
        except Exception:
            logger.debug("preview new-risk research check failed", exc_info=True)
            reasons.append("research_thin")
        else:
            if needs_research:
                try:
                    from abcxauto.desk_mode import new_risk_research_error

                    r_note = new_risk_research_error(
                        str(params.get("symbol") or params.get("underlying") or ""),
                        snap_d,
                        strat=strat,
                    )
                    if r_note:
                        reasons.append(str(r_note))
                except Exception:
                    logger.debug(
                        "preview new-risk research check failed",
                        exc_info=True,
                    )
                    reasons.append("research_thin")
        try:
            from abcxauto.agent_loop import validate_action_against_inventory

            ok_i, vmsg = validate_action_against_inventory(work, positions)
            if not ok_i:
                reasons.append(str(vmsg or "inventory"))
        except Exception:
            logger.debug("preview inventory check failed", exc_info=True)
        try:
            from abcxauto.agent_loop import equity_of, is_new_risk
            from abcxauto.mode_size import mode_size_ticket_error

            if is_new_risk(strat, params):
                nl = equity_of(snap_d.get("account") or {}) or float(
                    getattr(world, "net_liquidation", 0) or 0
                )
                px = None
                for key in ("entry_price", "limit_price", "price_hint"):
                    px = _finite(params.get(key))
                    if px is not None and px > 0:
                        break
                    px = None
                if px is None:
                    px = _preview_ibkr_last(
                        str(params.get("symbol") or params.get("underlying") or ""),
                        snap_d,
                        world,
                    )
                size_note = mode_size_ticket_error(
                    params, net_liq=nl, price=px, strategy=strat
                )
                if size_note:
                    reasons.append(str(size_note))
        except Exception:
            logger.debug("preview mode_size check failed", exc_info=True)

    return _dedupe(reasons)


def _ttl_helpers():
    """KEEP-4 liveness helpers. ImportError → place path fail-closed."""
    from abcxauto.token_ttl import (
        REASON_TOKEN_EXPIRED as ttl_expired,
        REASON_TOKEN_INVALID as ttl_invalid,
        REASON_TOKEN_USED as ttl_used,
        consume_place_token as ttl_consume,
        evaluate_place_token,
        issue_place_token,
        token_expired,
    )

    return {
        "consume": ttl_consume,
        "evaluate": evaluate_place_token,
        "expired": ttl_expired,
        "invalid": ttl_invalid,
        "issue": issue_place_token,
        "token_expired": token_expired,
        "used": ttl_used,
    }


def _payload_of(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    payload = record.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _issue_token(preview_hash: str, preview_id: str, *, now: Any = None) -> str:
    """Mint via KEEP-4 only. No local token row — H-TTL."""
    try:
        helpers = _ttl_helpers()
    except ImportError:
        logger.error("token_ttl missing — refuse to mint a TTL-less place token")
        return ""
    try:
        rec = helpers["issue"](
            kind="preview",
            payload={"preview_hash": preview_hash, "preview_id": preview_id},
            now=now,
        )
    except Exception:
        logger.exception("token_ttl issue failed — fail-closed, no local mint")
        return ""
    token = str((rec or {}).get("id") or "").strip()
    if not token:
        logger.error("token_ttl issue returned no id — fail-closed")
        return ""
    return token


def consume_place_token(
    token: Any,
    expected_hash: str,
    *,
    now: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Liveness SoT is KEEP-4 evaluate/consume/token_expired, then hash.

    No local token mirror. Expired refuses before place. Mismatch does
    not spend the token.
    """
    key = str(token or "").strip()
    if not key:
        return False, {
            "status": "blocked",
            "reason_code": REASON_PREVIEW_TOKEN,
            "note": "place requires a preview_token bound to this ticket hash",
            "would_refuse": [REASON_PREVIEW_TOKEN],
            "token_used": False,
        }
    try:
        helpers = _ttl_helpers()
    except ImportError:
        return False, {
            "status": "blocked",
            "reason_code": REASON_PREVIEW_INVALID,
            "note": "token_ttl unavailable — fail-closed",
            "would_refuse": [REASON_PREVIEW_INVALID],
            "token_used": False,
        }
    try:
        verdict = helpers["evaluate"](key, now=now)
    except Exception:
        logger.exception("token_ttl evaluate failed — fail-closed")
        return False, {
            "status": "blocked",
            "reason_code": REASON_PREVIEW_INVALID,
            "note": "place token liveness check failed — fail-closed",
            "would_refuse": [REASON_PREVIEW_INVALID],
            "token_used": False,
        }
    record = verdict.get("record")
    if isinstance(record, dict) and helpers["token_expired"](
        record.get("issued_at"),
        expires_at=record.get("expires_at"),
        now=now,
        ttl_s=record.get("ttl_s"),
    ):
        payload = _payload_of(record)
        return False, {
            "status": "blocked",
            "reason_code": REASON_TOKEN_EXPIRED,
            "note": "place token expired — unused authorization is dead",
            "preview_id": payload.get("preview_id"),
            "preview_hash": payload.get("preview_hash"),
            "would_refuse": [REASON_TOKEN_EXPIRED],
            "token_used": False,
        }
    if not verdict.get("ok"):
        reason = str(verdict.get("reason") or helpers["invalid"])
        payload = _payload_of(record if isinstance(record, dict) else verdict.get("record"))
        if reason == helpers["expired"]:
            reason_code = REASON_TOKEN_EXPIRED
            note = "place token expired — unused authorization is dead"
        elif reason == helpers["used"]:
            reason_code = REASON_PREVIEW_USED
            note = "preview_token already used"
        else:
            reason_code = REASON_PREVIEW_INVALID
            note = "preview_token missing or unreadable — fail-closed"
        return False, {
            "status": "blocked",
            "reason_code": reason_code,
            "note": note,
            "preview_id": payload.get("preview_id"),
            "preview_hash": payload.get("preview_hash"),
            "would_refuse": [reason_code],
            "token_used": reason == helpers["used"],
        }
    payload = _payload_of(verdict.get("record"))
    stored = str(payload.get("preview_hash") or "")
    preview_id = str(payload.get("preview_id") or "")
    if not stored or stored != str(expected_hash or ""):
        return False, {
            "status": "blocked",
            "reason_code": REASON_PREVIEW_MISMATCH,
            "note": "preview_token is bound to a different ticket hash",
            "preview_id": preview_id,
            "preview_hash": stored,
            "place_hash": str(expected_hash or ""),
            "would_refuse": [REASON_PREVIEW_MISMATCH],
            "token_used": False,
        }
    try:
        spent = helpers["consume"](key, now=now)
    except Exception:
        logger.exception("token_ttl consume failed — fail-closed")
        return False, {
            "status": "blocked",
            "reason_code": REASON_PREVIEW_INVALID,
            "note": "place token consume failed — fail-closed",
            "preview_id": preview_id,
            "preview_hash": stored,
            "would_refuse": [REASON_PREVIEW_INVALID],
            "token_used": False,
        }
    if not spent.get("ok"):
        reason = str(spent.get("reason") or helpers["invalid"])
        reason_code = (
            REASON_TOKEN_EXPIRED
            if reason == helpers["expired"]
            else REASON_PREVIEW_USED
            if reason == helpers["used"]
            else REASON_PREVIEW_INVALID
        )
        return False, {
            "status": "blocked",
            "reason_code": reason_code,
            "note": "place token refused after TTL consume",
            "preview_id": preview_id,
            "preview_hash": stored,
            "would_refuse": [reason_code],
            "token_used": reason == helpers["used"],
        }
    try:
        from abcxauto.memory import get_journal

        get_journal().mark_preview_token_used(preview_id)
    except Exception:
        logger.debug("preview token journal mark failed", exc_info=True)
    return True, {
        "preview_id": preview_id,
        "preview_hash": stored,
        "token_used": True,
    }


def bind_place_token(act: dict[str, Any], *, source: str = "bind") -> dict[str, Any]:
    """Mint a hash-bound token onto ``act``. Does not place."""
    digest = ticket_preview_hash(act)
    preview_id = f"prv_{uuid.uuid4().hex[:16]}"
    token = _issue_token(digest, preview_id)
    act["preview_token"] = token
    act["_preview_id"] = preview_id
    act["_preview_hash"] = digest
    max_loss = ticket_max_loss(act)
    try:
        from abcxauto.memory import get_journal

        get_journal().record_send_preview(
            preview_id=preview_id,
            preview_hash=digest,
            strategy=_strategy_of(act),
            symbol=str(_params_of(act).get("symbol") or ""),
            max_loss=max_loss,
            would_refuse=[],
            verdict="pass",
            token_used=False,
            source=source,
        )
    except Exception:
        logger.debug("bind_place_token journal failed", exc_info=True)
    return {
        "status": "preview",
        "preview": True,
        "pass": True,
        "refuse": False,
        "max_loss": max_loss,
        "would_refuse": [],
        "preview_id": preview_id,
        "preview_hash": digest,
        "preview_token": token,
        "token_used": False,
    }


def preview_ticket(
    act: Any,
    world: Any = None,
    snap: dict[str, Any] | None = None,
    *,
    source: str = "preview",
) -> dict[str, Any]:
    """Readonly preview. Never calls the broker."""
    work = copy.deepcopy(act) if isinstance(act, dict) else {}
    try:
        from abcxauto.tool_args import bind_send_card

        bind_send_card(work)
    except Exception:
        pass
    would_refuse = collect_would_refuse(work, world=world, snap=snap)
    digest = ticket_preview_hash(work)
    preview_id = f"prv_{uuid.uuid4().hex[:16]}"
    passed = not would_refuse
    token = _issue_token(digest, preview_id) if passed else ""
    max_loss = ticket_max_loss(work)
    verdict = "pass" if passed else "refuse"
    usd = {}
    try:
        from abcxauto.portfolio_loss import live_portfolio_usd_check

        usd = live_portfolio_usd_check(work, world=world, snap=snap)
    except Exception:
        logger.debug("preview portfolio usd stamp failed", exc_info=True)
        usd = {}
    try:
        from abcxauto.memory import get_journal

        get_journal().record_send_preview(
            preview_id=preview_id,
            preview_hash=digest,
            strategy=_strategy_of(work),
            symbol=str(_params_of(work).get("symbol") or ""),
            max_loss=max_loss,
            would_refuse=would_refuse,
            verdict=verdict,
            token_used=False,
            source=source,
            portfolio_max_loss_usd=usd.get("portfolio_max_loss_usd"),
            portfolio_cap_usd=usd.get("portfolio_cap_usd"),
            portfolio_usd_refused=False,
        )
    except Exception:
        logger.debug("preview journal failed", exc_info=True)
    out: dict[str, Any] = {
        "status": "preview" if passed else "blocked",
        "preview": True,
        "pass": passed,
        "refuse": not passed,
        "max_loss": max_loss,
        "would_refuse": list(would_refuse),
        "preview_id": preview_id,
        "preview_hash": digest,
        "preview_token": token or None,
        "token_used": False,
        "strategy": _strategy_of(work) or "blocked",
        "note": (
            "preview pass — place with preview_token on the same ticket"
            if passed
            else (would_refuse[0] if would_refuse else "preview refuse")
        ),
    }
    if usd:
        try:
            from abcxauto.portfolio_loss import stamp_portfolio_usd

            stamp_portfolio_usd(out, usd)
        except Exception:
            pass
    if not passed:
        out["reason_code"] = "preview_refuse"
    return out


def authorize_place(act: Any, *, now: Any = None) -> dict[str, Any] | None:
    """None if place may proceed. A dict is fail-closed — never write."""
    if is_preview_request(act):
        return preview_ticket(act, source="send_preview")
    if not needs_place_token(act):
        return None
    digest = ticket_preview_hash(act)
    token = extract_place_token(act)
    if not token:
        prev = preview_ticket(act, source="place_missing_token")
        prev["status"] = "blocked"
        prev["reason_code"] = REASON_PREVIEW_TOKEN
        prev["note"] = "place requires a preview_token bound to this ticket hash"
        refuses = list(prev.get("would_refuse") or [])
        if REASON_PREVIEW_TOKEN not in refuses:
            refuses.append(REASON_PREVIEW_TOKEN)
        prev["would_refuse"] = refuses
        prev["pass"] = False
        prev["refuse"] = True
        return prev
    ok, meta = consume_place_token(token, digest, now=now)
    if ok:
        if isinstance(act, dict):
            act["_preview_id"] = meta.get("preview_id")
            act["_preview_hash"] = digest
            act["_token_used"] = True
        return None
    meta.setdefault("strategy", _strategy_of(act) or "blocked")
    meta.setdefault("preview_hash", digest)
    if meta.get("reason_code") == REASON_PREVIEW_MISMATCH:
        meta["place_hash"] = digest
    return meta


def stamp_place_result(result: Any, act: Any) -> dict[str, Any]:
    """Copy preview journal fields onto a place result."""
    out = dict(result) if isinstance(result, dict) else {"raw": result}
    if not isinstance(act, dict):
        return out
    if act.get("_preview_id"):
        out.setdefault("preview_id", act.get("_preview_id"))
    if act.get("_preview_hash"):
        out.setdefault("preview_hash", act.get("_preview_hash"))
    if act.get("_token_used"):
        out["token_used"] = True
    out.setdefault("would_refuse", [])
    return out


__all__ = [
    "REASON_PREVIEW_EXPIRED",
    "REASON_PREVIEW_INVALID",
    "REASON_PREVIEW_MISMATCH",
    "REASON_PREVIEW_TOKEN",
    "REASON_PREVIEW_USED",
    "REASON_TOKEN_EXPIRED",
    "authorize_place",
    "bind_place_token",
    "collect_would_refuse",
    "consume_place_token",
    "extract_place_token",
    "is_preview_request",
    "needs_place_token",
    "preview_ticket",
    "reset_preview_state",
    "stamp_place_result",
    "ticket_max_loss",
    "ticket_preview_hash",
]
