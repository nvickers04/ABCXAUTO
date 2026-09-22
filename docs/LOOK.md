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
               Wait for fill / order_change / unprotected / book_move /
               socket-up / operator poke / leftover>deployed after 90s RTH.
               Then call again with this chat plus a fresh snap.
               Do not call the model again because it spoke.
```

- Closed / overnight: code park until 04:00 ET premarket. No Grok. Unprotected still interrupts.
- Premarket, postmarket, and RTH stay on this process.
- Stay-up waits for fill / order_change / unprotected / book_move / socket-up / operator poke. Do not re-enter because research has no sends.
- RTH leftover cash > deployed re-enters after 90s (engine cooldown, not a park file, not a general chair). Premarket / AH stay event-driven; RTH roll still starts a look.
- Dead socket / book_unreliable is not a billed look unless unprotected.
- Host does not invent 8-minute or 10-second mills. Overnight park is code.

Session look/token cap (Settings) idles when hit. Chat is kept.
Do not grow the system prompt as memory.

`send` → gates → IBKR. Journal write is code, not a Grok tool.
Repeat reads inside a think are served from cache, cleared on any mutating
tool or live book poke. Repeat-text detectors, a 64-call runaway ceiling,
and per-tool timeouts stay. There is no stream time box.
`wait_for_pace` is the pulse sleep until the next poke.

## Hard (code)

- Unprotected STK → last-stop first; hold forbidden until it rests at IBKR. Paper RTH + flat → hold is not a ticket. Combo close (`closing_position`) is one BAG, not new risk
- Premarket, postmarket, and RTH may use news/scan/web as COLOR; `research_brief` is this-look gathered color plus a prior-session stub, never a live trigger
- Capacity, defined-risk, cash-only, size/loss floors, fail-closed
- New risk without params.card naming a play (scorecard label, not a catalog)
- IBKR live last for ticket geometry (not MDA)
- Ticket last / IV / credit / width must be in this look's quote / option_quote / book cache (`stale_or_invented_number`)
- `candles`: IBKR hist, else live 5s stream; error if both miss (not MDA)
- Two books = two processes, two client ids

Universe is live IBKR screens; nothing about where to hunt persists. `self_tune` cannot restore a watchlist. `send` is not limited to a legal set.
