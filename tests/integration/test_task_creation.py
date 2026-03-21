"""Integration tests: task creation edge cases and options."""

import pytest


async def _setup_agent(mcp, funded_network, agent_id="tc-init", balance=10000.0):
    """Register agent and fund account."""
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "TC Agent",
        "description": "test",
        "domains": ["coding", "design"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": agent_id,
    })
    funded_network.escrow.get_or_create_account(agent_id, balance)
    funded_network.reputation._scores[agent_id] = 0.8


class TestTaskCreationOptions:
    @pytest.mark.asyncio
    async def test_create_task_with_max_concurrent(self, mcp, http, funded_network):
        """Task with max_concurrent_bidders limits concurrent slots."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Concurrent test",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
            "max_concurrent_bidders": 3,
        })
        task_id = task["task_id"]

        resp = await http.get(f"/api/tasks/{task_id}")
        data = resp.json()
        assert data["max_concurrent_bidders"] == 3

    @pytest.mark.asyncio
    async def test_create_task_with_expected_output(self, mcp, http, funded_network):
        """Task with expected_output metadata in content."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Output format test",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
            "expected_output": "JSON object with 'result' key",
        })
        task_id = task["task_id"]
        assert task_id

    @pytest.mark.asyncio
    async def test_create_task_multiple_domains(self, mcp, http, funded_network):
        """Task spanning multiple domains."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Multi-domain task",
            "budget": 200.0,
            "domains": ["coding", "design"],
            "initiator_id": "tc-init",
        })
        task_id = task["task_id"]

        resp = await http.get(f"/api/tasks/{task_id}")
        data = resp.json()
        assert "coding" in data["domains"]
        assert "design" in data["domains"]

    @pytest.mark.asyncio
    async def test_create_task_with_human_contact_disabled(self, mcp, http, funded_network):
        """Task with human_contact explicitly disabled."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "No human contact",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
            "human_contact": {"allowed": False},
        })
        task_id = task["task_id"]

        resp = await http.get(f"/api/tasks/{task_id}")
        data = resp.json()
        hc = data.get("human_contact")
        if hc:
            assert hc["allowed"] is False

    @pytest.mark.asyncio
    async def test_create_task_zero_budget(self, mcp, funded_network):
        """Task with zero budget (free task)."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Free task",
            "budget": 0.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        # Should either succeed or return a specific error — not crash
        assert "task_id" in task or "error" in task


class TestSubtaskCreation:
    @pytest.mark.asyncio
    async def test_subtask_budget_from_parent(self, mcp, http, funded_network):
        """Subtask budget is carved from parent's escrow."""
        await _setup_agent(mcp, funded_network)
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Sub Worker",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "sub-worker",
        })
        funded_network.reputation._scores["sub-worker"] = 0.8

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Parent for subtask budget test",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        parent_id = task["task_id"]

        # Worker bids on parent
        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": parent_id,
            "agent_id": "sub-worker",
            "confidence": 0.9,
            "price": 400.0,
        })

        # Worker creates subtask
        sub = await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Subtask budget test",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "sub-worker",
        })
        sub_id = sub.get("subtask_id") or sub.get("id") or sub.get("task_id")
        assert sub_id

    @pytest.mark.asyncio
    async def test_subtask_exceeding_parent_budget(self, mcp, http, funded_network):
        """Subtask with budget exceeding parent's remaining should fail."""
        await _setup_agent(mcp, funded_network)
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Over Sub Worker",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "over-sub",
        })
        funded_network.reputation._scores["over-sub"] = 0.8

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Parent for over-budget subtask",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        parent_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": parent_id,
            "agent_id": "over-sub",
            "confidence": 0.9,
            "price": 80.0,
        })

        # Try subtask with more budget than parent has
        sub = await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Over-budget subtask",
            "budget": 9999.0,
            "domains": ["coding"],
            "initiator_id": "over-sub",
        })
        err = sub.get("error") or sub.get("raw", "")
        assert err, f"Expected error for over-budget subtask, got: {sub}"

    @pytest.mark.asyncio
    async def test_subtask_depth_tracking(self, mcp, http, funded_network):
        """Subtask has depth = parent.depth + 1."""
        await _setup_agent(mcp, funded_network)
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Depth Worker",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "depth-worker",
        })
        funded_network.reputation._scores["depth-worker"] = 0.8

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Depth parent",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        parent_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": parent_id,
            "agent_id": "depth-worker",
            "confidence": 0.9,
            "price": 400.0,
        })

        sub = await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Depth child",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "depth-worker",
        })
        sub_id = sub.get("subtask_id") or sub.get("id") or sub.get("task_id")

        # Verify depth on network
        resp = await http.get(f"/api/tasks/{sub_id}")
        assert resp.status_code == 200
        assert resp.json()["depth"] == 1
        assert resp.json()["parent_id"] == parent_id
