# GOAL — ABCXAUTO Pro Desktop v0.2  
## Lean Self-Simplifying + Heavy Situational Testing (Full Dev Mode)

### Heart (every cycle)
1. **Reality Pulse** — time/session/countdown/ledger/freshness  
2. **Kahneman System 2** — base rates, pre-mortem, bias audit  
3. **Order Lab** — dry-validate ALL registered strategies (+ proposal/conId gates)  
4. **Safe execute** (conId guards; prefer_bracket_only when lab sick)  
5. **Auto-reconfig** from lab pass-rate + PnL (no FORCE TWEAK button)  
6. **Two simplification passes** — prune dead TWEAKS/logs; structure candidates (audited; no blind source deletion mid-trade)

### Modules
| File | Role |
|------|------|
| `abcxauto/reality_pulse.py` | Awareness JSON + clock |
| `abcxauto/kahneman.py` | System 2 scaffolding |
| `abcxauto/order_lab.py` | Heavy order-type suite + auto_reconfig |
| `abcxauto/simplify.py` | Two lean passes |
| `abcxauto/rocket.py` | Cycle orchestration |
| `abcxauto/pro_desktop.py` / `pro_engine.py` | Flet UI + worker |

### Run
```powershell
cd C:\Users\nvick\ABCXAUTO
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m abcxauto
```

### requirements.txt
`flet>=0.85.0` already present — no new deps.

### 4-step checklist
1. **Launch** `python -m abcxauto` → Market Clock + Overview shows Reality Pulse / Kahneman / Order Lab / Simplify panels.  
2. **START** ≥3 cycles → Logs show lab pass/fail, reconfig summary, simplify R1+R2.  
3. **Mixed STK+OPT** → `pytest tests/test_flatten_smoke.py tests/test_order_lab.py` — conId-independent closes + lab fixtures.  
4. **No FORCE TWEAK** in UI; improvements.log only auto-reconfig / reflections.

```powershell
python -m pytest tests/test_order_lab.py tests/test_reality_pulse.py tests/test_situational.py tests/test_flatten_smoke.py tests/test_pro_desktop.py -q
```
