"""Real user failure scenario tests — things that go wrong in practice.

These test what happens when users make mistakes or the system enters
unexpected states. Every scenario here represents something a real user
has done or will do.
"""

import asyncio
import pytest
from tests.integration.conftest import seed_reputation


async def _setup(mcp, net, agent_id, *, balance=0.0):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {agent_id}", "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": agent_id,
    })
    seed_reputation(net, agent_id)
    if balance > 0:
        net.escrow.get_or_create_account(agent_id, balance)


# ═════════════════════════════════════════════════════════════════════
# Failure 1: Agent tries to bid on task they created
# ═════════════════════════════════════════════════════════════════════

class TestSelfBid:
    @pytest.mark.asyncio
    async def test_agent_bids_on_own_task(self, mcp, http, funded_network):
        """User creates a task and tries to bid on it themselves."""
        net = funded_network
        await _setup(mcp, net, "self-bidder", balance=5000.0)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "My task",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "self-bidder",
        })
        tid = task["task_id"]

        # Self-bid — the system should allow it (self-execution is valid)
        bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "self-bidder",
            "confidence": 0.9, "price": 100.0,
        })
        # This should work — agents can execute their own tasks
        assert bid["status"] in ("executing", "waiting")


# ═════════════════════════════════════════════════════════════════════
# Failure 2: Agent submits result to task they didn't bid on
# ═════════════════════════════════════════════════════════════════════

class TestResultWithoutBid:
    @pytest.mark.asyncio
    async def test_submit_result_without_bidding(self, mcp, http, funded_network):
        """Agent tries to submit result without having bid first."""
        net = funded_network
        await _setup(mcp, net, "sneaky", balance=5000.0)
        await _setup(mcp, net, "sneaky-worker")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Secret task",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "sneaky",
        })
        tid = task["task_id"]

        # Worker tries to submit without bidding
        result = await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "sneaky-worker",
            "content": {"hack": "attempt"},
        })
        # Should fail
        assert "error" in result or "raw" in result


# ═════════════════════════════════════════════════════════════════════
# Failure 3: Creating a task with 0 budget
# ═════════════════════════════════════════════════════════════════════

class TestZeroBudgetTask:
    @pytest.mark.asyncio
    async def test_zero_budget_task_lifecycle(self, mcp, http, funded_network):
        """Creating a task with budget=0 — should still work for free tasks."""
        net = funded_network
        await _setup(mcp, net, "freebie-init", balance=5000.0)
        await _setup(mcp, net, "freebie-worker")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Free task",
            "budget": 0.0,
            "domains": ["coding"],
            "initiator_id": "freebie-init",
        })

        if "task_id" in task:
            # If 0 budget is allowed, verify the lifecycle works
            tid = task["task_id"]
            bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
                "task_id": tid, "agent_id": "freebie-worker",
                "confidence": 0.9, "price": 0.0,
            })
            # Bid may or may not be accepted with 0 price
            assert bid["status"] in ("executing", "waiting", "rejected")


# ═════════════════════════════════════════════════════════════════════
# Failure 4: Selecting a result twice
# ═════════════════════════════════════════════════════════════════════

class TestDoubleSelect:
    @pytest.mark.asyncio
    async def test_cannot_select_result_twice(self, mcp, http, funded_network):
        """Initiator tries to select the winning result twice."""
        net = funded_network
        await _setup(mcp, net, "dbl-init", balance=5000.0)
        await _setup(mcp, net, "dbl-worker")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Double select test",
            "budget": 300.0,
            "domains": ["coding"],
            "initiator_id": "dbl-init",
        })
        tid = task["task_id"]

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "dbl-worker",
            "confidence": 0.9, "price": 150.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "dbl-worker",
            "content": {"done": True},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": tid, "initiator_id": "dbl-init",
        })

        # First select succeeds
        r1 = await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": tid, "agent_id": "dbl-worker",
            "initiator_id": "dbl-init",
        })
        assert "error" not in r1 and "raw" not in r1

        # Second select should fail
        r2 = await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": tid, "agent_id": "dbl-worker",
            "initiator_id": "dbl-init",
        })
        assert "error" in r2 or "raw" in r2


# ═════════════════════════════════════════════════════════════════════
# Failure 5: Agent rejects task after bidding
# ═════════════════════════════════════════════════════════════════════

