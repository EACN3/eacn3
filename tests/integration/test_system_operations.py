"""Cluster and health endpoint E2E tests via MCP plugin.

Tests system-level operations:
- Health check
- Cluster status query
- Server info after multiple agent registrations
"""

import pytest
from tests.integration.conftest import seed_reputation


async def _reg(mcp, net, aid, *, balance=0.0, domains=None):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {aid}", "description": "test",
        "domains": domains or ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": aid,
    })
    seed_reputation(net, aid)
    if balance > 0:
        net.escrow.get_or_create_account(aid, balance)


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_via_mcp(self, mcp):
        """Health check via MCP tool."""
        result = await mcp.call_tool_parsed("eacn3_health")
        assert result.get("status") == "ok"

    @pytest.mark.asyncio
    async def test_health_via_http(self, http):
        """Health check via direct HTTP."""
        resp = await http.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestClusterStatus:
    @pytest.mark.asyncio
    async def test_cluster_status_via_mcp(self, mcp):
        """Cluster status shows standalone mode."""
        result = await mcp.call_tool_parsed("eacn3_cluster_status")
        assert result["mode"] == "standalone"
        assert "local" in result


class TestServerAfterRegistrations:
    @pytest.mark.asyncio
    async def test_server_tracks_multiple_agents(self, mcp, http, funded_network):
        """After registering 3 agents, server info reflects all of them."""
        net = funded_network
        for i in range(3):
            await _reg(mcp, net, f"track-{i}", domains=["coding"])

        info = await mcp.call_tool_parsed("eacn3_server_info")
        assert info["agents_count"] >= 3

        # All should be discoverable
        for i in range(3):
            resp = await http.get(f"/api/discovery/agents/track-{i}")
            assert resp.status_code == 200
