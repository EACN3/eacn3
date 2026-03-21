"""Integration tests: event buffer (drain, empty, ordering, count)."""

import asyncio

import pytest


class TestEventBuffer:
    @pytest.mark.asyncio
    async def test_empty_buffer_returns_zero_count(self, mcp):
        """Draining when no events returns count=0 and empty events list."""
        # Drain any existing events from connect
        await mcp.call_tool_parsed("eacn_get_events")

        # Second drain should be definitively empty
        result = await mcp.call_tool_parsed("eacn_get_events")
        assert result["count"] == 0
        assert result["events"] == []

    @pytest.mark.asyncio
    async def test_drain_clears_buffer(self, mcp, http, funded_network):
        """After draining, second drain returns 0 events."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Drain Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "drain-agent",
        })
        funded_network.escrow.get_or_create_account("drain-init", 5000.0)

        await asyncio.sleep(0.5)

        # Generate a task broadcast event
        resp = await http.post("/api/tasks", json={
            "task_id": "drain-test-task",
            "initiator_id": "drain-init",
            "content": {"description": "drain test"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201
        await asyncio.sleep(1.0)

        # First drain — should have events
        result1 = await mcp.call_tool_parsed("eacn_get_events")
        assert result1["count"] > 0
        assert len(result1["events"]) == result1["count"]

        # Second drain — buffer cleared
        result2 = await mcp.call_tool_parsed("eacn_get_events")
        assert result2["count"] == 0
        assert result2["events"] == []

    @pytest.mark.asyncio
    async def test_event_has_required_fields(self, mcp, http, funded_network):
        """Each event has type, task_id, payload, received_at."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Field Test",
            "description": "test",
            "domains": ["ev-domain"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "ev-field-agent",
        })
        funded_network.escrow.get_or_create_account("ev-field-init", 5000.0)
        await asyncio.sleep(0.5)
        await mcp.call_tool_parsed("eacn_get_events")  # drain

        resp = await http.post("/api/tasks", json={
            "task_id": "ev-field-task",
            "initiator_id": "ev-field-init",
            "content": {"description": "field test"},
            "domains": ["ev-domain"],
            "budget": 50.0,
        })
        assert resp.status_code == 201
        await asyncio.sleep(1.0)

        result = await mcp.call_tool_parsed("eacn_get_events")
        assert result["count"] >= 1
        event = result["events"][0]
        assert "type" in event
        assert "task_id" in event
        assert "payload" in event
        assert "received_at" in event
        assert isinstance(event["received_at"], (int, float))

    @pytest.mark.asyncio
    async def test_count_matches_events_length(self, mcp, http, funded_network):
        """count field exactly equals len(events)."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Count Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "count-agent",
        })
        funded_network.escrow.get_or_create_account("count-init", 5000.0)
        await asyncio.sleep(0.5)
        await mcp.call_tool_parsed("eacn_get_events")  # drain

        # Generate 2 events
        for i in range(2):
            resp = await http.post("/api/tasks", json={
                "task_id": f"count-task-{i}",
                "initiator_id": "count-init",
                "content": {"description": f"count test {i}"},
                "domains": ["coding"],
                "budget": 10.0,
            })
            assert resp.status_code == 201
            await asyncio.sleep(0.3)

        await asyncio.sleep(1.0)
        result = await mcp.call_tool_parsed("eacn_get_events")
        assert result["count"] == len(result["events"])
        assert result["count"] >= 2
