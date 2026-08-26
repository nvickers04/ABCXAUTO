# ABCXAUTO spec

Grok owns a paper IBKR book. The clerk is facts, hard gates, and the overnight park.
Brain is the `model` knob (default grok-4.6). Mainline is `master`.

Paper (TWS **7497**) is the lab. Live (TWS **7496**, confirm phrase, a different
client id) only follows a **promoted paper playbook**. It never copies paper fills.

Size, daily-loss, and the scorecard are **% of NetLiq**. Book return % must beat
the cost of the model. Same rules at $1k, $100k, or $1M.

## Split of labor

| Owner | Job |
|-------|-----|
| **Grok** | Tickets (`send`), knobs (`self_tune`), lab notebook (`write_lab_playbook`) |
| **Clerk** | Live facts, `ORDER EXAMPLES`, hard gates, overnight / after-close park |
| **Operator** | `.env` + paper TWS, Start, kill switch, Settings knobs (brain, pacing, link). No strategy card. |

Do not grow the system prompt. Do not inject strategy menus or a Judge/Act form.
Switch the brain from Pro Settings — `model` persists to `risk_settings.json`,
which beats the `ABCXAUTO_MODEL` env form. Keep the clerk.

## Hard gates (code)

- `send` is the only broker path
- Defined-risk and cash-only
- Size vs `max_risk_per_trade_pct` of NetLiq; daily-loss halt; max position %;
  capacity `max_open_positions` (default 15)
- One name across every lot vs `max_symbol_concentration_pct`. `max_position_pct`
  only sees the ticket in front of it, so N orders in a name could stack past it.
  Stock and its options sum — same underlying, one bet
- Unprotected STK: last-stop; hold blocked until it rests at IBKR. Combo close is one BAG (`closing_position`), not new risk
- Live new risk needs a promoted playbook
- New risk must name an existing lab playbook card (scorecard label; prose is not law)
- Ticket geometry uses **IBKR last**, not MDA
- Exits never blocked; fail-closed if the book is unknown
- Agent may tighten floors via `self_tune`; it cannot weaken them or switch to live

Walk-away ceilings: **25%** daily-loss, **25%** max position, **25%** risk/trade,
**25%** per name, defined-risk on, cash-only, full NetLiq (`trading_budget_usd=0`).
Paper operator may turn % floors off (`sizing_floors`); live cannot (forced ON); 25% walk-away is the live ceiling. `% of NL` review facts live on clerk surfaces (`day_facts` / `book` / wake / `compact_position`), not `brain.py`.

## Loop

See [`docs/CYCLE.md`](docs/CYCLE.md). Short form: snap → Grok tools →
clerk `send`. Paper RTH / premarket stay up (no sit clock). Closed/postmarket
does not call Grok (unprotected still does); park_clock until premarket.

## Priority

1. **Risk** — hard gates, LLM-proof
2. **Execution** — `send` → executor
3. **Monitoring** — protection, halt, P&L truth
4. **Thin UI** — Flet Pro cockpit

## Tools

IBKR live: `book`, `status`, `quote`, `fills`, `option_chain`, `option_quote`.

MDA ~15m delayed: `scan`, `news`, `option_facts` (greeks). `candles` is IBKR hist or the live 5s stream (error if both miss).

Other: `odds` (Polymarket, not send geometry), `playbook`, `write_lab_playbook`,
`send`, `self_tune` (flat knobs).

Universe is a watchlist Grok can change via `self_tune`; `send` is not limited
to it. Clerk writes `journal.db`; there is no `journal` tool.

## Runtime

`python -m abcxauto` — supervisor + Pro + think stream. Useful hours weekdays
**8:30–16:00 ET**. Probe TWS **7497** before launch. `--cleanup` marks operator
stop. Headless paper only (`ABCXAUTO_FORCE_HEADLESS=1`).

Two books = two processes, two client ids. Settings moves `ibkr_host` and
`ibkr_client_id` only while the IBKR link is down.

## Tech

Python 3.11 · ib_insync · Flet · xAI SDK · MarketData.app · pytest.
Memory: SQLite. No new packages without a profitability reason.
