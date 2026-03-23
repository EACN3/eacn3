"""Integration tests: discussions, deadline updates, budget confirmation."""

import asyncio

import pytest


async def _setup(mcp, funded_network):
    """Register agents + fund + create task in bidding state. Returns task_id."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "DD Init",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan"}],
        "agent_id": "dd-init",
    })
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "DD Worker",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "dd-worker",
    })
    funded_network.escrow.get_or_create_account("dd-init", 10000.0)
    funded_network.reputation._scores["dd-init"] = 0.8
    funded_network.reputation._scores["dd-worker"] = 0.8

    task = await mcp.call_tool_parsed("eacn3_create_task", {
        "description": "Discussions/deadline test",
        "budget": 500.0,
        "domains": ["coding"],
        "initiator_id": "dd-init",
    })
    return task["task_id"]


class TestDeadline:
    @pytest.mark.asyncio
    async def test_update_deadline_value(self, mcp, http, funded_network):
        """Initiator updates deadline, exact value persisted on network."""
        task_id = await _setup(mcp, funded_network)

        result = await mcp.call_tool_parsed("eacn3_update_deadline", {
            "task_id": task_id,
            "new_deadline": "2026-12-31T23:59:59Z",
            "initiator_id": "dd-init",
        })
        # Plugin returns Task object
        assert result["id"] == task_id
        assert result["deadline"] == "2026-12-31T23:59:59Z"

        # Verify on network
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["deadline"] == "2026-12-31T23:59:59Z"

    @pytest.mark.asyncio
    async def test_create_task_with_deadline(self, mcp, http, funded_network):
        """Task created with deadline has it stored."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "DL Init",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "dl-init",
        })
        funded_network.escrow.get_or_create_account("dl-init", 5000.0)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Has deadline",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "dl-init",
            "deadline": "2026-06-15T12:00:00Z",
        })
        task_id = task["task_id"]

        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["deadline"] == "2026-06-15T12:00:00Z"

    @pytest.mark.asyncio
    async def test_scan_expired_deadline(self, http, funded_network):
        """Task with past deadline is marked expired by scan."""
        funded_network.escrow.get_or_create_account("scan-init", 5000.0)

        resp = await http.post("/api/tasks", json={
            "task_id": "expired-dl-task",
            "initiator_id": "scan-init",
            "content": {"description": "Will expire"},
            "domains": ["coding"],
            "budget": 50.0,
            "deadline": "2020-01-01T00:00:00Z",
        })
        assert resp.status_code == 201

        # Scan deadlines
        scan = await http.post("/api/admin/scan-deadlines")
        assert scan.status_code == 200
        assert "expired-dl-task" in scan.json()["expired"]

        # Task should now be NO_ONE_ABLE (no results)
        resp = await http.get("/api/tasks/expired-dl-task")
        assert resp.json()["status"] == "no_one_able"


class TestDiscussions:
    @pytest.mark.asyncio
    async def test_add_discussion_stored_in_content(self, mcp, http, funded_network):
        """Discussion messages are stored in task.content.discussions."""
        task_id = await _setup(mcp, funded_network)

        # Need task in bidding state for discussions
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "dd-worker",
            "confidence": 0.9, "price": 80.0,
        })

        result = await mcp.call_tool_parsed("eacn3_update_discussions", {
            "task_id": task_id,
            "message": "Please follow code conventions",
            "initiator_id": "dd-init",
        })
        # Plugin returns Task object
        assert result["id"] == task_id

        # Verify via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        content = resp.json()["content"]
        discussions = content.get("discussions", [])
        assert len(discussions) >= 1
        assert discussions[0]["message"] == "Please follow code conventions"

    @pytest.mark.asyncio
    async def test_multiple_discussions_ordered(self, mcp, http, funded_network):
        """Multiple discussions are appended in order."""
        task_id = await _setup(mcp, funded_network)

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "dd-worker",
            "confidence": 0.9, "price": 80.0,
        })

        await mcp.call_tool_parsed("eacn3_update_discussions", {
            "task_id": task_id, "message": "First", "initiator_id": "dd-init",
        })
        await mcp.call_tool_parsed("eacn3_update_discussions", {
            "task_id": task_id, "message": "Second", "initiator_id": "dd-init",
        })

        resp = await http.get(f"/api/tasks/{task_id}")
        discussions = resp.json()["content"]["discussions"]
        assert len(discussions) == 2
        assert discussions[0]["message"] == "First"
        assert discussions[1]["message"] == "Second"

    @pytest.mark.asyncio
    async def test_discussion_push_event_received(self, mcp, http, funded_network):
        """Adding discussion pushes event to bidder's event buffer."""
        task_id = await _setup(mcp, funded_network)

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "dd-worker",
            "confidence": 0.9, "price": 80.0,
        })

        # Drain old events
        await asyncio.sleep(0.5)
        await mcp.call_tool_parsed("eacn3_get_events")

        # Add discussion
        await mcp.call_tool_parsed("eacn3_update_discussions", {
            "task_id": task_id,
            "message": "New requirement: add tests",
            "initiator_id": "dd-init",
        })

        await asyncio.sleep(1.0)
        result = await mcp.call_tool_parsed("eacn3_get_events")
        events = result["events"]
        event_types = [e["type"] for e in events]
        assert any("discussion" in t for t in event_types), (
            f"Expected discussion event, got types: {event_types}"
        )
