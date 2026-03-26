"""Discovery + Agent registration workflow tests.

Tests server/agent lifecycle: register → discover → update → unregister,
plus edge cases in the real usage patterns.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.api.routes import router as net_router, set_network
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network


def _make_app(network: Network) -> FastAPI:
    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    set_network(network)
    set_discovery_network(network)
    return app


@pytest.fixture
async def client():
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    app = _make_app(net)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await db.close()


class TestServerLifecycle:
    @pytest.mark.asyncio
    async def test_register_server(self, client):
        resp = await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["server_id"].startswith("srv-")
        assert data["token"] != ""  # Should include auth token

    @pytest.mark.asyncio
    async def test_get_server(self, client):
        reg = (await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
        })).json()
        resp = await client.get(f"/api/discovery/servers/{reg['server_id']}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "online"

    @pytest.mark.asyncio
    async def test_server_heartbeat(self, client):
        reg = (await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
        })).json()
        resp = await client.post(f"/api/discovery/servers/{reg['server_id']}/heartbeat")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unregister_server(self, client):
        reg = (await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
        })).json()
        resp = await client.delete(f"/api/discovery/servers/{reg['server_id']}")
        assert resp.status_code == 200

        # Should be gone now
        resp2 = await client.get(f"/api/discovery/servers/{reg['server_id']}")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_endpoint_protocol_rejected(self, client):
        """Server endpoint must use http/https (#103)."""
        resp = await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "javascript:alert(1)", "owner": "test",
        })
        assert resp.status_code == 422


class TestAgentLifecycle:
    async def _register_server(self, client) -> str:
        resp = await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
        })
        return resp.json()["server_id"]

    @pytest.mark.asyncio
    async def test_register_agent(self, client):
        sid = await self._register_server(client)
        resp = await client.post("/api/discovery/agents", json={
            "agent_id": "agent-1", "name": "Test Agent",
            "domains": ["coding"], "skills": [{"name": "code", "description": "coding"}],
            "url": "http://localhost:9000", "server_id": sid,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["agent_id"] == "agent-1"
        assert data["token"] != ""

    @pytest.mark.asyncio
    async def test_get_agent(self, client):
        sid = await self._register_server(client)
        await client.post("/api/discovery/agents", json={
            "agent_id": "agent-2", "name": "Test Agent",
            "domains": ["coding"], "skills": [{"name": "code", "description": "coding"}],
            "url": "http://localhost:9000", "server_id": sid,
        })
        resp = await client.get("/api/discovery/agents/agent-2")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Agent"

    @pytest.mark.asyncio
    async def test_update_agent_domains(self, client):
        sid = await self._register_server(client)
        await client.post("/api/discovery/agents", json={
            "agent_id": "agent-3", "name": "Test Agent",
            "domains": ["coding"], "skills": [{"name": "code", "description": "coding"}],
            "url": "http://localhost:9000", "server_id": sid,
        })
        # Update domains
        resp = await client.put("/api/discovery/agents/agent-3", json={
            "domains": ["coding", "design"],
        })
        assert resp.status_code == 200

        # Verify
        card = (await client.get("/api/discovery/agents/agent-3")).json()
        assert set(card["domains"]) == {"coding", "design"}

    @pytest.mark.asyncio
    async def test_unregister_agent(self, client):
        sid = await self._register_server(client)
        await client.post("/api/discovery/agents", json={
            "agent_id": "agent-del", "name": "Test Agent",
            "domains": ["coding"], "skills": [{"name": "code", "description": "coding"}],
            "url": "http://localhost:9000", "server_id": sid,
        })
        resp = await client.delete("/api/discovery/agents/agent-del")
        assert resp.status_code == 200

        resp2 = await client.get("/api/discovery/agents/agent-del")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_cascade_delete_agents_on_server_unregister(self, client):
        """Unregistering server should cascade-delete its agents."""
        sid = await self._register_server(client)
        for i in range(3):
            await client.post("/api/discovery/agents", json={
                "agent_id": f"cascade-{i}", "name": f"Agent {i}",
                "domains": ["coding"],
                "skills": [{"name": "code", "description": "coding"}],
                "url": f"http://localhost:900{i}", "server_id": sid,
            })

        # Delete server
        await client.delete(f"/api/discovery/servers/{sid}")

        # All agents should be gone
        for i in range(3):
            resp = await client.get(f"/api/discovery/agents/cascade-{i}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_register_agent_with_invalid_server(self, client):
        """Agent registration with non-existent server fails."""
        resp = await client.post("/api/discovery/agents", json={
            "agent_id": "orphan-1", "name": "Orphan",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "coding"}],
            "url": "http://localhost:9000", "server_id": "nonexistent-server",
        })
        assert resp.status_code == 400


class TestDiscoveryQuery:
    @pytest.mark.asyncio
    async def test_discover_by_domain(self, client):
        sid = await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
        })
        sid = sid.json()["server_id"]

        await client.post("/api/discovery/agents", json={
            "agent_id": "disc-1", "name": "Agent 1", "domains": ["python"],
            "skills": [{"name": "py", "description": "python"}],
            "url": "http://localhost:9000", "server_id": sid,
        })
        await client.post("/api/discovery/agents", json={
            "agent_id": "disc-2", "name": "Agent 2", "domains": ["python", "rust"],
            "skills": [{"name": "py", "description": "python"}],
            "url": "http://localhost:9001", "server_id": sid,
        })

        resp = await client.get("/api/discovery/query", params={"domain": "python"})
        assert resp.status_code == 200
        data = resp.json()
        assert "disc-1" in data["agent_ids"]
        assert "disc-2" in data["agent_ids"]

    @pytest.mark.asyncio
    async def test_list_agents_by_server(self, client):
        sid = (await client.post("/api/discovery/servers", json={
            "version": "1.0", "endpoint": "http://localhost:8000", "owner": "test",
        })).json()["server_id"]

        for i in range(3):
            await client.post("/api/discovery/agents", json={
                "agent_id": f"list-{i}", "name": f"Agent {i}",
                "domains": ["coding"],
                "skills": [{"name": "code", "description": "coding"}],
                "url": f"http://localhost:900{i}", "server_id": sid,
            })

        resp = await client.get("/api/discovery/agents",
                               params={"server_id": sid})
        assert resp.status_code == 200
        assert len(resp.json()) == 3
