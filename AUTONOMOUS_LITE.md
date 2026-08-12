# ABCXAUTO Autonomous Lite

**Goal**: Grok owns the paper (then small live) book and improves itself so the outcome is money, not operator workload.

## Non-negotiable floor (code-hard, never overridable by Grok)
- Daily loss halt
- Max position size % of equity
- Unprotected STK protection (hold forbidden while unprotected)
- Exits never blocked
- Fail-closed on missing account data
- Defined-risk preference / cash-only when set

These stay immutable. Everything else is agent-owned.

## Autonomy surface (fully self-modifiable, no approval)
- Strategy parameters and thresholds
- Prompt extras / judgment style / mandate tweaks (within safety)
- Universe focus / symbol selection
- Cycle timing, pacing, frequency, deliberation, budget, complexity, rotation
- Bounded risk knobs *inside* the hard envelopes
- Any other non-protection setting

Grok reads its journal + scorecard, decides changes, applies them, logs every self-change with before/after, and continues. Auto-reverts or tightens if scorecard degrades or risk metrics approach limits.

## Defaults for ~$1000 capital
- Paper mode
- Risk posture: balanced (tightened for small account)
- Max open positions: 2–3
- Max risk per trade: ~1%
- Daily loss limit: ~3–4%
- Longer default cycle (daily or multi-hour) to control model cost
- Stock brackets preferred; options only if defined-risk and capacity allows

## Operator role
- Set keys + start TWS paper
- Start the agent
- Monitor status / journal / P&L vs model cost
- Emergency kill switch only

No ongoing approvals. No steering dials required.

## Implementation status
This branch begins the cut. Core loop, risk gates, and journal already exist. Next: remove approval gates for tunable surface, lock $1k defaults, add explicit self-tune action + reflection cycle.
