"""Primary scorecard: book return % of starting NetLiq vs model API cost."""

from __future__ import annotations

from typing import Any

# Conservative Grok 4.5-ish placeholders; override via env if billing is known.
_DEFAULT_IN_USD_PER_MTOK = 3.0
_DEFAULT_OUT_USD_PER_MTOK = 15.0


def _cfg_float(name: str, default: float) -> float:
    import os

    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    *,
    in_rate: float | None = None,
    out_rate: float | None = None,
) -> float:
    inn = in_rate if in_rate is not None else _cfg_float(
        "ABCXAUTO_MODEL_INPUT_USD_PER_MTOK", _DEFAULT_IN_USD_PER_MTOK
    )
    out = out_rate if out_rate is not None else _cfg_float(
        "ABCXAUTO_MODEL_OUTPUT_USD_PER_MTOK", _DEFAULT_OUT_USD_PER_MTOK
    )
    return (max(0, input_tokens) / 1_000_000.0) * inn + (
        max(0, output_tokens) / 1_000_000.0
    ) * out


def estimate_tokens(text: str) -> int:
    """Rough token count when the SDK does not return usage."""
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def compute_scorecard(
    *,
    equity: float | None = None,
    journal: Any = None,
) -> dict[str, Any]:
    """Book P&L vs model cost, scored as % of starting NetLiq."""
    if journal is None:
        try:
            from abcxauto.memory import get_journal

            journal = get_journal()
        except Exception:
            journal = None

    startup = None
    current = equity
    usage = {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    if journal is not None:
        try:
            if hasattr(journal, "startup_cash"):
                startup = journal.startup_cash()
        except Exception:
            startup = None
        try:
            if current is None and hasattr(journal, "account_performance"):
                perf = journal.account_performance() or {}
                nl = perf.get("net_liquidation")
                if nl is not None:
                    current = float(nl)
        except Exception:
            pass
        try:
            if hasattr(journal, "model_usage_totals"):
                usage = dict(journal.model_usage_totals() or usage)
        except Exception:
            pass

    book_pnl = None
    book_return_pct = None
    if current is not None and startup is not None and startup > 0:
        try:
            current = float(current)
            book_pnl = current - float(startup)
            book_return_pct = (book_pnl / float(startup)) * 100.0
        except (TypeError, ValueError):
            current = None
            book_pnl = None

    cost = float(usage.get("cost_usd") or 0.0)
    edge = None if book_pnl is None else (book_pnl - cost)
    beating = None if edge is None else (edge > 0)

    return {
        "startup_cash": float(startup) if startup else None,
        "net_liquidation": current,
        "book_pnl": book_pnl,
        "book_return_pct": book_return_pct,
        "model_calls": int(usage.get("calls") or 0),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "model_cost_usd": cost,
        "edge_usd": edge,
        "beating_model": beating,
        "goal": "book return % of starting NetLiq > cost of the model",
    }


def format_scorecard_block(
    *,
    equity: float | None = None,
    journal: Any = None,
) -> str:
    sc = compute_scorecard(equity=equity, journal=journal)
    beat = sc.get("beating_model")
    if beat is True:
        verdict = "BEATING the model bill"
    elif beat is False:
        verdict = "LOSING to the model bill — tighten pacing / skip weak hunts"
    else:
        verdict = "insufficient book history"
    pnl = sc.get("book_pnl")
    pnl_s = f"{pnl:+.2f}" if pnl is not None else "n/a"
    ret = sc.get("book_return_pct")
    ret_s = f"{ret:+.2f}%" if ret is not None else "n/a"
    edge = sc.get("edge_usd")
    edge_s = f"{edge:+.2f}" if edge is not None else "n/a"
    start = sc.get("startup_cash")
    start_s = f"{start:.2f}" if start is not None else "n/a"
    return (
        "SCORECARD (primary goal — also a self-tune signal):\n"
        f"- first_NL={start_s} NL={sc.get('net_liquidation')}\n"
        f"- book_pnl={pnl_s} ({ret_s} of starting NetLiq)\n"
        f"- model_cost=${sc['model_cost_usd']:.4f} "
        f"({sc['model_calls']} calls, {sc['input_tokens']}+{sc['output_tokens']} tok)\n"
        f"- edge(book−model)={edge_s} → {verdict}\n"
        "If losing to the model bill: lengthen cycle_sleep / lower intelligence budget / "
        "narrow universe via self_tune. Do not skip protect."
    )
