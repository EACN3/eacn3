"""Tests: Discovery HTTP API — Server/Agent lifecycle + discovery queries.

Covers:
  POST /api/discovery/servers — register server
  GET /api/discovery/servers/{id} — get server card
  POST /api/discovery/servers/{id}/heartbeat — heartbeat
  DELETE /api/discovery/servers/{id} — unregister server (cascade)
  POST /api/discovery/agents — register agent
  GET /api/discovery/agents/{id} — get agent card
  PUT /api/discovery/agents/{id} — update agent
  DELETE /api/discovery/agents/{id} — unregister agent
  GET /api/discovery/query — domain-based discovery
  GET /api/discovery/agents?domain=... — list agents by domain
"""

import pytest

class TestServerRegistration:
    @pytest.mark.asyncio
    async def test_register_server(self, api):
        resp = await api.post("/api/discovery/servers", json={
            "version": "0.1.0", "endpoint": "http://srv1:8000", "owner": "alice",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["server_id"].startswith("srv-")
        assert data["status"] == "online"

    @pytest.mark.asyncio
    async def test_get_server_card(self, api):
        resp = await api.post("/api/discovery/servers", json={
            "version": "0.1.0", "endpoint": "http://srv1:8000", "owner": "alice",
        })
        sid = resp.json()["server_id"]

        resp2 = await api.get(f"/api/discovery/servers/{sid}")
        assert resp2.status_code == 200
        card = resp2.json()
        assert card["server_id"] == sid
        assert card["endpoint"] == "http://srv1:8000"
        assert card["owner"] == "alice"

    @pytest.mark.asyncio
    async def test_get_nonexistent_server_404(self, api):
        resp = await api.get("/api/discovery/servers/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_heartbeat(self, api):
        resp = await api.post("/api/discovery/servers", json={
            "version": "0.1.0", "endpoint": "http://srv1:8000", "owner": "alice",
        })
        sid = resp.json()["server_id"]

        resp2 = await api.post(f"/api/discovery/servers/{sid}/heartbeat")
        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_heartbeat_nonexistent_404(self, api):
        resp = await api.post("/api/discovery/servers/ghost/heartbeat")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_unregister_server(self, api):
        resp = await api.post("/api/discovery/servers", json={
            "version": "0.1.0", "endpoint": "http://srv1:8000", "owner": "alice",
        })
        sid = resp.json()["server_id"]

        resp2 = await api.delete(f"/api/discovery/servers/{sid}")
        assert resp2.status_code == 200

        # Should be gone
        resp3 = await api.get(f"/api/discovery/servers/{sid}")
        assert resp3.status_code == 404

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_404(self, api):
        resp = await api.delete("/api/discovery/servers/ghost")
        assert resp.status_code == 404

SAMPLE_SKILL = {"name": "translate", "description": "EN-CN translation"}

async def _register_server(api) -> str:
    resp = await api.post("/api/discovery/servers", json={
        "version": "0.1.0", "endpoint": "http://srv1:8000", "owner": "alice",
    })
    return resp.json()["server_id"]

class TestAgentRegistration:
    @pytest.mark.asyncio
    async def test_register_agent(self, api):
        sid = await _register_server(api)
        resp = await api.post("/api/discovery/agents", json={
            "agent_id": "agent1", "name": "Translator",
            "agent_type": "executor", "domains": ["translation"],
            "skills": [SAMPLE_SKILL], "url": "http://agent1:9000",
            "server_id": sid,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_id"] == "agent1"
        assert isinstance(data["seeds"], list)

    @pytest.mark.asyncio
    async def test_register_agent_invalid_server_400(self, api):
        resp = await api.post("/api/discovery/agents", json={
            "agent_id": "agent1", "name": "Test",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://a:9000",
            "server_id": "nonexistent",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_register_agent_invalid_type_422(self, api):
        sid = await _register_server(api)
        resp = await api.post("/api/discovery/agents", json={
            "agent_id": "agent1", "name": "Test",
            "agent_type": "invalid", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://a:9000",
            "server_id": sid,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_get_agent_card(self, api):
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "agent1", "name": "Translator",
            "agent_type": "executor", "domains": ["translation"],
            "skills": [SAMPLE_SKILL], "url": "http://agent1:9000",
            "server_id": sid,
        })

        resp = await api.get("/api/discovery/agents/agent1")
        assert resp.status_code == 200
        card = resp.json()
        assert card["agent_id"] == "agent1"
        assert card["domains"] == ["translation"]
        assert card["server_id"] == sid

    @pytest.mark.asyncio
    async def test_get_nonexistent_agent_404(self, api):
        resp = await api.get("/api/discovery/agents/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_agent(self, api):
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "agent1", "name": "Translator",
            "agent_type": "executor", "domains": ["translation"],
            "skills": [SAMPLE_SKILL], "url": "http://agent1:9000",
            "server_id": sid,
        })

        resp = await api.put("/api/discovery/agents/agent1", json={
            "domains": ["translation", "writing"],
            "description": "Updated description",
        })
        assert resp.status_code == 200

        card = (await api.get("/api/discovery/agents/agent1")).json()
        assert set(card["domains"]) == {"translation", "writing"}
        assert card["description"] == "Updated description"

    @pytest.mark.asyncio
    async def test_unregister_agent(self, api):
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "agent1", "name": "Translator",
            "agent_type": "executor", "domains": ["translation"],
            "skills": [SAMPLE_SKILL], "url": "http://agent1:9000",
            "server_id": sid,
        })

        resp = await api.delete("/api/discovery/agents/agent1")
        assert resp.status_code == 200

        resp2 = await api.get("/api/discovery/agents/agent1")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_seed_list_returns_same_domain_agents(self, api):
        """Register two agents in same domain — second should get first as seed."""
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "a1", "name": "Agent1",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://a1:9000",
            "server_id": sid,
        })
        resp = await api.post("/api/discovery/agents", json={
            "agent_id": "a2", "name": "Agent2",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://a2:9000",
            "server_id": sid,
        })
        data = resp.json()
        assert "a1" in data["seeds"]

class TestServerCascade:
    @pytest.mark.asyncio
    async def test_unregister_server_removes_agents_from_discovery(self, api):
        """Unregistering a server should remove its agents from DHT."""
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "agent1", "name": "A1",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://a1:9000",
            "server_id": sid,
        })

        # Agent should be discoverable
        resp = await api.get("/api/discovery/query", params={"domain": "coding"})
        assert "agent1" in resp.json()["agent_ids"]

        # Unregister server
        await api.delete(f"/api/discovery/servers/{sid}")

        # Agent should no longer be discoverable via DHT
        resp2 = await api.get("/api/discovery/query", params={"domain": "coding"})
        assert "agent1" not in resp2.json()["agent_ids"]

