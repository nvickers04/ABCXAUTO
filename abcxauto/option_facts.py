"""Option facts for Perceive — greeks/IV/DTE from MDA (labeled, unranked).

Fact only. Shell does not recommend structures or rank legs.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_LEGS = 8


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


async def fetch_option_facts(
    positions: list[dict] | None,
    *,
    max_legs: int = _MAX_LEGS,
) -> list[dict[str, Any]]:
    """MDA delayed quotes/greeks for open option legs. Empty if MDA off."""
    rows = _opt_rows(positions)[: max(0, int(max_legs))]
    if not rows:
        return []
    try:
        from abcxauto.marketdata.client import get_marketdata_client

        mda = get_marketdata_client()
        if not getattr(mda, "is_configured", False):
            return [
                {
                    "conId": p.get("conId") or p.get("con_id"),
                    "symbol": str(p.get("symbol") or "").upper(),
                    "sec": "OPT",
                    "qty": p.get("quantity") or p.get("position"),
                    "expiration": p.get("expiration") or p.get("lastTradeDateOrContractMonth"),
                    "strike": p.get("strike"),
                    "right": p.get("right"),
                    "source": "book",
                    "freshness": "broker_position",
                    "note": "MDA not configured — leg identity only",
                }
                for p in rows
            ]
    except Exception:
        logger.debug("option_facts: MDA client unavailable", exc_info=True)
        return []

    facts: list[dict[str, Any]] = []
    for p in rows:
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
        base: dict[str, Any] = {
            "conId": p.get("conId") or p.get("con_id"),
            "symbol": sym,
            "sec": "OPT",
            "qty": qty,
            "expiration": exp,
            "strike": strike,
            "right": right,
            "source": "mda",
            "freshness": "delayed",
            "heuristic": "greeks/IV from MDA — heuristic ≠ recommendation",
        }
        occ = occ_symbol(sym, exp, right, strike) if strike is not None else None
        if not occ:
            base["source"] = "book"
            base["freshness"] = "broker_position"
            facts.append(base)
            continue
        base["occ"] = occ
        try:
            oq = await mda.get_option_quote(occ)
        except Exception:
            oq = None
        if isinstance(oq, dict):
            for k in (
                "bid", "ask", "mid", "last", "delta", "gamma", "theta", "vega", "iv", "dte",
            ):
                if oq.get(k) is not None:
                    base[k] = oq.get(k)
        else:
            base["note"] = "MDA quote unavailable"
        facts.append(base)
    return facts


def format_option_facts_for_prompt(facts: list[dict] | None) -> str:
    """Compact FACT block for Judge/Act (no ranking)."""
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
        if f.get("iv") is not None:
            bits.append(f"iv={f.get('iv')}")
        if f.get("delta") is not None:
            bits.append(f"d={f.get('delta')}")
        if f.get("dte") is not None:
            bits.append(f"dte={f.get('dte')}")
        if f.get("mid") is not None:
            bits.append(f"mid={f.get('mid')}")
        lines.append("- " + " ".join(str(b) for b in bits if b))
    return "\n".join(lines)
