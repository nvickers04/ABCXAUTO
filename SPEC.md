# ABCXAUTO — Consolidated Spec (Monorepo Vision)

**One system**: an agentic Grok 4.5-powered IBKR portfolio agent that researches,
proposes, risk-validates, and auto-executes — with hard-coded capital protection that
the LLM cannot talk its way around. Consolidates `nvickers04/ABCXAUTO` (main),
`ABC/ABCX` (IBKR execution layer, MarketData.app client, xAI SDK integration),
`thesis` (strategy / thesis generation), and `GrokSimpleExecutionTrader` (minimal
execution loop) into this repo.

**Reality posture**: this is an **agentic portfolio** with a thin product shell
(`order_examples` → `connections` / `book` / `send` / `risk` → broker + gates),
not a playbook or scanner brain. The agent **owns** a paper IBKR book under hard
risk rules and can send every type in ORDER EXAMPLES. Protect first; act when edge
or risk requires. **Hold is valid** when the book is protected and within mandate;
hold is **forbidden only while unprotected STK** exists (code enforces). Risk gates,
brackets, and sizing are the floor so a wrong call cannot blow up the book; they may
block a bad proposal but they are not a strategy. Intelligence + journal memory drive
judgment. Paper (TWS 7497) until forward P&L shows the book survives and compounds.
Target footprint **~3.5–5k LOC**. Every feature must answer: *does this help the agent
own the book under constraints without raising blow-up risk or burning idle API?*
If not, it doesn't ship.

---

## Priority order (immutable)

1. **Risk** — hard gates, LLM-proof
2. **Execution** — correct order placement through `send` / executor
3. **Monitoring** — protection audit, auto-panic, P&L truth
4. **Thin UI** — Flet Pro cockpit only (operator surface; not the brain)

## Non-negotiable risk invariants (enforced in code, not prompts)

| Invariant | Enforcement point |
|---|---|
| Every stock entry is a bracket (SL + TP) | `proposals.py` schema + `executor.py` |
| Bare orders only to close, verified against live positions | `executor._verify_closes_position` |
| Daily loss circuit breaker → halt latch + auto flatten | `risk_gates.py` + `monitor.py` |
| Max position size (% of NetLiq) | `risk_gates.py` pre-trade check |
| Max open positions / max daily trades | `risk_gates.py` pre-trade check |
| Fail closed: no account data → no new entries | `risk_gates.py` |
| Exits are never blocked, even while halted | `risk_gates.py` |
| A working stop is never cancelled to "clean up" a failed target | `broker/orders.py` |
| Options entries: defined-risk only by default | `proposals.py` (Sprint 2) |
| Hold forbidden only while unprotected STK | cycle / protection audit |

All knobs env-driven (`ABCXAUTO_DAILY_LOSS_LIMIT_PCT`, `ABCXAUTO_MAX_POSITION_PCT`,
`ABCXAUTO_MAX_OPEN_POSITIONS`, `ABCXAUTO_MAX_DAILY_TRADES`, `ABCXAUTO_AUTO_PANIC_ON_BREACH`).
Conservative defaults; aggression is opt-in via `.env`, never the default.

## Architecture (target)

Thin shell (~3.5–5k LOC). Priority: **risk > execution > monitoring > thin UI**.

```
abcxauto/
  __main__.py        Pro UI default; --cleanup; --headless → Pro START message
  order_examples.py  ORDER EXAMPLES — agent sendable-type contract
  connections.py     IBKR + optional MDA façade
  send.py            dispatch façade → executor
  book.py            portfolio / book state façade
  risk.py            risk-gate façade
  agent_loop.py      autonomous cycle engine (snap → Grok JSON → send)
  cycle.py           thin shim re-exporting agent_loop for Pro/tests
  config.py          flat env config — every knob lives here
  llm.py             xAI client (ABCXAUTO_MODEL=grok-4.5)
  proposals.py       OrderProposal schemas + validation (risk layer 1)
  risk_gates.py      hard pre-trade gates + halt latch (risk layer 2)
  executor.py        single choke point: validate → gate → dispatch (risk layer 3)
  monitor.py         P&L/protection poll, Grok review, auto-panic
  memory/            durable state: trades, cycles, halts (SQLite)
  broker/            ib_insync layer (connector, orders, connection)
  marketdata/        MarketData.app client + market hours
  pro_desktop.py     Flet Pro cockpit (thin operator UI)
tests/               pytest; risk gates and executor paths must stay green
scripts/             cleanup_pro, pro_gui_contract_check
```

**Tech stack**: Python 3.11 · ib_insync · Flet (sole UI) ·
xAI SDK (Grok 4.5) · MarketData.app · pytest. Memory: SQLite (stdlib, zero new deps)
now, Postgres when multi-session analytics justify it. **No new packages without a
profitability justification.**

**Sendable types (ORDER EXAMPLES)**: `hold`, `bracket`, `market_bracket`, `oca`,
`modify_stop`, `modify_target`, `cancel_order`, bare exits with `closing_position`
(`market_order` / `limit_order` / `stop_order` / `stop_limit`), and `close_option`.

