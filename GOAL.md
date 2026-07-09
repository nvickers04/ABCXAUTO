# GOAL — ABCXAUTO Pro Desktop v0.1  
## Professional Self-Evolving IBKR Terminal with Hyper-Aware Position Management

### Objective
Single-command Flet desktop app (`python -m abcxauto`) that feels like a premium broker terminal while running an autonomous Grok portfolio manager that **never confuses instruments** (conId is the single source of truth for every close/flatten).

### Done when
1. App launches premium dark UI (Overview / Positions Ledger / AI Brain / Logs & Evolution / Settings).
2. Controls: **START AUTONOMOUS | PAUSE | PANIC FLATTEN | FORCE TWEAK | VALIDATE & EXECUTE**.
3. Every cycle injects full LIVE POSITION LEDGER + ORDER PROTOCOL into the agent prompt.
4. Logs & Evolution is filterable and auditable (ledger, reasoning with `Closing target = conId=…`, impact gate).
5. Smart PANIC FLATTEN closes **per conId** (STK vs OPT independently).
6. `python -m pytest -q` green.

### Architecture (module split — reuses ABCXAUTO)
| Module | Role |
|--------|------|
| `abcxauto/__main__.py` | Zero-flag entry: `python -m abcxauto` |
| `abcxauto/pro_desktop.py` | Flet Pro shell (premium UI) |
| `abcxauto/pro_engine.py` | Background rocket worker, pause/panic/force/validate |
| `abcxauto/rocket.py` | Snapshot → ledger → Grok → validate conId → execute → tweak |
| `abcxauto/broker/connector.py` | IBKR + `_flatten_one_position` / `flatten_all` |
| `abcxauto/executor.py` | conId-aware exit verification |
| `abcxauto/proposals.py` | Strategy schemas (`market_order` exit-only + conId) |

### Run commands
```powershell
cd C:\Users\nvick\ABCXAUTO
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# fill .env: XAI_API_KEY, MARKETDATA_TOKEN; start TWS paper API :7497
python -m abcxauto
```

Optional:
```powershell
python -m abcxauto --cleanup --aggressive   # kill stale Pro windows
python -m abcxauto --tk                     # legacy Tk cockpit
python -m pytest -q
python scripts/pro_headless_acceptance.py
```

### requirements.txt
Already includes `flet>=0.85.0` plus ib_insync, xai-sdk, pydantic, fastapi, etc. No extra packages required for Pro.

### 4-step test checklist (position awareness + order protocol)
1. **Launch** — `python -m abcxauto` (no flags) → dark Flet UI, conId columns, START / PAUSE / PANIC / FORCE TWEAK / VALIDATE & EXECUTE.
2. **START AUTONOMOUS** — ≥3 cycles; each cycle prompt/log contains `LIVE POSITION LEDGER` with `conId=…` rows; rationale names `Closing target = conId=…` before any close.
3. **Logs & Evolution** — filters All/Trades/Closes/Improvements/Errors/Position Mismatches; expandable cards show inventory, reasoning, Validate Order Impact / Replay / Apply Again; summary strip shows close-success + mismatches.
4. **Simulated PANIC on mixed SPY STK + OPT** — `tests/test_flatten_smoke.py` + connector `_flatten_one_position`: independent `position_results` per conId; stock MKT vs `close_option_position`; no cross-leg netting. UI panic writes before/after ledger into Logs.

Automated proofs:
```powershell
python -m pytest tests/test_situational.py tests/test_flatten_smoke.py tests/test_pro_desktop.py tests/test_pro_engine.py -q
```

### Progress
- [x] Flet Pro shell over ProEngine + Working… hang fixes
- [x] Full ORDER PROTOCOL embedded every cycle (`rocket.ORDER_PROTOCOL` / `RULES`)
- [x] LIVE POSITION LEDGER (conId, secType, OPT fields, exchange, uPnL…)
- [x] conId validation gate + simulate_close_impact + executor conId checks
- [x] PANIC per-leg flatten (STK vs OPT)
- [x] UI: Positions Ledger, PAUSE, FORCE TWEAK, VALIDATE & EXECUTE, proposal breakdown
- [x] Logs: close-success rate + mismatch counter
- [ ] User visual confirm on live paper with real mixed book
