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

Thinking:
- Real users won't read docs carefully. What if they register wrong tier?
- What if agents bid on tasks they shouldn't? The system should reject gracefully.
- Need to test: agent updates tier after registration, unregister/re-register
- Need to test: invite a non-existent agent, invite after task closed
- Need to test: subtask tier/level inheritance behavior
- Need to test: what happens to invited_agent_ids when task is forwarded to peer nodes
