# Asset Balancing Control X Auto (ABCX Auto)

**Autonomous Grok-powered agent for IBKR trading and portfolio management.**

Forked from ABCX. Grok researches with read-only tools (quotes, candles, option
chains, greeks, news, positions) and **auto-executes** order proposals — brackets,
OCA pairs, trailing stops, verticals, iron condors, collars, multi-leg. No human
confirmation step.

## Risk policy

- **Every stock position must carry a stop loss and a take profit.** New entries
  must be `bracket` (limit entry) or `market_bracket` (market entry) — both place
  an OCA stop + target pair sized to the actual fill. Bare limit/market/stop
  orders are rejected at validation unless they close an existing position
  (`closing_position=true`), and the executor re-verifies that against live
  positions before dispatch.
- **Grok manages all orders autonomously.** Entries, exits, and order-management
  actions (`modify_order`, `modify_stop`, `modify_target`, `cancel_order`, `oca`,
  trailing stops) all auto-execute. If Grok cancels a protective stop, the monitor
  flags the position as unprotected within its next poll and nudges Grok to replace
  protection immediately.
- **Background P&L monitor.** A monitor loop snapshots positions, account P&L,
  open orders, and a protection audit every `ABCXAUTO_MONITOR_POLL_S` (30s). During
  market hours it asks Grok to review the portfolio every
  `ABCXAUTO_MONITOR_REVIEW_S` (5 min); if any position is ever unprotected it nudges
  Grok immediately to propose protection.
- **Opportunity scans.** Every `ABCXAUTO_SCAN_INTERVAL_S` (15 min) during market
  hours, Grok scans for new trades — conservative by default.

## Setup

```powershell
cd C:\Users\nvick\ABCXAUTO
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.template .env   # then fill in XAI_API_KEY and MARKETDATA_TOKEN
```

Start TWS (paper) with API enabled on port 7497.

## Run

Pro desktop (Flet situational-awareness cockpit):

```powershell
python -m abcxauto
```

If an old Pro window keeps coming back, clean stale Flet/Python processes first:

```powershell
python -m abcxauto --cleanup --aggressive
# optional deep clean of the Flet desktop client cache:
python -m abcxauto --cleanup --aggressive --flet-cache
```

Legacy Tkinter cockpit:

```powershell
python -m abcxauto --tk
```

Monitoring dashboard (agent + live P&L/positions feed):

```powershell
python -m abcxauto.web
# open http://127.0.0.1:8000
```

## Configuration

Set your trading objective via `ABCXAUTO_TRADING_MANDATE` in `.env`, or use the
default conservative mandate. Other knobs:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ABCXAUTO_SCAN_ENABLED` | `true` | Periodic opportunity scans |
| `ABCXAUTO_SCAN_INTERVAL_S` | `900` | Scan interval (seconds) |
| `ABCXAUTO_MONITOR_POLL_S` | `30` | Snapshot refresh |
| `ABCXAUTO_MONITOR_REVIEW_S` | `300` | Grok portfolio review interval |

## Architecture

```
abcxauto/
  __main__.py      autonomous agent entry (python -m abcxauto)
  web.py           monitoring dashboard (python -m abcxauto.web)
  static/          read-only dashboard client
  agent.py         conversation loop, tool dispatch, auto-execution
  monitor.py       background P&L/protection monitor + Grok review injections
  llm.py           xAI AsyncClient wrapper
  tools.py         read-only tool schemas + handlers for Grok
  proposals.py     OrderProposal schemas, validation, ticket rendering
  executor.py      validated proposal -> IBKR gateway method
  config.py        flat env config
  broker/          IBKR layer (ib_insync)
  marketdata/      MarketData.app client + provider + market hours
```

Salvaged from ABCX: the IBKR execution layer, MarketData.app client, and xAI SDK
integration. The human chat portal, confirm/cancel flow, and operator REPL were
deliberately removed.