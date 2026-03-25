"""Integration tests: cluster layer with Network orchestration."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.api.routes import router as net_router, set_network
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn3.network.api.peer_routes import peer_router, set_peer_cluster, set_peer_network


@pytest.fixture
async def integrated_client():
    """Full integration: Network + Cluster, funded, with API."""
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    await net.start()

    # Fund
    net.escrow.get_or_create_account("user1", 10_000.0)
    net.escrow.get_or_create_account("user2", 5_000.0)
    for aid in ("a1", "a2", "a3"):
        await net.dht.announce("coding", aid)
    await net.dht.announce("design", "a4")
    net.reputation._scores.update({"a1": 0.8, "a2": 0.75, "a3": 0.7, "a4": 0.65})

    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    app.include_router(peer_router)
    set_network(net)
    set_discovery_network(net)
    set_peer_cluster(net.cluster)
    set_peer_network(net)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, net

    await db.close()


class TestNetworkWithCluster:
    async def test_create_task_with_cluster(self, integrated_client):
        client, net = integrated_client
        resp = await client.post("/api/tasks", json={
            "task_id": "ct1",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "ct1"
        assert data["status"] == "unclaimed"
        assert data["initiator_id"] == "user1"
        assert data["domains"] == ["coding"]
        assert data["budget"] == 100.0

    async def test_full_task_lifecycle(self, integrated_client):
        client, net = integrated_client

        # 1. Create
        resp = await client.post("/api/tasks", json={
            "task_id": "lifecycle-1",
            "initiator_id": "user1",
            "content": {"desc": "full lifecycle test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "unclaimed"

        # 2. Bid
        resp = await client.post("/api/tasks/lifecycle-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"
        assert resp.json()["agent_id"] == "a1"

        # 3. Submit result
        resp = await client.post("/api/tasks/lifecycle-1/result", json={
            "agent_id": "a1", "content": "done",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify result is stored
        task = net.task_manager.get("lifecycle-1")
        assert len(task.results) == 1
        assert task.results[0].content == "done"

        # 4. Close
        resp = await client.post("/api/tasks/lifecycle-1/close", json={
            "initiator_id": "user1",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "awaiting_retrieval"

        # 5. Select result
        resp = await client.post("/api/tasks/lifecycle-1/select", json={
            "initiator_id": "user1", "agent_id": "a1",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_cluster_standalone_preserves_local_behavior(self, integrated_client):
        client, net = integrated_client
        assert net.cluster.standalone is True

        # Create + bid + result should work exactly as before
        resp = await client.post("/api/tasks", json={
            "task_id": "standalone-1",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201

        resp = await client.post("/api/tasks/standalone-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 40.0,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

    async def test_local_task_not_forwarded(self, integrated_client):
        client, net = integrated_client

        # Create a local task
        resp = await client.post("/api/tasks", json={
            "task_id": "local-check",
            "initiator_id": "user1",
            "content": {"desc": "local"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Router should consider it local (no remote route)
        assert net.cluster.router.is_local("local-check") is True
        assert net.cluster.router.get_route("local-check") is None

        # Bid handled locally
        resp = await client.post("/api/tasks/local-check/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

        # Result handled locally
        resp = await client.post("/api/tasks/local-check/result", json={
            "agent_id": "a1", "content": "result",
        })
        assert resp.status_code == 200

        # Verify result actually stored
        task = net.task_manager.get("local-check")
        assert len(task.results) == 1
        assert task.results[0].content == "result"


class TestPeerBroadcastAndLocalDiscovery:
    async def test_peer_broadcast_stores_correct_route(self, integrated_client):
        client, net = integrated_client
        resp = await client.post("/peer/task/broadcast", json={
            "task_id": "remote-task-1",
            "origin": "remote-node-x",
            "initiator_id": "remote-user",
            "domains": ["coding"],
            "budget": 50.0,
            "content": {"desc": "remote task"},
        })
        assert resp.status_code == 200

        assert net.cluster.router.get_route("remote-task-1") == "remote-node-x"
        assert net.cluster.router.is_local("remote-task-1") is False

    async def test_peer_join_updates_discovery(self, integrated_client):
        client, net = integrated_client

        resp = await client.post("/peer/join", json={
            "node_card": {
                "node_id": "peer-node-1",
                "endpoint": "http://peer1:8000",
                "domains": ["design"],
                "version": "0.1.0",
            },
        })
        assert resp.status_code == 200

        assert net.cluster.members.contains("peer-node-1") is True
        assert net.cluster.members.get("peer-node-1").domains == ["design"]

        # Announce in cluster DHT
        await net.cluster.dht.announce("design", "peer-node-1")
        nodes = await net.cluster.discovery.discover("design")
        assert "peer-node-1" in nodes


class TestClusterDomainAnnouncement:
    async def test_announce_and_revoke(self, integrated_client):
        client, net = integrated_client

        await net.cluster.announce_domain("writing")
        assert "writing" in net.cluster.local_node.domains
        nodes = await net.cluster.dht.lookup("writing")
        assert net.cluster.node_id in nodes

        await net.cluster.revoke_domain("writing")
        assert "writing" not in net.cluster.local_node.domains
        nodes = await net.cluster.dht.lookup("writing")
        assert net.cluster.node_id not in nodes


class TestConfigIntegration:
    async def test_cluster_config_in_admin_endpoint(self, integrated_client):
        client, net = integrated_client
        resp = await client.get("/api/admin/config")
        assert resp.status_code == 200
        config = resp.json()

        assert "cluster" in config
        cluster = config["cluster"]
        assert cluster["seed_nodes"] == []
        assert cluster["heartbeat_interval"] == 10
        assert cluster["heartbeat_fan_out"] == 3
        assert cluster["suspect_rounds"] == 3
        assert cluster["offline_rounds"] == 6
        assert cluster["protocol_version"] == "0.1.0"
