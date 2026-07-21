# Asset Balancing Control X Auto (ABCXAUTO)

**Autonomous Grok-powered agentic portfolio for IBKR (paper-first).**

## Goal

**Greater return on startup cash than the cost of the AI model.**

Intelligence is a billable input, not free alpha. The wrapper should earn more
from the book than it spends on model calls — or you are financing a very
expensive journal. That bar is an experiment, not a fixed dollar rule:

- At small paper edge, burning large API spend on “looks fine → hold” is waste.
- At real notionals (e.g. multi‑thousand‑dollar trades), under‑spending on
  thinking can be the expensive mistake.
- The frontier to find: **return from intelligence > cost of intelligence.**
  Weigh API spend (calls / rough $) against book P&L and size over time — the
  right unit may not be “$100 per decision,” but the question stays the same.

## Control + Unbiased (non-negotiable)

1. **You control** — capacity (posture / capital box), deliberation vs quiet
   process, intelligence budget (cadence / how often Grok may burn tokens),
   Risk gates, optional Operator Card, Pro START/Stop/pause, and mandate.
   The product is a wrapper you tune — including when to pay for deeper work.
2. **Shell is unbiased** — Fact / Gate / labeled Heuristic only. Never invents
   stance or hold, never ranks SCAN TAPE or “top idea,” never ships strategy
   taste in prompts, never skips Judge behind your back to “save money.”
   Taste = Controls / optional Card (you) or Grok judgment (AI).
3. **Grok owns judgment** — when your budget allows a cycle, Judge runs. Act
   may skip only under process rules you set (e.g. after Grok already chose
   idle or manage+hold). Hard gates protect capital; they do not pick strategies.

### Capacity ≠ deliberation ≠ budget

These are different levers (Kahneman-shaped, not “trade more”):

| Lever | Question |
|-------|----------|
| **Capacity** | How much capital risk is *allowed*? (posture / envelope) |
| **Deliberation** | System 1 lean (fast / quiet when protected) vs System 2 lean (mega‑worker effort — verifiable work on picks, not a shrug-hold) |
| **Intelligence budget** | How often / how expensive may those decisions be? (cadence, Act-skip policy) so API $ tracks opportunity, not boredom |

Posture is not eagerness. Quiet hold when protected can be correct — or it can
be System 1 coasting. You set how careful and how costly thinking may be;
Grok still decides *what* to do inside that box.

Grok 4.5 **owns** a paper IBKR portfolio under hard risk rules. Protect first;
hold is valid when the book is protected. See `SPEC.md` for full doctrine.

## 1. Connections

| Need | How |
|------|-----|
| TWS paper | API on port **7497** (or Gateway 4002) |
| xAI | `XAI_API_KEY` in `.env` |
| MarketData.app | optional `MARKETDATA_TOKEN` for quotes/chains/news |

```powershell
cd C:\Users\nvick\ABCXAUTO
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.template .env   # fill XAI_API_KEY; optional MARKETDATA_TOKEN
```

## 2. How the agent thinks

Each cycle is **Perceive → Judge → Act**:

| Stage | Owner | Role |
|-------|--------|------|
| Perceive | Code | Book, orders, SCAN TAPE, news, open risk, option facts (MDA greeks/IV) |
| Judge | Grok | Operates scanner (`scan_request` → MDA); stance + thesis + intent |
| Act | Grok | Structure vs **IBKR live** quote (not MDA tape last) |
| Grade | Code | Geometry / share-lot / risk gates — accept or reject with reason codes |

Act may send stock brackets **and** allowlisted option structures (vertical,
iron condor, CSP, roll, overlays, …) under stance maps + `defined_risk_only`
gate. Partial stock/option closes by quantity; `stop_qty_fact` flags post-trim
stop size mismatch. Shell does not prefer a structure — Controls dials / Grok
choose (optional Card is secondary free-text).

