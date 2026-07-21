# ABCXAUTO Training Log

Paper account: **DUN976979** · NetLiq ≈ **$37k** · port **7497**  
Phases **0–4: PASS**. Phase **5: PRIMARY**. Phase **6: BLOCKED** until 5E.

**Attention budget:** human ritual (this log) + agent pacing (`abcxauto/pacing.py`) —
Cursor skill `.cursor/skills/abcxauto-gym/`.  
Daily numbers: `python scripts/phase5_day_report.py` · Weekly: `… --week`.

---

## Phase 0 — Environment — PASS

| Check | Result |
|-------|--------|
| `.env` present | yes |
| `XAI_API_KEY` | configured |
| `MARKETDATA_TOKEN` | configured |
| Trading mode | paper |
| IBKR `connect()` | ok |

## Phase 1 — Operator cockpit — PASS

Connect ≠ Start. Book · Agent (World / Judgment / Action + pace) · Activity · Risk · Test Suite.

## Phase 2 — Risk core — PASS

Stock entries = bracket; bare stock = exit only; exits never blocked by halt; hold forbidden only while unprotected STK.

## Phase 3 — Execution surface — PASS

ORDER EXAMPLES + Test Suite dry-run green; structure vocab for Act.

## Phase 4 — Perceive → Judge → Act — PASS

WorldState → Judge → Act → geometry → send → journal. Open-risk continuity across Stop/Start.

---

## Phase 5 — Forward-test (PRIMARY) — IN PROGRESS

### 5A — Experiment card — LOCKED 2026-07-20

Edit only on weekly review (5D). Do not mid-week churn.

| Field | Value (locked) |
|-------|----------------|
| Window | **2026-07-20 → 2026-08-03** (≥10 RTH days) |
| Posture | **aggressive** (from `risk_settings.json`) |
| Capital knobs snapshot | `risk_gates_enabled=false`; `daily_loss_limit_pct=8`; `max_position_pct=18`; `max_risk_per_trade_pct=2.5`; `max_open_positions=10`; `max_daily_trades=20`; `min_reward_risk=0.8`; `defined_risk_only=true`; `cash_only=true`; `auto_panic_on_breach=false` |
| Cycle / pace | Hunt floor `ABCXAUTO_CYCLE_SLEEP_S=120`; adaptive protect/manage/idle via `pacing.py` |
| Allowed code changes | Bugfixes that unblock metrics only; **no** new strategies / taste prompts |
| Success metrics | Scrape rate falling (&lt;20% of entries by window end); **0** unprotected left open; coherent PJA; ≥2 hunt symbols/week if #1 cooling |
| Kill criteria | Naked STK overnight; unexplained halt; same stop recycled across sessions; scrape rate not improving |

**Pass (5A):** Card locked before next Start agent — done.

---

### 5B — Daily RTH ritual

**Pre-open (2 min)**

1. Connect IBKR only — Book refresh, NetLiq sane, no unprotected STK.  
2. Risk tab posture = **aggressive** (matches card).  
3. Optional: Test Suite dry-run if nothing changed overnight.

**During (hands-off)**

4. Start agent. Watch Agent / Activity.  
5. Intervene only for: unprotected stuck, runaway same-symbol scrapes, broker disconnect.

**Mid-session spot-check (1–2×)** — without opening prompts:

- **World** — features #1, posture, idle streak, cooldown, open risk, pace tier  
- **Judgment** — stance / thesis / dismiss / intent  
- **Action** — strategy + structure grade  

**Close (5 min)**

```powershell
python scripts/phase5_day_report.py
```

Paste skeleton into a dated block below; fill the five bullets.

#### 5B template (copy per session)

**Date:** ________ · **Cycles:** ________ · **Posture:** aggressive

1. Survive:  
2. Structure (scrapes / geometry_* / ok hunts / rate from day report):  
3. Judgment (idle dismiss? symbol switch on cooldown?):  
4. Book (flat / plan / fills make sense?):  
5. Next (one note for weekly bucket — **no** mid-day code):  

#### Session log

<!-- Add newest sessions at the top -->

**Date:** 2026-07-20 · **Cycles:** ________ · **Posture:** aggressive

