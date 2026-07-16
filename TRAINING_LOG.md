# ABCXAUTO Training Log

Session date: **2026-07-16**  
Paper account: **DUN976979** · NetLiq ≈ **$37,016.88** · port **7497**

## Phase 0 — Environment — PASS

| Check | Result |
|-------|--------|
| `.env` present | yes |
| `XAI_API_KEY` | configured |
| `MARKETDATA_TOKEN` | configured |
| Trading mode | paper |
| IBKR `connect()` | ok |
| Connection status | IBKR + xAI + MDA all ready |

## Phase 1 — Operator cockpit — PASS (automated) / hands-on remaining

Pro UI launched (`python -m abcxauto`). Contract check: **PASS**.

| Surface | Role |
|---------|------|
| **Connect IBKR** | Broker + monitor only |
| **Start agent** | Autonomous Grok cycles (~120s) |
| **Book** | Working orders + fills |
| **Agent** | Live decision / Reality Pulse |
| **Activity** | Chronological connect/start/cycle/errors |
| Nav | Dashboard · Positions · Risk · Scorecard · Test Suite |

**Your drills in the open Pro window**

1. **Connect IBKR** (if not already) → Account IBKR green; Activity shows CONNECT; cycles stay 0 until Start.  
2. Open **Book** → Working / Fills; hit the refresh circle.  
3. **Start agent** → Activity shows START + `#N` lines; Agent tab updates.  
4. **Stop agent** (keep Connect) vs **Disconnect IBKR** — know the difference.

## Phase 2 — Risk core — PASS

```
pytest tests/test_risk_gates.py tests/test_executor.py tests/test_risk_config.py
→ 89 passed
```

Invariants to keep memorized (enforced in code, not prompts):

- Stock entries = bracket (SL + TP)
- Bare stock orders = exit only (`closing_position`)
- Exits never blocked by halt
- Fail closed if no account data
- Working stop never cancelled to “clean up” a failed target
- Hold forbidden only while unprotected STK exists

## Phase 3 — Execution surface — PASS

```
pytest order_suite / order_examples / connections_send → 58 passed
run_order_suite(force_dry=True) → 45 pass / 0 fail (mode=dry_run)
```

Sendable contract includes hold, brackets, OCA, modify_*, cancel, bare exits, close_option, plus extended types in `ORDER_EXAMPLES`.

**Optional next (paper place→cancel):** Pro → Test Suite → Re-test all (only while connected on paper).

## Phase 4 — Agent loop + memory — PASS

```
pytest agent_loop / pro_engine / journal / monitor / account_returns
→ 47 passed
```

Journal live:

- enabled: true  
- NAV history: **6 days** (`source=ibkr_nav`)  
- Equity curve updating (latest NetLiq 37016.88)  
- Today’s proposals still 0 until you **Start agent** for cycles  

Cycle path: snap → Grok JSON → normalize → `send` → executor → journal; monitor snapshots/fills in parallel.

## Phase 5 — Forward-test protocol — IN PROGRESS (multi-week)

Run this on paper until evidence graduates you:

### Daily (RTH)

1. Connect IBKR → Start agent.  
2. Leave capital knobs documented (today defaults mostly **0** = off).  
3. End of day Scorecard + Activity review (3 bullets):
   - Survive? (unprotected STK incidents)  
   - Mistake? (bad cancel / surprise halt)  
   - Next? (one change max)  

### Weekly

- Sample 10 Activity cycles: was hold valid? protection first?  
- Journal: hold vs trade, gate blocks, max drawdown  
- Change **one** knob or constraint per week only  

### Graduation (before any live thought)

- [ ] No unprotected STK left open across sessions  
- [ ] Every halt/flatten is explainable  
- [ ] Journal shows survival under fixed knobs for agreed window  
- [ ] Then consider tighter aggression — not before  

## Phase 6 — Live readiness — BLOCKED until Phase 5

Do not switch to live until Phase 5 boxes are checked.

Checklist when ready:

- [ ] Capital limits **on** (not zeros)  
- [ ] Auto-panic policy explicit  
- [ ] Live confirm phrase / mode switch understood  
- [ ] Tiny size first days  

## Suggested cadence (rest of month)

| Week | Focus |
|------|--------|
| This week | Hands-on Phase 1 drills + Start agent ≥5 cycles/day |
| Next | Test Suite paper place→cancel; Risk knobs experiment (one) |
| Weeks 3–4 | Phase 5 protocol locked; first weekly edge review |
