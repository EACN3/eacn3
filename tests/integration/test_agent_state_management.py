"""Agent state management E2E tests.

Tests how agent state is managed across operations:
- Agent registers, checks its own info
- Agent updates its profile (domains, skills)
- Agent queries its task history
- Multiple operations on the same agent's state
- Server info reflects registered agents
"""

import pytest
from tests.integration.conftest import seed_reputation


async def _reg(mcp, net, agent_id, *, domains=None, balance=0.0, tier="general"):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {agent_id}", "description": f"Agent {agent_id}",
        "domains": domains or ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": agent_id, "tier": tier,
    })
    seed_reputation(net, agent_id)
    if balance > 0:
        net.escrow.get_or_create_account(agent_id, balance)


class TestAgentSelfInfo:
    @pytest.mark.asyncio
    async def test_agent_sees_own_card_via_http(self, mcp, http, funded_network):
        """After registering, agent card is visible via HTTP API."""
        await _reg(mcp, funded_network, "self-info-1")

        resp = await http.get("/api/discovery/agents/self-info-1")
        assert resp.status_code == 200
        card = resp.json()
        assert card["agent_id"] == "self-info-1"
        assert card["name"] == "Agent self-info-1"
        assert "coding" in card["domains"]


class TestAgentProfileUpdate:
    @pytest.mark.asyncio
    async def test_update_agent_domains(self, mcp, http, funded_network):
        """Agent updates its domains — now discoverable under new domain."""
        await _reg(mcp, funded_network, "update-1")

        # Update domains
        await mcp.call_tool_parsed("eacn3_update_agent", {
            "agent_id": "update-1",
            "domains": ["coding", "data-science"],
        })

        # Should be discoverable under new domain
        found = await mcp.call_tool_parsed("eacn3_discover_agents", {
            "domain": "data-science",
        })
        assert "update-1" in found["agent_ids"]

    @pytest.mark.asyncio
    async def test_update_agent_name(self, mcp, http, funded_network):
        """Agent updates its display name."""
        await _reg(mcp, funded_network, "update-2")

        await mcp.call_tool_parsed("eacn3_update_agent", {
            "agent_id": "update-2",
            "name": "Super Coder v2",
        })

        card = (await http.get("/api/discovery/agents/update-2")).json()
        assert card["name"] == "Super Coder v2"


class TestServerInfoReflectsAgents:
    @pytest.mark.asyncio
    async def test_server_info_counts_agents(self, mcp, http, funded_network):
        """Server info shows correct agent count after registration."""
        await _reg(mcp, funded_network, "count-1")
        await _reg(mcp, funded_network, "count-2")

        info = await mcp.call_tool_parsed("eacn3_server_info")
        assert info["agents_count"] >= 2

    @pytest.mark.asyncio
    async def test_server_info_lists_agents(self, mcp, http, funded_network):
        """Server info lists registered agent IDs."""
        await _reg(mcp, funded_network, "list-1")

        info = await mcp.call_tool_parsed("eacn3_server_info")
        # agents may be list of dicts or list of strings depending on plugin version
        agents = info.get("agents", [])
        if agents and isinstance(agents[0], dict):
            agent_ids = [a["agent_id"] for a in agents]
        else:
            agent_ids = [str(a) for a in agents]
        assert "list-1" in agent_ids


class TestAgentUnregistration:
    @pytest.mark.asyncio
    async def test_unregister_removes_from_discovery(self, mcp, http, funded_network):
        """After unregistering, agent is no longer discoverable."""
        await _reg(mcp, funded_network, "unreg-1")

        # Verify discoverable
        found = await mcp.call_tool_parsed("eacn3_discover_agents", {
            "domain": "coding",
        })
        assert "unreg-1" in found["agent_ids"]

        # Unregister
        await mcp.call_tool_parsed("eacn3_unregister_agent", {
            "agent_id": "unreg-1",
        })

        # No longer discoverable
        found2 = await mcp.call_tool_parsed("eacn3_discover_agents", {
            "domain": "coding",
        })
        assert "unreg-1" not in found2["agent_ids"]

        # Card should be gone
        resp = await http.get("/api/discovery/agents/unreg-1")
        assert resp.status_code == 404


class TestMultiDomainAgent:
    @pytest.mark.asyncio
    async def test_agent_with_multiple_domains(self, mcp, http, funded_network):
        """Agent registered with multiple domains is discoverable under each."""
        await _reg(mcp, funded_network, "multi-dom",
                   domains=["python", "rust", "go"])

        for domain in ["python", "rust", "go"]:
            found = await mcp.call_tool_parsed("eacn3_discover_agents", {
                "domain": domain,
            })
            assert "multi-dom" in found["agent_ids"], \
                f"Agent not found under domain '{domain}'"


class TestAgentWithTier:
    @pytest.mark.asyncio
    async def test_tool_tier_agent_registration(self, mcp, http, funded_network):
        """Tool-tier agent registers correctly."""
        await _reg(mcp, funded_network, "tool-tier-1", tier="tool")

        card = (await http.get("/api/discovery/agents/tool-tier-1")).json()
        assert card["tier"] == "tool"

    @pytest.mark.asyncio
    async def test_expert_tier_agent(self, mcp, http, funded_network):
        """Expert-tier agent registers correctly."""
        await _reg(mcp, funded_network, "expert-1", tier="expert")

        card = (await http.get("/api/discovery/agents/expert-1")).json()
        assert card["tier"] == "expert"
