"""Messaging and event delivery E2E tests via MCP plugin.

Tests direct messaging between agents and event delivery:
- Send message between two agents
- Event buffer drain and ordering
- Events after task operations
"""

import asyncio
import pytest
from tests.integration.conftest import seed_reputation


async def _reg(mcp, net, aid, *, balance=0.0):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {aid}", "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": aid,
    })
    seed_reputation(net, aid)
    if balance > 0:
        net.escrow.get_or_create_account(aid, balance)


class TestEventAfterBid:
    @pytest.mark.asyncio
    async def test_bid_result_in_server_queue(self, mcp, http, funded_network):
        """After bidding, bid_result event appears in server's message queue."""
        net = funded_network
        await _reg(mcp, net, "ev-init", balance=5000.0)
        await _reg(mcp, net, "ev-bidder")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Event test task",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "ev-init",
        })
        tid = task["task_id"]

        # Clear existing events
        await http.get("/api/events/ev-bidder", params={"timeout": 0})

        # Bid
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "ev-bidder",
            "confidence": 0.9, "price": 100.0,
        })

        # Check server queue
        resp = await http.get("/api/events/ev-bidder", params={"timeout": 0})
        events = resp.json()["events"]
        types = [e["type"] for e in events]
        assert "bid_result" in types, f"Expected bid_result, got: {types}"


class TestEventAfterClose:
    @pytest.mark.asyncio
    async def test_close_produces_collected_event(self, mcp, http, funded_network):
        """Closing task with results produces task_collected event for initiator."""
        net = funded_network
        await _reg(mcp, net, "cl-init", balance=5000.0)
        await _reg(mcp, net, "cl-worker")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Close event test",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "cl-init",
        })
        tid = task["task_id"]

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "cl-worker",
            "confidence": 0.9, "price": 100.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "cl-worker",
            "content": {"done": True},
        })

        # Clear events
        await http.get("/api/events/cl-init", params={"timeout": 0})

        # Close
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": tid, "initiator_id": "cl-init",
        })

        # Initiator should get task_collected
        resp = await http.get("/api/events/cl-init", params={"timeout": 0})
        events = resp.json()["events"]
        types = [e["type"] for e in events]
        assert "task_collected" in types


class TestPluginEventBuffer:
    @pytest.mark.asyncio
    async def test_get_events_returns_and_clears(self, mcp, http, funded_network):
        """eacn3_get_events returns buffered events and clears them."""
        # Get events (may have some from connect/register)
        r1 = await mcp.call_tool_parsed("eacn3_get_events")

        # Second call should be empty (buffer cleared)
        r2 = await mcp.call_tool_parsed("eacn3_get_events")
        assert r2["count"] == 0
        assert r2["events"] == []
