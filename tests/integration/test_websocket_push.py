"""Integration tests: WebSocket push events.

Verifies that events pushed by the network reach the plugin's event buffer.
"""

import asyncio

import pytest


class TestWebSocketPush:
    @pytest.mark.asyncio
    async def test_task_broadcast_event(self, mcp, http, funded_network):
        """Register agent → create task (via HTTP) → plugin receives task_broadcast."""
        # Register an agent in the "coding" domain via plugin
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "WS Listener",
            "description": "listens for broadcasts",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "ws-listener",
        })

        # Give time for WebSocket to connect
        await asyncio.sleep(0.5)

        # Create a task via HTTP (simulating another server's agent)
        funded_network.escrow.get_or_create_account("external-init", 5000.0)
        resp = await http.post("/api/tasks", json={
            "task_id": "ws-test-task",
            "initiator_id": "external-init",
            "content": {"description": "WS broadcast test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Give time for the push event to arrive
        await asyncio.sleep(1.0)

        # Drain events from plugin
        result = await mcp.call_tool_parsed("eacn_get_events")
        events = result.get("events", [])
        event_types = [e.get("type") for e in events]
        assert "task_broadcast" in event_types, (
            f"Expected task_broadcast in events, got: {event_types}"
        )

    @pytest.mark.asyncio
    async def test_discussions_updated_event(self, mcp, http, funded_network):
        """Executor receives discussions_updated when initiator adds a message."""
        # Register executor
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Disc Listener",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "disc-exec",
        })
        funded_network.reputation._scores["disc-exec"] = 0.8

        await asyncio.sleep(0.5)

        # Create task + bid (via HTTP, simulating the flow)
        funded_network.escrow.get_or_create_account("disc-init", 5000.0)
        resp = await http.post("/api/tasks", json={
            "task_id": "disc-task",
            "initiator_id": "disc-init",
            "content": {"description": "Discussion test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Executor bids
        resp = await http.post("/api/tasks/disc-task/bid", json={
            "agent_id": "disc-exec",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert resp.status_code == 200

        # Drain events so far (clear task_broadcast)
        await asyncio.sleep(0.5)
        await mcp.call_tool_parsed("eacn_get_events")

        # Initiator adds discussion
        resp = await http.post("/api/tasks/disc-task/discussions", json={
            "initiator_id": "disc-init",
            "message": "请注意代码风格",
        })
        assert resp.status_code == 200

        # Wait for push
        await asyncio.sleep(1.0)

        result = await mcp.call_tool_parsed("eacn_get_events")
        events = result.get("events", [])
        event_types = [e.get("type") for e in events]
        assert "discussions_updated" in event_types or "discussion_update" in event_types, (
            f"Expected discussions_updated or discussion_update, got: {event_types}"
        )
