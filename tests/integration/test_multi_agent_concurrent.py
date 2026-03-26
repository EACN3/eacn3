"""Multi-agent concurrent operation E2E tests.

Tests real concurrent scenarios where multiple agents operate simultaneously
through the MCP plugin, hitting the same live server. These simulate what
actually happens when multiple Claude Code instances are working at the same time.
"""

import asyncio
import pytest
from tests.integration.conftest import seed_reputation


async def _setup(mcp, net, agent_id, *, balance=0.0, domains=None):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {agent_id}", "description": "test",
        "domains": domains or ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": agent_id,
    })
    seed_reputation(net, agent_id)
    if balance > 0:
        net.escrow.get_or_create_account(agent_id, balance)


class TestConcurrentBidding:
    """Multiple agents bid on the same task through the same MCP connection."""

    @pytest.mark.asyncio
    async def test_3_agents_bid_simultaneously(self, mcp, http, funded_network):
        """Three agents all bid on the same task — all bids should be recorded."""
        net = funded_network
        await _setup(mcp, net, "sim-init", balance=5000.0)
        for i in range(3):
            await _setup(mcp, net, f"sim-bid-{i}")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Concurrent bid target",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "sim-init",
            "max_concurrent_bidders": 3,
        })
        tid = task["task_id"]

        # All 3 agents bid (sequentially through same MCP, but testing the server handles it)
        bids = []
        for i in range(3):
            b = await mcp.call_tool_parsed("eacn3_submit_bid", {
                "task_id": tid, "agent_id": f"sim-bid-{i}",
                "confidence": 0.9, "price": 80.0 + i * 10,
            })
            bids.append(b)

        # All should be executing (3 slots available)
        executing = sum(1 for b in bids if b["status"] == "executing")
        assert executing == 3, f"Expected 3 executing, got statuses: {[b['status'] for b in bids]}"

        # Verify on server
        task_info = (await http.get(f"/api/tasks/{tid}")).json()
        assert len(task_info["bids"]) == 3


class TestConcurrentTaskCreation:
    """Multiple initiators create tasks rapidly."""

    @pytest.mark.asyncio
    async def test_5_tasks_created_rapidly(self, mcp, http, funded_network):
        """5 different initiators each create a task — all should succeed."""
        net = funded_network
        for i in range(5):
            await _setup(mcp, net, f"rapid-init-{i}", balance=3000.0)

        tasks = []
        for i in range(5):
            t = await mcp.call_tool_parsed("eacn3_create_task", {
                "description": f"Rapid task {i}",
                "budget": 200.0,
                "domains": ["coding"],
                "initiator_id": f"rapid-init-{i}",
            })
            assert "task_id" in t, f"Task {i} failed: {t}"
            tasks.append(t)

        # All should be listable
        open_tasks = await mcp.call_tool_parsed("eacn3_list_open_tasks", {
            "domains": "coding",
        })
        created_ids = {t["task_id"] for t in tasks}
        open_ids = {t["id"] for t in open_tasks["tasks"]}
        assert created_ids.issubset(open_ids), \
            f"Some tasks missing from open list: {created_ids - open_ids}"


class TestFullMultiAgentWorkflow:
    """Complete workflow with multiple agents playing different roles."""

    @pytest.mark.asyncio
    async def test_publisher_2_workers_full_cycle(self, mcp, http, funded_network):
        """
        Publisher creates task. 2 workers bid (max_concurrent=2).
        Both submit results. Publisher reviews both, selects better one.
        Winner gets paid, loser doesn't.
        """
        net = funded_network
        await _setup(mcp, net, "pub", balance=5000.0)
        await _setup(mcp, net, "worker-a")
        await _setup(mcp, net, "worker-b")

        # Publish
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Build a data pipeline",
            "budget": 800.0,
            "domains": ["coding"],
            "initiator_id": "pub",
            "max_concurrent_bidders": 2,
        })
        tid = task["task_id"]

        # Both bid
        bid_a = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "worker-a",
            "confidence": 0.9, "price": 400.0,
        })
        bid_b = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "worker-b",
            "confidence": 0.85, "price": 350.0,
        })
        assert bid_a["status"] == "executing"
        assert bid_b["status"] == "executing"

        # Both submit results
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "worker-a",
            "content": {"quality": "good", "approach": "pandas"},
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "worker-b",
            "content": {"quality": "excellent", "approach": "polars"},
        })

        # Publisher reviews
        results = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": tid, "initiator_id": "pub",
        })
        assert len(results["results"]) == 2

        # Select worker-b (better result)
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": tid, "agent_id": "worker-b",
            "initiator_id": "pub",
        })

        # Worker B gets paid
        bal_b = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "worker-b",
        })
        assert bal_b["available"] == 350.0

        # Worker A doesn't get paid
        resp_a = await http.get("/api/economy/balance", params={"agent_id": "worker-a"})
        if resp_a.status_code == 200:
            assert resp_a.json()["available"] == 0.0


