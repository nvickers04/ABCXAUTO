# Asset Balancing Control X Auto (ABCXAUTO)

**Autonomous Grok-powered agentic portfolio for IBKR (paper-first).**

The product is **Grokfolio / full NetLiq** on `master`. The $1k sleeve is gone.

## Goal

**Grok owns the full paper book, in % of NetLiq**, like Autopilot's public
Grok Portfolio — scoring → ~15 holdings → calendar rebalance — but on an
**hourly and/or daily** RTH clock (not monthly), executed on **this IBKR paper
account** (not Autopilot copy-trading). Hunt/hold scalping is not the product.

Same rules at $1k, $100k, or $1M. Book return % must beat the cost of the AI model.

Intelligence is a billable input, not free alpha. The wrapper should earn more
from the book than it spends on model calls — or you are financing a very
expensive journal. That bar is an experiment, not a fixed dollar rule:

- At small paper edge, burning large API spend on “looks fine → hold” is waste.
- At real notionals (e.g. multi‑thousand‑dollar trades), under‑spending on
  thinking can be the expensive mistake.
- The frontier to find: **return from intelligence > cost of intelligence.**
  Weigh API spend (calls / rough $) against book P&L and size over time — the
  right unit may not be “$100 per decision,” but the question stays the same.

## Autonomous + immutable floor (non-negotiable)

1. **Grok owns the dials** — settings, parameters, prompts, pacing, universe
   focus, and strategy knobs. `self_tune` (alias `set_risk`) applies immediately.
   No human approval, no proposal step, no operator Controls save.
2. **Immutable floor is code** — daily-loss halt, max position size, max open
   positions, defined-risk, cash-only, auto-panic, unprotected-STK protect-first,
   exits never blocked, fail-closed. The agent may *tighten* these; it cannot
   weaken them. Live remains gated.
3. **Operator is setup + kill switch** — `.env` + paper TWS, then Start.
   Stop / Halt / Panic / Ctrl+C. UI is status/monitoring only.
4. **Scorecard** — book return **% of starting NetLiq** must beat model API
   cost. The agent reads its journal + scorecard every cycle and tunes itself.

Size, daily-loss, risk-per-trade, and scorecard are **% of the portfolio**.
Walk-away defaults: **15** max positions (Grokfolio book), 2% daily-loss halt,
1% risk/trade, 20% max position, defined-risk on, `trading_budget_usd=0`
(full NetLiq). Daily construct at 10:00 ET; hourly slots 10-15 ET RTH.

Grok 4.5 **owns** a paper IBKR portfolio under that floor. Protect first;
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

When Grokfolio is on (default), each cycle is **Perceive → (protect if needed) →
Grokfolio construct/diff** on the hourly/daily clock. Hunt Judge/Act scalping
is skipped. Protect-first still interrupts. Outside RTH the shell waits until
the next 10:00 ET weekday slot.

If Grokfolio is disabled, the legacy cycle is **Perceive → Judge → Act**:

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

- **Pro Dashboard** — status only: pace, open risk, last cycle, tape, activity.
- **Positions** — book table + working orders / fills.
- **Controls / Universe / Risk tabs** — live values (agent-owned). Saves are
  no-ops. Grok `self_tune`s these. Risk halt/resume remain the kill switch.
- **Scorecard** — book return % of starting NetLiq vs model cost (the only goal).
- **Test Suite** — paper place/cancel gym for order mechanics.

Kill switch: Stop agent, Risk Halt, Panic, or `Ctrl+C` on `--headless`.
Positions stay at IBKR.

## 4. Run

```powershell
python -m abcxauto              # Pro desktop + Grok stream (autostarts in Cursor)
python -m abcxauto --headless   # console-only outside Cursor; Ctrl+C = kill
python -m abcxauto --cleanup --aggressive   # stale Flet/Python cleanup
```

