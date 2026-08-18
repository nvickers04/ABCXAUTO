"""Established structures Grok can research. Not a hunt menu.

Each row is a known play mapped to a send strategy. Taste stays with Grok.
defined_risk_only still rejects unlimited/naked shapes at send.
"""

from __future__ import annotations

from typing import Any

# Encyclopedia rows. send must exist in ORDER EXAMPLES.
CATALOG: tuple[dict[str, Any], ...] = (
    {
        "key": "debit_vertical",
        "send": "vertical_spread",
        "defined_risk": True,
        "shape": "long option + short further OTM, same expiry",
        "established": "Bull/bear call or put spread. Directional, debit-defined.",
    },
    {
        "key": "credit_vertical",
        "send": "vertical_spread",
        "defined_risk": True,
        "shape": "short option + long further OTM, same expiry",
        "established": "Bull put / bear call spread. Premium-in, width-defined.",
    },
    {
        "key": "iron_condor",
        "send": "iron_condor",
        "defined_risk": True,
        "shape": "short put spread + short call spread, same expiry",
        "established": "Range / short-vol. Both wings defined.",
    },
    {
        "key": "iron_butterfly",
        "send": "iron_butterfly",
        "defined_risk": True,
        "shape": "short straddle with long wings",
        "established": "Tighter short-vol than a condor. Defined wings.",
    },
    {
        "key": "butterfly",
        "send": "butterfly",
        "defined_risk": True,
        "shape": "long 1 / short 2 / long 1, same expiry",
        "established": "Pin a strike. Debit-defined.",
    },
    {
        "key": "calendar",
        "send": "calendar_spread",
        "defined_risk": True,
        "shape": "short near expiry, long same strike further expiry",
        "established": "Term-structure / theta on the front. Debit-defined.",
    },
    {
        "key": "diagonal",
        "send": "diagonal_spread",
        "defined_risk": True,
        "shape": "calendar with different strikes",
        "established": "Directional calendar. Debit-defined.",
    },
    {
        "key": "long_option",
        "send": "buy_option",
        "defined_risk": True,
        "shape": "long call or put",
        "established": "Directional, risk = premium. No short leg.",
    },
    {
        "key": "cash_secured_put",
        "send": "cash_secured_put",
        "defined_risk": True,
        "shape": "short put, cash reserved for assignment",
        "established": "Want-the-shares income. Cash-secured, not naked.",
    },
    {
        "key": "covered_call",
        "send": "covered_call",
        "defined_risk": True,
        "shape": "long 100 shares + short call",
        "established": "Overwrite a long stock lot. Needs the shares.",
    },
    {
        "key": "collar",
        "send": "collar",
        "defined_risk": True,
        "shape": "long stock + long put + short call",
        "established": "Hedge a long lot, finance the put with a call.",
    },
    {
        "key": "protective_put",
        "send": "protective_put",
        "defined_risk": True,
        "shape": "long stock + long put",
        "established": "Insurance on a long lot. Risk = premium.",
    },
    {
        "key": "long_straddle",
        "send": "straddle",
        "defined_risk": True,
        "shape": "long call + long put, same strike",
        "established": "Long vol / event. Risk = premium. Short straddle is blocked.",
    },
    {
        "key": "long_strangle",
        "send": "strangle",
        "defined_risk": True,
        "shape": "long OTM call + long OTM put",
        "established": "Cheaper long vol than a straddle. Short strangle is blocked.",
    },
    {
        "key": "stock_bracket",
        "send": "bracket",
        "defined_risk": False,
        "shape": "stock with stop + target",
        "established": "Cash-only long/short stock. Clerk requires the stop.",
    },
    {
        "key": "roll",
        "send": "roll_option",
        "defined_risk": True,
        "shape": "close a working option, open a later/different strike",
        "established": "Lifecycle. Not new thesis — extend or reposition a lot.",
    },
)


def catalog_keys() -> set[str]:
    keys = {str(row["key"]) for row in CATALOG}
    keys.update(str(row["send"]) for row in CATALOG)
    return keys


def resolve_basis(name: str) -> str | None:
    raw = str(name or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not raw:
        return None
    aliases = {
        "vertical": "debit_vertical",
        "debit_spread": "debit_vertical",
        "credit_spread": "credit_vertical",
        "bull_call": "debit_vertical",
        "bear_put": "debit_vertical",
        "bull_put": "credit_vertical",
        "bear_call": "credit_vertical",
        "csp": "cash_secured_put",
        "long_call": "long_option",
        "long_put": "long_option",
        "buy_option": "long_option",
        "vertical_spread": "debit_vertical",
        "calendar_spread": "calendar",
        "diagonal_spread": "diagonal",
        "bracket": "stock_bracket",
        "market_bracket": "stock_bracket",
        "roll_option": "roll",
        "straddle": "long_straddle",
        "strangle": "long_strangle",
    }
    if raw in aliases:
        raw = aliases[raw]
    for row in CATALOG:
        if raw == row["key"] or raw == row["send"]:
            return str(row["key"])
    return None


def catalog_payload(name: str | None = None) -> dict[str, Any]:
    """Facts Grok can pull. Not standing orders."""
    q = str(name or "").strip().lower()
    rows = []
    for row in CATALOG:
        if q and q not in row["key"] and q not in row["send"] and q not in str(row["established"]).lower():
            continue
        rows.append(dict(row))
    return {
        "source": "established",
        "note": "Research rows. send is the ticket shape.",
        "n": len(rows),
        "rows": rows,
    }
