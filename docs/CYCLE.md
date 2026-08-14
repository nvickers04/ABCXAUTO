# Agent cycle — paper lab, live follower

## Goal vs control

**Long-run goal:** book return % of starting NetLiq > model cost.
That is the **primary scorecard** and a **self-tune signal**.
Size and risk are % of the book — the same at $1k, $100k, or $1M. No dollar sleeve.

**Product:** Grok is the trader. The shell does not teach IBKR. Paper is the lab
(TWS 7497). Live only follows a promoted snapshot. Operator = setup + kill switch.

Grok reads journal + scorecard every cycle. Protect-first is code.

## Autonomy

No operator approval. `self_tune` / `set_risk` applies immediately inside the
immutable floor. Agent cannot switch to live or set a dollar sleeve.
In Cursor, `python -m abcxauto` opens Pro and autostarts so the think stream
is on screen (F5 = ABCXAUTO Pro). `--headless` is paper-only.

## The loop

```
SNAP      code facts (book, orders, protection) — 25s box
    |
GROK      tool loop (stalled stream / dead chat reset once)
          skip if IBKR down or session closed (unless unprotected)
          book / quote / universe / journal / scan / send
    |
CLERK     send → execute_ticket (risk, geometry, allowlist, protect)
          hold forbidden: unprotected STK; paper+flat+RTH
    |
REMEMBER  journal + playbook_lab.json
          promote → playbook_live.json when beating + ready_to_promote
    |
WAIT      pace from book/session (protect still wakes)
```

Optional: none. The hourly/daily calendar construct was removed.

## What stayed (non-negotiable)

- Risk gates LLM-proof; exits never blocked
- Unprotected STK → protect first; hold forbidden while unprotected
- Universe allowlist; shell does not rank ideas
- IBKR live last for geometry (not MDA tape last)
- Fail-closed; paper lab always; live gated + promoted playbook
- Two books = two processes, two client ids (never share 77)

## Controls dials

Agent-owned via `self_tune`. UI shows current values (status only).
Capacity 1-25 open positions (Grok sets N; default 15; 0 forbidden).

## Hard vs soft (slim gates)

| Hard (reject) | Structured field |
|---|---|
| Unprotected → protect | |
| Capacity / legal set / RiskGate | Schema: lab_playbook JSON |
| Live hunt without promoted playbook | |
| Geometry / inventory | |
| Defined-risk / cash-only / 2% daily loss / 20% max pos / 1% risk/trade / 8% peak DD | |

Shell does **not** rank ideas. Do not add prompt tactics for a smarter model.
