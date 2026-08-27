# Agent cycle — paper lab, live follower

**Goal:** book return % of starting NetLiq > model cost.
Size and risk are % of the book. No dollar sleeve. No operator strategy card.

Grok is the trader. The shell does not teach IBKR. Paper is the lab (TWS 7497).
Live only follows a promoted snapshot. Operator = setup + kill switch.

## Autonomy

No operator approval. `self_tune` applies immediately inside the immutable floor.
Grok cannot switch to live or set a dollar sleeve.
`python -m abcxauto` opens Pro so the think stream is on screen.

## The loop

```
WAKE     Overnight / after-close: park_clock until premarket
         Paper RTH and premarket stay up on this process — no sit clock
         fill / order_change / unprotected poke the open think
    |
SNAP     IBKR book, orders, protection
    |
GROK     tools (facts + send + self_tune + write_lab_playbook). Wake is a short line.
    |
SEND     send → gates → IBKR. Journal write is code, not a Grok tool.
    |
LOOK     Finished RTH look writes no grok_wake.json. Empty/? retries on this process.
         Overnight skip parks. Stay-up has no sit clock.
```

        Paper RTH / premarket stay-up continues the live chat across successful
        looks. Empty / junk / dead stream drop it so the next think is cold.
        Overnight / after-close also drop the chat. Repeat reads inside a think are
        served from cache, cleared on any mutating tool or live poke. Stall/loop
        detectors, a 64-step runaway ceiling, and per-tool timeouts stay. There
        is no stream time box and no max-look ceiling. `wait_for_pace` is just
        the pulse sleep until the next wake.

After a think:

- Closed / postmarket: no Grok (unprotected still interrupts); park until premarket
- Paper RTH / premarket: stay up on this process. Empty / `?` retries immediately
- Session-card opening print is a send gate, not a park
- Last hour to the open is stay-up, not a sit clock

## Hard (code)

- Unprotected STK → last-stop first; hold forbidden until it rests at IBKR. Paper RTH + flat → hold is not a ticket. Combo close (`closing_position`) is one BAG, not new risk
- Capacity, defined-risk, cash-only, size/loss floors, fail-closed
- Live new risk without a promoted playbook
- New risk without params.card naming an existing lab card (label, not law)
- IBKR live last for ticket geometry (not MDA)
- `candles`: IBKR hist, else live 5s stream; error if both miss (not MDA)
- Two books = two processes, two client ids

Universe is a watchlist. `send` is not a legal-set sandbox.
