"""Integration tests: cluster layer with Network orchestration."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from eacn.network.app import Network
from eacn.network.db import Database
from eacn.network.api.routes import router as net_router, set_network
from eacn.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn.network.api.peer_routes import peer_router, set_peer_cluster, set_peer_network
from eacn.network.api.websocket import ws_router


@pytest.fixture
async def integrated_client():
    """Full integration: Network + Cluster, funded, with API."""
    db = Database()
    await db.connect()
    net = Network(db=db)
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
    app.include_router(ws_router)
    set_network(net)
    set_discovery_network(net)
    set_peer_cluster(net.cluster)
    set_peer_network(net)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, net

    await db.close()


class TestNetworkWithCluster:
    """Verify that the existing Network flows still work with cluster integrated."""

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
        assert resp.json()["id"] == "ct1"

    async def test_full_task_lifecycle_with_cluster(self, integrated_client):
        client, net = integrated_client
        # Create
        resp = await client.post("/api/tasks", json={
            "task_id": "lifecycle-1",
            "initiator_id": "user1",
            "content": {"desc": "full lifecycle test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Bid
        resp = await client.post("/api/tasks/lifecycle-1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

        # Submit result
        resp = await client.post("/api/tasks/lifecycle-1/result", json={
            "agent_id": "a1", "content": "done",
        })
        assert resp.status_code == 200

        # Close
        resp = await client.post("/api/tasks/lifecycle-1/close", json={
            "initiator_id": "user1",
        })
        assert resp.status_code == 200

        # Select result
        resp = await client.post("/api/tasks/lifecycle-1/select", json={
            "initiator_id": "user1", "agent_id": "a1",
        })
        assert resp.status_code == 200

    async def test_cluster_standalone_does_not_affect_local(self, integrated_client):
        client, net = integrated_client
        assert net.cluster.standalone
        # All operations should work exactly as before
        resp = await client.post("/api/tasks", json={
            "task_id": "standalone-1",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201

    async def test_route_forwarding_check_on_local_task(self, integrated_client):
        """Tasks created locally should be handled locally (not forwarded)."""
        client, net = integrated_client
        resp = await client.post("/api/tasks", json={
            "task_id": "local-check",
            "initiator_id": "user1",
            "content": {"desc": "local"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Bid should be handled locally
        resp = await client.post("/api/tasks/local-check/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })
        assert resp.status_code == 200

        # Result handled locally
        resp = await client.post("/api/tasks/local-check/result", json={
            "agent_id": "a1", "content": "result",
        })
        assert resp.status_code == 200


class TestPeerBroadcastAndLocalDiscovery:
    """Test that peer broadcasts trigger local agent discovery."""

    async def test_peer_broadcast_stores_route(self, integrated_client):
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

    async def test_peer_join_and_discover(self, integrated_client):
        client, net = integrated_client

        # A peer joins
        resp = await client.post("/peer/join", json={
            "node_card": {
                "node_id": "peer-node-1",
                "endpoint": "http://peer1:8000",
                "domains": ["design"],
                "version": "0.1.0",
            },
        })
        assert resp.status_code == 200

        # Peer should be in members
        assert net.cluster.members.contains("peer-node-1")

        # Store domain in cluster DHT
        await net.cluster.dht.announce("design", "peer-node-1")

        # Discovery should find this peer for design domain
        nodes = await net.cluster.discovery.discover("design")
        assert "peer-node-1" in nodes


class TestClusterDomainAnnouncement:
    async def test_announce_domain_via_cluster(self, integrated_client):
        client, net = integrated_client
        await net.cluster.announce_domain("writing")
        assert "writing" in net.cluster.local_node.domains
        nodes = await net.cluster.dht.lookup("writing")
        assert net.cluster.node_id in nodes

    async def test_revoke_domain_via_cluster(self, integrated_client):
        client, net = integrated_client
        await net.cluster.announce_domain("temp-domain")
        await net.cluster.revoke_domain("temp-domain")
        assert "temp-domain" not in net.cluster.local_node.domains
        nodes = await net.cluster.dht.lookup("temp-domain")
        assert net.cluster.node_id not in nodes


class TestConfigIntegration:
    async def test_cluster_config_in_network_config(self, integrated_client):
        client, net = integrated_client
        resp = await client.get("/api/admin/config")
        assert resp.status_code == 200
        config = resp.json()
        assert "cluster" in config
        assert "seed_nodes" in config["cluster"]
        assert "heartbeat_interval" in config["cluster"]
