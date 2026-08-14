"""Option facts — open-leg identity from IBKR; MDA greeks labeled delayed.

Fact only. Shell does not recommend structures or rank legs.
MDA bid/ask/mid/last are stripped — send geometry is IBKR live only.
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
    return out if len(out) > 3 else {}


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
    try:
        qty = float(
            p.get("quantity") if p.get("quantity") is not None else p.get("position") or 0
        )
    except (TypeError, ValueError):
        qty = 0.0
    return {
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
    return facts


async def _empty() -> dict[str, Any]:
    return {}


def format_option_facts_for_prompt(facts: list[dict] | None) -> str:
    """Compact FACT block (no ranking)."""
    if not facts:
        return "OPTION FACTS: (none — no open OPT legs or MDA silent)"
    lines = [
        "OPTION FACTS (open legs — Fact; source-labeled; heuristic ≠ recommendation)",
    ]
    for f in facts[:_MAX_LEGS]:
        bits = [
            f"conId={f.get('conId')}",
            str(f.get("symbol") or ""),
            f"{f.get('right') or '?'}{f.get('strike')}",
            f"exp={f.get('expiration')}",
            f"qty={f.get('qty')}",
            f"src={f.get('source')}/{f.get('freshness')}",
        ]
        mda = f.get("mda") if isinstance(f.get("mda"), dict) else {}
        ibkr = f.get("ibkr") if isinstance(f.get("ibkr"), dict) else {}
        iv = (mda or f).get("iv")
        delta = (mda or f).get("delta")
        dte = (mda or f).get("dte")
        mid = ibkr.get("mid") if ibkr else f.get("mid")
        if iv is not None:
            bits.append(f"iv={iv}")
        if delta is not None:
            bits.append(f"d={delta}")
        if dte is not None:
            bits.append(f"dte={dte}")
        if mid is not None:
            bits.append(f"mid={mid}")
        lines.append("- " + " ".join(str(b) for b in bits if b))
    return "\n".join(lines)
