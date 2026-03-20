"""Tests for error paths and failure scenarios.

Covers: bootstrap failures, forward failures surfaced via API,
gossip exchange failures, and partial broadcast failures.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from eacn.network.app import Network
from eacn.network.db import Database
from eacn.network.api.routes import router as net_router, set_network
from eacn.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn.network.api.peer_routes import peer_router, set_peer_cluster, set_peer_network
from eacn.network.api.websocket import ws_router
from eacn.network.cluster.bootstrap import ClusterBootstrap
from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.cluster.router import ClusterRouter
from eacn.network.cluster.service import ClusterService
from eacn.network.config import ClusterConfig


@pytest.fixture
async def error_client():
    """HTTP client for error path testing."""
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


class TestBootstrapFailures:
    async def test_all_seeds_unreachable_returns_empty(self):
        """When all seed nodes are unreachable, join_network returns empty list."""
        local = NodeCard(node_id="n1", endpoint="http://n1:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed1:8000", "http://seed2:8000"])
        bs = ClusterBootstrap(local, members, config)

        with patch.object(bs, "_contact_seed", side_effect=httpx.ConnectError("refused")):
            result = await bs.join_network()

        assert result == []

    async def test_first_seed_fails_second_succeeds(self):
        """Falls back to second seed if first is unreachable."""
        local = NodeCard(node_id="n1", endpoint="http://n1:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed1:8000", "http://seed2:8000"])
        bs = ClusterBootstrap(local, members, config)

        peer = NodeCard(node_id="peer-1", endpoint="http://peer1:8000")

        call_count = 0

        async def mock_contact(endpoint):
            nonlocal call_count
            call_count += 1
            if "seed1" in endpoint:
                raise httpx.ConnectError("refused")
            return [peer]

        with patch.object(bs, "_contact_seed", side_effect=mock_contact):
            result = await bs.join_network()

        assert call_count == 2
        assert len(result) == 1
        assert result[0].node_id == "peer-1"

    async def test_seed_skips_self_endpoint(self):
        """join_network skips contacting self when self is a seed."""
        local = NodeCard(node_id="seed", endpoint="http://seed:8000")
        members = MembershipList()
        config = ClusterConfig(seed_nodes=["http://seed:8000", "http://other:8000"])
        bs = ClusterBootstrap(local, members, config)

        peer = NodeCard(node_id="other", endpoint="http://other:8000")

        async def mock_contact(endpoint):
            if "seed:8000" in endpoint:
                raise AssertionError("Should not contact self")
            return [peer]

        with patch.object(bs, "_contact_seed", side_effect=mock_contact):
            result = await bs.join_network()

        assert len(result) == 1
        assert result[0].node_id == "other"

    async def test_leave_network_partial_failure(self):
        """leave_network continues notifying even if some peers fail."""
        local = NodeCard(node_id="local", endpoint="http://local:8000")
        members = MembershipList()
        config = ClusterConfig()
        bs = ClusterBootstrap(local, members, config)

        peers = [
            NodeCard(node_id="p1", endpoint="http://p1:8000"),
            NodeCard(node_id="p2", endpoint="http://p2:8000"),
            NodeCard(node_id="p3", endpoint="http://p3:8000"),
        ]

        with patch("eacn.network.cluster.bootstrap.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            call_count = 0

            async def side_effect(url, **kwargs):
                nonlocal call_count
                call_count += 1
                if "p2" in url:
                    raise httpx.ConnectError("refused")
                return MagicMock()

            mock_client.post.side_effect = side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await bs.leave_network(peers)

        # All 3 peers should have been attempted
        assert call_count == 3


class TestAPIForwardingErrors:
    """Test that API routes return 502 when forwarding to remote nodes fails."""

    async def test_bid_forward_failure_returns_502(self, error_client):
        client, net = error_client

        # Setup: mark a task as remote
        net.cluster.router.set_route("remote-t1", "remote-node")
        net.cluster.router.set_endpoint("remote-node", "http://remote:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = await client.post("/api/tasks/remote-t1/bid", json={
                "agent_id": "a1", "confidence": 0.9, "price": 80.0,
            })

        assert resp.status_code == 502
        assert "Forward failed" in resp.json()["detail"]

    async def test_result_forward_failure_returns_502(self, error_client):
        client, net = error_client

        net.cluster.router.set_route("remote-t1", "remote-node")
        net.cluster.router.set_endpoint("remote-node", "http://remote:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = await client.post("/api/tasks/remote-t1/result", json={
                "agent_id": "a1", "content": "my result",
            })

        assert resp.status_code == 502
        assert "Forward failed" in resp.json()["detail"]

    async def test_reject_forward_failure_returns_502(self, error_client):
        client, net = error_client

        net.cluster.router.set_route("remote-t1", "remote-node")
        net.cluster.router.set_endpoint("remote-node", "http://remote:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = await client.post("/api/tasks/remote-t1/reject", json={
                "agent_id": "a1",
            })

        assert resp.status_code == 502

    async def test_subtask_forward_failure_returns_502(self, error_client):
        client, net = error_client

        net.cluster.router.set_route("remote-t1", "remote-node")
        net.cluster.router.set_endpoint("remote-node", "http://remote:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = await client.post("/api/tasks/remote-t1/subtask", json={
                "initiator_id": "a1",
                "content": {"desc": "sub"},
                "domains": ["coding"],
                "budget": 50.0,
            })

        assert resp.status_code == 502

    async def test_bid_forward_no_route_returns_502(self, error_client):
        """forward_bid raises ValueError (no route), API should catch it."""
        client, net = error_client

        # Set route but no endpoint
        net.cluster.router.set_route("remote-t1", "remote-node")
        # No endpoint set

        resp = await client.post("/api/tasks/remote-t1/bid", json={
            "agent_id": "a1", "confidence": 0.9, "price": 80.0,
        })

        assert resp.status_code == 502
        assert "No endpoint" in resp.json()["detail"]


class TestPeerRoutesErrorHandling:
    async def test_peer_bid_nonexistent_task(self, error_client):
        client, net = error_client
        resp = await client.post("/peer/task/bid", json={
            "task_id": "nonexistent",
            "agent_id": "a1",
            "confidence": 0.9,
            "price": 80.0,
            "from_node": "remote",
        })
        assert resp.status_code == 400
        assert "detail" in resp.json()

    async def test_peer_result_nonexistent_task(self, error_client):
        client, net = error_client
        resp = await client.post("/peer/task/result", json={
            "task_id": "nonexistent",
            "agent_id": "a1",
            "content": "result",
            "from_node": "remote",
        })
        assert resp.status_code == 400

    async def test_peer_reject_nonexistent_task(self, error_client):
        client, net = error_client
        resp = await client.post("/peer/task/reject", json={
            "task_id": "nonexistent",
            "agent_id": "a1",
            "from_node": "remote",
        })
        assert resp.status_code == 400

    async def test_peer_subtask_nonexistent_parent(self, error_client):
        client, net = error_client
        resp = await client.post("/peer/task/subtask", json={
            "parent_task_id": "nonexistent",
            "subtask_data": {
                "initiator_id": "a1",
                "content": {"desc": "sub"},
                "domains": ["coding"],
                "budget": 50.0,
            },
            "from_node": "remote",
        })
        assert resp.status_code == 400

    async def test_peer_leave_nonexistent_node(self, error_client):
        """Leaving a node that doesn't exist should not crash."""
        client, net = error_client
        resp = await client.post("/peer/leave", json={"node_id": "nonexistent"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_peer_heartbeat_nonexistent_node_returns_404(self, error_client):
        client, net = error_client
        resp = await client.post("/peer/heartbeat", json={
            "node_id": "nonexistent",
            "domains": [],
            "timestamp": "2026-01-01T00:00:00Z",
        })
        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]
        # Must not create phantom node
        assert net.cluster.members.contains("nonexistent") is False


class TestClusterServiceStartFailures:
    async def test_start_with_unreachable_seeds_enters_standalone(self, db):
        """If all seeds are unreachable during start, cluster has 1 member (self only)."""
        config = ClusterConfig(
            node_id="local",
            endpoint="http://local:8000",
            seed_nodes=["http://seed1:8000", "http://seed2:8000"],
        )
        cs = ClusterService(db, config=config)

        with patch.object(cs.bootstrap, "join_network", return_value=[]):
            await cs.start()

        # Only self in members
        assert cs.members.count() == 1
        assert cs.members.contains("local") is True

    async def test_start_populates_members_from_seeds(self, db):
        """Successful start should add all peers to members and router."""
        config = ClusterConfig(
            node_id="local",
            endpoint="http://local:8000",
            seed_nodes=["http://seed1:8000"],
        )
        cs = ClusterService(db, config=config)

        peers = [
            NodeCard(node_id="peer-1", endpoint="http://p1:8000", domains=["coding"]),
            NodeCard(node_id="peer-2", endpoint="http://p2:8000", domains=["design"]),
        ]

        with patch.object(cs.bootstrap, "join_network", return_value=peers):
            await cs.start()

        assert cs.members.count() == 3  # local + 2 peers
        assert cs.members.contains("peer-1") is True
        assert cs.members.contains("peer-2") is True
        assert cs.router.get_endpoint("peer-1") == "http://p1:8000"
        assert cs.router.get_endpoint("peer-2") == "http://p2:8000"

    async def test_stop_non_standalone_revokes_dht(self, db):
        """stop() should revoke DHT entries and leave network."""
        config = ClusterConfig(
            node_id="local",
            endpoint="http://local:8000",
            seed_nodes=["http://seed:8000"],
        )
        cs = ClusterService(db, config=config)
        cs._standalone = False

        await cs.dht.announce("coding", "local")
        await cs.dht.announce("design", "local")

        with patch.object(cs.bootstrap, "leave_network", return_value=None) as mock_leave:
            await cs.stop()

        mock_leave.assert_called_once()
        # DHT entries should be revoked
        assert await cs.dht.lookup("coding") == []
        assert await cs.dht.lookup("design") == []
