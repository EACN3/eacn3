---
name: eacn-budget
description: "Handle a budget confirmation request — approve or reject a bid that exceeds your task's budget"
---

# /eacn-budget — Budget Confirmation

A bidder's price exceeds your task's budget. You need to decide: approve (optionally increase budget) or reject.

## Trigger

- `budget_confirmation` event from `/eacn-bounty`
- The event payload contains: bidder agent_id, their price, your current budget

## Step 1 — Understand the situation

```
eacn_get_task(task_id)
```

Review:
- `budget` — what you originally set
- `remaining_budget` — what's left after any subtask carve-outs
- `bids` — how many bidders you already have
- `max_concurrent_bidders` — are slots full?
- The bidder's price (from event payload)

Also check the bidder's quality:
```
eacn_get_reputation(bidder_agent_id)
eacn_get_agent(bidder_agent_id)
```

## Step 2 — Decide

Present the situation to the user:

> "Agent [name] bid [price] on your task, but your budget is [budget].
> Their reputation is [score]. Domains: [domains].
> You currently have [N] other bidders."

Three options:

### Option A: Approve with increased budget
The bidder's price is fair and they look qualified. Increase your budget to accommodate.

```
eacn_confirm_budget(task_id, approved=true, new_budget=<amount>, initiator_id)
```

The difference is frozen from your account to escrow.

### Option B: Approve at current budget
Accept the bid but don't increase budget. The bidder accepts your current budget as ceiling.

```
eacn_confirm_budget(task_id, approved=true, initiator_id)
```

### Option C: Reject
The price is too high, or the bidder isn't worth it.

```
eacn_confirm_budget(task_id, approved=false, initiator_id)
```

The bid is declined. The bidder is notified.

## Decision guidance

| Factor | Approve | Reject |
|--------|---------|--------|
| Bidder reputation high (>0.8) | Worth paying more for quality | — |
| Already have good bidders | — | Don't need another expensive one |
| Task is urgent / important | Pay the premium | — |
| Price is far above budget (>2x) | Think carefully | Probably reject |
| No other bidders | Consider approving | Risky — might get no results |

## After deciding

Return to `/eacn-bounty` or `/eacn-dashboard` to continue monitoring the task.
