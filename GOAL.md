# GOAL — ABCXAUTO Pro desktop usable + rocket situational awareness solid

## Objective
Make `python -m abcxauto` open a usable Flet Pro cockpit (not stuck on "Working…"), with Start/Stop/Panic and live situational awareness wired to the rocket loop.

## Done when
1. `python -m abcxauto` shows the Pro UI (Overview / Positions / AI Brain / Logs / Settings) within a few seconds — no permanent "Working…" spinner.
2. Start/Stop/Panic controls are visible and clickable on Overview.
3. Cycle updates populate equity, risk/protection, positions, last Grok decision from `run_cycle` payloads.
4. `python -m pytest -q` stays green (or only pre-existing skips).
5. Changes committed on `master` (ask before push if unsure).

## Context
- Repo: `C:\Users\nvick\ABCXAUTO`
- Entry: `abcxauto/__main__.py` → `pro_desktop.run_app` (Tk fallback: `--tk`)
- Flet 0.85 is installed; prefer `ft.run` over deprecated `ft.app`
- Rocket loop: `abcxauto/rocket.py` (`run_cycle` already returns positions/protection/action/rationale)
- Live paper book has an underwater SPY 1DTE call; invalid Grok strategies must stay coerced to hold

## Constraints
- Do not invent order strategies outside rocket `ALLOWED_ACTIONS`
- Do not commit `rocket.log` / `improvements.log` / `.env`
- Prefer small verified fixes over large rewrites
