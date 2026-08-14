# Asset Balancing Control X Auto (ABCXAUTO)

**Autonomous Grok-powered agentic portfolio for IBKR (paper-first).**

The product is **Grok's lab on paper, live as a follower** — full NetLiq, no dollar sleeve.

## Goal

**Grok owns the book.** The operator does not write a strategy. Grok invents
standing instructions, tries them on **paper** (TWS 7497), reads the journal +
scorecard, keeps what made book return % beat model cost, and does those
winners more. **Live** (TWS 7496, confirm phrase, different client id) only
follows a **promoted paper playbook**. It never copies paper fills.

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
   weaken them. Live remains gated until you connect 7496 with the confirm phrase
   **and** a promoted playbook exists.
3. **Operator is setup + kill switch** — `.env` + paper TWS, then Start.
   Stop / Halt / Panic / Ctrl+C. UI is status/monitoring only.
4. **Scorecard** — book return **% of starting NetLiq** must beat model API
   cost. The agent reads its journal + scorecard every cycle and tunes itself.

Size, daily-loss, risk-per-trade, and scorecard are **% of the portfolio**.
Walk-away defaults: **15** max positions, 2% daily-loss halt,
1% risk/trade, 20% max position, defined-risk on, `trading_budget_usd=0`
(full NetLiq).

Grok (`ABCXAUTO_MODEL`) **owns** a paper IBKR portfolio under that floor.
The shell does not teach IBKR. Protect-first is code. See `SPEC.md`.

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

Each cycle is **Perceive → Judge → Act**.
Grok writes `lab_playbook` (its own instructions). Paper explores/exploits.
Live hunt is blocked until that playbook is promoted (scorecard beating +
`ready_to_promote`). Protect-first still interrupts.

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

**Mega-worker:** shell hard-gates unprotected / halt / capacity. One Act per
cycle: work the open book, or hunt a new entry if capacity allows. Steer via
Controls dials — Grok self_tunes those; the UI is status only.

## 3. Operator surfaces

- **Pro Dashboard** — status only: pace, open risk, last cycle, tape, activity.
- **Positions** — book table + working orders / fills.
- **Risk** — floor display + Halt/Resume kill switch. Sliders are status-only.
- **Scorecard** — book return % of starting NetLiq vs model cost (the only goal).

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
| `ABCXAUTO_MAX_OPEN_POSITIONS` | `15` | Book capacity (Grok sets 1-25; 0 forbidden) |
| `ABCXAUTO_DAILY_LOSS_LIMIT_PCT` | `25` | Daily-loss halt vs NetLiq (agent cannot raise) |
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
  lab_playbook.py       Grok-written instructions; live follows a promote
  brain.py              Grok tool loop (book, quote, scan, send)
  agent_loop.py         Snap facts → Grok tools → clerk gates/send
  universe.py           Legal symbol set (no Universe tab)
  trade_plan.py         Multi-plan open-risk book
  pacing.py             Adaptive sleep + wake whitelist
  world_state.py        Code truth for prompts
  opportunity_scan.py   SCAN TAPE + MDA metrics
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

**Adaptive pacing** (process): protect ~20s interrupts. Hunt-floor
`CYCLE_SLEEP` when hunting. Wakes on unprotected/fill/halt/flat_confirmed.

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

## Desktop icon

```powershell
python scripts/install_desktop_icon.py
```

Launches Flet Pro (`python -m abcxauto`). See [`docs/CYCLE.md`](docs/CYCLE.md).
