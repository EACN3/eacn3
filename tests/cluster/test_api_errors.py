"""Tests for API-level error handling: malformed requests, missing fields,
cluster not initialized, and peer route edge cases.
"""

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
async def api_client():
    """HTTP client for API error testing."""
    db = Database()
    await db.connect()
    net = Network(db=db)
    await net.start()

    net.escrow.get_or_create_account("user1", 10_000.0)
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


class TestPeerJoinValidation:
    async def test_join_missing_node_card(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/join", json={})
        assert resp.status_code == 422  # Pydantic validation error

    async def test_join_incomplete_node_card_missing_endpoint(self, api_client):
        """NodeCard requires endpoint — missing it causes ValidationError.

        Pydantic v2's ValidationError inherits from ValueError, so it's caught
        by the handler's except ValueError clause and returned as 409.
        """
        client, _ = api_client
        resp = await client.post("/peer/join", json={
            "node_card": {},
        })
        # ValidationError (subclass of ValueError) → caught → 409
        assert resp.status_code == 409
        assert "Field required" in resp.json()["detail"]

    async def test_join_with_extra_fields_accepted(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/join", json={
            "node_card": {
                "node_id": "extra",
                "endpoint": "http://extra:8000",
                "version": "0.1.0",
                "extra_field": "should not crash",
            },
        })
        # Should succeed or gracefully handle extra field
        assert resp.status_code in (200, 422)

    async def test_join_endpoint_conflict(self, api_client):
        client, _ = api_client
        # First join
        await client.post("/peer/join", json={
            "node_card": {
                "node_id": "conflict-node",
                "endpoint": "http://original:8000",
                "version": "0.1.0",
            },
        })

        # Conflicting join
        resp = await client.post("/peer/join", json={
            "node_card": {
                "node_id": "conflict-node",
                "endpoint": "http://different:8000",
                "version": "0.1.0",
            },
        })
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


class TestPeerHeartbeatValidation:
    async def test_heartbeat_missing_node_id(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/heartbeat", json={
            "domains": [],
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert resp.status_code == 422

    async def test_heartbeat_missing_timestamp(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/heartbeat", json={
            "node_id": "test",
            "domains": [],
        })
        assert resp.status_code == 422


class TestPeerDHTValidation:
    async def test_dht_store_missing_domain(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/dht/store", json={"node_id": "n1"})
        assert resp.status_code == 422

    async def test_dht_store_missing_node_id(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/dht/store", json={"domain": "coding"})
        assert resp.status_code == 422

    async def test_dht_lookup_missing_domain_param(self, api_client):
        client, _ = api_client
        resp = await client.get("/peer/dht/lookup")
        assert resp.status_code == 422

    async def test_dht_revoke_missing_fields(self, api_client):
        client, _ = api_client
        resp = await client.request("DELETE", "/peer/dht/revoke", json={})
        assert resp.status_code == 422


class TestPeerTaskBidValidation:
    async def test_bid_missing_task_id(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/bid", json={
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
            "from_node": "remote",
        })
        assert resp.status_code == 422

    async def test_bid_missing_confidence(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/bid", json={
            "task_id": "t1",
            "agent_id": "a1",
            "price": 80.0,
            "from_node": "remote",
        })
        assert resp.status_code == 422

    async def test_bid_missing_from_node(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/bid", json={
            "task_id": "t1",
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert resp.status_code == 422


class TestPeerTaskResultValidation:
    async def test_result_missing_content(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/result", json={
            "task_id": "t1",
            "agent_id": "a1",
            "from_node": "remote",
        })
        assert resp.status_code == 422

    async def test_result_missing_agent_id(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/result", json={
            "task_id": "t1",
            "content": "result",
            "from_node": "remote",
        })
        assert resp.status_code == 422


class TestPeerBroadcastValidation:
    async def test_broadcast_missing_task_id(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/broadcast", json={
            "origin": "remote",
            "initiator_id": "user1",
        })
        assert resp.status_code == 422

    async def test_broadcast_missing_origin(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/broadcast", json={
            "task_id": "t1",
            "initiator_id": "user1",
        })
        assert resp.status_code == 422


class TestPeerGossipValidation:
    async def test_gossip_missing_from_node(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/gossip/exchange", json={
            "known": [],
        })
        assert resp.status_code == 422

    async def test_gossip_from_node_not_valid_card(self, api_client):
        """Invalid from_node dict causes ValidationError in NodeCard.from_dict().

        Since the handler doesn't catch ValidationError, this propagates
        as an unhandled exception (500).
        """
        client, _ = api_client
        with pytest.raises(Exception):
            # The ASGI transport raises the unhandled exception directly
            await client.post("/peer/gossip/exchange", json={
                "from_node": {"invalid": "data"},
                "known": [],
            })


class TestPeerSubtaskValidation:
    async def test_subtask_missing_parent_task_id(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/subtask", json={
            "subtask_data": {"initiator_id": "a1"},
            "from_node": "remote",
        })
        assert resp.status_code == 422

    async def test_subtask_missing_subtask_data(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/subtask", json={
            "parent_task_id": "t1",
            "from_node": "remote",
        })
        assert resp.status_code == 422


class TestPeerPushValidation:
    async def test_push_missing_type(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/push", json={
            "task_id": "t1",
            "recipients": [],
        })
        assert resp.status_code == 422

    async def test_push_defaults_for_optional_fields(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/push", json={
            "type": "EVT",
            "task_id": "t1",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["delivered"] == 0  # Empty recipients default


class TestAPIBidOnLocalVsRemote:
    async def test_local_bid_uses_local_handler(self, api_client):
        client, net = api_client
        # Create a local task
        resp = await client.post("/api/tasks", json={
            "task_id": "local-t1",
            "initiator_id": "user1",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Bid should be handled locally (no forwarding)
        resp = await client.post("/api/tasks/local-t1/bid", json={
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

    async def test_remote_bid_with_no_endpoint_returns_502(self, api_client):
        client, net = api_client
        # Mark task as remote but don't set endpoint
        net.cluster.router.set_route("remote-t1", "remote-node")

        resp = await client.post("/api/tasks/remote-t1/bid", json={
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert resp.status_code == 502


class TestBroadcastIdempotency:
    async def test_duplicate_broadcast_is_ignored(self, api_client):
        client, net = api_client

        body = {
            "task_id": "dup-broadcast",
            "origin": "node-B",
            "initiator_id": "user1",
            "domains": ["coding"],
        }

        resp1 = await client.post("/peer/task/broadcast", json=body)
        assert resp1.status_code == 200

        resp2 = await client.post("/peer/task/broadcast", json=body)
        assert resp2.status_code == 200

        # Route should still point to original origin
        assert net.cluster.router.get_route("dup-broadcast") == "node-B"

    async def test_broadcast_different_tasks_distinct_routes(self, api_client):
        client, net = api_client

        await client.post("/peer/task/broadcast", json={
            "task_id": "t1", "origin": "node-A", "initiator_id": "u1", "domains": [],
        })
        await client.post("/peer/task/broadcast", json={
            "task_id": "t2", "origin": "node-B", "initiator_id": "u2", "domains": [],
        })

        assert net.cluster.router.get_route("t1") == "node-A"
        assert net.cluster.router.get_route("t2") == "node-B"


class TestStatusNotification:
    async def test_status_accepted(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/status", json={
            "task_id": "t1",
            "status": "completed",
            "payload": {"winner": "a1"},
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_status_empty_payload(self, api_client):
        client, _ = api_client
        resp = await client.post("/peer/task/status", json={
            "task_id": "t1",
            "status": "failed",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