Stop agent = pause decisions only. Positions stay at IBKR. Open risk is a
**multi-plan book** reconciled from the broker across Stop/Start
(`active_trade_plans.json`; legacy `active_trade_plan.json` migrates).

**Mega-worker (fluid + objective):** shell hard-gates unprotected / halt /
capacity / flat-unconfirmed only. Open book does **not** forbid new-risk when
slots remain. Allocator may run open-risk vs new-risk (and budgeted escapade)
Grok Act streams → one send. Steer via Controls dials — no free-text work briefs.

## 3. Operator surfaces

- **Pro Dashboard** — Live ops: pace/attention, open risk, last cycle (judge/act), unranked
  tape, activity. **Positions** holds the book table + working orders / fills blotter.
- **Controls tab** — Deliberation, intelligence budget, trade frequency, **structure complexity**
  (Act allowlist), **book capacity**. Disjoint from Risk.
- **Universe tab** — Arena toggles (IBKR vs MDA-seed labeled), legal-set browser with source tags,
  Save arenas vs Refresh membership. Grok picks inside; shell does not rank.
- **Risk tab** — Capital survival sliders + halt (preset seeds Risk only). Save risk separate.
- **Test Suite** — paper place/cancel gym for order mechanics (not live curriculum trading).
- **Operator Card** (optional, advanced) — free-text beliefs; empty by default.
  Prefer Controls dials. Copy [`operator_card.example.txt`](operator_card.example.txt)
  → `operator_card.txt` (gitignored) or set `ABCXAUTO_OPERATOR_CARD` only if you
  need prose beyond the dials. The example is a blank outline — **not** loaded
  into prompts and not shell strategy defaults.

## 4. Run

```powershell
python -m abcxauto
```

Launches **Pro** (Flet). Connect IBKR · Start agent · Close All Positions.

```powershell
python -m abcxauto --cleanup --aggressive   # stale Flet/Python cleanup
python -m abcxauto --headless               # exits 0; autonomy via Pro START
```

## 5. Supported orders

Matches `ORDER_EXAMPLES` / sendable types. Manage stance can also use overlays
(`covered_call`, `collar`, `protective_put`) when long ≥100 shares — see
TRADE PLAYBOOK (preconditions + shell rejects only).

