"""Tests for peer routes (/peer/ endpoints)."""

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
async def peer_client():
    """HTTP client with Network + ClusterService for peer route testing."""
    db = Database()
    await db.connect()
    net = Network(db=db)
    await net.start()

    # Fund accounts for task tests
    net.escrow.get_or_create_account("user1", 10_000.0)
    # DHT entries
    await net.dht.announce("coding", "a1")
    net.reputation._scores["a1"] = 0.8

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


class TestPeerJoin:
    async def test_join(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/join", json={
            "node_card": {
                "node_id": "new-node",
                "endpoint": "http://new:8000",
                "domains": ["coding"],
                "version": "0.1.0",
            },
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert net.cluster.members.contains("new-node")

    async def test_join_conflict(self, peer_client):
        client, net = peer_client
        card = {
            "node_card": {
                "node_id": "dup-node",
                "endpoint": "http://dup:8000",
                "domains": [],
                "version": "0.1.0",
            },
        }
        await client.post("/peer/join", json=card)
        # Join again with different endpoint
        card["node_card"]["endpoint"] = "http://different:8000"
        resp = await client.post("/peer/join", json=card)
        assert resp.status_code == 409


class TestPeerLeave:
    async def test_leave(self, peer_client):
        client, net = peer_client
        # First join
        await client.post("/peer/join", json={
            "node_card": {
                "node_id": "leaving-node",
                "endpoint": "http://leaving:8000",
                "version": "0.1.0",
            },
        })
        resp = await client.post("/peer/leave", json={"node_id": "leaving-node"})
        assert resp.status_code == 200
        assert not net.cluster.members.contains("leaving-node")


class TestPeerHeartbeat:
    async def test_heartbeat(self, peer_client):
        client, net = peer_client
        # Join first
        await client.post("/peer/join", json={
            "node_card": {
                "node_id": "hb-node",
                "endpoint": "http://hb:8000",
                "domains": ["old"],
                "version": "0.1.0",
            },
        })
        resp = await client.post("/peer/heartbeat", json={
            "node_id": "hb-node",
            "domains": ["new"],
            "timestamp": "2026-03-20T12:00:00Z",
        })
        assert resp.status_code == 200
        node = net.cluster.members.get("hb-node")
        assert node.domains == ["new"]

    async def test_heartbeat_unknown_node(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/heartbeat", json={
            "node_id": "unknown",
            "domains": [],
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert resp.status_code == 404


class TestPeerDHT:
    async def test_store_and_lookup(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/dht/store", json={
            "domain": "design",
            "node_id": "node-x",
        })
        assert resp.status_code == 200

        resp = await client.get("/peer/dht/lookup", params={"domain": "design"})
        assert resp.status_code == 200
        assert "node-x" in resp.json()["node_ids"]

    async def test_revoke(self, peer_client):
        client, net = peer_client
        await client.post("/peer/dht/store", json={
            "domain": "design", "node_id": "node-x",
        })
        resp = await client.request("DELETE", "/peer/dht/revoke", json={
            "domain": "design", "node_id": "node-x",
        })
        assert resp.status_code == 200

        resp = await client.get("/peer/dht/lookup", params={"domain": "design"})
        assert resp.json()["node_ids"] == []

    async def test_lookup_empty(self, peer_client):
        client, net = peer_client
        resp = await client.get("/peer/dht/lookup", params={"domain": "nonexistent"})
        assert resp.status_code == 200
        assert resp.json()["node_ids"] == []


class TestPeerGossip:
    async def test_exchange(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/gossip/exchange", json={
            "from_node": {
                "node_id": "peer-x",
                "endpoint": "http://px:8000",
                "domains": ["coding"],
                "version": "0.1.0",
            },
            "known": [
                {
                    "node_id": "peer-y",
                    "endpoint": "http://py:8000",
                    "domains": ["design"],
                    "version": "0.1.0",
                },
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "known" in data
        # peer-x should now be in members
        assert net.cluster.members.contains("peer-x")
        assert net.cluster.members.contains("peer-y")


class TestPeerTaskBroadcast:
    async def test_broadcast(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/task/broadcast", json={
            "task_id": "remote-t1",
            "origin": "remote-node",
            "initiator_id": "user-remote",
            "domains": ["coding"],
            "type": "normal",
            "budget": 100.0,
            "content": {"desc": "test"},
            "max_concurrent_bidders": 5,
        })
        assert resp.status_code == 200
        # Route should be stored
        assert net.cluster.router.get_route("remote-t1") == "remote-node"

    async def test_broadcast_idempotent(self, peer_client):
        client, net = peer_client
        body = {
            "task_id": "remote-t2",
            "origin": "remote-node",
            "initiator_id": "user-remote",
            "domains": ["coding"],
        }
        await client.post("/peer/task/broadcast", json=body)
        resp = await client.post("/peer/task/broadcast", json=body)
        assert resp.status_code == 200


class TestPeerTaskOperations:
    async def test_bid_on_local_task(self, peer_client):
        client, net = peer_client
        # Create a local task first
        resp = await client.post("/api/tasks", json={
            "task_id": "local-t1",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Peer bids
        resp = await client.post("/peer/task/bid", json={
            "task_id": "local-t1",
            "agent_id": "a1",
            "server_id": "srv-1",
            "confidence": 0.9,
            "price": 80.0,
            "from_node": "remote-node",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] in ("executing", "waiting", "rejected")

    async def test_bid_invalid_task(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/task/bid", json={
            "task_id": "nonexistent",
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
            "from_node": "remote",
        })
        assert resp.status_code == 400

    async def test_result_on_local_task(self, peer_client):
        client, net = peer_client
        # Create task and bid
        await client.post("/api/tasks", json={
            "task_id": "res-t1", "initiator_id": "user1",
            "content": {"desc": "test"}, "domains": ["coding"], "budget": 100.0,
        })
        await client.post("/peer/task/bid", json={
            "task_id": "res-t1", "agent_id": "a1",
            "confidence": 0.9, "price": 80.0, "from_node": "remote",
        })
        # Submit result
        resp = await client.post("/peer/task/result", json={
            "task_id": "res-t1", "agent_id": "a1",
            "content": "my result", "from_node": "remote",
        })
        assert resp.status_code == 200

    async def test_reject_on_local_task(self, peer_client):
        client, net = peer_client
        await client.post("/api/tasks", json={
            "task_id": "rej-t1", "initiator_id": "user1",
            "content": {"desc": "test"}, "domains": ["coding"], "budget": 100.0,
        })
        await client.post("/peer/task/bid", json={
            "task_id": "rej-t1", "agent_id": "a1",
            "confidence": 0.9, "price": 80.0, "from_node": "remote",
        })
        resp = await client.post("/peer/task/reject", json={
            "task_id": "rej-t1", "agent_id": "a1", "from_node": "remote",
        })
        assert resp.status_code == 200

    async def test_status_notification(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/task/status", json={
            "task_id": "t1", "status": "completed", "payload": {},
        })
        assert resp.status_code == 200

    async def test_subtask_on_local_task(self, peer_client):
        client, net = peer_client
        # Create parent task and bid
        await client.post("/api/tasks", json={
            "task_id": "sub-parent", "initiator_id": "user1",
            "content": {"desc": "test"}, "domains": ["coding"], "budget": 200.0,
        })
        await client.post("/peer/task/bid", json={
            "task_id": "sub-parent", "agent_id": "a1",
            "confidence": 0.9, "price": 80.0, "from_node": "remote",
        })
        # Create subtask
        resp = await client.post("/peer/task/subtask", json={
            "parent_task_id": "sub-parent",
            "subtask_data": {
                "initiator_id": "a1",
                "content": {"desc": "subtask"},
                "domains": ["coding"],
                "budget": 50.0,
            },
            "from_node": "remote",
        })
        assert resp.status_code == 200
        assert "subtask_id" in resp.json()
