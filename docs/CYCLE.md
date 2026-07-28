# Agent cycle — own the book (not a decision tree)

## Goal vs control

**Long-run goal:** book return on startup cash > model cost.  
That is a **scorecard metric**, not a control signal.

Do **not** skip Judge, skip Act, stretch sleep, or collapse streams to “save API $.”  
Cost shows up in Scorecard. Behavior stays: own the book under hard risk.

## The loop (straight pipeline)

```
PERCEIVE  code facts (book, orders, account, protection, tape, news)
    ↓
JUDGE     Grok — stance / thesis / intent (JSON only)
    ↓
ACT       Grok — always runs after valid Judge; ONE action
    ↓
VERIFY    hard gates only (risk $, geometry, allowlist, protect rules)
    ↓
SEND      single choke point → broker (or reject with reason)
    ↓
REMEMBER  journal + trade plans
    ↓
WAIT      market rhythm (protect fast, manage, session closed) — not thrift
```

## What we removed (allocator tree)

| Old | Why it was wrong |
|-----|------------------|
| `_should_skip_act` thrift on idle/manage | Model cost became behavior |
| Parallel open_risk / new_risk / escapade merge | Decision tree, not ownership |
| “Allocator budget spend” prompt frame | Grok managed cost, not the book |

## What stayed (non-negotiable)

- Risk gates LLM-proof; exits never blocked  
- Unprotected STK → protect first; hold forbidden while unprotected  
- Universe allowlist; shell does not rank ideas  
- IBKR live last for geometry (not MDA tape last)  
- One send per cycle  

## Focus stream (label only)

`primary_stream` picks **prompt focus** for the single Act call:

- `open_risk` — book continuity (protect/manage)  
- `new_risk` — entry under capacity  

Not a multi-branch merge. Not escapade parallelism.

## Controls dials

Still real: capacity, complexity allowlist, frequency as operator preference for **how aggressive the book can be** — not how many Grok calls to skip.