class TestDiscoveryQuery:
    @pytest.mark.asyncio
    async def test_discover_by_domain(self, api):
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "coder1", "name": "Coder",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://c1:9000",
            "server_id": sid,
        })

        resp = await api.get("/api/discovery/query", params={"domain": "coding"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["domain"] == "coding"
        assert "coder1" in data["agent_ids"]

    @pytest.mark.asyncio
    async def test_discover_empty_domain(self, api):
        resp = await api.get("/api/discovery/query", params={"domain": "nonexistent"})
        assert resp.status_code == 200
        assert resp.json()["agent_ids"] == []

    @pytest.mark.asyncio
    async def test_list_agents_by_domain(self, api):
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "coder1", "name": "Coder",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://c1:9000",
            "server_id": sid,
        })

        resp = await api.get("/api/discovery/agents", params={"domain": "coding"})
        assert resp.status_code == 200
        cards = resp.json()
        assert len(cards) == 1
        assert cards[0]["agent_id"] == "coder1"

    @pytest.mark.asyncio
    async def test_list_agents_by_server(self, api):
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "coder1", "name": "Coder",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://c1:9000",
            "server_id": sid,
        })

        resp = await api.get("/api/discovery/agents", params={"server_id": sid})
        assert resp.status_code == 200
        cards = resp.json()
        assert len(cards) == 1

    @pytest.mark.asyncio
    async def test_list_agents_no_filter_400(self, api):
        resp = await api.get("/api/discovery/agents")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_update_domains_re_announces_dht(self, api):
        """Updating agent domains should update DHT — old domain revoked, new announced."""
        sid = await _register_server(api)
        await api.post("/api/discovery/agents", json={
            "agent_id": "a1", "name": "A1",
            "agent_type": "executor", "domains": ["coding"],
            "skills": [SAMPLE_SKILL], "url": "http://a1:9000",
            "server_id": sid,
        })

        # discoverable under "coding"
        resp = await api.get("/api/discovery/query", params={"domain": "coding"})
        assert "a1" in resp.json()["agent_ids"]

        # Update domains: remove "coding", add "design"
        await api.put("/api/discovery/agents/a1", json={"domains": ["design"]})

        # No longer under "coding"
        resp2 = await api.get("/api/discovery/query", params={"domain": "coding"})
        assert "a1" not in resp2.json()["agent_ids"]

        # Now under "design"
        resp3 = await api.get("/api/discovery/query", params={"domain": "design"})
        assert "a1" in resp3.json()["agent_ids"]
