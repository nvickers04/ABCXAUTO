# Off-desk crew

Grok owns the book. This crew does not.

Five seats sit **outside** the look. They improve runtime, research,
recordability, tools, and architecture. They never send. They never
become a second RTH process. They never grow the system prompt.

Parent issue: [#220](https://github.com/nvickers04/ABCXAUTO/issues/220).

## Constitution

Copied from `SPEC.md` / `.cursor/rules/grok-owns-book.mdc`. If a seat
conflicts with this list, the seat is wrong.

- Grok is the only RTH process. There is no clerk process.
- `send` is the only broker path. Paper is TWS **7497**. Live is not this crew.
- Do not grow the system prompt. Do not inject strategy menus or a Judge/Act form.
- `recall` / `research_brief` stay fetch-only. Never auto-inject into
  `book` / `status` / `day_facts` / the system prompt. Not send geometry.
- Journal write is code, not a Grok tool.
- Agent may tighten floors via `self_tune`. It cannot weaken them.
- No new packages without a profitability reason.
- Operator owns merge of the Cursor-agent PR pile. This crew does not.
- Hard cap: **five seats**. A sixth is clerk-creep. Kill it or merge it.

This is the lesson from ABC-Application (22 agents, 65k-line blob).

## Seats

Do not name a seat Clerk. The clerk process was deleted.

| Seat | Bucket | Lives | Does not |
|------|--------|-------|----------|
| **Runtime Steward** | runtime | supervisor, park_clock, abort_fuse, IB link | sit clock, second process, live socket |
| **Research Steward** | research | `research_brief.json`, MDA/news/odds/web budget | playbook, lessons shelf, RTH block on stale brief |
| **Record Auditor** | recordability | `journal.db`, look snapshots, model $ | `journal` tool, auto-inject notes, tutoring Grok |
| **Tools Surgeon** | tools | brain_tools vs `ORDER EXAMPLES`, quote cache | strategy catalog, new RTH tool without a gate |
| **Architecture Surgeon** | architecture | Grok/Code/Operator split, file size, branch hygiene | parallel rebuild, god-file split war with open PRs |

Grok (the `model` knob) stays the trader. These seats are GitHub / Cursor
/ chat agents that open PRs against `master`. They are not `tool_calls`
inside a look.

## First tickets

Concrete work from the last ten days of `master` plus the open PR pile.
Do the smallest honest change.

### Runtime Steward — child of #220

Symptoms that already ate this desk:

- TWS **7497** not probed before launch.
- IB 1100 / disconnect halt vs leftover park on Start.
- Supervisor crash-relaunch that looks up while the book is unknown.
- `--cleanup` PID substring match (fixed on some branches, verify on master).
- RTH sit clock creep (`park_clock` is overnight / after-close only).

Acceptance:

- [ ] Launch refuses when 7497 is dark. No silent start.
- [ ] Disconnect halt and leftover park are two different states on Start.
- [ ] Crash relaunch does not invent a book. Fail-closed until IBKR facts exist.
- [ ] No sit clock in paper RTH / premarket.
- [ ] Tests in `tests/test_cleanup_pro.py`, `tests/test_disconnect_halt_resume.py`,
      `tests/test_headless.py` stay green. Add one if a path is silent today.

### Research Steward — child of #220

Premarket / AH / closed already writes `data/state/research_brief.json`.
RTH treats it as COLOR. Missing/stale brief must not block RTH.

Acceptance:

- [ ] Brief has age, source counts (news/scan/odds/web), and expectancy COLOR.
      No strategy essay. No playbook cathedral.
- [ ] A journal index of briefs exists **or** the existing file is replayable
      by date. Index is not injected into wake bodies.
- [ ] Token / MDA budget for research is visible on the scorecard or a file.
      Re-billing a cached scan this look is a bug (already patched 2026-09-17;
      keep a test).
- [ ] `research_brief` tool remains fetch-only. No new RTH trigger.
- [ ] Tests that a stale brief is COLOR, not a refuse.

### Record Auditor — child of #220

A human desk trader cannot reconstruct a day from memory. Neither can Grok
after overnight park drops the chat.

Design: [`docs/LOOK_REPLAY.md`](LOOK_REPLAY.md).

Acceptance:

- [ ] `python -m abcxauto.scripts.look_replay` (or `scripts/look_replay.py`)
      reads `journal.db` and writes a pack for one session date.
- [ ] Pack contains: session marker + NetLiq, proposals, gate_decisions,
      dispatches, send_marks, fills (slip + label), model_usage, self_tunes,
      notes created that flight, cards linked, halts.
- [ ] Pack does **not** reconstruct think-stream prose as if it were fact.
      Spoken lines are optional and labeled `unverified`.
- [ ] No new brain tool. No `replay` on the RTH chat.
- [ ] Pytest builds a tiny journal and diffs the pack. Honesty > pretty.

### Tools Surgeon — child of #220

Symptoms:

- Scan clip silently dropped the tail of a cached page (patched 2026-09-17).
- Old structure-grade refusals cooled symbols as if they were a ban
  (patched 2026-09-17).
- Ticket last / IV / credit / width must sit in this look's quote /
  option_quote / book cache (`stale_or_invented_number`).
- `brain_tools.py` ~95k and `order_examples.py` can drift.

Acceptance:

- [ ] A test that every sendable strategy in `ORDER EXAMPLES` has a matching
      tool schema path (or an explicit `not-a-tool` note).
- [ ] Cached scan / quote hand-back does not re-clip or re-bill.
- [ ] Unknown tool name fails closed with a catalog, not a silent no-op.
- [ ] No new sendable shape without a gate test.

### Architecture Surgeon — child of #220

God files on `master` (bytes, 2026-09-17 tree):

- `abcxauto/desktop/sync.py` ~107k
- `abcxauto/broker/connector.py` ~97k
- `abcxauto/brain_tools.py` ~95k
- `abcxauto/pro_engine.py` ~94k
- `abcxauto/brain.py` ~92k

Open splits already in flight: journal facade (`memory/`), desktop package,
PR #208, PR #211. Do not start a second rebuild.

Acceptance:

- [ ] Inventory of files >40k with an owner seat. No silent growth this week.
- [ ] Review-only on #208 / #211. Merge is operator.
- [ ] `cursor/*` branch graveyard list: stale vs still-open PR. Do not delete
      operator branches.
- [ ] Any split keeps public import paths. Tests that import `TradeJournal`
      / `get_journal` stay green.

## Operating cadence

Weekday paper hours are 8:30–16:00 ET. Crew work happens **off those hours**
or on a branch that cannot reach 7497.

One seat, one PR, one concern. PR body cites the child issue and the
constitution line it refuses to violate.

Kill switch for the crew is the same as the desk: stop, do not merge, do
not talk around a hard gate.
