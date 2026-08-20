# ABCXAUTO Web Pro

X Lights Out **operator shell** for ABCXAUTO — browser companion to the Flet Pro desktop (`abcxauto/pro_desktop.py`).

## What this is

- Layout / UX redesign of the Pro cockpit (Dashboard, Positions, Controls, Universe, Risk, Scorecard, Suite)
- **Paper simulation** of connect / START / cycles — does **not** replace `ProEngine`, IBKR, or risk gates
- Safe to merge **layout patterns** back into Flet; do not treat this as live trading

## Doctrine (unchanged)

Control + Unbiased. Priority: **risk > execution > monitoring > thin UI**.

Universe language: **hunt pool** (fence), not “legal set.” Sync = rebuild membership from IBKR scanners (seeds offline).

## Run

```bash
cd web-pro
npm install
npm run dev
```

Opens on `http://localhost:8080`.

## Relation to desktop

| Path | Role |
|------|------|
| `python -m abcxauto` | Live Pro (Flet) + agent loop |
| `web-pro/` | Browser UI prototype / layout reference |

Push of 2026-07-28: Universe revamp, denser dashboard, quieter left-rail actions.
