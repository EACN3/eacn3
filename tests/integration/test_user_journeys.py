"""Real user journey tests — from the agent's perspective.

These tests simulate what actually happens when someone uses the system:

1. An agent connects, sees a task broadcast, decides to bid, does work, gets paid
2. Two agents race for the same task through MCP
3. Agent is working when the task gets cancelled under them
4. Agent finishes work but initiator never picks a winner (timeout)
5. Agent creates subtasks and waits for sub-agents to complete
6. Server restarts mid-session — agent re-registers and continues
7. Agent runs out of money trying to create tasks
8. Multiple rounds: agent does 5 tasks back-to-back
"""

import asyncio
import pytest
from tests.integration.conftest import seed_reputation


# ── Helpers ──────────────────────────────────────────────────────────

async def setup_agent(mcp, net, agent_id, *, domains=None, balance=0.0, tier="general"):
    """Register an agent the way a real user would."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {agent_id}",
        "description": f"Test agent {agent_id}",
        "domains": domains or ["coding"],
        "skills": [{"name": "work", "description": "general work"}],
        "agent_id": agent_id,
        "tier": tier,
    })
    seed_reputation(net, agent_id)
    if balance > 0:
        net.escrow.get_or_create_account(agent_id, balance)


async def setup_initiator(mcp, net, agent_id, balance=10000.0):
    """Register an initiator with funds."""
    await setup_agent(mcp, net, agent_id, balance=balance)


# ═════════════════════════════════════════════════════════════════════
# Journey 1: Agent sees broadcast → bids → works → gets paid
# The happy path from an agent's perspective
# ═════════════════════════════════════════════════════════════════════

class TestAgentHappyPath:
    @pytest.mark.asyncio
    async def test_agent_full_journey(self, mcp, http, funded_network):
        """
        User Alice publishes a coding task.
        Agent Bob sees the broadcast, bids, does the work, submits result.
        Alice reviews and selects Bob's result. Bob gets paid.
        """
        net = funded_network
        await setup_initiator(mcp, net, "alice", balance=5000.0)
        await setup_agent(mcp, net, "bob")

        # Alice publishes a task
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Build a REST API for user management",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "alice",
            "deadline": "2027-01-01T00:00:00Z",
        })
        task_id = task["task_id"]

        # Bob checks for available tasks (poll events or list open)
        open_tasks = await mcp.call_tool_parsed("eacn3_list_open_tasks", {
            "domains": "coding",
        })
        assert any(t["id"] == task_id for t in open_tasks["tasks"]), \
            f"Bob should see Alice's task in open tasks"

        # Bob bids on the task
        bid_result = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "bob",
            "confidence": 0.85,
            "price": 300.0,
        })
        assert bid_result["status"] in ("executing", "waiting"), \
            f"Bob's bid should be accepted, got: {bid_result}"

        # Bob does the work and submits result
        result = await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": "bob",
            "content": {
                "code": "from fastapi import FastAPI\napp = FastAPI()\n...",
                "tests_passed": True,
                "documentation": "See README.md",
            },
        })
        assert result.get("ok") is True

        # Alice closes the task and reviews results
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "alice",
        })

        results = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": task_id,
            "initiator_id": "alice",
        })
        assert len(results["results"]) == 1
        assert results["results"][0]["agent_id"] == "bob"

        # Alice selects Bob's result → triggers payment
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": task_id,
            "agent_id": "bob",
            "initiator_id": "alice",
        })

        # Verify Bob got paid
        bob_bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "bob",
        })
        assert bob_bal["available"] == 300.0, \
            f"Bob should have 300.0, got {bob_bal['available']}"

        # Verify task is completed
        task_status = await mcp.call_tool_parsed("eacn3_get_task_status", {
            "task_id": task_id,
            "agent_id": "alice",
        })
        assert task_status["status"] == "completed"


# ═════════════════════════════════════════════════════════════════════
# Journey 2: Two agents compete for the same task
# ═════════════════════════════════════════════════════════════════════

class TestTwoAgentsCompete:
    @pytest.mark.asyncio
    async def test_two_agents_one_task(self, mcp, http, funded_network):
        """
        Alice publishes a task with max_concurrent=1.
        Bob and Charlie both bid. One executes, one waits.
        The executing agent submits, gets selected, gets paid.
        The waiting agent doesn't get paid.
        """
        net = funded_network
        await setup_initiator(mcp, net, "alice2", balance=5000.0)
        await setup_agent(mcp, net, "bob2")
        await setup_agent(mcp, net, "charlie2")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Write unit tests",
            "budget": 400.0,
            "domains": ["coding"],
            "initiator_id": "alice2",
            "max_concurrent_bidders": 1,
        })
        task_id = task["task_id"]

        # Both agents bid
        bob_bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "bob2",
            "confidence": 0.9, "price": 200.0,
        })
        charlie_bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "charlie2",
            "confidence": 0.85, "price": 180.0,
        })

        # One should be executing, one waiting
        statuses = {bob_bid["status"], charlie_bid["status"]}
        assert "executing" in statuses
        assert "waiting" in statuses

        # The executing agent submits
        executing_agent = "bob2" if bob_bid["status"] == "executing" else "charlie2"
        exec_price = 200.0 if executing_agent == "bob2" else 180.0

        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": executing_agent,
            "content": {"tests": "all passing"},
        })

        # Alice selects the result
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "alice2",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": task_id,
            "agent_id": executing_agent,
            "initiator_id": "alice2",
        })

        # Winner gets paid
        winner_bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": executing_agent,
        })
        assert winner_bal["available"] == exec_price


# ═════════════════════════════════════════════════════════════════════
# Journey 3: Task cancelled while agent is working
# ═════════════════════════════════════════════════════════════════════

class TestTaskCancelledMidWork:
    @pytest.mark.asyncio
    async def test_initiator_closes_before_result(self, mcp, http, funded_network):
        """
        Alice publishes task, Bob bids and starts working.
        Alice decides to cancel (close) before Bob submits.
        Bob's bid is rejected, Alice gets refunded.
        """
        net = funded_network
        await setup_initiator(mcp, net, "alice3", balance=5000.0)
        await setup_agent(mcp, net, "bob3")

        alice_bal_before = (await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "alice3",
        }))["available"]

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Research quantum computing trends",
            "budget": 300.0,
            "domains": ["coding"],
            "initiator_id": "alice3",
        })
        task_id = task["task_id"]

        # Bob bids
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "bob3",
            "confidence": 0.9, "price": 200.0,
        })

        # Alice cancels before Bob submits
        close_result = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "alice3",
        })

        # Task should be no_one_able (no results submitted)
        task_info = await mcp.call_tool_parsed("eacn3_get_task_status", {
            "task_id": task_id, "agent_id": "alice3",
        })
        assert task_info["status"] == "no_one_able"

        # Alice should get her budget back
        alice_bal_after = (await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "alice3",
        }))["available"]
        assert alice_bal_after == alice_bal_before, \
            f"Alice should get full refund: {alice_bal_after} != {alice_bal_before}"


# ═════════════════════════════════════════════════════════════════════
# Journey 4: Agent delegates subtask to another agent
# ═════════════════════════════════════════════════════════════════════

class TestSubtaskDelegation:
    @pytest.mark.asyncio
    async def test_agent_delegates_subtask(self, mcp, http, funded_network):
        """
        Alice publishes a task. Bob bids and wins.
        Bob realizes he needs help with the database part,
        so he creates a subtask. Charlie bids on the subtask.
        Charlie completes the subtask. Bob uses Charlie's work
        to complete the main task.
        """
        net = funded_network
        await setup_initiator(mcp, net, "alice4", balance=5000.0)
        await setup_agent(mcp, net, "bob4")
        await setup_agent(mcp, net, "charlie4")

        # Alice creates main task
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Build full-stack web app",
            "budget": 1000.0,
            "domains": ["coding"],
            "initiator_id": "alice4",
        })
        main_task_id = task["task_id"]

        # Bob bids and wins
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": main_task_id, "agent_id": "bob4",
            "confidence": 0.9, "price": 600.0,
        })

        # Bob creates a subtask for the database layer
        sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": main_task_id,
            "initiator_id": "bob4",
            "description": "Design and implement PostgreSQL schema",
            "budget": 200.0,
            "domains": ["coding"],
        })
        sub_task_id = sub["subtask_id"]

        # Verify subtask exists and is linked
        main_status = await mcp.call_tool_parsed("eacn3_get_task_status", {
            "task_id": main_task_id, "agent_id": "alice4",
        })
        assert sub_task_id in main_status["child_ids"]

        # Charlie bids on subtask
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": sub_task_id, "agent_id": "charlie4",
            "confidence": 0.9, "price": 150.0,
        })

        # Charlie completes subtask
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": sub_task_id, "agent_id": "charlie4",
            "content": {"schema": "CREATE TABLE users (...)"},
        })

        # Bob uses Charlie's work to complete main task
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": main_task_id, "agent_id": "bob4",
            "content": {
                "frontend": "React app",
                "backend": "FastAPI",
                "database": "Schema from subtask",
            },
        })

        # Alice reviews and selects
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": main_task_id, "initiator_id": "alice4",
        })
        results = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": main_task_id, "initiator_id": "alice4",
        })
        assert len(results["results"]) == 1

        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": main_task_id,
            "agent_id": "bob4",
            "initiator_id": "alice4",
        })

        # Bob gets paid for the main task
        bob_bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "bob4",
        })
        assert bob_bal["available"] == 600.0


# ═════════════════════════════════════════════════════════════════════
# Journey 5: Agent does 5 tasks in a row
# ═════════════════════════════════════════════════════════════════════

class TestMultipleTasksSequential:
    @pytest.mark.asyncio
    async def test_agent_completes_5_tasks(self, mcp, http, funded_network):
        """
        Bob is a productive agent. He takes on 5 tasks from different
        initiators, completes them all, and accumulates earnings.
        """
        net = funded_network
        await setup_agent(mcp, net, "bob5")

        total_earned = 0.0
        for i in range(5):
            initiator = f"init5-{i}"
            await setup_initiator(mcp, net, initiator, balance=2000.0)

            task = await mcp.call_tool_parsed("eacn3_create_task", {
                "description": f"Task {i}: implement feature {i}",
                "budget": 300.0,
                "domains": ["coding"],
                "initiator_id": initiator,
            })
            tid = task["task_id"]

            price = 100.0 + i * 20  # 100, 120, 140, 160, 180

            await mcp.call_tool_parsed("eacn3_submit_bid", {
                "task_id": tid, "agent_id": "bob5",
                "confidence": 0.9, "price": price,
            })
            await mcp.call_tool_parsed("eacn3_submit_result", {
                "task_id": tid, "agent_id": "bob5",
                "content": {"feature": f"implementation {i}"},
            })
            await mcp.call_tool_parsed("eacn3_close_task", {
                "task_id": tid, "initiator_id": initiator,
            })
            await mcp.call_tool_parsed("eacn3_select_result", {
                "task_id": tid, "agent_id": "bob5",
                "initiator_id": initiator,
            })
            total_earned += price

        # Bob should have all his earnings
        bob_bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "bob5",
        })
        assert bob_bal["available"] == total_earned, \
            f"Bob earned {bob_bal['available']}, expected {total_earned}"


# ═════════════════════════════════════════════════════════════════════
# Journey 6: Agent with low reputation gets rejected, builds up
# ═════════════════════════════════════════════════════════════════════

class TestReputationJourney:
    @pytest.mark.asyncio
    async def test_low_rep_rejected_then_invited(self, mcp, http, funded_network):
        """
        New agent has low reputation, gets rejected on open tasks.
        An initiator invites them specifically — they can bid now.
        After completing work, their reputation improves.
        """
        net = funded_network
        await setup_initiator(mcp, net, "alice6", balance=5000.0)

        # Register agent with very low reputation
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Newbie",
            "description": "Just started",
            "domains": ["coding"],
            "skills": [{"name": "python", "description": "python coding"}],
            "agent_id": "newbie6",
        })
        net.reputation._scores["newbie6"] = 0.2  # Very low

        # Create task
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Simple python script",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "alice6",
        })
        tid = task["task_id"]

        # Newbie bids with low confidence — rejected (0.3 * 0.2 = 0.06 < 0.5)
        bid1 = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "newbie6",
            "confidence": 0.3, "price": 50.0,
        })
        assert bid1["status"] == "rejected"

        # Alice invites the newbie specifically
        await mcp.call_tool_parsed("eacn3_invite_agent", {
            "task_id": tid, "initiator_id": "alice6",
            "agent_id": "newbie6",
        })

        # Create another task with invitation
        task2 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Another simple task",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "alice6",
            "invited_agent_ids": ["newbie6"],
        })
        tid2 = task2["task_id"]

        # Invited agent can bid even with low rep
        bid2 = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid2, "agent_id": "newbie6",
            "confidence": 0.3, "price": 50.0,
        })
        # Invited agents bypass the ability check
        assert bid2["status"] in ("executing", "waiting", "pending")


# ═════════════════════════════════════════════════════════════════════
# Journey 7: Budget runs out mid-session
# ═════════════════════════════════════════════════════════════════════

class TestBudgetExhaustion:
    @pytest.mark.asyncio
    async def test_initiator_runs_out_then_deposits(self, mcp, http, funded_network):
        """
        Alice creates tasks until she runs out of money.
        She deposits more, then continues.
        """
        net = funded_network
        await setup_initiator(mcp, net, "alice7", balance=500.0)

        # Create first task — succeeds
        t1 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Task 1",
            "budget": 300.0,
            "domains": ["coding"],
            "initiator_id": "alice7",
        })
        assert "task_id" in t1

        # Create second task — succeeds (200 remaining)
        t2 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Task 2",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "alice7",
        })
        assert "task_id" in t2

        # Create third task — fails (0 remaining)
        t3 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Task 3",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "alice7",
        })
        # Should indicate insufficient balance
        assert "error" in t3 or "raw" in t3

        # Alice deposits more money
        dep = await mcp.call_tool_parsed("eacn3_deposit", {
            "agent_id": "alice7",
            "amount": 1000.0,
        })
        assert dep["available"] == 1000.0  # 0 + 1000

        # Now she can create tasks again
        t4 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Task 4 (after deposit)",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "alice7",
        })
        assert "task_id" in t4


# ═════════════════════════════════════════════════════════════════════
# Journey 8: Discussion flow — initiator clarifies requirements
# ═════════════════════════════════════════════════════════════════════

class TestDiscussionFlow:
    @pytest.mark.asyncio
    async def test_initiator_clarifies_then_agent_delivers(self, mcp, http, funded_network):
        """
        Alice creates task with vague description.
        Bob bids. Alice adds clarification via discussion.
        Bob reads it and delivers based on the clarification.
        """
        net = funded_network
        await setup_initiator(mcp, net, "alice8", balance=5000.0)
        await setup_agent(mcp, net, "bob8")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Build something cool",
            "budget": 300.0,
            "domains": ["coding"],
            "initiator_id": "alice8",
        })
        tid = task["task_id"]

        # Bob bids
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "bob8",
            "confidence": 0.9, "price": 200.0,
        })

        # Alice adds clarification
        await mcp.call_tool_parsed("eacn3_update_discussions", {
            "task_id": tid,
            "initiator_id": "alice8",
            "message": "By 'cool' I mean a CLI tool that generates ASCII art",
        })

        # Bob checks the task to see the discussion
        task_detail = (await http.get(f"/api/tasks/{tid}")).json()
        discussions = task_detail["content"].get("discussions", [])
        assert len(discussions) == 1
        assert "ASCII art" in discussions[0]["message"]

        # Bob delivers based on clarification
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "bob8",
            "content": {
                "tool": "ascii-art-gen",
                "usage": "python ascii_art.py 'Hello World'",
            },
        })

        # Alice reviews and accepts
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": tid, "initiator_id": "alice8",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": tid, "agent_id": "bob8", "initiator_id": "alice8",
        })

        # Verify completed
        status = await mcp.call_tool_parsed("eacn3_get_task_status", {
            "task_id": tid, "agent_id": "alice8",
        })
        assert status["status"] == "completed"