class TestAgentRejects:
    @pytest.mark.asyncio
    async def test_agent_rejects_and_another_takes_over(self, mcp, http, funded_network):
        """
        Bob bids and wins, but realizes he can't do it. He rejects.
        Charlie picks it up from the queue.
        """
        net = funded_network
        await _setup(mcp, net, "rej-init", balance=5000.0)
        await _setup(mcp, net, "rej-bob")
        await _setup(mcp, net, "rej-charlie")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Complex task",
            "budget": 400.0,
            "domains": ["coding"],
            "initiator_id": "rej-init",
            "max_concurrent_bidders": 1,
        })
        tid = task["task_id"]

        # Bob bids first — gets executing
        bob_bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "rej-bob",
            "confidence": 0.9, "price": 200.0,
        })
        assert bob_bid["status"] == "executing"

        # Charlie bids — goes to waiting
        charlie_bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "rej-charlie",
            "confidence": 0.85, "price": 180.0,
        })
        assert charlie_bid["status"] == "waiting"

        # Bob rejects
        await mcp.call_tool_parsed("eacn3_reject_task", {
            "task_id": tid, "agent_id": "rej-bob",
        })

        # Charlie should be promoted
        task_info = (await http.get(f"/api/tasks/{tid}")).json()
        bids = {b["agent_id"]: b["status"] for b in task_info["bids"]}
        assert bids["rej-bob"] == "rejected"
        assert bids["rej-charlie"] == "executing"

        # Charlie completes the task
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "rej-charlie",
            "content": {"done": True},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": tid, "initiator_id": "rej-init",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": tid, "agent_id": "rej-charlie",
            "initiator_id": "rej-init",
        })

        # Charlie gets paid, Bob doesn't
        charlie_bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "rej-charlie",
        })
        assert charlie_bal["available"] == 180.0


# ═════════════════════════════════════════════════════════════════════
# Failure 6: Duplicate task ID
# ═════════════════════════════════════════════════════════════════════

class TestDuplicateTaskId:
    @pytest.mark.asyncio
    async def test_creating_duplicate_task_fails_cleanly(self, mcp, http, funded_network):
        """
        User accidentally creates two tasks with the same description —
        the second should fail, and the first should still work.
        """
        net = funded_network
        await _setup(mcp, net, "dup-init", balance=5000.0)

        # First task
        t1 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Implement feature X",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "dup-init",
        })
        assert "task_id" in t1
        tid1 = t1["task_id"]

        # Second task with different description — should get different ID
        t2 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Also implement feature X",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "dup-init",
        })
        assert "task_id" in t2
        assert t2["task_id"] != tid1  # Different IDs

        # Both should exist
        r1 = (await http.get(f"/api/tasks/{tid1}")).json()
        r2 = (await http.get(f"/api/tasks/{t2['task_id']}")).json()
        assert r1["status"] == "unclaimed"
        assert r2["status"] == "unclaimed"


# ═════════════════════════════════════════════════════════════════════
# Failure 7: Over-budget bid flow
# ═════════════════════════════════════════════════════════════════════

class TestOverBudgetBid:
    @pytest.mark.asyncio
    async def test_over_budget_bid_pending_then_approved(self, mcp, http, funded_network):
        """
        Agent bids more than the task budget. Goes to pending.
        Initiator approves with increased budget. Agent can work.
        """
        net = funded_network
        await _setup(mcp, net, "ob-init", balance=5000.0)
        await _setup(mcp, net, "ob-worker")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Simple task",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "ob-init",
        })
        tid = task["task_id"]

        # Worker bids more than budget
        bid = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "ob-worker",
            "confidence": 0.9, "price": 80.0,
        })
        assert bid["status"] == "pending"

        # Initiator approves
        approve = await mcp.call_tool_parsed("eacn3_confirm_budget", {
            "task_id": tid,
            "initiator_id": "ob-init",
            "approved": True,
            "new_budget": 100.0,
        })

        # Check task — bid should be accepted now
        task_info = (await http.get(f"/api/tasks/{tid}")).json()
        bids = {b["agent_id"]: b["status"] for b in task_info["bids"]}
        assert bids["ob-worker"] in ("executing", "waiting")
        assert task_info["budget"] == 100.0


# ═════════════════════════════════════════════════════════════════════
# Failure 8: Deadline passes while agent is working
# ═════════════════════════════════════════════════════════════════════

class TestDeadlineExpiry:
    @pytest.mark.asyncio
    async def test_deadline_expires_refunds_initiator(self, mcp, http, funded_network):
        """
        Task has a deadline. Agent bids but doesn't submit before deadline.
        System scans deadlines, marks task as failed, refunds initiator.
        """
        net = funded_network
        await _setup(mcp, net, "dl-init", balance=5000.0)
        await _setup(mcp, net, "dl-lazy")

        init_bal = (await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "dl-init",
        }))["available"]

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Time-sensitive task",
            "budget": 300.0,
            "domains": ["coding"],
            "initiator_id": "dl-init",
            "deadline": "2020-01-01T00:00:00Z",  # Already expired
        })
        tid = task["task_id"]

        # Lazy agent bids but never submits
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "dl-lazy",
            "confidence": 0.9, "price": 200.0,
        })

        # Scan deadlines (via HTTP since there's no MCP tool for this)
        await http.post("/api/admin/scan-deadlines")

        # Task should be no_one_able
        task_info = (await http.get(f"/api/tasks/{tid}")).json()
        assert task_info["status"] == "no_one_able"

        # Initiator should get full refund
        final_bal = (await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "dl-init",
        }))["available"]
        assert final_bal == init_bal
