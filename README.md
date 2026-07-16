# Asset Balancing Control X Auto (ABCXAUTO)

**Autonomous Grok-powered agentic portfolio for IBKR (paper-first).**

Grok 4.5 **owns** a paper IBKR portfolio under hard risk rules. Protect first;
hold is valid when the book is protected. See `SPEC.md` for doctrine.

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

## 2. Agent order surface

The agent sees **ORDER EXAMPLES** (`abcxauto.order_examples`) and can send each
allowlisted type through the shell (`send` → executor → broker). Entries require
stop + target; bare stock orders are exit-only.

## 3. Portfolio ownership

- Owns the book: entries, exits, modify/cancel, hold when protected.
- **Tools only** — no symbol allowlist; capital/daily-loss gates default off (opt-in via env).
- Halt latch still blocks new entries on Panic / broker disconnect.
- Hold is valid when protected; unprotected STK must be fixed first.

## 4. Run

```powershell
python -m abcxauto
```

Launches **Pro** (Flet). Press **Start**.  
Controls: Connect IBKR · Start agent · Close All Positions.

```powershell
python -m abcxauto --cleanup --aggressive   # stale Flet/Python cleanup
python -m abcxauto --headless               # exits 0; autonomy via Pro START
```

## 5. Supported orders

Matches `ORDER_EXAMPLES` / sendable types:

| Type | Role |
|------|------|
| `hold` | No-op when protected |
| `bracket` / `market_bracket` | Entry with stop + target |
| `oca` | Attach stop + target to an open position |
| `modify_stop` / `modify_target` | Adjust working protection |
| `cancel_order` | Cancel by order id |
| `market_order` / `limit_order` / `stop_order` / `stop_limit` | Exit only (`closing_position`) |
| `close_option` | Close an option position |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ABCXAUTO_CYCLE_SLEEP_S` | `120` | Idle sleep between autonomous cycles |
| `ABCXAUTO_GROK_MIN_INTERVAL_S` | `120` | Min seconds between Grok calls when flat/protected |
| `ABCXAUTO_MONITOR_POLL_S` | `30` | Snapshot refresh |
| `ABCXAUTO_MONITOR_REVIEW_S` | `300` | Grok portfolio review interval |
| `ABCXAUTO_MODEL` | `grok-4.5` | xAI model |
| `ABCXAUTO_RISK_GATES_ENABLED` | `true` | Pre-trade path (halt latch); capital rules opt-in |
| `ABCXAUTO_DAILY_LOSS_LIMIT_PCT` | `0` | Daily loss circuit breaker (% NetLiq; 0=off) |
| `ABCXAUTO_MAX_POSITION_PCT` | `0` | Max entry notional (% NetLiq; 0=off) |
| `ABCXAUTO_MAX_OPEN_POSITIONS` | `0` | Max simultaneous positions (0=off) |
| `ABCXAUTO_MAX_DAILY_TRADES` | `0` | Max entries per day (0=off) |
| `ABCXAUTO_AUTO_PANIC_ON_BREACH` | `false` | Auto halt + flatten on daily-loss breach |
| `ABCXAUTO_JOURNAL_ENABLED` | `true` | SQLite trade journal |
| `ABCXAUTO_JOURNAL_PATH` | `journal.db` | Journal path |
| `ABCXAUTO_RISK_SETTINGS_PATH` | `risk_settings.json` | Persisted Risk-tab knobs (gitignored) |
| `ABCXAUTO_RISK_POSTURE` | _(empty)_ | `defensive` / `balanced` / `aggressive` (or set in Risk tab) |

Pro **Risk** tab: one operator knob — **risk posture**. Apply seeds capital gates and a wide envelope (persists to `risk_settings.json`). The agent sizes risk **per trade** and may `set_risk` inside that envelope; it cannot change posture. Cycles also get an ideas-only **opportunity scan** (MDA) in the prompt — never auto-trades.

## Architecture

Thin product shell over broker / risk / executor. Target ~3.5–5k LOC.

```
abcxauto/
  __main__.py        Pro UI default; --cleanup; --headless → Pro START message
  order_examples.py  ORDER EXAMPLES catalog (agent contract)
  connections.py     IBKR + optional MDA façade
  send.py            dispatch façade → executor
  book.py            portfolio / book state façade
  risk.py            risk-gate façade
  agent_loop.py      autonomous cycle engine (snap → Grok JSON → send)
  cycle.py           thin shim re-exporting agent_loop for Pro/tests
  proposals.py       OrderProposal schemas + validation
  risk_gates.py      hard pre-trade gates + halt latch
  executor.py        validate → gate → IBKR dispatch
  monitor.py         P&L / protection / auto-panic
  llm.py             xAI client
  tools.py           read-only tools (snap helpers; legacy agent path)
  memory/            SQLite journal
  config.py          flat env config
  broker/            IBKR layer
  marketdata/        MarketData.app + market hours
  pro_desktop.py     Flet Pro cockpit (thin operator UI)
```

Priority: **risk > execution > monitoring > thin UI**.
