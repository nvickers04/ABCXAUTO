# GOAL — ABCXAUTO Pro Desktop v0.1  
## Situational Awareness as the Literal Heart

### Objective
Premium one-command Flet terminal (`python -m abcxauto`) whose **core is a Reality Pulse** — a live situational-awareness snapshot injected at the start of every agent cycle. Automation flows from awareness; PnL is the truth signal.

### Reality Pulse (mandatory every cycle)
Built in `abcxauto/reality_pulse.py`, attached in `rocket.snap()` / `run_cycle()`:
- Time (UTC + America/New_York), day of week, date
- Session: Pre-market / Regular / Post-market / Closed + countdown
- Tradable-now flags + liquidity
- Data freshness (MDA SPY age, IBKR snapshot age, VIX if available)
- Full position ledger (conId, secType, OPT fields, qty, PnL)
- Account summary + awareness checklist

Agent system rules (`rocket.AWARENESS_HEART` + `ORDER_PROTOCOL`) force:
1. Open rationale with `Current reality: …`
2. Walk the checklist before any order
3. Close only by exact **conId**

### UI
- **Market Clock** in top nav (clock, session badge, countdown, data age)
- Overview Reality Pulse narrative strip
- Logs & Evolution: each cycle card shows Reality Pulse + ledger + reasoning
- START / PAUSE / PANIC / FORCE TWEAK / VALIDATE & EXECUTE

### Architecture
| Module | Role |
|--------|------|
| `abcxauto/reality_pulse.py` | Heart — build pulse + clock view |
| `abcxauto/rocket.py` | Pulse → Grok → validate → execute → tweak |
| `abcxauto/pro_engine.py` | Background worker + state |
| `abcxauto/pro_desktop.py` | Flet shell + Market Clock |
| `abcxauto/broker/connector.py` | Per-conId panic flatten |

### Run (zero flags)
```powershell
cd C:\Users\nvick\ABCXAUTO
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# .env: XAI_API_KEY; TWS paper :7497
python -m abcxauto
```

### requirements.txt
`flet>=0.85.0` already listed. No new packages.

### 4-step checklist (awareness heart)
1. **Launch** `python -m abcxauto` → Market Clock ticks; session badge + countdown visible.
2. **START AUTONOMOUS** ≥3 cycles → Logs show **Reality Pulse** narrative + `LIVE POSITION LEDGER` with conIds; rationale starts from current reality.
3. **Mixed book panic** (SPY STK + OPT) → PANIC or `pytest tests/test_flatten_smoke.py` → independent close per conId (never stock vs option swap).
4. **Logs & Evolution** → expandable pulse + impact gate + close-success / mismatch chips; self-tweaks reference context vs PnL.

```powershell
python -m pytest tests/test_reality_pulse.py tests/test_situational.py tests/test_flatten_smoke.py tests/test_pro_desktop.py -q
```

### Progress
- [x] Reality Pulse module + injection every cycle
- [x] Awareness checklist in system rules
- [x] Market Clock + Overview pulse strip
- [x] Logs pulse cards
- [x] conId order protocol + per-leg panic (prior)
- [ ] Live paper visual confirm with real mixed book
