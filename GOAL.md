# GOAL — ABCXAUTO Pro desktop usable + rocket situational awareness solid

## Objective
Make `python -m abcxauto` open a usable Flet Pro cockpit (not stuck on "Working…"), with Start/Stop/Panic and live situational awareness wired to the rocket loop.

## Done when
1. `python -m abcxauto` shows the Pro UI (Overview / Positions / AI Brain / Logs / Settings) within a few seconds — no permanent "Working…" spinner.
2. Start/Stop/Panic controls are visible and clickable on Overview.
3. Cycle updates populate equity, risk/protection, positions, last Grok decision from `run_cycle` payloads.
4. `python -m pytest -q` stays green (or only pre-existing skips).
5. Changes committed on `master` (ask before push if unsure).

## Progress
- [x] Switch entrypoint to `ft.run` (ca2e3fc)
- [x] Fix Flutter "Working..." hang: remove unbounded Column expand spacer; force window reveal; `assets_dir=None`
- [x] Situational awareness wired via enriched `run_cycle` payload
- [ ] User visual confirm of Start/Stop/Panic on live paper session

## Context
- Repo: `C:\Users\nvick\ABCXAUTO`
- Entry: `abcxauto/__main__.py` → `pro_desktop.run_app` (Tk fallback: `--tk`)
- Escape hatch: `ABCXAUTO_PRO_WEB=1` opens browser view instead of desktop client
