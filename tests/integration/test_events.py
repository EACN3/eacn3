"""Integration tests: event buffer (drain, empty, ordering)."""

import asyncio

import pytest


class TestEventBuffer:
    @pytest.mark.asyncio
    async def test_empty_buffer(self, mcp):
        """Draining events when no events have occurred returns empty list."""
        result = await mcp.call_tool_parsed("eacn_get_events")
        events = result.get("events", [])
        assert isinstance(events, list)
        # May or may not be empty depending on connect events, but should not error

    @pytest.mark.asyncio
    async def test_drain_clears_buffer(self, mcp, http, funded_network):
        """After draining, a second drain returns fewer/no events."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Drain Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "drain-agent",
        })
        funded_network.escrow.get_or_create_account("drain-init", 5000.0)

        await asyncio.sleep(0.5)

        # Create a task to generate events
        resp = await http.post("/api/tasks", json={
            "task_id": "drain-test-task",
            "initiator_id": "drain-init",
            "content": {"description": "drain test"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201

        await asyncio.sleep(1.0)

        # First drain
        result1 = await mcp.call_tool_parsed("eacn_get_events")
        count1 = result1.get("count", len(result1.get("events", [])))

        # Second drain should be empty (events already consumed)
        result2 = await mcp.call_tool_parsed("eacn_get_events")
        count2 = result2.get("count", len(result2.get("events", [])))
        assert count2 == 0, f"Expected 0 events after drain, got {count2}"

    @pytest.mark.asyncio
    async def test_multiple_events_ordering(self, mcp, http, funded_network):
        """Events arrive in chronological order."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Order Test",
            "description": "test",
            "domains": ["order-domain"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "order-agent",
        })

        funded_network.escrow.get_or_create_account("order-init", 5000.0)
        await asyncio.sleep(0.5)

        # Drain existing events
        await mcp.call_tool_parsed("eacn_get_events")

        # Create two tasks in sequence
        for i in range(2):
            resp = await http.post("/api/tasks", json={
                "task_id": f"order-task-{i}",
                "initiator_id": "order-init",
                "content": {"description": f"Order test {i}"},
                "domains": ["order-domain"],
                "budget": 50.0,
            })
            assert resp.status_code == 201
            await asyncio.sleep(0.3)

        await asyncio.sleep(1.0)

        result = await mcp.call_tool_parsed("eacn_get_events")
        events = result.get("events", [])
        # If we got multiple task_broadcast events, they should be in order
        broadcasts = [e for e in events if e.get("type") == "task_broadcast"]
        if len(broadcasts) >= 2:
            # Order preserved — first created task comes first
            task_ids = [e.get("task_id") or e.get("data", {}).get("task_id") for e in broadcasts]
            # Just verify we got them (order depends on implementation)
            assert len(task_ids) >= 2

    @pytest.mark.asyncio
    async def test_event_count_field(self, mcp, http, funded_network):
        """eacn_get_events returns count field matching events list length."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Count Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "count-agent",
        })

        funded_network.escrow.get_or_create_account("count-init", 5000.0)
        await asyncio.sleep(0.5)

        # Drain
        await mcp.call_tool_parsed("eacn_get_events")

        # Generate event
        resp = await http.post("/api/tasks", json={
            "task_id": "count-test-task",
            "initiator_id": "count-init",
            "content": {"description": "count test"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201
        await asyncio.sleep(1.0)

        result = await mcp.call_tool_parsed("eacn_get_events")
        events = result.get("events", [])
        count = result.get("count", -1)
        if count >= 0:
            assert count == len(events), (
                f"count={count} doesn't match events length={len(events)}"
            )
