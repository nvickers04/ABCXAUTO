"""Payoff page: loss_per_unit by opening structure. No picked structure, no solved q."""

from __future__ import annotations

import math
from typing import Any


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _pos(raw: Any) -> float | None:
    v = _finite(raw)
    if v is None or v <= 0:
        return None
    return v


def _fmt(v: float | None) -> str:
    if v is None:
        return "?"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    text = f"{v:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _entry_eff(entry: Any, price: Any) -> float | None:
    e = _finite(entry)
    if e is not None:
        return e
    return _finite(price)


def loss_per_unit_stock(entry: Any, stop: Any) -> float | None:
    """Unit loss |entry-stop| per share. Never a share count."""
    e = _finite(entry)
    s = _finite(stop)
    if e is None or s is None:
        return None
    dist = abs(e - s)
    if dist <= 0:
        return None
    return dist


def loss_per_unit_debit(debit: Any) -> float | None:
    """Unit loss debit*100. Never a contract count."""
    d = _finite(debit)
    if d is None:
        return None
    return d * 100.0


def loss_per_unit_credit_vertical(width: Any, credit: Any) -> float | None:
    """Unit loss (width-credit)*100. Never a contract count."""
    w = _finite(width)
    c = _finite(credit)
    if w is None or c is None:
        return None
    loss = (w - c) * 100.0
    if loss < 0:
        return None
    return loss


def _spread_mid(bid: Any, ask: Any) -> float | None:
    """(ask-bid)/mid when both bid and ask are finite and positive."""
    b = _pos(bid)
    a = _pos(ask)
    if b is None or a is None:
        return None
    mid = (a + b) / 2.0
    if mid == 0:
        return None
    return (a - b) / mid


def _stk_bits(
    *,
    entry: Any,
    stop: Any,
    target: Any,
    price: Any,
    atr: Any,
) -> list[str]:
    e = _entry_eff(entry, price)
    loss = loss_per_unit_stock(e, stop)
    bits: list[str] = []
    if loss is not None:
        bits.append(f"loss_per_unit=|entry-stop|={_fmt(loss)}")
    px = _finite(price)
    if px is not None:
        bits.append(f"cash_per_share={_fmt(px)}")
    t = _finite(target)
    if e is not None and t is not None:
        reward = abs(t - e)
        bits.append(f"reward_per_share={_fmt(reward)}")
        if loss is not None and loss > 0:
            bits.append(f"reward/risk={_fmt(reward / loss)}")
    a = _pos(atr)
    if loss is not None and a is not None:
        bits.append(f"stop_atr={_fmt(loss / a)}")
    return bits


def _stk_line(name: str, bits: list[str]) -> str:
    if bits:
        return f"{name} " + " ".join(bits)
    return name


