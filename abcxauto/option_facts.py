"""Option facts — open-leg identity from IBKR; MDA greeks labeled delayed.

Fact only. Shell does not recommend structures or rank legs.
MDA bid/ask/mid/last are stripped — send geometry is IBKR live only.

Open-leg cash is signed fill premium from avg cost: debit negative, credit
positive. last / mid / mark are not fills. Qty-blind premium is not cash.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_LEGS = 15
_MDA_GREEK_KEYS = (
    "delta", "gamma", "theta", "vega", "iv",
    "dte", "open_interest", "volume",
)
_AVG_COST_KEYS = ("avgCost", "avg_cost", "averageCost", "average_cost")
_FILL_PX_KEYS = (
    "avg_fill_price",
    "fill_price",
    "avgFillPrice",
    "avgPrice",
    "avg_price",
)
_BUY = frozenset({"BUY", "BOT"})
_SELL = frozenset({"SELL", "SLD"})


def occ_symbol(symbol: str, expiration: str, right: str, strike: float) -> str | None:
    """OCC: ROOT + YYMMDD + C/P + strike*1000 (8 digits)."""
    sym = (symbol or "").upper().strip()
    exp = str(expiration or "").strip()
    r = str(right or "").upper().strip()[:1]
    if not sym or len(exp) < 6 or r not in ("C", "P"):
        return None
    # Accept YYYYMMDD or YYMMDD
    yymmdd = exp[2:] if len(exp) == 8 else exp
    if len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    try:
        strike_i = int(round(float(strike) * 1000))
    except (TypeError, ValueError):
        return None
    return f"{sym}{yymmdd}{r}{strike_i:08d}"


def mda_greeks_only(oq: dict[str, Any] | None, *, occ: str | None = None) -> dict[str, Any]:
    """MDA blob with prices stripped so they cannot be used as live."""
    if not isinstance(oq, dict):
        return {}
    out: dict[str, Any] = {
        "source": "mda",
        "freshness": "delayed_15m",
        "use": "greeks_only_not_send_geometry",
    }
    if occ:
        out["occ"] = occ
    for k in _MDA_GREEK_KEYS:
        if oq.get(k) is not None:
            out[k] = oq.get(k)
    from abcxauto.prints import asof_fields

    out.update(asof_fields(oq.get("asof") or oq.get("updated")))
    return out if len(out) > 3 else {}


def _finite(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _qty(row: dict[str, Any]) -> float | None:
    raw = row.get("quantity") if row.get("quantity") is not None else row.get("position")
    if raw is None:
        raw = row.get("qty")
    return _finite(raw) if raw is not None else None


def _premium_sign(row: dict[str, Any], qty: float) -> float | None:
    side = str(row.get("side") or row.get("action") or "").upper()
    if side in _BUY:
        return -1.0
    if side in _SELL:
        return 1.0
    if abs(qty) <= 1e-9:
        return None
    # Open lot: IBKR quantity is signed. +long debit, -short credit.
    return -1.0 if qty > 0 else 1.0


def _multiplier(row: dict[str, Any]) -> float:
    raw = _finite(row.get("multiplier"))
    if raw is not None and raw > 0:
        return raw
    return 100.0


def _mark_for_units(row: dict[str, Any]) -> float | None:
    """Unit hint only. last is not a fill and is not used here."""
    for key in ("market_price", "marketPrice"):
        if row.get(key) is None:
            continue
        mkt = _finite(row.get(key))
        if mkt is not None:
            return mkt
    return None


def _fill_px(row: dict[str, Any]) -> float | None:
    """Per-share or contract fill. last / mid / mark are not fills."""
    for key in _AVG_COST_KEYS + _FILL_PX_KEYS:
        if row.get(key) is None:
            continue
        px = _finite(row.get(key))
        if px is not None and px >= 0:
            return px
    return None


def _avg_cost_is_contract_cash(row: dict[str, Any], px: float) -> bool:
    # IBKR OPT averageCost is usually contract cash when it dwarfs the mark.
    if not any(row.get(k) is not None for k in _AVG_COST_KEYS):
        return False
    mkt = _mark_for_units(row)
    return abs(px) >= 5.0 and (mkt is None or abs(px) > abs(mkt) * 3)


def _fill_contract_usd(row: dict[str, Any]) -> float | None:
    """Cash of one contract from fill/avg cost. last / mid / mark are ignored."""
    px = _fill_px(row)
    if px is None:
        return None
    if _avg_cost_is_contract_cash(row, px):
        return abs(px)
    return abs(px) * _multiplier(row)


def _fill_px_per_share(row: dict[str, Any]) -> float | None:
    px = _fill_px(row)
    if px is None:
        return None
    if _avg_cost_is_contract_cash(row, px):
        return px / _multiplier(row)
    return px


def signed_fill_premium_usd(row: dict[str, Any] | None) -> float | None:
    """Open-leg cash from fill/avg cost. Debit negative, credit positive.

    Requires qty and a fill price. A last-only row is not cash.
    """
    if not isinstance(row, dict):
        return None
    qty = _qty(row)
    if qty is None or abs(qty) <= 1e-9:
        return None
    cash = _fill_contract_usd(row)
    if cash is None:
        return None
    sign = _premium_sign(row, qty)
    if sign is None:
        return None
    return sign * abs(qty) * cash


def net_fill_premium_usd(legs: list[Any] | None) -> float | None:
    """Net cash of a vertical / combo. Each wing must be a fill, not a last."""
    total = 0.0
    n = 0
    for leg in legs or []:
        if not isinstance(leg, dict):
            return None
        cash = _finite(leg.get("fill_premium_usd"))
        if cash is None:
            cash = signed_fill_premium_usd(leg)
        if cash is None:
            return None
        total += cash
        n += 1
    return total if n >= 2 else None


def _attach_combo_net(facts: list[dict[str, Any]]) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        sym = str(fact.get("symbol") or "").upper()
        if not sym:
            continue
        groups.setdefault(sym, []).append(fact)
    for group in groups.values():
        net = net_fill_premium_usd(group)
        if net is None:
            continue
        for fact in group:
            fact["combo_net_usd"] = net


def _opt_rows(positions: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for p in positions or []:
        sec = str(p.get("secType") or p.get("sec_type") or "").upper()
        if sec not in ("OPT", "FOP"):
            continue
        try:
            qty = float(
                p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
            )
        except (TypeError, ValueError):
            continue
        if abs(qty) < 1e-9:
            continue
        out.append(p)
    return out


def _leg_base(p: dict[str, Any]) -> dict[str, Any]:
    sym = str(p.get("symbol") or "").upper()
    exp = str(
        p.get("expiration") or p.get("lastTradeDateOrContractMonth") or ""
    ).strip()
    right = str(p.get("right") or "").upper()[:1]
    try:
        strike = float(p.get("strike"))
    except (TypeError, ValueError):
        strike = None
    qty = _qty(p) or 0.0
    out: dict[str, Any] = {
        "conId": p.get("conId") or p.get("con_id"),
        "symbol": sym,
        "sec": "OPT",
        "qty": qty,
        "expiration": exp,
        "strike": strike,
        "right": right,
        "source": "book",
        "freshness": "broker_position",
    }
    cash = signed_fill_premium_usd(p)
    if cash is not None:
        out["fill_premium_usd"] = cash
        fill_px = _fill_px_per_share(p)
        if fill_px is not None:
            out["fill_px"] = fill_px
    return out


async def _ibkr_leg_quote(connector: Any, base: dict[str, Any]) -> dict[str, Any]:
    fn = getattr(connector, "get_live_option_quote", None)
    if not callable(fn):
        return {}
    try:
        live = await fn(
            base.get("symbol") or "",
            str(base.get("expiration") or ""),
            base.get("strike"),
            str(base.get("right") or ""),
        )
    except Exception:
        logger.debug("option_facts: IBKR quote failed", exc_info=True)
        return {}
    return live if isinstance(live, dict) else {}


async def _mda_leg_greeks(mda: Any, base: dict[str, Any]) -> dict[str, Any]:
    strike = base.get("strike")
    occ = occ_symbol(
        str(base.get("symbol") or ""),
        str(base.get("expiration") or ""),
        str(base.get("right") or ""),
        strike,
    ) if strike is not None else None
    if not occ:
        return {}
    try:
        oq = await mda.get_option_quote(occ)
    except Exception:
        return {}
    return mda_greeks_only(oq if isinstance(oq, dict) else None, occ=occ)


async def fetch_option_facts(
    positions: list[dict] | None,
    *,
    max_legs: int = _MAX_LEGS,
    connector: Any = None,
) -> list[dict[str, Any]]:
    """Open OPT legs: IBKR live bid/ask when connector given; MDA greeks delayed."""
    rows = _opt_rows(positions)[: max(0, int(max_legs))]
    if not rows:
        return []
    bases = [_leg_base(p) for p in rows]
    mda = None
    try:
        from abcxauto.marketdata.client import get_marketdata_client

        client = get_marketdata_client()
        if getattr(client, "is_configured", False):
            mda = client
    except Exception:
        logger.debug("option_facts: MDA client unavailable", exc_info=True)

    live_rows, greek_rows = await asyncio.gather(
        asyncio.gather(*[_ibkr_leg_quote(connector, b) for b in bases], return_exceptions=True),
        asyncio.gather(
            *[
                _mda_leg_greeks(mda, b) if mda is not None else _empty()
                for b in bases
            ],
            return_exceptions=True,
        ),
    )

    facts: list[dict[str, Any]] = []
    for i, base in enumerate(bases):
        fact = dict(base)
        live = live_rows[i] if i < len(live_rows) else {}
        if isinstance(live, Exception):
            live = {}
        if isinstance(live, dict) and live and not live.get("error"):
            fact["ibkr"] = live
            fact["source"] = "ibkr"
            fact["freshness"] = "live"
        elif isinstance(live, dict) and live.get("error"):
            fact["ibkr"] = {"error": live.get("error"), "source": "ibkr"}
        greeks = greek_rows[i] if i < len(greek_rows) else {}
        if isinstance(greeks, Exception):
            greeks = {}
        if isinstance(greeks, dict) and greeks:
            fact["mda"] = greeks
            if greeks.get("occ"):
                fact["occ"] = greeks.get("occ")
            if fact.get("source") == "book":
                fact["source"] = "ibkr_legs+mda_greeks"
                fact["freshness"] = "greeks_delayed_15m"
        facts.append(fact)
    _attach_combo_net(facts)
    return facts


async def _empty() -> dict[str, Any]:
    return {}


