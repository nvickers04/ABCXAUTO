# Asset Balancing Control X Auto (ABCXAUTO)

**Grok owns a paper IBKR book. The clerk is facts, hard gates, and an overnight park clock.**

Grok (the `model` knob, default grok-4.6) invents tickets and standing notes. Live (TWS **7496**, confirm phrase, a different client id) only follows a **promoted paper playbook**. It never copies paper fills.

Same rules at $1k, $100k, or $1M. Size, daily-loss, and the scorecard are **% of NetLiq**. Book return % must beat the cost of the model.

## Split of labor

| Owner | Job |
|-------|-----|
| **Grok** | Tickets (`send`), risk/watchlist knobs (`self_tune`), lab notebook (`write_lab_playbook`, optional card `next_look_s`) |
| **Clerk (code)** | Live facts, `ORDER EXAMPLES` schema, hard gates Grok cannot talk around, overnight/after-close park |
| **Operator** | `.env` + paper TWS, Start, kill switch, Settings knobs (brain, pacing, link). No approval step. |

Do not grow the system prompt. Strategy is Grok’s. Switch the brain from Pro Settings — `model` persists to `risk_settings.json`, which beats the `ABCXAUTO_MODEL` env form. Keep the clerk.

## Hard gates (code)

- `send` is the only broker path
- Defined-risk and cash-only
- Size vs `max_risk_per_trade_pct` of NetLiq; daily-loss halt; max position %; capacity `max_open_positions` (default 15)
- One name across every lot vs `max_symbol_concentration_pct` — `max_position_pct` only sees one ticket, so N orders in a name could stack past it. Stock and its options sum
- Unprotected STK: last-stop at IBKR; hold is blocked until it exists. Combo close (`closing_position` on the matching multi-leg send) is one BAG, not new risk
- Live new risk needs a promoted playbook
- Ticket geometry uses **IBKR last**, not MDA
- Exits are never blocked; fail-closed if the book is unknown
- Agent may **tighten** floors via `self_tune`; it cannot weaken them or switch to live

Grok may retune knobs immediately. No proposal step.

## Loop

```
WAKE     Book event / Start / stay-up. Overnight park_clock until the last hour.
         fill / order_change / unprotected poke the open think.
         closed/postmarket does not call Grok (unprotected still does)
    |
SNAP     IBKR book, orders, protection
    |
GROK     tools (facts + send). Wake is a short line — Grok fetches what it needs.
    |
CLERK    send → gates → IBKR. Journal write is clerk, not a Grok tool.
    |
LOOK     RTH has no sit clock. Clerk is not a runner. Overnight parks.
```

`python -m abcxauto` wraps Pro in a supervisor: useful hours are weekdays **8:30–16:00 ET**, TWS **7497** must be listening, crash relaunches, clean window close stays down. `--cleanup` marks operator stop.

## 1. Connections

| Need | How |
|------|-----|
| TWS paper | API on port **7497** (Gateway 4002). Probe the port before launch. |
| xAI | `XAI_API_KEY` in `.env` |
| MarketData.app | optional `MARKETDATA_TOKEN` — `scan` / `news` / `option_facts` greeks, ~15m delayed. `candles` is IBKR hist or the live 5s stream (error if both miss). |
| Polymarket | `odds` implied probs — context, not send geometry |

```powershell
cd C:\Users\nvick\ABCXAUTO
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.template .env   # fill XAI_API_KEY; optional MARKETDATA_TOKEN
```

Paper client id is `IBKR_CLIENT_ID` (template default **42**). Live needs a **different** id on **7496**. Two books = two processes. Settings may change host and client id only while the desk is disconnected.

## 2. Grok tools

IBKR live: `book`, `status`, `quote`, `fills`, `option_chain`, `option_quote`.

MDA delayed: `scan`, `news`, `option_facts` (greeks). `candles` is IBKR hist or the live 5s stream (error if both miss).

Other: `odds` (Polymarket), `playbook` (notebook + score since last write), `write_lab_playbook` (paper notebook, up to 16000 chars, plus setup `cards` — trigger, ticket shape, invalidation, testing/working/retired, an optional `expect_hit_rate` scored against what the card actually hit, optional `next_look_s` clerk cadence, and looks/days with no send so a trigger that never prints is visible; Grok judges), `send`, `self_tune` (flat knobs; `send self_tune` still works).

Universe is a **watchlist** Grok can change via `self_tune`; `send` is not limited to it. Clerk still writes `journal.db`; there is no `journal` tool.

## 3. Operator surfaces

- **Pro** — Flet cockpit + Grok think stream
- **Book / positions** — lots, working orders, fills
- **Risk** — floor display + Halt / Resume / Panic. Knob fields are tighten-only (the writers clamp to the floor); gate switches may re-arm a floor, never disarm one
- **Scorecard** — book return % of starting NetLiq vs model cost

