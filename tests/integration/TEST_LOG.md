# Test Iteration Log

## Round 1: Feature-point tests (11 tests)
- Basic tier restriction, hierarchy, invite bypass
- Issues found: DB missing tier column, client-side check format mismatch, conftest missing /health
- All fixed, 11/11 pass

## Round 2: Real-world story tests (8 stories)
Focus: Multi-agent narratives that exercise the system the way real users would.

Stories:
1. Planner decomposes across tiers (subtask delegation chain)
2. Newcomer reputation journey (tool tasks → reputation growth → invited to expert)
3. Competitive bidding (3 agents compete, publisher picks best)
4. Slot contention with invite (1 slot, invited low-rep agent queued then promoted)
5. Discussion + deadline extension during execution
6. Budget negotiation with tier (expert bids over budget, publisher confirms)
7. Multi-domain routing (partial vs full domain overlap)
8. Direct messaging during task execution

Issues found:
- isTierEligible rule was wrong: expert couldn't bid on general tasks. User intent: only tool restricted.
- Subtask had no level support — tool agents could never participate in delegation chains.
- Test budget math: planner price exceeded remaining escrow after subtask deductions.

## Round 3: Real-world narrative tests + subtask level fix (12 stories total)
Focus: What real users would actually do with this system.

New stories added:
9. Subtask level inheritance — tool subtask vs inherited general subtask
10. Agent at capacity — max_concurrent_tasks respected by auto-match
11. Task expires with no bidders — budget refund flow
12. Agent updates domains mid-session — can bid on new categories

Code fixes:
- create_subtask now supports `level` parameter through entire stack
  (plugin → network-client → routes → app → task_manager)
- Default: subtask inherits parent's level (not always general)

Thinking for next round:
- Multi-server cross-node scenarios (agents on different servers)
- Adjudication flow (third party evaluates result quality)
- What if an agent registers as planner but only does executor work?
- Economy edge: what if budget is exactly equal to bid price?
- Reputation decay / recovery over multiple tasks
