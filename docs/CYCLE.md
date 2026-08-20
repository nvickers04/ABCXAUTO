# Agent cycle — paper lab, live follower

**Goal:** book return % of starting NetLiq > model cost.
Size and risk are % of the book. No dollar sleeve. No operator strategy card.

Grok is the trader. The clerk does not teach IBKR. Paper is the lab (TWS 7497).
Live only follows a promoted snapshot. Operator = setup + kill switch.

## Autonomy

No operator approval. `self_tune` applies immediately inside the immutable floor.
Grok cannot switch to live or set a dollar sleeve.
`python -m abcxauto` opens Pro so the think stream is on screen.

## The loop

```
WAKE     Grok set_wake (honored; min floor only), or clerk default if skipped
         fill / order_change / mark move / unprotected can come sooner
         pulse ~10s; closed/postmarket does not call Grok (unprotected still does)
    |
SNAP     IBKR book, orders, protection
    |
GROK     tools (facts + send + self_tune + set_wake). Wake is a short line.
    |
CLERK    send → gates → IBKR. Journal write is clerk, not a Grok tool.
    |
LOOK     ensure_next_look so the desk is never parked
```

One wake is one think: it runs until Grok stops calling tools, then the chat is
dropped. Repeat reads inside a think are served from cache, cleared on any
mutating tool or live poke. Stall/loop detectors, a 64-step runaway ceiling, and
per-tool timeouts stay. There is no stream time box and no max-look ceiling.
`wait_for_pace` is just the pulse sleep until the next wake.

## Hard (code)

- Unprotected STK → last-stop first; hold forbidden until it rests at IBKR. Paper RTH + flat + clerk open → hold is not a ticket. Combo close (`closing_position`) is one BAG, not new risk
- Capacity, defined-risk, cash-only, size/loss floors, fail-closed
- Live new risk without a promoted playbook
- IBKR live last for ticket geometry (not MDA)
- `candles`: IBKR hist, else live 5s stream; error if both miss (not MDA)
- Two books = two processes, two client ids

Universe is a watchlist. `send` is not a legal-set sandbox.