1. Survive: _(fill after RTH)_  
2. Structure:  
3. Judgment:  
4. Book:  
5. Next:  

---

### 5C — Structure & edge KPIs

Glance daily (day report); tally weekly (`--week`).

| KPI | Target (paper window) | This week | Notes |
|-----|----------------------|-----------|-------|
| Unprotected STK left open | 0 | | Any overnight naked = kill |
| Scrape rate | Falling; &lt;20% entries by end | | Same stop recycled = red |
| Geometry rejects | Early teaching → declining | | Zero rejects + scrapes = blind gate |
| Hunt symbol diversity | ≥2/week if #1 cooling | | QQQ-only loop = red |
| Idle quality | Flat+ideas → dismiss cites #1 | | Empty idle under agg+ideas = red |
| Gate blocks | Explainable | | Unknown cascade = red |
| Hold vs trade | Not optimizing trade count | | Forced activity = red |
| Max DD / day PnL | Within card tolerance (8% daily loss knob) | | Unexplained cliff = red |

**Anti-patterns (never again)**

- Recycled stop (e.g. 711.99) vs live ~708  
- Judge hunt → judgment_rejected cooldown loop  
- Suite fixture lessons drowning real scrapes in Agent  
- Mid-day strategy features during a locked window  

```powershell
python scripts/phase5_day_report.py --week
```

---

### 5D — Weekly edge review (30–45 min, once/week)

1. Sample **10 Activity cycles** across the week — World → Judgment → Action coherent?  
2. Journal / day-report stats: entries, scrapes, geometry, fills, gate notes.  
3. Structure rollup: `scrape_suspect`, `geometry_*`, `ok` hunts.  
4. Thesis continuity: affirmed / revised / closed — or amnesia?  
5. **Exactly one** change for next week: posture **or** one capital knob **or** cycle sleep **or** one prompt/pressure line — **not** a new module.  

#### 5D template

**Week of:** ________  

10-cycle sample: ________  

Structure rollup: ________  

Thesis: ________  

One change for next week: ________  

Why: ________  

Card updated? yes / no  

---

### 5E — Graduation checklist (all required before Phase 6)

- [ ] Window **2026-07-20 → 2026-08-03** complete under locked card  
- [ ] Zero unprotected STK left open across the window  
- [ ] Every halt/flatten explainable from Activity/journal  
- [ ] Scrape rate improved or &lt;20% of entries; cooldowns fire after scrapes  
- [ ] Geometry rejects taught (later cycles show different stops/symbols)  
- [ ] Knobs held fixed except the single weekly (5D) change  
- [ ] Can narrate 3 random cycles World → Judgment → Action without prompts  
- [ ] Written decision: **continue paper** / **tighten aggression** / **kill edge**  

**Decision (fill at window end):** ________  

---

## Phase 6 — Live readiness — BLOCKED until 5E

Do not enable live ports or `ABCXAUTO_LIVE_CONFIRM` until every 5E box is checked.

- [ ] Capital limits **on** (not zeros) — daily loss, max position, max open, max daily trades  
- [ ] `risk_gates_enabled=true` on live  
- [ ] Auto-panic policy explicit (`ABCXAUTO_AUTO_PANIC_ON_BREACH`)  
- [ ] Live confirm phrase understood (`ABCXAUTO_LIVE_CONFIRM=I_UNDERSTAND_LIVE_TRADING_RISK`)  
- [ ] Mode switch understood: paper 7497/4002 vs live 7496/4001  
- [ ] Tiny size first days; posture reconsidered (aggressive paper ≠ aggressive live)  
- [ ] Journal backup + experiment card snapshot archived  

---

## Suggested cadence

| Window | Focus |
|--------|--------|
| Today | 5A locked; one full 5B + day report |
| This week | Daily 5B + 5C; no feature work |
| Week 2 | First 5D; ≤1 knob change |
| Through 2026-08-03 | Push 5E decision |
| After 5E | Phase 6 checklist or explicit stay-paper |

## Artifacts to keep

- This log + locked 5A card  
- `risk_settings.json` snapshot (aggressive window above)  
- Daily 5B blocks  
- `structure_events.jsonl` (gitignored; local)  
- Journal DB backups when changing posture/gates  
- Never-again list (above)
