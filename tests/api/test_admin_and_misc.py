"""Tests for admin endpoints and miscellaneous API features.

Covers:
- /admin/offline-stats
- /admin/logs querying
- /admin/config get and update
- /cluster/status
- /health
- Admin auth enforcement (when configured)
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.offline_store import OfflineStore
from eacn3.network.api.routes import router as net_router, set_network, set_offline_store
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network

from tests.api.conftest import create_task, bid, submit_result


@pytest.fixture
async def env():
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    store = OfflineStore(db=db)

    import uuid
    async def queue_push(event):
        for aid in event.recipients:
            await store.store(uuid.uuid4().hex, aid, event.type.value, event.task_id, event.payload)
    net.push.set_handler(queue_push)

    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    set_network(net)
    set_discovery_network(net)
    set_offline_store(store)

    net.escrow.get_or_create_account("user1", 10000.0)
    for aid in ("a1", "a2"):
        await net.dht.announce("coding", aid)
        net.reputation._scores[aid] = 0.8

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "net": net, "store": store}
    await db.close()


class TestOfflineStats:
    @pytest.mark.asyncio
    async def test_offline_stats_empty(self, env):
        c = env["client"]
        resp = await c.get("/api/admin/offline-stats")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_offline_stats_after_events(self, env):
        c = env["client"]
        # Create task → broadcasts to a1, a2
        await c.post("/api/tasks", json={
            "task_id": "stats-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })

        resp = await c.get("/api/admin/offline-stats")
        data = resp.json()
        assert data["total"] >= 2  # At least a1 and a2 got broadcasts


class TestAdminLogs:
    @pytest.mark.asyncio
    async def test_query_logs(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "log-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })

        resp = await c.get("/api/admin/logs")
        assert resp.status_code == 200
        logs = resp.json()
        assert len(logs) >= 1

        # Should have a create_task entry
        fn_names = [l["fn_name"] for l in logs]
        assert "create_task" in fn_names

    @pytest.mark.asyncio
    async def test_query_logs_by_task_id(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "log-2", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })

        resp = await c.get("/api/admin/logs", params={"task_id": "log-2"})
        logs = resp.json()
        assert all(l["task_id"] == "log-2" for l in logs)


class TestConfigEndpoints:
    @pytest.mark.asyncio
    async def test_get_config(self, env):
        c = env["client"]
        resp = await c.get("/api/admin/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "reputation" in data
        assert "economy" in data
        assert "push" in data

    @pytest.mark.asyncio
    async def test_update_config(self, env):
        c = env["client"]
        resp = await c.put("/api/admin/config", json={
            "economy": {"platform_fee_rate": 0.03},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["economy"]["platform_fee_rate"] == 0.03

    @pytest.mark.asyncio
    async def test_update_config_invalid_key(self, env):
        c = env["client"]
        resp = await c.put("/api/admin/config", json={
            "nonexistent_section": {"foo": "bar"},
        })
        assert resp.status_code == 400


class TestClusterStatus:
    @pytest.mark.asyncio
    async def test_cluster_status_standalone(self, env):
        c = env["client"]
        resp = await c.get("/api/cluster/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "standalone"
        assert "local" in data
        assert data["local"]["node_id"] != ""


class TestMiscEndpoints:
    @pytest.mark.asyncio
    async def test_get_task_status_endpoint(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "misc-1", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })

        resp = await c.get("/api/tasks/misc-1/status", params={"agent_id": "user1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "misc-1"
        assert data["status"] == "unclaimed"
        assert data["initiator_id"] == "user1"

    @pytest.mark.asyncio
    async def test_get_task_status_wrong_initiator(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "misc-2", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        resp = await c.get("/api/tasks/misc-2/status", params={"agent_id": "wrong-user"})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_escrow_detail_query(self, env):
        c = env["client"]
        await c.post("/api/tasks", json={
            "task_id": "esc-detail", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 300.0,
        })
        resp = await c.get("/api/economy/escrows", params={"agent_id": "user1"})
        assert resp.status_code == 200
        data = resp.json()
        task_ids = [e["task_id"] for e in data["escrows"]]
        assert "esc-detail" in task_ids
