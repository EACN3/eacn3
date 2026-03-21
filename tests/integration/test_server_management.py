"""Integration tests: server management (disconnect, reconnect, info)."""

import pytest


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_removes_server(self, mcp, http):
        """After disconnect, server is no longer visible on network."""
        info = await mcp.call_tool_parsed("eacn_server_info")
        sid = info["server_card"]["server_id"]

        # Verify exists before disconnect
        resp = await http.get(f"/api/discovery/servers/{sid}")
        assert resp.status_code == 200

        # Disconnect
        result = await mcp.call_tool_parsed("eacn_disconnect")
        assert result.get("ok") is True or "disconnect" in str(result).lower()

        # Server should be gone
        resp = await http.get(f"/api/discovery/servers/{sid}")
        assert resp.status_code == 404


class TestServerInfo:
    @pytest.mark.asyncio
    async def test_server_info_has_expected_fields(self, mcp):
        """eacn_server_info returns server_card with expected structure."""
        result = await mcp.call_tool_parsed("eacn_server_info")
        card = result["server_card"]
        assert "server_id" in card
        assert card["server_id"].startswith("srv-")


class TestDisconnectCascade:
    @pytest.mark.asyncio
    async def test_disconnect_cascades_to_agents(self, mcp, http):
        """Disconnecting a server should also remove its agents."""
        # Register an agent first
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Cascade Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "cascade-agent",
        })

        # Verify agent exists
        resp = await http.get("/api/discovery/agents/cascade-agent")
        assert resp.status_code == 200

        # Disconnect server
        await mcp.call_tool_parsed("eacn_disconnect")

        # Agent should be gone too
        resp = await http.get("/api/discovery/agents/cascade-agent")
        assert resp.status_code == 404
