# Agent cycle — Grokfolio owns the book

## Goal vs control

**Long-run goal:** book return % of starting NetLiq > model cost.
That is the **primary scorecard** and a **self-tune signal**.
Size and risk are % of the book — the same at $1k, $100k, or $1M. No dollar sleeve.

**Product:** Autopilot-style Grokfolio on this IBKR paper account. A scoring
pass feeds Grok; Grok constructs ~15 long holdings as % of NetLiq; the shell
diffs the book and sends gated actions. Clock is **hourly and/or daily** RTH
(default both: daily at 10:00 ET, hourly 10-15 ET), not monthly. Hunt/hold
scalping is not the product. Operator = setup + kill switch.

Grok reads journal + scorecard every construct. Protect is never skipped.

## Autonomy

No operator approval. `self_tune` / `set_risk` applies immediately inside the
immutable floor. Agent cannot disable grokfolio or set a dollar sleeve.
Operator = setup + kill switch. Paper-first; live gated.
In Cursor, `python -m abcxauto` opens Pro and autostarts so the think stream
is on screen (F5 = ABCXAUTO Pro). `--headless` is console-only outside Cursor.

## The loop (Grokfolio)

```
PERCEIVE  code facts (book, orders, account, protection, tape, news)
    |
PROTECT   if unprotected STK: Judge/Act hunt path (hold forbidden)
    |
GROKFOLIO if enabled and not protect:
            wait until next slot  OR  construct weights + gated diffs
            (multiple sends in one session is intended)
    |
VERIFY    hard gates only (risk $, geometry, allowlist, protect rules)
    |
SEND      choke point → broker (or reject with reason)
    |
REMEMBER  journal + grokfolio_state.json
    |
WAIT      until next hourly/daily slot (overnight to next 10:00 ET weekday OK)
          protect-first still wakes
```

If `ABCXAUTO_GROKFOLIO_ENABLED=false`, the legacy Perceive → Judge → Act
hunt path remains.

## What stayed (non-negotiable)

- Risk gates LLM-proof; exits never blocked
- Unprotected STK → protect first; hold forbidden while unprotected
- Universe allowlist; shell does not rank ideas
- IBKR live last for geometry (not MDA tape last)
- Fail-closed; paper-first; live gated
- Grokfolio may send several gated actions in one cycle (old one-action
  model does not apply here)

## Controls dials

Agent-owned via `self_tune`. UI shows current values (status only).
Capacity floor is 8-15 open positions (default 15) when grokfolio is on.
Old persisted `max_open_positions=2` is repaired to 15.

## Hard vs soft (slim gates)

| Hard (reject) | Structured field | Soft (prompt) |
|---|---|---|
| Unprotected → protect | Idle + tape → `dismissed` | setup_grade × posture |
| Capacity / legal set / RiskGate | Schema: holdings JSON | regime_fit |
| Geometry / inventory | | thesis AFFIRM/REVISE |
| Defined-risk / cash-only / 2% daily loss / 20% max pos / 1% risk/trade / 8% peak DD | | |

Shell does **not** rank ideas. Soft lessons never block a cycle.
