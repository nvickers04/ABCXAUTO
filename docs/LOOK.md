# One look — one book, socket is the live switch

**Goal:** book return % of starting NetLiq > model cost.
Size and risk are % of the book. No dollar sleeve. No operator strategy card.

Grok is the trader. The shell does not teach IBKR. Paper is TWS 7497.
Live is TWS 7496 after the confirm phrase. Same constitution. Operator = setup + kill switch.

## Autonomy

No operator approval. `self_tune` applies immediately inside the immutable floor.
Grok cannot switch to live or set a dollar sleeve.
`python -m abcxauto` opens Pro so the think stream is on screen.

## Physics

One look stays open. The chat is kept. Tool results stay in it. A spoken
line does not wipe it. A poke does not start a new messages list.

```
Call the model.
  tool_calls → run tools, append results to this chat, call the model again.
               Repeat until there are no tool_calls.
  words only → stop calling the model.
               Wait for fill / order_change / unprotected / operator poke.
               Then call again with this chat plus a fresh snap.
               Do not call the model again because it spoke.
```

Overnight / after-close / park drop the chat. Paper RTH and premarket stay
up on this process — no sit clock. Closed / postmarket does not call Grok
(unprotected still does); park_clock until premarket.

Session look/token cap (Settings) idles when hit. Chat is kept. No sit clock.
Do not grow the system prompt as memory.

`send` → gates → IBKR. Journal write is code, not a Grok tool.
Repeat reads inside a think are served from cache, cleared on any mutating
tool or live book poke. Repeat-text detectors, a 64-call runaway ceiling,
and per-tool timeouts stay. There is no stream time box.
`wait_for_pace` is the pulse sleep until the next poke.

After a think:

- Closed / postmarket: no Grok (unprotected still interrupts); park until premarket
- Paper RTH / premarket: stay up on this process. Next model call is fill / order_change / unprotected / a lead fact that actually changed
- Session look/token cap hit: stay idle. Chat kept. No grok_wake / set_wake
- Last hour to the open is stay-up, not a sit clock

## Hard (code)

- Unprotected STK → last-stop first; hold forbidden until it rests at IBKR. Paper RTH + flat → hold is not a ticket. Combo close (`closing_position`) is one BAG, not new risk
- RTH may use news/scan/web as COLOR; the brief remains prior-session color on wake
- Capacity, defined-risk, cash-only, size/loss floors, fail-closed
- New risk without params.card naming a play (scorecard label, not a catalog)
- IBKR live last for ticket geometry (not MDA)
- Ticket last / IV / credit / width must be in this look's quote / option_quote / book cache (`stale_or_invented_number`)
- `candles`: IBKR hist, else live 5s stream; error if both miss (not MDA)
- Two books = two processes, two client ids

Universe is a watchlist. `send` is not a legal-set sandbox.