## One runtime, one risk core

- **Pro path** (`python -m abcxauto`): default UI; **START AUTONOMOUS** runs
  `agent_loop` (via `cycle` shim) on `ABCXAUTO_CYCLE_SLEEP_S`. Headless CLI
  (`--headless`) exits 0 and points operators to Pro START — autonomy is Pro-driven.
  Monitor on the worker loop (auto-panic, snapshots, fills, peak equity). UI is
  subordinate to portfolio state and journal memory.

Every *trading* order funnels through `send` → `executor.safe_execute`.

## Sprint plan

**Sprint 1 — Ironclad risk (DONE 2026-07-09)**
- `risk_gates.py`: halt latch, daily-loss circuit breaker, position sizing, max
  positions, max daily trades, fail-closed; exits always bypass.
- Executor integration at the single choke point; monitor auto-panic (halt + flatten once
  per breach); fix `place_oca` naked-position bug; env knobs + tests.
- Upgrade brain to Grok 4.5 (`ABCXAUTO_MODEL`).

**Sprint 2 — Truth & memory (DONE 2026-07-09)**
- Options risk parity: defined-risk-only default, `close_option` live-position check,
  option protection audit in monitor.
- Gate `cancel_order` when it would strip the last stop from a position.
- Salvage from `thesis` repo (audit verdict: only its hard-gate ideas; GSET is empty,
  ABC's platform is weaker than what's here): cash-only sizing off `TotalCashValue`
  (config `ABCXAUTO_CASH_ONLY`), peak-drawdown gate, option premium exposure cap,
  ATR/risk-per-trade size cap, min reward:risk validator on brackets — all into
  `risk_gates.py` / `proposals.py`. Do NOT port its mechanical theses, lunar timing,
  bare-market stock executor, or ABC's signal zoo / Postgres research host.
- `memory/`: SQLite trade journal — every proposal, gate decision, fill, exit, halt, and
  cycle P&L. This is the dataset that proves or kills the edge.

**Sprint 3 — Prove the edge (engine + scorecard landed 2026-07-09; evidence pending)**
- Historical backtest harness removed from the live monorepo (LOC reduction); forward-test
  scorecard in Pro UI remains the live evidence path.
- Forward-test scorecard in Pro UI: paper P&L vs SPY benchmark, per-strategy attribution.
  [Scorecard tab DONE — daily counters, equity sparkline, recent dispatches; SPY
  benchmark + per-strategy attribution still open]
- Remaining: accumulate forward-test days; wire kill criteria into live strategy config.

**Backtest evidence log (2021-01-01 → 2026-07-01 daily, auto-sized 1% risk /
10% max position, 95% sizing headroom after bias fix):**
- `sma_pullback` — APPROVED for paper, SPY/QQQ ONLY. SPY +0.322 E_R (71 trades),
  QQQ +0.169 (59), positive through 2022 bear + 2024-25 chop; SPY parameter surface
  uniformly positive (18/18 cells — plateau, not a spike; expect out-of-sample
  regression from the best cell). Breadth test FAILED elsewhere: DIA +0.055 marginal,
  XLK/XLF/XLE/GLD all negative with n>=50 (kill candidates). Edge is index-specific.
- `breakout` — not enabled. After the sizing-bias fix: SPY +0.063 (198), QQQ +0.054
  (185, earlier kill flag withdrawn as bias artifact), IWM −0.032 (118). Roughly
  zero-edge after costs everywhere; nothing argues for enabling it.
- `mean_reversion` — watch-only. SPY +0.142 (33), QQQ +0.752 (34) but samples too
  thin to promote; IWM killed (−0.325, 21).
- Methodology note: original auto-sizing rejected gap-up fills (sizing vs starting
  cash at signal close vs gate at fill open); fixed via 95% sizing headroom. Verdicts
  above are post-fix.
- Kill criteria: any strategy with negative expectancy after N trades is disabled in code.

**Sprint 4 — Live readiness (partial DONE 2026-07-09; live still gated on forward evidence)**
- Live port + `ABCXAUTO_LIVE_CONFIRM` guard; disconnect reconnect + halt (kind=`disconnect`,
  persists until manual resume); bracket emergency flatten; rotating `logs/app.log`.
- Final re-audit NO-GO items closed: Pro monitor wired; suite dry-run default; decision_space
  dry-run; kind-aware halt reset; market_bracket conservative sizing / `price_hint` R:R;
  protection orders require an open position; connector client id from config.

**Paper readiness (2026-07-09):** **CONDITIONAL GO** for supervised paper on SPY/QQQ —
operator present, Panic known, suite dry-run. Not unsupervised overnight
until ≥ several clean paper sessions. Live money still blocked by SPEC exit criteria.

## Definition of "profitable" (exit criteria for paper)

- ≥ 60 trading days forward paper record, positive net P&L after modeled commissions.
- Max drawdown within the daily/total loss limits with zero risk-gate violations.
- Positive expectancy in forward test for every enabled strategy.
