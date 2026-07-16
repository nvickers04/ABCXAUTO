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

## Phase 5 — Forward-test (PRIMARY) — IN PROGRESS

Full protocol: plan `abcxauto_training_roadmap` Phase 5A–5E. Phases 0–4 done; stay here.

### 5A — Experiment card (lock; edit only on weekly review)

| Field | Value |
|-------|--------|
| Window | 2026-07-16 → ________ (aim ≥10 RTH days) |
| Posture | ________ (e.g. aggressive) |
| Knobs snapshot | risk_settings / .env noted: ________ |
| Success | scrape rate ↓; 0 unprotected left open; coherent PJA |
| Kill if | naked STK overnight; unexplained halt; same stop recycled |

### 5B — Daily debrief template (copy per session)

**Date:** ________ · **Cycles:** ________ · **Posture:** ________

1. Survive:  
2. Structure (scrapes / geometry_* / ok hunts):  
3. Judgment (idle dismiss? symbol switch on cooldown?):  
4. Book (flat / plan / fills make sense?):  
5. Next (one note for weekly bucket — no mid-day code):  

### 5C — KPIs (glance daily, tally weekly)

| KPI | This week | Notes |
|-----|-----------|--------|
| Unprotected left open | | target 0 |
| Scrapes / entries | | falling |
| Geometry rejects | | teaching, then declining |
| Distinct hunt symbols | | ≥2 if #1 cooling |
| Gate blocks (explainable?) | | |
| Max DD / day PnL | | |

### 5D — Weekly review notes

**Week of:** ________  
10-cycle sample: ________  
One change for next week: ________  
Why: ________  

### 5E — Graduation (all required before Phase 6)

- [ ] Window complete under locked card  
- [ ] Zero unprotected left open  
- [ ] Halts/flattens explainable  
- [ ] Scrapes improved or under target; cooldown visible after scrape  
- [ ] Can narrate 3 random cycles World → Judgment → Action  
- [ ] Written decision: continue paper / tighten / kill edge  

## Phase 6 — Live readiness — BLOCKED until 5E

- [ ] Capital limits **on** (not zeros)  
- [ ] Auto-panic policy explicit  
- [ ] Live confirm phrase / mode switch understood  
- [ ] Tiny size first days  

## Suggested cadence (Phase 5–heavy)

| Window | Focus |
|--------|--------|
| Today | Fill 5A card; one full 5B session |
| This week | Daily 5B + 5C; no new features |
| Week 2 | First 5D; ≤1 knob change |
| Weeks 3–4 | Push toward 5E decision |
