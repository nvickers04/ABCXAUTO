# Asset Balancing Control X Auto (ABCXAUTO)

**Autonomous Grok-powered agentic portfolio for IBKR (paper-first).**

Grok 4.5 **owns** a paper IBKR portfolio under hard risk rules. Protect first;
hold is valid when the book is protected. The shell stays **objective** (facts,
gates, labeled heuristics). Optional human beliefs go only in `operator_card.txt`
or `ABCXAUTO_OPERATOR_CARD`. See `SPEC.md` for doctrine.

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

Each cycle is **Perceive → Judge → Act**:

| Stage | Owner | Role |
|-------|--------|------|
| Perceive | Code | Book, orders, MARKET FEATURES (heuristic≠recommendation), news, open risk |
| Judge | Grok | Stance `protect` / `manage` / `hunt` / `idle` + thesis + intent |
| Act | Grok | One allowlisted action; Grok owns structure (stops/targets/overlays) |
| Grade | Code | Geometry / share-lot / risk gates — accept or reject with reason codes |

Stop agent = pause decisions only. Positions stay at IBKR. Open risk is
reconciled from the broker book across Stop/Start (`active_trade_plan.json`).

## 3. Operator surfaces

- **Pro Dashboard → Book | Agent** — Agent combines Now (open risk + latest PJA) and Activity.
- **Risk tab** — posture envelopes; Save risk vs Apply posture are separate.
- **Test Suite** — paper place/cancel gym for order mechanics (not live curriculum trading).
- **Operator Card** (optional) — your beliefs only; empty by default:

```text
# operator_card.txt  (gitignored)  or  ABCXAUTO_OPERATOR_CARD=...
```

## 4. Run

```powershell
python -m abcxauto
```

Launches **Pro** (Flet). Connect IBKR · Start agent · Close All Positions.

```powershell
python -m abcxauto --cleanup --aggressive   # stale Flet/Python cleanup
python -m abcxauto --headless               # exits 0; autonomy via Pro START
```

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
| `set_risk` | Retune capital knobs inside posture envelope (no broker send) |

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ABCXAUTO_CYCLE_SLEEP_S` | `120` | Idle sleep between autonomous cycles |
| `ABCXAUTO_GROK_MIN_INTERVAL_S` | `120` | Min seconds between Grok calls when flat/protected |
| `ABCXAUTO_MONITOR_POLL_S` | `30` | Snapshot refresh |
| `ABCXAUTO_MODEL` | `grok-4.5` | xAI model |
| `ABCXAUTO_RISK_POSTURE` | _(empty)_ | `defensive` / `balanced` / `aggressive` |
| `ABCXAUTO_OPERATOR_CARD` | _(empty)_ | Human beliefs injected into Judge/Act |
| `ABCXAUTO_OPERATOR_CARD_PATH` | `operator_card.txt` | File fallback for Operator Card |
| `ABCXAUTO_JOURNAL_PATH` | `journal.db` | SQLite journal |
| `ABCXAUTO_RISK_SETTINGS_PATH` | `risk_settings.json` | Persisted Risk-tab knobs (gitignored) |

Capital / daily-loss gates default off until you Apply posture or set env knobs.
See `.env.template` for the full list.

## Architecture

Thin product shell. Priority: **risk > execution > monitoring > thin UI**.

```
abcxauto/
  agent_loop.py         Perceive → Judge → Act
  world_state.py        Code truth for prompts
  opportunity_scan.py   MARKET FEATURES (heuristic ranking)
  trade_playbook.py     Preconditions + shell rejects (no style dogma)
  trade_plan.py         Open-risk continuity across Stop/Start
  objective_language.py Banned taste phrases + taxonomy helpers
  structure_grade.py    Geometry / scrape lessons
  order_examples.py     How to send (param shapes)
  risk_gates.py         Hard pre-trade gates + halt latch
  executor.py / send.py Validate → gate → IBKR
  pro_desktop.py        Flet Pro cockpit
  config.py             Env + Operator Card + posture envelopes
  memory/               SQLite journal
  broker/               IBKR layer
```

## Objectivity rule

| Bucket | Where it lives |
|--------|----------------|
| Fact / Gate | Code + prompts |
| Heuristic | Labeled `heuristic ≠ recommendation` |
| Taste | Operator Card or Grok — **not** hard-coded shell prose |

## Tests

```powershell
$env:PYTHONPATH='.'; python -m pytest tests/ -q
```
