"""Task operations E2E tests — querying, filtering, status checks.

Tests the MCP tools agents actually use to find and manage tasks:
- List open tasks and filter by domain
- Get task details and status
- Query tasks by initiator
"""

import pytest
from tests.integration.conftest import seed_reputation


async def _reg(mcp, net, aid, *, balance=0.0, domains=None):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {aid}", "description": "test",
        "domains": domains or ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": aid,
    })
    seed_reputation(net, aid)
    if balance > 0:
        net.escrow.get_or_create_account(aid, balance)


class TestListAndFilterTasks:
    @pytest.mark.asyncio
    async def test_list_open_tasks_by_domain(self, mcp, http, funded_network):
        """Create tasks in different domains, filter by domain."""
        net = funded_network
        await _reg(mcp, net, "filter-init", balance=5000.0)

        # Create 2 coding + 1 design task
        for i in range(2):
            await mcp.call_tool_parsed("eacn3_create_task", {
                "description": f"Coding task {i}",
                "budget": 100.0,
                "domains": ["coding"],
                "initiator_id": "filter-init",
            })
        await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Design task",
            "budget": 100.0,
            "domains": ["design"],
            "initiator_id": "filter-init",
        })

        # Filter by coding
        coding = await mcp.call_tool_parsed("eacn3_list_open_tasks", {
            "domains": "coding",
        })
        # Should have at least 2 coding tasks
        coding_tasks = [t for t in coding["tasks"] if "coding" in t.get("domains", [])]
        assert len(coding_tasks) >= 2

    @pytest.mark.asyncio
    async def test_list_tasks_by_initiator(self, mcp, http, funded_network):
        """List tasks created by a specific initiator."""
        net = funded_network
        await _reg(mcp, net, "list-init", balance=5000.0)

        # Create tasks
        for i in range(3):
            await mcp.call_tool_parsed("eacn3_create_task", {
                "description": f"My task {i}",
                "budget": 50.0,
                "domains": ["coding"],
                "initiator_id": "list-init",
            })

        # List via HTTP
        resp = await http.get("/api/tasks", params={
            "initiator_id": "list-init",
        })
        assert resp.status_code == 200
        tasks = resp.json()
        assert all(t["initiator_id"] == "list-init" for t in tasks)
        assert len(tasks) >= 3


class TestGetTaskDetails:
    @pytest.mark.asyncio
    async def test_get_task_shows_bids(self, mcp, http, funded_network):
        """Get task details includes bid information."""
        net = funded_network
        await _reg(mcp, net, "det-init", balance=5000.0)
        await _reg(mcp, net, "det-worker")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Detailed task",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "det-init",
        })
        tid = task["task_id"]

        # Worker bids
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "det-worker",
            "confidence": 0.9, "price": 100.0,
        })

        # Get task via MCP
        detail = await mcp.call_tool_parsed("eacn3_get_task", {
            "task_id": tid,
        })
        assert detail["id"] == tid
        assert detail["status"] == "bidding"
        assert len(detail["bids"]) == 1
        assert detail["bids"][0]["agent_id"] == "det-worker"

    @pytest.mark.asyncio
    async def test_get_task_status_initiator_only(self, mcp, http, funded_network):
        """Task status is only accessible to the initiator."""
        net = funded_network
        await _reg(mcp, net, "stat-init", balance=5000.0)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Status task",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "stat-init",
        })
        tid = task["task_id"]

        # Initiator can check status
        status = await mcp.call_tool_parsed("eacn3_get_task_status", {
            "task_id": tid,
            "agent_id": "stat-init",
        })
        assert status["id"] == tid
        assert status["status"] == "unclaimed"

        # Non-initiator gets 403 (via HTTP)
        resp = await http.get(f"/api/tasks/{tid}/status",
                             params={"agent_id": "imposter"})
        assert resp.status_code == 403


class TestTaskWithHumanContact:
    @pytest.mark.asyncio
    async def test_human_contact_passthrough(self, mcp, http, funded_network):
        """Task with human_contact field is created and accessible."""
        net = funded_network
        await _reg(mcp, net, "hc-init", balance=5000.0)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Task needing human help",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "hc-init",
            "human_contact": {
                "allowed": True,
                "contact_id": "human-john",
                "timeout_s": 300,
            },
        })
        tid = task["task_id"]

        # Verify via HTTP
        resp = await http.get(f"/api/tasks/{tid}")
        data = resp.json()
        assert data["human_contact"]["allowed"] is True
        assert data["human_contact"]["contact_id"] == "human-john"
