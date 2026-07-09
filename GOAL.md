# GOAL — ABCXAUTO Pro Desktop v0.3  
## Simple Constant Test → Fix → Re-test → Reconfigure Loop

### Heart (every cycle)
1. **Reality Pulse** — time/session/freshness/conId ledger  
2. **Order lab (test)** — all registered strategies dry-validated  
3. **Fix/simplify** — two audited lean passes  
4. **Re-test** — lab again immediately after fix (`retest` payload)  
5. **Safe execute** + **auto-reconfig** from post-fix lab + PnL  

### UI
- Market Clock chrome  
- Sidebar: **Overview | Positions | Logs & Evolution**  
- Logs show lab, simplify, re-test, reconfig (no FORCE TWEAK)

### Run
```powershell
cd C:\Users\nvick\ABCXAUTO
.\.venv\Scripts\Activate.ps1
python -m abcxauto
```

### requirements.txt
`flet>=0.85.0` already present.

### 3-step checklist
1. Launch `python -m abcxauto` → title Pro v0.3, Market Clock, 3-nav shell.  
2. START ≥2 cycles → Logs show order lab + simplify + **re-test after fix** + auto-reconfig.  
3. conId / panic: `pytest tests/test_flatten_smoke.py tests/test_order_lab.py tests/test_rocket.py -q`
