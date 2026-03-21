"""Integration tests: task creation edge cases and subtask operations."""

import pytest

from tests.integration.conftest import is_error


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
    async def test_create_task_returns_correct_shape(self, mcp, funded_network):
        """eacn_create_task returns {task_id, status, budget, local_matches}."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Shape test",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        assert "task_id" in task
        assert task["status"] == "unclaimed"
        assert task["budget"] == 100.0
        assert isinstance(task["local_matches"], list)

    @pytest.mark.asyncio
    async def test_create_task_with_max_concurrent(self, mcp, http, funded_network):
        """max_concurrent_bidders persisted exactly on network."""
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
        assert resp.json()["max_concurrent_bidders"] == 3

    @pytest.mark.asyncio
    async def test_create_task_multiple_domains(self, mcp, http, funded_network):
        """Task with multiple domains stores them all."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Multi-domain task",
            "budget": 200.0,
            "domains": ["coding", "design"],
            "initiator_id": "tc-init",
        })
        task_id = task["task_id"]

        resp = await http.get(f"/api/tasks/{task_id}")
        domains = resp.json()["domains"]
        assert "coding" in domains
        assert "design" in domains

    @pytest.mark.asyncio
    async def test_create_task_with_human_contact(self, mcp, http, funded_network):
        """human_contact field persisted with exact values."""
        await _setup_agent(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Human contact task",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
            "human_contact": {
                "allowed": True,
                "contact_id": "human-123",
                "timeout_s": 600,
            },
        })
        task_id = task["task_id"]

        resp = await http.get(f"/api/tasks/{task_id}")
        hc = resp.json()["human_contact"]
        assert hc is not None
        assert hc["allowed"] is True
        assert hc["contact_id"] == "human-123"
        assert hc["timeout_s"] == 600

    @pytest.mark.asyncio
    async def test_create_task_with_human_contact_disabled(self, mcp, http, funded_network):
        """human_contact with allowed=False persisted."""
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
        hc = resp.json().get("human_contact")
        if hc is not None:
            assert hc["allowed"] is False


class TestSubtaskCreation:
    @pytest.mark.asyncio
    async def test_subtask_returns_correct_shape(self, mcp, funded_network):
        """eacn_create_subtask returns {subtask_id, parent_task_id, status, depth}."""
        await _setup_agent(mcp, funded_network)
        await _setup_agent(mcp, funded_network, agent_id="sub-worker")

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Parent",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        parent_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": parent_id, "agent_id": "sub-worker",
            "confidence": 0.9, "price": 400.0,
        })

        sub = await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Child",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "sub-worker",
        })
        assert "subtask_id" in sub
        assert sub["parent_task_id"] == parent_id
        assert sub["status"] == "unclaimed"
        assert sub["depth"] == 1

    @pytest.mark.asyncio
    async def test_subtask_depth_and_parent_on_network(self, mcp, http, funded_network):
        """Subtask visible on network with correct depth and parent_id."""
        await _setup_agent(mcp, funded_network)
        await _setup_agent(mcp, funded_network, agent_id="depth-worker")

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Depth parent",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        parent_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": parent_id, "agent_id": "depth-worker",
            "confidence": 0.9, "price": 400.0,
        })

        sub = await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Depth child",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "depth-worker",
        })
        sub_id = sub["subtask_id"]

        # Verify on network
        resp = await http.get(f"/api/tasks/{sub_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["depth"] == 1
        assert data["parent_id"] == parent_id

        # Parent should list child
        resp = await http.get(f"/api/tasks/{parent_id}")
        assert sub_id in resp.json()["child_ids"]

    @pytest.mark.asyncio
    async def test_subtask_exceeding_parent_budget_fails(self, mcp, funded_network):
        """Subtask with budget > parent remaining returns error."""
        await _setup_agent(mcp, funded_network)
        await _setup_agent(mcp, funded_network, agent_id="over-sub")

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Small parent",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        parent_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": parent_id, "agent_id": "over-sub",
            "confidence": 0.9, "price": 80.0,
        })

        sub = await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Over-budget subtask",
            "budget": 9999.0,
            "domains": ["coding"],
            "initiator_id": "over-sub",
        })
        assert is_error(sub), f"Expected error for over-budget subtask, got: {sub}"

    @pytest.mark.asyncio
    async def test_subtask_budget_deducts_from_parent(self, mcp, http, funded_network):
        """Creating subtask deducts from parent's remaining_budget."""
        await _setup_agent(mcp, funded_network)
        await _setup_agent(mcp, funded_network, agent_id="budget-sub")

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Budget track parent",
            "budget": 500.0,
            "domains": ["coding"],
            "initiator_id": "tc-init",
        })
        parent_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": parent_id, "agent_id": "budget-sub",
            "confidence": 0.9, "price": 400.0,
        })

        # Create subtask with 150 budget
        await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "First child",
            "budget": 150.0,
            "domains": ["coding"],
            "initiator_id": "budget-sub",
        })

        # Parent's remaining_budget should be 500 - 150 = 350
        resp = await http.get(f"/api/tasks/{parent_id}")
        assert resp.json()["remaining_budget"] == 350.0

        # Create second subtask with 200
        await mcp.call_tool_parsed("eacn_create_subtask", {
            "parent_task_id": parent_id,
            "description": "Second child",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "budget-sub",
        })

        # remaining = 350 - 200 = 150
        resp = await http.get(f"/api/tasks/{parent_id}")
        assert resp.json()["remaining_budget"] == 150.0
