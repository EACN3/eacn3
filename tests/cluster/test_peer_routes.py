"""Tests for peer routes (/peer/ endpoints)."""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from eacn.network.app import Network
from eacn.network.config import NetworkConfig
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
    net = Network(db=db, config=NetworkConfig())
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
    async def test_join_returns_membership_list(self, peer_client):
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

        # Must return nodes list
        assert isinstance(data["nodes"], list)
        assert len(data["nodes"]) >= 2  # At least local + new node

        # Check new node is in membership
        assert net.cluster.members.contains("new-node") is True
        card = net.cluster.members.get("new-node")
        assert card.endpoint == "http://new:8000"
        assert card.domains == ["coding"]

        # Check returned nodes contain the new node
        returned_ids = {n["node_id"] for n in data["nodes"]}
        assert "new-node" in returned_ids

    async def test_join_conflict_returns_409(self, peer_client):
        client, net = peer_client
        card = {
            "node_card": {
                "node_id": "dup-node",
                "endpoint": "http://dup:8000",
                "domains": [],
                "version": "0.1.0",
            },
        }
        resp1 = await client.post("/peer/join", json=card)
        assert resp1.status_code == 200

        card["node_card"]["endpoint"] = "http://different:8000"
        resp2 = await client.post("/peer/join", json=card)
        assert resp2.status_code == 409
        assert "already exists" in resp2.json()["detail"]

        # Original card should be unchanged
        assert net.cluster.members.get("dup-node").endpoint == "http://dup:8000"


