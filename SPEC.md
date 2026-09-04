# ABCXAUTO spec

Grok owns a paper IBKR book. Silent code is facts, hard gates, and the overnight park.
Brain is the `model` knob (default grok-4.6). Mainline is `master`. There is no clerk process.

Paper (TWS **7497**) is the book. Live (TWS **7496**, confirm phrase, a different
client id) is the same constitution on the live socket. It never copies paper fills.

Size, daily-loss, and the scorecard are **% of NetLiq**. Book return % must beat
the cost of the model. Same rules at $1k, $100k, or $1M.

## Split of labor

| Owner | Job |
|-------|-----|
| **Grok** | Tickets (`send`), knobs (`self_tune`) |
| **Code** | Live facts, `ORDER EXAMPLES`, hard gates, overnight / after-close park |
| **Operator** | `.env` + paper TWS, Start, kill switch, Settings knobs (brain, pacing, link). No strategy card. |

Do not grow the system prompt. Do not inject strategy menus or a Judge/Act form.
Switch the brain from Pro Settings — `model` persists to `risk_settings.json`,
which beats the `ABCXAUTO_MODEL` env form. Grok is the only RTH process.

## Hard gates (code)

- `send` is the only broker path. Premarket / after-hours / closed is
  **research mode**: stronger `model_research` when set, MDA/news/odds/web
  for an expectancy brief under `data/state/research_brief.json`, and
  `send` fail-closes (`research_no_send`). RTH is the thin defined-risk
  sender (`model_rth` or current `model`). RTH may use news/scan/web as
  COLOR; the brief remains prior-session color on wake. Missing/stale
  brief: RTH still runs. News/scan/web are never an automatic trigger.

- Defined-risk and cash-only
- Size vs `max_risk_per_trade_pct` of NetLiq; daily-loss halt; max position %;
  optional capacity `max_open_positions` (default 0 = off; a positive N is Grok's ceiling)
- One name across every lot vs `max_symbol_concentration_pct`. `max_position_pct`
  only sees the ticket in front of it, so N orders in a name could stack past it.
  Stock and its options sum — same underlying, one bet
- One sector/theme arena vs `max_arena_concentration_pct` of NL. Per-name cannot
  see NVDA+SMCI+ARM+AVGO as four names. Catalog arenas we already scan (industry /
  cap / ETF / commodity), not scan sorts. Send still fires when paper gates are off
- Unprotected STK: last-stop; hold blocked until it rests at IBKR. Combo close is one BAG (`closing_position`), not new risk
- New risk must name a play (`params.card` is a scorecard label, not a catalog)
- Ticket geometry uses **IBKR last**, not MDA
- Ticket last / IV / credit / width must appear in this look's `quote` /
  `option_quote` / `book` cache (`stale_or_invented_number`; unverifiable kills)
- Exits never blocked; fail-closed if the book is unknown
- Agent may tighten floors via `self_tune`; it cannot weaken them or switch to live
- Session look and token caps (Settings). Hit stays idle / park-ready; chat is
  kept; no sit clock. Grok may tighten via `self_tune`, not raise.

Walk-away ceilings: **25%** daily-loss, **25%** max position, **25%** risk/trade,
**25%** per name, defined-risk on, cash-only, full NetLiq (`trading_budget_usd=0`).
Paper operator may turn % floors off (`sizing_floors`); live cannot (forced ON); 25% walk-away is the live ceiling. `% of NL` review facts live on `day_facts` / `book` / wake / `compact_position`, not `brain.py`.

## Look

One look stays open (chat kept). Call the model. If `tool_calls`: run tools,
call the model again with those results on the same chat. Repeat until there
are no `tool_calls`. If words only: stop calling the model until fill /
order_change / unprotected / operator poke. Do not call the model again
because it spoke. A poke does not start a new messages list. Tool results
stay on the chat. A spoken line does not wipe it.

Overnight / after-close / park drop the chat. Paper RTH / premarket stay up
(no sit clock). Closed/postmarket does not call Grok (unprotected still does);
park_clock until premarket. Session cap idles; chat is kept. Durable notes
across days are gone.

See [`docs/LOOK.md`](docs/LOOK.md). Snap facts → Grok tools → `send` gates.

## Priority

1. **Risk** — hard gates, LLM-proof
2. **Execution** — `send` → executor
3. **Monitoring** — protection, halt, P&L truth
4. **Thin UI** — Flet Pro cockpit

## Tools

IBKR live: `book`, `status`, `quote`, `fills`, `option_chain`, `option_quote`.

MDA ~15m delayed: `scan`, `news`, `option_facts` (greeks). `candles` is IBKR hist or the live 5s stream (error if both miss).

Other: `odds` (Polymarket, not send geometry), `web` (public page, COLOR not a
live trigger), `send`, `self_tune` (flat knobs).

Universe is a watchlist Grok can change via `self_tune`; `send` is not limited
to it. Code writes `journal.db`; there is no `journal` tool.

## Runtime

`python -m abcxauto` — supervisor + Pro + think stream. Useful hours weekdays
**8:30–16:00 ET**. Probe TWS **7497** before launch. `--cleanup` marks operator
stop. Headless paper only (`ABCXAUTO_FORCE_HEADLESS=1`).

Two books = two processes, two client ids. Settings moves `ibkr_host` and
`ibkr_client_id` only while the IBKR link is down.

## Tech

Python 3.11 · ib_insync · Flet · xAI SDK · MarketData.app · pytest.
Memory: SQLite. No new packages without a profitability reason.
