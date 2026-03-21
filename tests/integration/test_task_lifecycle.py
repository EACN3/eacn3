"""Integration tests: full task lifecycle through plugin → network.

Tests the complete flow: create → bid → result → close → collect → select.
"""

import pytest


async def _register_two_agents(mcp, funded_network):
    """Helper: register an initiator agent and an executor agent."""
    # Initiator
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "Initiator",
        "description": "Task initiator agent",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan tasks"}],
        "agent_id": "initiator",
        "agent_type": "planner",
    })
    # Executor
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "Executor",
        "description": "Task executor agent",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "write code"}],
        "agent_id": "executor",
    })
    # Seed reputation so bids pass ability gate (confidence × reputation ≥ 0.5)
    funded_network.reputation._scores["initiator"] = 0.8
    funded_network.reputation._scores["executor"] = 0.8


class TestTaskLifecycle:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self, mcp, http, funded_network):
        """create → bid → result → close → collect → select → completed."""
        await _register_two_agents(mcp, funded_network)

        # Fund the initiator (need budget to create tasks)
        funded_network.escrow.get_or_create_account("initiator", 5000.0)

        # 1. Create task
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Write a hello world program",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "initiator",
        })
        task_id = task["task_id"]
        assert task_id

        # 2. Verify task exists on network
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] in ("unclaimed", "bidding")

        # 3. Executor bids
        bid_result = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "executor",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert bid_result["status"] in ("accepted", "executing")

        # 4. Executor submits result
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": "executor",
            "content": {"code": "print('hello world')"},
        })

        # 5. Close task
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "initiator",
        })

        # 6. Collect results
        results = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": task_id,
            "initiator_id": "initiator",
        })
        assert len(results["results"]) >= 1

        # 7. Select result
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": task_id,
            "agent_id": "executor",
            "initiator_id": "initiator",
        })

        # 8. Verify completed on network
        resp = await http.get(f"/api/tasks/{task_id}")
        final = resp.json()
        selected = [r for r in final["results"] if r.get("selected")]
        assert len(selected) == 1

    @pytest.mark.asyncio
    async def test_human_contact_passthrough(self, mcp, http, funded_network):
        """human_contact set at creation should be visible in task response."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "HC-Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "hc-init",
        })
        funded_network.escrow.get_or_create_account("hc-init", 5000.0)
        funded_network.reputation._scores["hc-init"] = 0.8

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Task with human contact",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "hc-init",
            "human_contact": {
                "allowed": True,
                "contact_id": "human-owner-1",
                "timeout_s": 300,
            },
        })
        task_id = task["task_id"]

        # Verify via network HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        data = resp.json()
        hc = data.get("human_contact")
        assert hc is not None
        assert hc["allowed"] is True
        assert hc["contact_id"] == "human-owner-1"
        assert hc["timeout_s"] == 300

    @pytest.mark.asyncio
    async def test_subtask_lifecycle(self, mcp, http, funded_network):
        """Parent task → executor creates subtask → subtask bid+result."""
        await _register_two_agents(mcp, funded_network)
        funded_network.escrow.get_or_create_account("initiator", 5000.0)

        # Create parent
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Big project",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "initiator",
        })
        parent_id = task["task_id"]

        # Executor bids on parent
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": parent_id,
            "agent_id": "executor",
            "confidence": 0.9,
            "price": 400.0,
        })

        # Executor creates subtask
        sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Sub work",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "executor",
        })
        sub_id = sub.get("subtask_id") or sub.get("id") or sub.get("task_id")
        assert sub_id

        # Verify parent has child
        resp = await http.get(f"/api/tasks/{parent_id}")
        parent_data = resp.json()
        assert sub_id in parent_data["child_ids"]
