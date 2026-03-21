"""Integration tests: connection and agent registration.

Plugin → Network: connect, register agent, heartbeat, discover.
"""

import pytest


class TestConnect:
    @pytest.mark.asyncio
    async def test_connect_returns_server_id(self, mcp):
        """eacn3_connect registers a server — server_info shows it."""
        result = await mcp.call_tool_parsed("eacn3_server_info")
        assert result["server_card"]["server_id"].startswith("srv-")

    @pytest.mark.asyncio
    async def test_connect_server_visible_on_network(self, mcp, http):
        """After connect, the server is visible via network discovery API."""
        info = await mcp.call_tool_parsed("eacn3_server_info")
        sid = info["server_card"]["server_id"]
        resp = await http.get(f"/api/discovery/servers/{sid}")
        assert resp.status_code == 200
        card = resp.json()
        assert card["server_id"] == sid
        assert card["status"] == "online"

    @pytest.mark.asyncio
    async def test_heartbeat(self, mcp):
        """eacn3_heartbeat succeeds after connect."""
        result = await mcp.call_tool_parsed("eacn3_heartbeat")
        assert result.get("ok") is True or result.get("message") == "heartbeat ok"


class TestRegisterAgent:
    @pytest.mark.asyncio
    async def test_register_agent(self, mcp):
        """Register an agent — returns agent_id and seeds."""
        result = await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "测试翻译",
            "description": "中英互译",
            "domains": ["translation", "english"],
            "skills": [{"name": "translate", "description": "中英互译"}],
        })
        assert result["registered"] is True
        assert result["agent_id"]
        assert "translation" in result["domains"]

    @pytest.mark.asyncio
    async def test_registered_agent_visible_on_network(self, mcp, http):
        """After registration, agent card is visible via network discovery."""
        result = await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "网络可见测试",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "写代码"}],
            "agent_id": "visible-test",
        })
        aid = result["agent_id"]
        resp = await http.get(f"/api/discovery/agents/{aid}")
        assert resp.status_code == 200
        card = resp.json()
        assert card["name"] == "网络可见测试"
        assert "coding" in card["domains"]

    @pytest.mark.asyncio
    async def test_discover_registered_agent(self, mcp):
        """Register agent in domain, then discover should find it."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "发现测试",
            "description": "test",
            "domains": ["rare-domain"],
            "skills": [{"name": "rare", "description": "rare skill"}],
            "agent_id": "disc-test",
        })
        result = await mcp.call_tool_parsed("eacn3_discover_agents", {
            "domain": "rare-domain",
        })
        assert "disc-test" in result["agent_ids"]

    @pytest.mark.asyncio
    async def test_register_agent_custom_id(self, mcp):
        """Register agent with explicit agent_id."""
        result = await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "自定义ID",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "my-custom-agent",
        })
        assert result["agent_id"] == "my-custom-agent"
