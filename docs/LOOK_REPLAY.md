# Look replay (Record Auditor)

Operator reconstructs a paper look from code-written facts.
Grok is not asked to remember. The pack is not a brain tool.

## Why

Overnight / after-close / park drop the chat. Session cap idles and keeps
the chat, but the next calendar day does not. Human desk traders lose the
same tape: what was on the quote, what Grok sent, what the gate said, what
IBKR filled, what the model cost.

`journal.db` already has the tables. This spec only exports them.

## Sources (read-only)

From `abcxauto/memory/schema.py`:

- `session_markers` — model + NetLiq at session open
- `proposals` — tickets Grok invented
- `gate_decisions` — allow / refuse + stage
- `dispatches` — broker result
- `send_marks` / `send_mark_orders` — working vs fill marks
- `fills` — exec_id, slip, spread_paid, fill_label
- `model_usage` — tokens + cost_usd
- `self_tunes` — applied / clamped / rejected knobs
- `notes` / `cards` / `card_links` — fetch-only memory, not send geometry
- `halts` — latch reasons
- `snapshots` — NetLiq / positions / open orders
- `pcs_kill_sessions` / `pcs_fill_events` — if the session used PCS

Not sources:

- Think-stream prose (optional appendix, labeled `unverified`)
- MDA delayed last as if it were IBKR last
- `research_brief.json` as a live trigger (pointer + age only)

## Shape

```
look_replay/
  <session_date>/
    manifest.json      # journal path, session window ET, model, starting NL
    timeline.jsonl     # one event per line, ts-ordered
    scorecard.md       # book return % vs model cost; by_card if present
    gaps.md            # missing exec_id, unmarked send, NL=None markers
```

`timeline.jsonl` event kinds: `session`, `proposal`, `gate`, `dispatch`,
`send_mark`, `fill`, `self_tune`, `note`, `card`, `halt`, `snapshot`,
`model_usage`.

A look is **replayable** when every `dispatch.ok=1` has a `send_mark` and
every fill `exec_id` joins a mark or is listed in `gaps.md`. Honesty is
the point. A pretty pack that hides gaps is a fail.

## Command

Off-desk only:

```powershell
python scripts/look_replay.py --date 2026-09-17 --journal journal.db --out look_replay
```

No IBKR socket. No xAI call. No write back into `journal.db` except an
optional `replay_runs` table later — default is files only.

## Red lines

- No `replay` tool on the RTH chat.
- No auto-inject of the pack into wake / system prompt / `day_facts`.
- No new package.
- Do not teach Grok how to trade from the pack. The pack is for Noah.
