# GOAL — ABCXAUTO Pro Desktop v0.4  
## Brutal testing of ALL registered IBKR order types (paper)

### Heart
Every **startup** and **cycle**:
1. Reality Pulse  
2. **Brutal suite** — schema + place→validate→cancel (or dry-run) for every strategy in `proposals.STRATEGIES` + panic flatten legs  
3. Fix/simplify → re-test suite  
4. Safe execute → auto-reconfig  

Never idle: `idle_prevented=True` on every suite report.

### UI
- Market Clock  
- **Overview | Positions | Test Suite Results | Logs & Evolution**  
- Test Suite table: Strategy / Pass / Mode / Detail  

### Run
```powershell
cd C:\Users\nvick\ABCXAUTO
.\.venv\Scripts\Activate.ps1
python -m abcxauto
```

### requirements.txt
No new packages.

### Checklist
1. Launch → suite runs on startup (Logs: STARTUP BRUTAL SUITE)  
2. Test Suite Results page fills pass/fail for market/limit/stop/bracket/OCA/trailing/multi-leg  
3. `pytest tests/test_brutal_suite.py tests/test_rocket.py tests/test_flatten_smoke.py -q`
