# ABCXAUTO — Autonomous Grok Agent for IBKR Trading

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

Web escape hatch (browser instead of desktop client):

```powershell
$env:ABCXAUTO_PRO_WEB=1; python -m abcxauto
```

Monitoring dashboard (full tool-calling agent + live P&L/positions feed):

```powershell
python -m abcxauto.web
# open http://127.0.0.1:8000
```

Dev utilities:

```powershell
python scripts/smoke_connect.py   # IBKR + MarketData smoke
python scripts/dev_verify.py      # AgentSession auto-exec against fake gateway
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
  __main__.py      Pro desktop entry (python -m abcxauto)
  cleanup.py       stale Flet/process cleanup (--cleanup)
  web.py           monitoring dashboard (python -m abcxauto.web)
  static/          read-only dashboard client
  agent.py         full tool-calling AgentSession (used by web)
  rocket.py        JSON cycle engine (used by Pro UI)
  monitor.py       background P&L/protection monitor
  llm.py           xAI AsyncClient wrapper
  tools.py         read-only tool schemas + handlers for Grok
  proposals.py     OrderProposal schemas, validation, ticket rendering
  executor.py      validated proposal -> IBKR gateway method
  config.py        flat env config
  aio.py           shared async helpers
  ui/              Flet Pro cockpit (theme, terminal, app entry)
  broker/          IBKR layer (connector + order/options/query mixins)
  marketdata/      MarketData.app (transport, quotes, options, research)
scripts/
  smoke_connect.py live IBKR + quote smoke
  dev_verify.py    AgentSession e2e with fake connector
tests/             proposals, executor, monitor, rocket, Pro UI
```

Two runtimes, one risk policy:
- **Pro desktop** drives `rocket.run_cycle` for situational awareness and
  autonomous JSON actions.
- **Web dashboard** drives `agent.AgentSession` for full native tool calling.

Salvaged from ABCX: the IBKR execution layer, MarketData.app client, and xAI SDK
integration. The human chat portal, confirm/cancel flow, and operator REPL were
deliberately removed.
