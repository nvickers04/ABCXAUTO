"""Kahneman System 2 deliberative scaffolding — bone structure of every cycle.

Awareness (Reality Pulse) supplies facts. This module enforces disciplined,
bias-resistant processing before any order proposal. PnL remains the final
truth signal for self-tweaks.
"""

from __future__ import annotations

from typing import Any

# Injected verbatim into system rules + user prompt every cycle.
KAHNEMAN_HEART = """
=== KAHNEMAN SYSTEM 2 DELIBERATIVE SCAFFOLDING (MANDATORY BEFORE ANY PROPOSAL) ===
Awareness gives facts. System 2 processes them. You MUST fill kahneman in your JSON.

1. System 1 fast scan → System 2 slow verification
   - List the base rate / reference class for this trade (not the story of the last candle).
2. Debias common traps (name which apply):
   - Anchoring: ignore recent price as sole signal
   - Availability: require full history / ledger, not just last move
   - Overconfidence: state probabilities explicitly (p_win, p_loss)
   - Representativeness: check regime (session, VIX, liquidity)
   - Loss aversion: frame in final wealth, not arbitrary reference points
   - Prospect theory asymmetry: cut losses fast, let winners run
3. Pre-mortem: "Assume this trade fails — most likely reason?"
4. Alternatives: "What other actions are possible given session + data freshness?"
5. Bias audit: "Which Kahneman biases could be influencing this decision?"

Closes still require exact conId. If System 2 is incomplete for a non-hold action,
prefer hold and explain the gap. PnL impact is the scoreboard for self-tweaks.
"""

CHECKLIST_KEYS = (
    "system1_scan",
    "system2_base_rate",
    "debias",
    "pre_mortem",
    "alternatives",
    "bias_audit",
)

# Soft completeness: non-hold actions should populate these.
REQUIRED_FOR_TRADE = ("system2_base_rate", "pre_mortem", "bias_audit")


def kahneman_prompt_block() -> str:
    return KAHNEMAN_HEART.strip()


def empty_kahneman() -> dict[str, Any]:
    return {
        "system1_scan": "",
        "system2_base_rate": "",
        "debias": {
            "anchoring": "",
            "availability": "",
            "overconfidence": "",
            "representativeness": "",
            "loss_aversion": "",
            "prospect_theory": "",
        },
        "pre_mortem": "",
        "alternatives": [],
        "bias_audit": [],
        "complete": False,
        "missing": list(REQUIRED_FOR_TRADE),
    }


def extract_kahneman(act: dict | None) -> dict[str, Any]:
    """Normalize Grok's kahneman block (or synthesize sparse fields from rationale)."""
    act = act or {}
    raw = act.get("kahneman") if isinstance(act.get("kahneman"), dict) else {}
    out = empty_kahneman()

    out["system1_scan"] = str(
        raw.get("system1_scan") or raw.get("fast_scan") or ""
    ).strip()
    out["system2_base_rate"] = str(
        raw.get("system2_base_rate")
        or raw.get("base_rate")
        or raw.get("reference_class")
        or ""
    ).strip()

    debias = raw.get("debias") if isinstance(raw.get("debias"), dict) else {}
    for k in out["debias"]:
        if debias.get(k) is not None:
            out["debias"][k] = str(debias[k]).strip()
    # Allow list form: ["anchoring: ...", ...]
    if isinstance(raw.get("debias"), list):
        out["debias"]["_notes"] = [str(x) for x in raw["debias"][:8]]

    out["pre_mortem"] = str(
        raw.get("pre_mortem") or raw.get("premortem") or ""
    ).strip()

    alts = raw.get("alternatives") or raw.get("alt_actions") or []
    if isinstance(alts, str):
        alts = [alts]
    out["alternatives"] = [str(a).strip() for a in alts if str(a).strip()][:8]

    audit = raw.get("bias_audit") or raw.get("biases") or []
    if isinstance(audit, str):
        audit = [audit]
    out["bias_audit"] = [str(b).strip() for b in audit if str(b).strip()][:8]

    missing: list[str] = []
    if not out["system2_base_rate"]:
        missing.append("system2_base_rate")
    if not out["pre_mortem"]:
        missing.append("pre_mortem")
    if not out["bias_audit"]:
        missing.append("bias_audit")

    out["complete"] = len(missing) == 0
    out["missing"] = missing
    return out


def format_kahneman_trace(k: dict | None) -> str:
    """Human-readable block for Logs & Evolution."""
    k = k or empty_kahneman()
    lines = [
        "KAHNEMAN SYSTEM 2 TRACE",
        f"  complete={k.get('complete')} missing={k.get('missing') or []}",
        f"  S1 scan: {k.get('system1_scan') or '—'}",
        f"  S2 base rate / ref class: {k.get('system2_base_rate') or '—'}",
    ]
    debias = k.get("debias") or {}
    if isinstance(debias, dict):
        bits = [f"{kk}={vv}" for kk, vv in debias.items() if vv]
        lines.append(f"  debias: {'; '.join(bits) if bits else '—'}")
    lines.append(f"  pre-mortem: {k.get('pre_mortem') or '—'}")
    alts = k.get("alternatives") or []
    lines.append(f"  alternatives: {'; '.join(alts) if alts else '—'}")
    audit = k.get("bias_audit") or []
    lines.append(f"  bias audit: {'; '.join(audit) if audit else '—'}")
    return "\n".join(lines)


def gate_incomplete_system2(strat: str, k: dict | None) -> tuple[bool, str]:
    """Soft gate: non-hold trades with incomplete System 2 → hold + reason.

    Returns (ok_to_trade, message). ok_to_trade False means coerce to hold.
    """
    strat = (strat or "hold").lower()
    if strat in ("hold", "none", ""):
        return True, "hold — System 2 optional"
    k = k or empty_kahneman()
    if k.get("complete"):
        return True, "System 2 complete"
    missing = k.get("missing") or REQUIRED_FOR_TRADE
    return (
        False,
        f"System 2 incomplete (missing {', '.join(missing)}); coerced to hold",
    )


def expected_json_shape_hint() -> str:
    """Compact schema hint appended to the cycle user prompt."""
    return (
        'JSON shape: {"action":"...","strategy":"...","params":{},'
        '"rationale":"Current reality: ... then System 2 ...",'
        '"target_conId":"...",'
        '"reasoning_chain":"Closing target = conId=...",'
        '"kahneman":{'
        '"system1_scan":"...",'
        '"system2_base_rate":"reference class / base rate",'
        '"debias":{"anchoring":"...","availability":"...","overconfidence":"p_win/p_loss",'
        '"representativeness":"...","loss_aversion":"...","prospect_theory":"..."},'
        '"pre_mortem":"if this fails, most likely because...",'
        '"alternatives":["hold","..."],'
        '"bias_audit":["anchoring","..."]'
        "}}"
    )