def format_order_type_page(
    *,
    nl: Any = None,
    cash_only: bool = True,
    price: Any = None,
    entry: Any = None,
    stop: Any = None,
    target: Any = None,
    atr: Any = None,
    debit: Any = None,
    credit: Any = None,
    width: Any = None,
    wing: Any = None,
    strike: Any = None,
    put_strike: Any = None,
    call_strike: Any = None,
    net_debit: Any = None,
    bid: Any = None,
    ask: Any = None,
    stock_bid: Any = None,
    stock_ask: Any = None,
    delta: Any = None,
    theta: Any = None,
    vega: Any = None,
    iv: Any = None,
    rv: Any = None,
    front_iv: Any = None,
    back_iv: Any = None,
) -> str:
    """Payoff page: identity + structures with finite inputs only. f and q stay blank."""
    lines: list[str] = []
    option_printed = False

    nl_v = _finite(nl)
    nl_s = _fmt(nl_v)
    lines.append(f"q=floor(NL * f / loss_per_unit) NL={nl_s} f=? q=?")
    lines.append("dollars_at_risk=q * loss_per_unit")

    # --- long_stk ---
    if _pos(price) is not None or _pos(entry) is not None:
        lines.append(_stk_line("long_stk", _stk_bits(
            entry=entry, stop=stop, target=target, price=price, atr=atr
        )))

    # --- short_stk ---
    if cash_only:
        lines.append("short_stk unavailable cash_only=on")
    elif _pos(price) is not None or _pos(entry) is not None:
        lines.append(_stk_line("short_stk", _stk_bits(
            entry=entry, stop=stop, target=target, price=price, atr=atr
        )))

    d = _finite(debit)
    c = _finite(credit)
    w = _finite(width)
    wg = _finite(wing)
    k = _finite(strike)
    e = _entry_eff(entry, price)

    # --- long_opt ---
    if d is not None:
        loss_opt = loss_per_unit_debit(d)
        opt_bits = [f"loss_per_unit=debit*100={_fmt(loss_opt)}"]
        if k is not None:
            be_call = k + d
            be_put = k - d
            opt_bits.append(f"breakeven_call={_fmt(be_call)}")
            opt_bits.append(f"breakeven_put={_fmt(be_put)}")
            px = _pos(price)
            if px is not None:
                opt_bits.append(f"move_pct={_fmt(abs(be_call - px) / px * 100.0)}")
        if _finite(delta) is not None:
            opt_bits.append(f"delta(risk-neutral)={_fmt(_finite(delta))}")
        if _finite(theta) is not None:
            opt_bits.append(f"theta={_fmt(_finite(theta))}")
        if _finite(vega) is not None:
            opt_bits.append(f"vega={_fmt(_finite(vega))}")
        spr = _spread_mid(bid, ask)
        if spr is not None:
            opt_bits.append(f"spread=(ask-bid)/mid={_fmt(spr)}")
        lines.append("long_opt " + " ".join(opt_bits))
        option_printed = True

    # --- vertical debit ---
    vd = _finite(debit)
    if vd is None:
        vd = _finite(net_debit)
    if vd is not None and w is not None:
        vd_loss = loss_per_unit_debit(vd)
        vd_bits = [f"loss_per_unit=net_debit*100={_fmt(vd_loss)}"]
        max_p = (w - vd) * 100.0
        vd_bits.append(f"max_profit=(width-debit)*100={_fmt(max_p)}")
        if vd_loss is not None and vd_loss > 0:
            vd_bits.append(f"reward/risk={_fmt(max_p / vd_loss)}")
        if w > 0:
            vd_bits.append(f"fraction_of_width={_fmt((w - vd) / w)}")
        lines.append("vertical_debit " + " ".join(vd_bits))
        option_printed = True

    # --- vertical credit ---
    if c is not None and w is not None:
        vc_loss = loss_per_unit_credit_vertical(w, c)
        vc_bits = [f"loss_per_unit=(width-credit)*100={_fmt(vc_loss)}"]
        vc_bits.append(f"max_profit=credit*100={_fmt(c * 100.0)}")
        if w > 0:
            vc_bits.append(f"fraction_of_width={_fmt(c / w)}")
        lines.append("vertical_credit " + " ".join(vc_bits))
        option_printed = True

    # --- iron_condor / iron_butterfly ---
    if wg is not None and c is not None:
        ic_loss = (wg - c) * 100.0
        if ic_loss < 0:
            ic_loss = None
        ic_bits = [f"loss_per_unit=(wing-credit)*100={_fmt(ic_loss)}"]
        ic_bits.append(f"max_profit=credit*100={_fmt(c * 100.0)}")
        if wg > 0:
            ic_bits.append(f"fraction_of_wing={_fmt(c / wg)}")
        for name in ("iron_condor", "iron_butterfly"):
            lines.append(f"{name} " + " ".join(ic_bits))
        option_printed = True

    # --- butterfly ---
    if d is not None:
        lines.append(f"butterfly loss_per_unit=debit*100={_fmt(loss_per_unit_debit(d))}")
        option_printed = True
    elif wg is not None and c is not None:
        bf_loss = (wg - c) * 100.0
        if bf_loss < 0:
            bf_loss = None
        lines.append(
            f"butterfly loss_per_unit=(wing-credit)*100={_fmt(bf_loss)}"
        )
        option_printed = True

    # --- long_straddle / long_strangle ---
    if d is not None:
        for name in ("long_straddle", "long_strangle"):
            bits = [f"loss_per_unit=debit*100={_fmt(loss_per_unit_debit(d))}"]
            px = _pos(price)
            if px is not None:
                bits.append(f"move_pct={_fmt(d / px * 100.0)}")
            lines.append(f"{name} " + " ".join(bits))
        option_printed = True

    # --- cash_secured_put ---
    if k is not None and c is not None:
        csp_loss = (k - c) * 100.0
        csp_bits = [
            f"loss_per_unit=(strike-credit)*100={_fmt(csp_loss)}",
            f"cash_secured={_fmt(k * 100.0)}",
            f"effective_buy={_fmt(k - c)}",
        ]
        if k > 0:
            csp_bits.append(f"yield_if_expire={_fmt(c / k)}")
        lines.append("cash_secured_put " + " ".join(csp_bits))
        option_printed = True

    # --- covered_call ---
    if c is not None:
        cc_bits: list[str] = []
        if e is not None and _finite(stop) is not None:
            cc_loss = abs(e - float(_finite(stop))) - c  # type: ignore[arg-type]
            cc_bits.append(f"loss_per_share=|entry-stop|-credit={_fmt(cc_loss)}")
        px = _finite(price)
        if k is not None and px is not None:
            cc_bits.append(f"upside_cap={_fmt(k - px)}")
        lines.append(_stk_line("covered_call", cc_bits))
        option_printed = True

    # --- protective_put ---
    if d is not None:
        pp_bits = [f"loss_per_unit=put_debit*100_extra={_fmt(loss_per_unit_debit(d))}"]
        if k is not None:
            pp_bits.append(f"floor={_fmt(k - d)}")
        lines.append("protective_put " + " ".join(pp_bits))
        option_printed = True

    # --- collar ---
    nd = _finite(net_debit)
    pk = _finite(put_strike)
    if e is not None and pk is not None and nd is not None:
        collar_loss = e - pk + nd
        lines.append(
            f"collar loss_per_share=entry-put_strike+net_debit={_fmt(collar_loss)}"
        )
        option_printed = True

    # --- long_calendar / long_diagonal ---
    if d is not None:
        for name in ("long_calendar", "long_diagonal"):
            bits = [f"loss_per_unit=debit*100={_fmt(loss_per_unit_debit(d))}"]
            fi = _finite(front_iv)
            bi = _finite(back_iv)
            if fi is not None:
                bits.append(f"front_iv={_fmt(fi)}")
            if bi is not None:
                bits.append(f"back_iv={_fmt(bi)}")
            if fi is not None and bi is not None:
                bits.append(f"front_minus_back={_fmt(fi - bi)}")
            lines.append(f"{name} " + " ".join(bits))
        option_printed = True

    # --- execution / management (stock spread only) ---
    ex = "execution limit|market|stop|vwap|twap does not change loss_per_unit"
    stock_spr = _spread_mid(stock_bid, stock_ask)
    if stock_spr is not None:
        ex += f" spread=(ask-bid)/mid={_fmt(stock_spr)}"
    lines.append(ex)
    lines.append("cancel modify close roll size=existing_order")

    iv_v = _finite(iv)
    rv_v = _finite(rv)
    if option_printed and iv_v is not None and rv_v is not None:
        lines.append(f"iv_minus_rv={_fmt(iv_v - rv_v)}")

    return "\n".join(lines)