In Cursor, F5 / Run **ABCXAUTO Pro** (or any `python -m abcxauto`) opens the
cockpit and starts the agent so you can watch the think stream. Console-only:
`$env:ABCXAUTO_FORCE_HEADLESS=1`. One IBKR client id **77**.

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
| `self_tune` / `set_risk` | Agent retunes knobs (cannot weaken the floor; no broker send) |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ABCXAUTO_CYCLE_SLEEP_S` | `300` | Hunt-floor sleep (agent may lengthen, not shorten) |
| `ABCXAUTO_GROK_MIN_INTERVAL_S` | `300` | Min Grok spacing unless protect / urgent wake |
| `ABCXAUTO_PACE_PROTECT_S` | `20` | Sleep when unprotected STK |
| `ABCXAUTO_PACE_MANAGE_S` | `60` | Sleep with open risk / trade plan |
| `ABCXAUTO_PACE_IDLE_S` | `600` | Idle-floor when flat/idle |
| `ABCXAUTO_MAX_OPEN_POSITIONS` | `15` | Book capacity (floor 8-15; grokfolio default 15) |
| `ABCXAUTO_GROKFOLIO_ENABLED` | `true` | Own the full book on hourly/daily clock |
| `ABCXAUTO_GROKFOLIO_CADENCE` | `both` | `hourly` / `daily` / `both` |
| `ABCXAUTO_GROKFOLIO_HOLDINGS` | `15` | Target names in the construct |
| `ABCXAUTO_DAILY_LOSS_LIMIT_PCT` | `2` | Daily-loss halt vs NetLiq (agent cannot raise) |
| `ABCXAUTO_MAX_POSITION_PCT` | `20` | Max position vs NetLiq (agent cannot raise) |
| `ABCXAUTO_DEFINED_RISK_ONLY` | `true` | Locked on |
| `ABCXAUTO_JOURNAL_PATH` | `journal.db` | SQLite journal |
| `ABCXAUTO_RISK_SETTINGS_PATH` | `risk_settings.json` | Persisted Risk + Controls (gitignored) |

Capital / daily-loss gates are **on** as % of NetLiq. The agent cannot
turn them off. See `.env.template` for the full list.

## Architecture

Thin product shell. Priority: **risk > execution > monitoring > thin UI**.

```
abcxauto/
  grokfolio.py          Hourly/daily book owner (construct + gated diffs)
  agent_loop.py         Perceive → Grokfolio or Judge/Act → hard gates/send
  mega_worker.py        Capacity / stream select / send merge
  universe.py           IBKR sandbox legal set (Universe tab)
  structure_complexity.py  Act strategy allowlist from Controls dial
  trade_plan.py         Multi-plan open-risk book
  pacing.py             Adaptive sleep tiers + grokfolio wait + wake whitelist
  world_state.py        Code truth for prompts (capacity + exposure Fact)
  opportunity_scan.py   SCAN TAPE from Universe legal set + MDA metrics
  trade_playbook.py     Preconditions + shell rejects (no style dogma)
  objective_language.py Banned taste phrases + taxonomy helpers
  structure_grade.py    Geometry / scrape lessons
  order_examples.py     How to send (param shapes)
  risk_gates.py         Hard pre-trade gates + halt latch
  executor.py / send.py Validate → gate → IBKR
  pro_desktop.py        Flet Pro cockpit
  self_tune.py          Agent self-mod (floor-clamped; no approval)
  scorecard.py          Book return vs model cost
  headless.py           Paper loop without UI
  think_stream.py       Live Grok think/say (ASCII on Windows)
  config.py             Env + walk-away floor + agent_state
  memory/               SQLite journal
  broker/               IBKR layer
```

**Adaptive pacing** (process): protect ~20s interrupts Grokfolio wait.
Grokfolio sleeps until the next hourly/daily slot (overnight until next
10:00 ET weekday is OK). Hunt-floor `CYCLE_SLEEP` applies only when
Grokfolio is off. Wakes on unprotected/fill/halt/flat_confirmed.
Doctrine + Phase 5 ritual: `.cursor/skills/abcxauto-gym/`.
Daily/weekly KPIs: `python scripts/phase5_day_report.py` (`--week`).

## Autonomy + floor

| Pillar | Meaning |
|--------|---------|
| Goal | Book return % of starting NetLiq > cost of the model |
| Autonomy | Grok self_tunes all non-risk knobs — no approval |
| Immutable floor | Code: daily loss, size, defined-risk, protect-first, fail-closed, exits always |
| Operator | Initial setup + emergency kill switch. UI = status |

## Tests

```powershell
$env:PYTHONPATH='.'; python -m pytest tests/ -q
```

## Desktop Pro (web shell)

Native window + Desktop icon for the web Pro UI:

```bash
pip install pywebview
cd web-pro && npm install && npm run build && cd ..
python scripts/install_desktop_icon.py
python -m abcxauto --desktop
```

Default `python -m abcxauto` still launches Flet Pro. Use `--desktop` for this shell.

**Live data:** `--desktop` serves FastAPI + UI. Connect requires paper TWS (7497).
Book/Focus then read IBKR positions, stops, and historical bars (MDA fallback).
See [`web-pro/README.md`](web-pro/README.md) and [`docs/CYCLE.md`](docs/CYCLE.md).
