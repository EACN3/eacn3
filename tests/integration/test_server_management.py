"""Integration tests: server management (disconnect, cascade, info)."""

import pytest


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_removes_server(self, mcp, http):
        """After disconnect, server returns 404 on network."""
        info = await mcp.call_tool_parsed("eacn3_server_info")
        sid = info["server_card"]["server_id"]

        # Verify exists
        resp = await http.get(f"/api/discovery/servers/{sid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "online"

        # Disconnect
        result = await mcp.call_tool_parsed("eacn3_disconnect")
        assert result["disconnected"] is True

        # Server should be gone
        resp = await http.get(f"/api/discovery/servers/{sid}")
        assert resp.status_code == 404


class TestServerInfo:
    @pytest.mark.asyncio
    async def test_server_info_structure(self, mcp):
        """eacn3_server_info returns complete server state."""
        result = await mcp.call_tool_parsed("eacn3_server_info")
        # Verify exact structure
        assert "server_card" in result
        card = result["server_card"]
        assert card["server_id"].startswith("srv-")
        assert card["status"] == "online"
        assert "network_endpoint" in result
        assert isinstance(result["agents_count"], int)
        assert isinstance(result["agents"], list)
        assert isinstance(result["tasks_count"], int)


class TestDisconnectCascade:
    @pytest.mark.asyncio
    async def test_disconnect_cascades_to_agents(self, mcp, http):
        """Disconnecting a server removes all its agents from network."""
        # Register agent
        reg = await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Cascade Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "cascade-agent",
        })
        assert reg["registered"] is True

        # Verify agent exists on network
        resp = await http.get("/api/discovery/agents/cascade-agent")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Cascade Test"

        # Disconnect server
        result = await mcp.call_tool_parsed("eacn3_disconnect")
        assert result["disconnected"] is True

        # Agent should be cascade-deleted
        resp = await http.get("/api/discovery/agents/cascade-agent")
        assert resp.status_code == 404