| Type | Role |
|------|------|
| `hold` | No-op when protected |
| `bracket` / `market_bracket` | Entry with stop + target |
| `oca` | Attach stop + target to an open position |
| `modify_stop` / `modify_target` / `trailing_stop` | Adjust protection |
| `cancel_order` | Cancel by order id |
| `market_order` / `limit_order` / `stop_order` / `stop_limit` | Exit only (`closing_position`) |
| `close_option` | Close an option position |
| `covered_call` / `collar` / `protective_put` | Manage overlays (share-lot gated) |
| `vertical_spread` / `iron_condor` / `butterfly` / … | Multi-leg / CSP / roll (hunt or manage; see playbook) |
| `set_risk` | Retune capital knobs inside posture envelope (no broker send) |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ABCXAUTO_CYCLE_SLEEP_S` | `120` | Hunt-floor sleep (adaptive pacing may sleep longer) |
| `ABCXAUTO_GROK_MIN_INTERVAL_S` | `120` | Min Grok spacing unless protect / urgent wake |
| `ABCXAUTO_PACE_PROTECT_S` | `20` | Sleep when unprotected STK |
| `ABCXAUTO_PACE_MANAGE_S` | `60` | Sleep with open risk / trade plan |
| `ABCXAUTO_PACE_IDLE_S` | `240` | Idle-floor component when flat/idle |
| `ABCXAUTO_MONITOR_POLL_S` | `30` | Snapshot refresh (may wake cycle) |
| `ABCXAUTO_MODEL` | `grok-4.5` | xAI model |
| `ABCXAUTO_RISK_POSTURE` | _(empty)_ | Risk capital preset: `defensive` / `balanced` / `aggressive` (Risk tab) |
| `ABCXAUTO_CONTROL_DELIBERATION_PCT` | `50` | 0=S1 lean … 100=S2 mega-worker (≥60 disables cheap Act-skip) |
| `ABCXAUTO_CONTROL_BUDGET_PCT` | `50` | Intelligence budget: 0=protect API $ … 100=more frequent Grok |
| `ABCXAUTO_CONTROL_FREQUENCY_PCT` | `50` | Trade frequency: process/streams only (no max_daily_trades gate) |
| `ABCXAUTO_CONTROL_ROTATION_PCT` | `50` | Capital rotation: hold OK ↔ redeploy/free cash OK (process; no auto-sell) |
| `ABCXAUTO_CONTROL_COMPLEXITY_PCT` | `50` | Structure complexity: 0=stock only … 100=full multi-leg allowlist |
| `ABCXAUTO_MAX_OPEN_POSITIONS` | `0` | Book capacity (Controls-owned; 0=unlimited) |
| `ABCXAUTO_UNIVERSE_PATH` | `universe_allowlist.json` | Universe sandbox persistence |
| `ABCXAUTO_OPERATOR_CARD` | _(empty)_ | Optional free-text (secondary to Controls) |
| `ABCXAUTO_OPERATOR_CARD_PATH` | `operator_card.txt` | File fallback for Operator Card |
| `ABCXAUTO_JOURNAL_PATH` | `journal.db` | SQLite journal |
| `ABCXAUTO_RISK_SETTINGS_PATH` | `risk_settings.json` | Persisted Risk + Controls (gitignored) |

Capital / daily-loss gates default off until you Apply posture or set env knobs.
See `.env.template` for the full list.

## Architecture

Thin product shell. Priority: **risk > execution > monitoring > thin UI**.

```
abcxauto/
  agent_loop.py         Perceive → Allocator → Act streams → one send
  mega_worker.py        Capacity / stream select / send merge
  universe.py           IBKR sandbox legal set (Universe tab)
  structure_complexity.py  Act strategy allowlist from Controls dial
  trade_plan.py         Multi-plan open-risk book
  pacing.py             Adaptive sleep tiers + wake whitelist + grok budget
  world_state.py        Code truth for prompts (capacity + exposure Fact)
  opportunity_scan.py   SCAN TAPE from Universe legal set + MDA metrics
  trade_playbook.py     Preconditions + shell rejects (no style dogma)
  trade_plan.py         Open-risk continuity across Stop/Start
  objective_language.py Banned taste phrases + taxonomy helpers
  structure_grade.py    Geometry / scrape lessons
  order_examples.py     How to send (param shapes)
  risk_gates.py         Hard pre-trade gates + halt latch
  executor.py / send.py Validate → gate → IBKR
  pro_desktop.py        Flet Pro cockpit
  config.py             Env + Risk/Controls + optional Operator Card
  memory/               SQLite journal
  broker/               IBKR layer
```

**Adaptive pacing** (process): protect ~20s · manage ~60s · hunt floor =
`CYCLE_SLEEP` · idle longer · wakes on unprotected/fill/halt/flat_confirmed.
Doctrine + Phase 5 ritual: `.cursor/skills/abcxauto-gym/`.
Daily/weekly KPIs: `python scripts/phase5_day_report.py` (`--week`).

## Control + Unbiased rule

| Pillar | Meaning |
|--------|---------|
| Goal | Book return on startup cash > cost of the model (weigh; don’t fixate on one $) |
| Control | You set capacity, deliberation, intelligence budget, Risk gates, Card, start/stop — shell does not steal dials or fake hold to save tokens |
| Unbiased Fact / Gate | Code + prompts; gates protect capital, never pick strategies |
| Heuristic | Labeled `heuristic ≠ recommendation` |
| Taste | Controls / Operator Card or Grok — **not** hard-coded shell prose |

## Tests

```powershell
$env:PYTHONPATH='.'; python -m pytest tests/ -q
```