Kill switch: Stop agent, Risk Halt, Panic, `Ctrl+C` on headless, or `python -m abcxauto --cleanup`. Positions stay at IBKR. Open risk is a multi-plan book reconciled from the broker (`active_trade_plans.json`).

## 4. Run

```powershell
python -m abcxauto              # supervisor + Pro desktop + think stream
python -m abcxauto --cleanup --aggressive   # kill leftovers; marks operator stop
```

Console-only (paper): `$env:ABCXAUTO_FORCE_HEADLESS=1`. Do not launch if 7497 refuses. Do not enable live unless you typed the confirm phrase and a promoted playbook exists.

Desktop icon: `python scripts/install_desktop_icon.py`.

## 5. Send

Tickets must match `ORDER EXAMPLES` (`abcxauto/order_examples.py`). Stock entries are brackets (stop + target). Bare stock orders close only. Options: verticals, condors, CSP, rolls, overlays, and the rest of the schema — **defined-risk** still gates.

| Kind | Role |
|------|------|
| `hold` | No-op when protected |
| `bracket` / `market_bracket` / `oca` | Entry or attach protection |
| `modify_stop` / `modify_target` / `trailing_stop` | Adjust protection |
| `cancel_order` | Cancel by order id |
| `market_order` / `limit_order` / `stop_order` / … | Exit (`closing_position`) |
| `close_option` / `roll_option` / spreads / overlays | Option book |
| `self_tune` tool (`set_risk` alias) | Retune knobs inside the floor (not a ticket) |

## Configuration

Walk-away ceilings (agent cannot raise or disable): **25%** daily-loss, **25%** max position, **25%** risk/trade, **25%** per name, defined-risk on, cash-only, `trading_budget_usd=0` (full NetLiq), **15** max open positions.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ABCXAUTO_MODEL` | `grok-4.6` | Brain — Pro Settings `model` wins over this |
| `IBKR_PORT` | `7497` | Paper TWS |
| `IBKR_CLIENT_ID` | `42` | One id per process |
| `ABCXAUTO_MAX_OPEN_POSITIONS` | `15` | Capacity (Grok may set 1–25) |
| `ABCXAUTO_DAILY_LOSS_LIMIT_PCT` | `25` | Daily-loss halt vs NetLiq |
| `ABCXAUTO_MAX_POSITION_PCT` | `25` | Max position vs NetLiq |
| `ABCXAUTO_MAX_RISK_PER_TRADE_PCT` | `25` | Max risk per ticket vs NetLiq |
| `ABCXAUTO_MAX_SYMBOL_CONCENTRATION_PCT` | `25` | Max one underlying, all lots, vs NetLiq |
| `ABCXAUTO_DEFINED_RISK_ONLY` | `true` | Locked on |
| `ABCXAUTO_JOURNAL_PATH` | `journal.db` | Clerk SQLite journal |
| `ABCXAUTO_DEFAULT_LOOK_S` | `90` | Overnight park default when minutes-to-open is unknown |

See `.env.template` for the rest. Live: `TRADING_MODE=live`, port **7496**, `ABCXAUTO_LIVE_CONFIRM=I_UNDERSTAND_LIVE_TRADING_RISK`, and a promoted playbook.

## Architecture

Priority: **risk > execution > monitoring > thin UI**.

```
abcxauto/
  __main__.py           Pro + supervisor; --cleanup = operator stop
  supervisor.py         Useful hours + TWS probe; relaunch on crash
  park_clock.py         Overnight / after-close park + book-event pulse. RTH has no sit clock
  agent_loop.py         Snap → Grok tools → clerk send
  brain.py              Tool loop (facts + send + self_tune + playbook)
  llm.py                Short system prompt; no prompt_extra
  order_examples.py     Sendable ticket shapes
  executor.py / send.py Validate → gate → IBKR
  risk_gates.py         Hard pre-trade gates + halt latch
  lab_playbook.py       Paper notebook; live follows a promote
  universe.py           Watchlist for scan seed; not a send sandbox
  self_tune.py          Floor-clamped knobs
  scorecard.py          Book return vs model cost
  prediction_odds.py    Polymarket implied probs
  path_math.py          Expectancy / Kelly facts (Grok still sizes)
  world_state.py        Wake facts
  trade_plan.py         Multi-plan open-risk book
  pro_desktop.py        Flet cockpit
  think_stream.py       Live Grok think/say
  config.py             Env + walk-away floor
  memory/               SQLite journal (clerk)
  broker/               IBKR
```

See [`SPEC.md`](SPEC.md) and [`docs/CYCLE.md`](docs/CYCLE.md).

## Tests

```powershell
$env:PYTHONPATH='.'; python -m pytest tests/ -q
```
