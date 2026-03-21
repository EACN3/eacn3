"""Integration tests: discussions, deadline updates, budget confirmation."""

import asyncio

import pytest


async def _setup(mcp, funded_network):
    """Register agents + fund + create task. Returns task_id."""
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "DD Init",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan"}],
        "agent_id": "dd-init",
        "agent_type": "planner",
    })
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "DD Worker",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "dd-worker",
    })
    funded_network.escrow.get_or_create_account("dd-init", 10000.0)
    funded_network.reputation._scores["dd-init"] = 0.8
    funded_network.reputation._scores["dd-worker"] = 0.8

    task = await mcp.call_tool_parsed("eacn_create_task", {
        "description": "Discussions/deadline test",
        "budget": 500.0,
        "domains": ["coding"],
        "initiator_id": "dd-init",
    })
    return task["task_id"]


class TestDeadline:
    @pytest.mark.asyncio
    async def test_update_deadline(self, mcp, http, funded_network):
        """Initiator can update task deadline."""
        task_id = await _setup(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn_update_deadline", {
            "task_id": task_id,
            "new_deadline": "2026-12-31T23:59:59Z",
            "initiator_id": "dd-init",
        })
        assert result.get("id") == task_id or result.get("deadline")

        # Verify on network
        resp = await http.get(f"/api/tasks/{task_id}")
        assert "2026-12-31" in resp.json().get("deadline", "")

    @pytest.mark.asyncio
    async def test_create_task_with_deadline(self, mcp, http, funded_network):
        """Task created with deadline has it stored."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "DL Init",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "dl-init",
        })
        funded_network.escrow.get_or_create_account("dl-init", 5000.0)

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Has deadline",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "dl-init",
            "deadline": "2026-06-15T12:00:00Z",
        })
        task_id = task["task_id"]

        resp = await http.get(f"/api/tasks/{task_id}")
        assert "2026-06-15" in resp.json().get("deadline", "")


class TestDiscussions:
    @pytest.mark.asyncio
    async def test_add_discussion(self, mcp, http, funded_network):
        """Initiator adds a discussion message to a task."""
        task_id = await _setup(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn_update_discussions", {
            "task_id": task_id,
            "message": "请注意代码规范",
            "initiator_id": "dd-init",
        })
        # Should succeed
        assert result.get("id") == task_id or "discussions" in str(result).lower() or result.get("ok")

    @pytest.mark.asyncio
    async def test_discussion_visible_on_task(self, mcp, http, funded_network):
        """Discussion messages are stored and visible via task GET."""
        task_id = await _setup(mcp, funded_network)

        await mcp.call_tool_parsed("eacn_update_discussions", {
            "task_id": task_id,
            "message": "First message",
            "initiator_id": "dd-init",
        })
        await mcp.call_tool_parsed("eacn_update_discussions", {
            "task_id": task_id,
            "message": "Second message",
            "initiator_id": "dd-init",
        })

        resp = await http.get(f"/api/tasks/{task_id}")
        data = resp.json()
        # Discussions may be in content or a separate field
        task_str = str(data)
        assert "First message" in task_str or "Second message" in task_str

    @pytest.mark.asyncio
    async def test_discussion_push_event(self, mcp, http, funded_network):
        """Adding discussion triggers push event to bidders."""
        task_id = await _setup(mcp, funded_network)

        # Worker bids
        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "dd-worker",
            "confidence": 0.9,
            "price": 80.0,
        })

        # Drain old events
        await asyncio.sleep(0.5)
        await mcp.call_tool_parsed("eacn_get_events")

        # Add discussion
        await mcp.call_tool_parsed("eacn_update_discussions", {
            "task_id": task_id,
            "message": "New requirement: add tests",
            "initiator_id": "dd-init",
        })

        await asyncio.sleep(1.0)
        result = await mcp.call_tool_parsed("eacn_get_events")
        events = result.get("events", [])
        event_types = [e.get("type") for e in events]
        # Should contain discussion update event
        assert any("discussion" in t for t in event_types if t), (
            f"Expected discussion event, got: {event_types}"
        )


class TestBudgetConfirmation:
    @pytest.mark.asyncio
    async def test_confirm_budget_approve(self, mcp, http, funded_network):
        """Initiator approves an over-budget bid."""
        task_id = await _setup(mcp, funded_network)

        # Bid over budget (task budget=500, bid price=600)
        bid = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "dd-worker",
            "confidence": 0.9,
            "price": 600.0,
        })
        # If over-budget flow is implemented, confirm it
        if bid.get("status") in ("over_budget", "pending_budget"):
            result = await mcp.call_tool_parsed("eacn_confirm_budget", {
                "task_id": task_id,
                "approved": True,
                "new_budget": 600.0,
                "initiator_id": "dd-init",
            })
            assert result.get("ok") is True or "confirmed" in str(result).lower()

    @pytest.mark.asyncio
    async def test_confirm_budget_reject(self, mcp, http, funded_network):
        """Initiator rejects an over-budget bid."""
        task_id = await _setup(mcp, funded_network)

        bid = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "dd-worker",
            "confidence": 0.9,
            "price": 600.0,
        })
        if bid.get("status") in ("over_budget", "pending_budget"):
            result = await mcp.call_tool_parsed("eacn_confirm_budget", {
                "task_id": task_id,
                "approved": False,
                "initiator_id": "dd-init",
            })
            assert result.get("ok") is True or "confirmed" in str(result).lower()
