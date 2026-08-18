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
WAKE     Grok set_wake, or clerk default look (60s open / 90s else)
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

Stall/loop detectors, 24 tool rounds, and per-tool timeouts stay. There is no
stream time box and no metronome nap ladder. `wait_for_pace` is just the pulse
sleep until the next wake.

## Hard (code)

- Unprotected STK → protect first; hold forbidden only while unprotected
- Capacity, defined-risk, cash-only, size/loss floors, fail-closed
- Live hunt without a promoted playbook
- IBKR live last for ticket geometry (not MDA)
- Two books = two processes, two client ids

Universe is a watchlist. `send` is not a legal-set sandbox.
Controls sliders are status; they do not shrink the ticket allowlist.