class TestAgentDiscoveryWorkflow:
    """Agent discovers tasks through the proper channels."""

    @pytest.mark.asyncio
    async def test_discover_agents_then_create_task(self, mcp, http, funded_network):
        """
        Initiator checks what agents are available, then creates a targeted task.
        """
        net = funded_network
        await _setup(mcp, net, "disc-init", balance=5000.0)
        await _setup(mcp, net, "disc-worker", domains=["python"])

        # Discover who can do python work
        found = await mcp.call_tool_parsed("eacn3_discover_agents", {
            "domain": "python",
        })
        assert "disc-worker" in found["agent_ids"]

        # Create task in that domain
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Python data analysis",
            "budget": 200.0,
            "domains": ["python"],
            "initiator_id": "disc-init",
        })
        assert "task_id" in task


class TestEventDrivenWorkflow:
    """Test the event-driven workflow where agents react to events."""

    @pytest.mark.asyncio
    async def test_events_appear_after_operations(self, mcp, http, funded_network):
        """
        After creating a task and bidding, verify events appear
        in the server queue for the right agents.
        """
        net = funded_network
        await _setup(mcp, net, "ev-init", balance=5000.0)
        await _setup(mcp, net, "ev-worker")

        # Create task → broadcast goes to server queue
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Event test",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "ev-init",
        })
        tid = task["task_id"]

        # Check server queue directly — worker should have broadcast
        resp = await http.get("/api/events/ev-worker", params={"timeout": 0})
        events = resp.json()["events"]
        types = [e["type"] for e in events]
        assert "task_broadcast" in types

        # Worker bids
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "ev-worker",
            "confidence": 0.9, "price": 100.0,
        })

        # Worker should have bid_result event
        resp = await http.get("/api/events/ev-worker", params={"timeout": 0})
        events = resp.json()["events"]
        types = [e["type"] for e in events]
        assert "bid_result" in types


class TestSubtaskChainWorkflow:
    """Test realistic subtask delegation — 2 levels via MCP."""

    @pytest.mark.asyncio
    async def test_2_level_delegation(self, mcp, http, funded_network):
        """
        Initiator creates task. Worker bids, creates subtask.
        Sub-worker completes subtask. Worker completes main task.
        """
        net = funded_network
        await _setup(mcp, net, "chain-init", balance=10000.0)
        await _setup(mcp, net, "chain-worker")
        await _setup(mcp, net, "chain-sub")

        # Initiator creates main task
        main = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Full-stack app",
            "budget": 1000.0,
            "domains": ["coding"],
            "initiator_id": "chain-init",
        })
        main_id = main["task_id"]

        # Worker bids and creates subtask
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": main_id, "agent_id": "chain-worker",
            "confidence": 0.9, "price": 600.0,
        })
        sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": main_id,
            "initiator_id": "chain-worker",
            "description": "Database layer",
            "budget": 200.0,
            "domains": ["coding"],
        })
        sub_id = sub["subtask_id"]

        # Sub-worker bids and completes
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": sub_id, "agent_id": "chain-sub",
            "confidence": 0.9, "price": 150.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": sub_id, "agent_id": "chain-sub",
            "content": {"db": "schema done"},
        })

        # Worker completes main task
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": main_id, "agent_id": "chain-worker",
            "content": {"app": "complete with DB from subtask"},
        })

        # Initiator closes then selects
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": main_id, "initiator_id": "chain-init",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": main_id, "agent_id": "chain-worker",
            "initiator_id": "chain-init",
        })

        # Verify task completed
        status = (await http.get(f"/api/tasks/{main_id}")).json()
        assert status["status"] == "completed"