class TestPeerLeave:
    async def test_leave_removes_from_membership(self, peer_client):
        client, net = peer_client
        await client.post("/peer/join", json={
            "node_card": {
                "node_id": "leaving-node",
                "endpoint": "http://leaving:8000",
                "version": "0.1.0",
            },
        })
        assert net.cluster.members.contains("leaving-node") is True

        resp = await client.post("/peer/leave", json={"node_id": "leaving-node"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert net.cluster.members.contains("leaving-node") is False


class TestPeerHeartbeat:
    async def test_heartbeat_updates_node(self, peer_client):
        client, net = peer_client
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
            "domains": ["new_domain", "updated"],
            "timestamp": "2026-03-20T12:00:00Z",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        node = net.cluster.members.get("hb-node")
        assert node.domains == ["new_domain", "updated"]
        assert node.last_seen == "2026-03-20T12:00:00Z"
        assert node.status == "online"

    async def test_heartbeat_unknown_node_returns_404(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/heartbeat", json={
            "node_id": "unknown",
            "domains": [],
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert resp.status_code == 404
        assert "unknown" in resp.json()["detail"]

        # Should not create a phantom node
        assert net.cluster.members.contains("unknown") is False


class TestPeerDHT:
    async def test_store_and_lookup_roundtrip(self, peer_client):
        client, net = peer_client
        store_resp = await client.post("/peer/dht/store", json={
            "domain": "design", "node_id": "node-x",
        })
        assert store_resp.status_code == 200
        assert store_resp.json()["ok"] is True

        lookup_resp = await client.get("/peer/dht/lookup", params={"domain": "design"})
        assert lookup_resp.status_code == 200
        data = lookup_resp.json()
        assert data["domain"] == "design"
        assert data["node_ids"] == ["node-x"]

    async def test_revoke_removes_mapping(self, peer_client):
        client, net = peer_client
        await client.post("/peer/dht/store", json={
            "domain": "design", "node_id": "node-x",
        })

        revoke_resp = await client.request("DELETE", "/peer/dht/revoke", json={
            "domain": "design", "node_id": "node-x",
        })
        assert revoke_resp.status_code == 200
        assert revoke_resp.json()["ok"] is True

        lookup_resp = await client.get("/peer/dht/lookup", params={"domain": "design"})
        assert lookup_resp.json()["node_ids"] == []

    async def test_lookup_empty_domain(self, peer_client):
        client, net = peer_client
        resp = await client.get("/peer/dht/lookup", params={"domain": "nonexistent"})
        assert resp.status_code == 200
        assert resp.json()["domain"] == "nonexistent"
        assert resp.json()["node_ids"] == []

    async def test_store_multiple_nodes_same_domain(self, peer_client):
        client, net = peer_client
        await client.post("/peer/dht/store", json={"domain": "ml", "node_id": "n1"})
        await client.post("/peer/dht/store", json={"domain": "ml", "node_id": "n2"})

        resp = await client.get("/peer/dht/lookup", params={"domain": "ml"})
        assert set(resp.json()["node_ids"]) == {"n1", "n2"}


class TestPeerGossip:
    async def test_exchange_adds_nodes_to_membership(self, peer_client):
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

        # Both peer-x and peer-y should be in membership
        assert net.cluster.members.contains("peer-x") is True
        assert net.cluster.members.contains("peer-y") is True
        assert net.cluster.members.get("peer-x").endpoint == "http://px:8000"
        assert net.cluster.members.get("peer-y").domains == ["design"]

        # Response should contain local nodes (excluding peer-x)
        returned_ids = {n["node_id"] for n in data["known"]}
        assert "peer-x" not in returned_ids  # Excluded
        assert net.cluster.node_id in returned_ids


class TestPeerTaskBroadcast:
    async def test_broadcast_stores_route(self, peer_client):
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
        assert resp.json()["ok"] is True

        # Route must be stored
        assert net.cluster.router.get_route("remote-t1") == "remote-node"
        assert net.cluster.router.is_local("remote-t1") is False

    async def test_broadcast_idempotent(self, peer_client):
        client, net = peer_client
        body = {
            "task_id": "remote-t2",
            "origin": "remote-node",
            "initiator_id": "user-remote",
            "domains": ["coding"],
        }
        resp1 = await client.post("/peer/task/broadcast", json=body)
        assert resp1.status_code == 200

        # Second broadcast of same task is ignored
        resp2 = await client.post("/peer/task/broadcast", json=body)
        assert resp2.status_code == 200

        # Route should still point to original origin
        assert net.cluster.router.get_route("remote-t2") == "remote-node"


class TestPeerTaskOperations:
    async def test_bid_on_local_task_returns_status(self, peer_client):
        client, net = peer_client
        # Create a local task
        create_resp = await client.post("/api/tasks", json={
            "task_id": "local-t1",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert create_resp.status_code == 201

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
        data = resp.json()
        assert data["status"] == "executing"
        assert data["bid"]["agent_id"] == "a1"
        assert data["bid"]["status"] == "executing"

        # Participant should be tracked
        participants = net.cluster.router.get_participants("local-t1")
        assert "remote-node" in participants

    async def test_bid_invalid_task_returns_400(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/task/bid", json={
            "task_id": "nonexistent",
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
            "from_node": "remote",
        })
        assert resp.status_code == 400
        assert "detail" in resp.json()

    async def test_result_submission(self, peer_client):
        client, net = peer_client
        # Setup: create task + bid
        await client.post("/api/tasks", json={
            "task_id": "res-t1", "initiator_id": "user1",
            "content": {"desc": "test"}, "domains": ["coding"], "budget": 100.0,
        })
        bid_resp = await client.post("/peer/task/bid", json={
            "task_id": "res-t1", "agent_id": "a1",
            "confidence": 0.9, "price": 80.0, "from_node": "remote",
        })
        assert bid_resp.json()["status"] == "executing"

        # Submit result
        resp = await client.post("/peer/task/result", json={
            "task_id": "res-t1", "agent_id": "a1",
            "content": "my result", "from_node": "remote",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify result was recorded
        task = net.task_manager.get("res-t1")
        assert len(task.results) == 1
        assert task.results[0].agent_id == "a1"
        assert task.results[0].content == "my result"

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
        assert resp.json()["ok"] is True

        # Verify bid was rejected
        task = net.task_manager.get("rej-t1")
        bid = [b for b in task.bids if b.agent_id == "a1"][0]
        assert bid.status.value == "rejected"

    async def test_status_notification(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/task/status", json={
            "task_id": "t1", "status": "completed", "payload": {"key": "val"},
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

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
        data = resp.json()
        assert "subtask_id" in data
        assert data["subtask_id"] != ""
        assert data["status"] == "unclaimed"

        # Verify subtask was created in task manager
        subtask = net.task_manager.get(data["subtask_id"])
        assert subtask.parent_id == "sub-parent"
        assert subtask.budget == 50.0

    async def test_push_endpoint(self, peer_client):
        client, net = peer_client
        resp = await client.post("/peer/push", json={
            "type": "TASK_BROADCAST",
            "task_id": "t1",
            "recipients": ["a1", "a2"],
            "payload": {"data": "test"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["delivered"] == 2
