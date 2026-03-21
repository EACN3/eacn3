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
        # Plugin returns {updated: true, agent_id, ok, message}
        assert result["updated"] is True
        assert result["agent_id"] == "upd-name"
        assert result["ok"] is True

        # Verify the actual value changed on network
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
        result = await mcp.call_tool_parsed("eacn_update_agent", {
            "agent_id": "domain-swap",
            "domains": ["beta"],
        })
        assert result["updated"] is True

        # Verify REMOVED from "alpha" DHT
        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "alpha"})
        assert "domain-swap" not in disc["agent_ids"]

        # Verify ADDED to "beta" DHT
        disc = await mcp.call_tool_parsed("eacn_discover_agents", {"domain": "beta"})
        assert "domain-swap" in disc["agent_ids"]

        # Verify network card updated
        resp = await http.get("/api/discovery/agents/domain-swap")
        assert resp.json()["domains"] == ["beta"]

    @pytest.mark.asyncio
    async def test_update_agent_skills(self, mcp, http):
        """Update agent skills, verify old replaced and new present on network."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Skill Update",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "old-skill", "description": "old"}],
            "agent_id": "skill-upd",
        })

        result = await mcp.call_tool_parsed("eacn_update_agent", {
            "agent_id": "skill-upd",
            "skills": [
                {"name": "new-skill-1", "description": "new 1"},
                {"name": "new-skill-2", "description": "new 2"},
            ],
        })
        assert result["updated"] is True

        resp = await http.get("/api/discovery/agents/skill-upd")
        assert resp.status_code == 200
        skill_names = [s["name"] for s in resp.json()["skills"]]
        assert skill_names == ["new-skill-1", "new-skill-2"]


class TestUnregisterAgent:
    @pytest.mark.asyncio
    async def test_unregister_removes_from_network(self, mcp, http):
        """Unregistered agent should return 404 on network."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "To Remove",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "to-remove",
        })
        resp = await http.get("/api/discovery/agents/to-remove")
        assert resp.status_code == 200

        result = await mcp.call_tool_parsed("eacn_unregister_agent", {
            "agent_id": "to-remove",
        })
        # Plugin returns {unregistered: true, agent_id, ok, message}
        assert result["unregistered"] is True
        assert result["agent_id"] == "to-remove"
        assert result["ok"] is True

        # Network should 404
        resp = await http.get("/api/discovery/agents/to-remove")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_unregister_removes_from_dht(self, mcp):
        """Unregistered agent disappears from domain discovery."""
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
        assert disc["domain"] == "nonexistent-domain-xyz"
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
        assert disc["domain"] == "multi-domain"
        assert len(disc["agent_ids"]) >= 3
        for i in range(3):
            assert f"multi-{i}" in disc["agent_ids"]


class TestListAgents:
    @pytest.mark.asyncio
    async def test_list_my_agents(self, mcp):
        """list_my_agents returns exact agent cards under this server."""
        reg = await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "My Agent",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "list-mine",
        })
        assert reg["registered"] is True

        result = await mcp.call_tool_parsed("eacn_list_my_agents")
        # Plugin returns {count, agents: [{agent_id, name, domains, ws_connected}, ...]}
        assert result["count"] >= 1
        agent_ids = [a["agent_id"] for a in result["agents"]]
        assert "list-mine" in agent_ids
        # Verify agent entry has expected fields
        agent = next(a for a in result["agents"] if a["agent_id"] == "list-mine")
        assert agent["name"] == "My Agent"
        assert "coding" in agent["domains"]

    @pytest.mark.asyncio
    async def test_get_agent_full_card(self, mcp):
        """eacn_get_agent returns complete AgentCard with all fields."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Get Me",
            "description": "a description",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "writes code"}],
            "agent_id": "get-me",
        })
        result = await mcp.call_tool_parsed("eacn_get_agent", {"agent_id": "get-me"})
        assert result["agent_id"] == "get-me"
        assert result["name"] == "Get Me"
        assert result["description"] == "a description"
        assert "coding" in result["domains"]
        assert result["skills"][0]["name"] == "code"
        assert result["skills"][0]["description"] == "writes code"

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent_returns_error(self, mcp):
        """Getting a non-existent agent returns error with 404."""
        result = await mcp.call_tool_parsed("eacn_get_agent", {
            "agent_id": "does-not-exist",
        })
        assert "error" in result
        assert "404" in str(result["error"])
