"""Integration tests: agent management (update, unregister, list, discover)."""

import pytest


class TestUpdateAgent:
    @pytest.mark.asyncio
    async def test_update_agent_name(self, mcp, http):
        """Update agent name via plugin, verify on network."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Original Name",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "upd-name",
        })

        result = await mcp.call_tool_parsed("eacn_update_agent", {
            "agent_id": "upd-name",
            "name": "Updated Name",
        })
        assert result.get("ok") is True or "updated" in str(result).lower()

        # Verify on network
        resp = await http.get("/api/discovery/agents/upd-name")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_agent_domains(self, mcp, http):
        """Updating domains re-announces to DHT — old domain gone, new discoverable."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Domain Swap",
            "description": "test",
            "domains": ["alpha"],
            "skills": [{"name": "s", "description": "s"}],
            "agent_id": "domain-swap",
        })

        # Should be discoverable in "alpha"
        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "alpha"})
        assert "domain-swap" in disc["agent_ids"]

        # Update domains to "beta"
        await mcp.call_tool_parsed("eacn_update_agent", {
            "agent_id": "domain-swap",
            "domains": ["beta"],
        })

        # Should NOT be in "alpha" anymore
        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "alpha"})
        assert "domain-swap" not in disc["agent_ids"]

        # Should be in "beta"
        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "beta"})
        assert "domain-swap" in disc["agent_ids"]

    @pytest.mark.asyncio
    async def test_update_agent_skills(self, mcp, http):
        """Update agent skills, verify on network."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Skill Update",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "old-skill", "description": "old"}],
            "agent_id": "skill-upd",
        })

        await mcp.call_tool_parsed("eacn_update_agent", {
            "agent_id": "skill-upd",
            "skills": [
                {"name": "new-skill-1", "description": "new 1"},
                {"name": "new-skill-2", "description": "new 2"},
            ],
        })

        resp = await http.get("/api/discovery/agents/skill-upd")
        assert resp.status_code == 200
        skill_names = [s["name"] for s in resp.json()["skills"]]
        assert "new-skill-1" in skill_names
        assert "new-skill-2" in skill_names
        assert "old-skill" not in skill_names


class TestUnregisterAgent:
    @pytest.mark.asyncio
    async def test_unregister_removes_from_network(self, mcp, http):
        """Unregistered agent should not be findable on network."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "To Remove",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "to-remove",
        })
        # Verify exists
        resp = await http.get("/api/discovery/agents/to-remove")
        assert resp.status_code == 200

        # Unregister
        result = await mcp.call_tool_parsed("eacn_unregister_agent", {
            "agent_id": "to-remove",
        })
        assert result.get("ok") is True or "unregistered" in str(result).lower()

        # Verify gone
        resp = await http.get("/api/discovery/agents/to-remove")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_unregister_removes_from_discovery(self, mcp):
        """Unregistered agent should not appear in domain discovery."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Discoverable",
            "description": "test",
            "domains": ["unreg-domain"],
            "skills": [{"name": "s", "description": "s"}],
            "agent_id": "unreg-disc",
        })
        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "unreg-domain"})
        assert "unreg-disc" in disc["agent_ids"]

        await mcp.call_tool_parsed("eacn_unregister_agent", {"agent_id": "unreg-disc"})

        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "unreg-domain"})
        assert "unreg-disc" not in disc["agent_ids"]


class TestDiscoverAgents:
    @pytest.mark.asyncio
    async def test_discover_empty_domain(self, mcp):
        """Discovering in a domain with no agents returns empty list."""
        disc = await mcp.call_tool_parsed("eacn_discover_agents", {
            "domain": "nonexistent-domain-xyz",
        })
        assert disc["agent_ids"] == []

    @pytest.mark.asyncio
    async def test_discover_multiple_agents(self, mcp):
        """Multiple agents in same domain all appear in discovery."""
        for i in range(3):
            await mcp.call_tool_parsed("eacn_register_agent", {
                "name": f"Multi Agent {i}",
                "description": "test",
                "domains": ["multi-domain"],
                "skills": [{"name": "s", "description": "s"}],
                "agent_id": f"multi-{i}",
            })

        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "multi-domain"})
        for i in range(3):
            assert f"multi-{i}" in disc["agent_ids"]


class TestListAgents:
    @pytest.mark.asyncio
    async def test_list_my_agents(self, mcp):
        """list_my_agents returns agents registered under this server."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "My Agent",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "list-mine",
        })
        result = await mcp.call_tool_parsed("eacn_list_my_agents")
        agent_ids = [a["agent_id"] for a in result.get("agents", result.get("agent_ids", []))]
        # Could be nested differently, check flexible
        found = "list-mine" in str(result)
        assert found, f"Expected list-mine in result: {result}"

    @pytest.mark.asyncio
    async def test_get_agent(self, mcp, http):
        """eacn_get_agent retrieves a specific agent card."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Get Me",
            "description": "a description",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "get-me",
        })
        result = await mcp.call_tool_parsed("eacn_get_agent", {"agent_id": "get-me"})
        assert result["agent_id"] == "get-me"
        assert result["name"] == "Get Me"

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent(self, mcp):
        """Getting a non-existent agent returns error."""
        result = await mcp.call_tool_parsed("eacn_get_agent", {
            "agent_id": "does-not-exist",
        })
        err = result.get("error") or result.get("raw", "")
        assert "404" in str(err) or "not found" in str(err).lower()
