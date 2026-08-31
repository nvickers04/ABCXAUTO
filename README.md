# Asset Balancing Control X Auto (ABCXAUTO)

**Grok owns a paper IBKR book. Silent code is facts, hard gates, and the overnight park.**

Grok (the `model` knob, default grok-4.6) invents tickets and standing notes. Live is the **7496** socket after the confirm phrase and a different client id. Same constitution as paper **7497**. It never copies paper fills.

Same rules at $1k, $100k, or $1M. Size, daily-loss, and the scorecard are **% of NetLiq**. Book return % must beat the cost of the model.

## Split of labor

| Owner | Job |
|-------|-----|
| **Grok** | Tickets (`send`), risk/watchlist knobs (`self_tune`) |
| **Code** | Live facts, `ORDER EXAMPLES` schema, hard gates Grok cannot talk around, overnight / after-close park |
| **Operator** | `.env` + paper TWS, Start, kill switch, Settings knobs (brain, pacing, link). No approval step. |

Do not grow the system prompt. Strategy is Grok’s. Switch the brain from Pro Settings — `model` persists to `risk_settings.json`, which beats the `ABCXAUTO_MODEL` env form. Grok is the only RTH process. There is no clerk.

## Hard gates (code)

- `send` is the only broker path
- Defined-risk and cash-only
- Size vs `max_risk_per_trade_pct` of NetLiq; daily-loss halt; max position %; optional capacity `max_open_positions` (default 0 = off; a positive N is Grok's ceiling)
- One name across every lot vs `max_symbol_concentration_pct` — `max_position_pct` only sees one ticket, so N orders in a name could stack past it. Stock and its options sum
- One sector/theme arena across names vs `max_arena_concentration_pct` — per-name cannot see NVDA+SMCI+ARM+AVGO as one bet. Scan sorts are not the bucket. Fires on send even when paper gates are off
- Unprotected STK: last-stop at IBKR; hold is blocked until it exists. Combo close (`closing_position` on the matching multi-leg send) is one BAG, not new risk
- Ticket geometry uses **IBKR last**, not MDA
- Ticket last / IV / credit / width must be in this look's `quote` / `option_quote` / `book` cache
- Exits are never blocked; fail-closed if the book is unknown
- Agent may **tighten** floors via `self_tune`; it cannot weaken them or switch to live

Grok may retune knobs immediately. No proposal step.

## Look

One look stays open (chat kept). Call the model. If `tool_calls`: run tools,
call the model again with those results on the same chat. Repeat until there
are no `tool_calls`. If words only: stop calling the model until fill /
order_change / unprotected / operator poke. Do not call the model again
because it spoke. A poke does not start a new messages list.

```
WAKE     Overnight / after-close park until premarket. Paper RTH / premarket stay up.
         fill / order_change / unprotected can poke the open think.
    |
SNAP     IBKR book, orders, protection
    |
GROK     tools (facts + send). Wake is a short line — Grok fetches what it needs.
    |
SEND     send → gates → IBKR. Journal write is code, not a Grok tool.
    |
LOOK     Finished RTH look writes no grok_wake.json. Stay-up has no sit clock.
         Session cap idles; chat is kept. Overnight / park drop the chat.
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

Other: `odds` (Polymarket), `send`, `self_tune` (flat knobs; `send self_tune` still works).

Universe is a **watchlist** Grok can change via `self_tune`; `send` is not limited to it. Code still writes `journal.db`; there is no `journal` tool.

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

Console-only (paper): `$env:ABCXAUTO_FORCE_HEADLESS=1`. Do not launch if 7497 refuses. Do not enable live unless you typed the confirm phrase.

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

Walk-away ceilings (agent cannot raise or disable): **25%** daily-loss, **25%** max position, **25%** risk/trade, **25%** per name, defined-risk on, cash-only, `trading_budget_usd=0` (full NetLiq). Book width is Grok's; `max_open_positions` default **0** (off).

| Variable | Default | Purpose |
|----------|---------|---------|
| `ABCXAUTO_MODEL` | `grok-4.6` | Brain — Pro Settings `model` wins over this |
| `IBKR_PORT` | `7497` | Paper TWS |
| `IBKR_CLIENT_ID` | `42` | One id per process |
| `ABCXAUTO_MAX_OPEN_POSITIONS` | `0` | Slot cap (0 = off; Grok may set a positive ceiling) |
| `ABCXAUTO_DAILY_LOSS_LIMIT_PCT` | `25` | Daily-loss halt vs NetLiq |
| `ABCXAUTO_MAX_POSITION_PCT` | `25` | Max position vs NetLiq |
| `ABCXAUTO_MAX_RISK_PER_TRADE_PCT` | `25` | Max risk per ticket vs NetLiq |
| `ABCXAUTO_MAX_SYMBOL_CONCENTRATION_PCT` | `25` | Max one underlying, all lots, vs NetLiq |
| `ABCXAUTO_DEFINED_RISK_ONLY` | `true` | Locked on |
| `ABCXAUTO_JOURNAL_PATH` | `journal.db` | Clerk SQLite journal |
| `ABCXAUTO_DEFAULT_LOOK_S` | `90` (`60` open-book; `600` flat hunt) | Clerk look when a card has no `next_look_s` |

See `.env.template` for the rest. Live: `TRADING_MODE=live`, port **7496**, `ABCXAUTO_LIVE_CONFIRM=I_UNDERSTAND_LIVE_TRADING_RISK`. Same gates as paper.

## Architecture

Priority: **risk > execution > monitoring > thin UI**.

```
abcxauto/
  __main__.py           Pro + supervisor; --cleanup = operator stop
  supervisor.py         Useful hours + TWS probe; relaunch on crash
  park_clock.py         Overnight / after-close park; book-event pulse. No RTH sit clock
  agent_loop.py         Snap facts, send gates
  brain.py              Call the model; tool results stay on the same chat
  llm.py                Short system prompt; no prompt_extra
  order_examples.py     Sendable ticket shapes
  executor.py / send.py Validate → gate → IBKR
  risk_gates.py         Hard pre-trade gates + halt latch
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
  memory/               SQLite journal
  broker/               IBKR
```

See [`SPEC.md`](SPEC.md) and [`docs/LOOK.md`](docs/LOOK.md).

## Tests

```powershell
$env:PYTHONPATH='.'; python -m pytest tests/ -q
```
